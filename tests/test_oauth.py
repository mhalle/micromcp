"""OAuth: `Unauthorized`'s 403 step-up form (core; runs in the single-file
bundle too), then `micromcp.contrib.oauth.OAuth` against local fake
authorization servers: discovery, JWT checks, scopes, introspection, static
tokens, slow and broken providers, and the warnings that predict a failed
sign-in.
"""
import asyncio, base64, collections, concurrent.futures, hashlib, hmac, http.server, inspect, io
import json, logging, sys, threading, time
from urllib.parse import parse_qs
from micromcp import (MCP, Server, ASGIServer, Context, Error, Principal, Unauthorized, PROTOCOL,
                      META_VER, META_CAPS, WELL_KNOWN)
from _helpers import free_port

OK = FAIL = 0


def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc as e:
        return type(e).__name__
    except Exception as e:  # noqa: BLE001 - the test reports the wrong type
        return f"wrong: {type(e).__name__}: {e}"
    return None


RESOURCE = "https://todos.example/mcp"
WK = f"https://todos.example{WELL_KNOWN}/mcp"
META = "/.well-known/oauth-authorization-server"
STEP_UP = "The access token lacks a scope this operation needs"


def call(app, method, params=None, token=None, name=None):
    """One WSGI request; (status, JSON body, lowercase headers)."""
    params = dict(params or {})
    params["_meta"] = {META_VER: PROTOCOL, META_CAPS: {}}
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/mcp", "CONTENT_LENGTH": str(len(raw)),
           "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(raw),
           "HTTP_ACCEPT": "application/json", "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL,
           "HTTP_MCP_METHOD": method}
    if name:
        env["HTTP_MCP_NAME"] = name
    if token is not None:
        env["HTTP_AUTHORIZATION"] = token if token.lower().startswith(("bearer ", "basic ")) \
            else f"Bearer {token}"
    box = {}
    out = b"".join(app(env, lambda s, h: box.update(s=s, h=h)))
    return int(box["s"].split()[0]), json.loads(out) if out else None, \
        {k.lower(): v for k, v in box["h"]}


def tool(app, name, token, **args):
    return call(app, "tools/call", {"name": name, "arguments": args}, token=token, name=name)


def result(resp):
    return resp[1]["result"].get("structuredContent")


def timed(fn):
    t0 = time.monotonic()
    out = fn()
    return out, time.monotonic() - t0


def burst(n, fn):
    """`fn()` from n threads at once."""
    with concurrent.futures.ThreadPoolExecutor(n) as ex:
        return [f.result() for f in [ex.submit(fn) for _ in range(n)]]


# ── core: the 403 step-up form ─────────────────────────────────────────────
print("Unauthorized.insufficient_scope (core)")
e = Unauthorized.insufficient_scope("files:write")
check("insufficient_scope is 403", e.status, 403)
check("... with the RFC 6750 parameters", e.challenge("https://x/wk")[0][1],
      f'Bearer resource_metadata="https://x/wk", scope="files:write", '
      f'error="insufficient_scope", error_description="{STEP_UP}"')
check("Unauthorized(error='insufficient_scope') is 403 too",
      Unauthorized(error="insufficient_scope").status, 403)
check("the other forms stay 401", (Unauthorized().status, Unauthorized.invalid().status),
      (401, 401))
changed = Unauthorized()
changed.error = "insufficient_scope"
check("the status follows `error` even without refresh()", changed.status, 403)
core = MCP("core-403", "0.1.0")


@core.tool
def writer() -> dict:
    """Needs a scope the caller lacks."""
    raise Unauthorized.insufficient_scope("files:write")


capp = Server(core, path="/mcp",
              resource_metadata={"resource": RESOURCE, "authorization_servers": ["https://as.x"]})
s, j, h = tool(capp, "writer", None)
check("a handler's insufficient_scope answers 403", s, 403)
check("... with the step-up challenge", h.get("www-authenticate"),
      f'Bearer resource_metadata="{WK}", scope="files:write", error="insufficient_scope", '
      f'error_description="{STEP_UP}"')
check("... and -32001 in the body", j["error"]["code"], -32001)

print("streaming tools keep an early Error's status (core)")
STREAM_HDRS = {"content-type": "application/json", "accept": "application/json, text/event-stream",
               "mcp-protocol-version": PROTOCOL, "mcp-method": "tools/call"}


def stream_body(name):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": {},
                                  "_meta": {META_VER: PROTOCOL, META_CAPS: {}}}}).encode()


def asgi_call(app, name):
    """One ASGI request; (status, headers, body)."""
    hdrs = {**STREAM_HDRS, "mcp-name": name}
    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()]}
    msgs, sent = [{"type": "http.request", "body": stream_body(name), "more_body": False}], []

    async def receive():
        if msgs:
            return msgs.pop(0)
        await asyncio.sleep(3600)

    async def send(m):
        sent.append(m)
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return (start["status"], {k.decode(): v.decode() for k, v in start["headers"]},
            b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))


@core.tool
async def scoped(ctx: Context) -> dict:
    """Checks a scope before it streams anything."""
    raise Unauthorized.insufficient_scope("files:write")


@core.tool
async def chatty(ctx: Context) -> dict:
    """Streams, then fails."""
    await ctx.info("working")
    raise Unauthorized.insufficient_scope("files:write")


@core.tool
async def quiet(ctx: Context) -> dict:
    """Streams nothing, answers after a pause."""
    await asyncio.sleep(0.3)
    return {"done": True}


aapp = ASGIServer(core, path="/mcp", keepalive=0.1,
                  resource_metadata={"resource": RESOURCE, "authorization_servers": ["https://as.x"]})
s, h, b = asgi_call(aapp, "scoped")
check("a Context tool failing before its first notification keeps its 403",
      (s, h.get("content-type"), 'error="insufficient_scope"' in h.get("www-authenticate", "")),
      (403, "application/json", True))
s, h, b = asgi_call(aapp, "chatty")
check("... after a notification the stream is open and the error in-band",
      (s, h.get("content-type"), b'"code": -32001' in b), (200, "text/event-stream", True))
s, h, b = asgi_call(aapp, "quiet")
check("a silent Context tool still streams, keepalives first",
      (s, h.get("content-type"), b.startswith(b": keepalive"), b'"done": true' in b),
      (200, "text/event-stream", True, True))


async def begun(name):
    early, req = await aapp.prepare("POST", {**STREAM_HDRS, "mcp-name": name}, stream_body(name))
    out, chunks = await aapp.begin_stream(req, {**STREAM_HDRS, "mcp-name": name})
    return (out[0] if out else None), (b"".join([c async for c in chunks]) if chunks else None)


check("begin_stream (for adapters) hands back an early Error as a reply",
      asyncio.run(begun("scoped")), (403, None))
check("... and streams the rest", asyncio.run(begun("quiet"))[1].count(b"data:"), 1)

print("several scope guards answer one 403 (core)")


def needs(scope):
    def guard(who):
        raise Unauthorized.insufficient_scope(scope)
    return guard


@core.tool(guards=[needs("a:write"), needs("b:delete")])
def both() -> dict:
    """Needs two scopes, from two guards."""
    return {}


@core.tool(guards=[needs("a:write"), lambda who: False])
def refused() -> dict:
    """A scope guard, then one that plainly denies."""
    return {}


s, j, h = tool(capp, "both", None)
check("two scope guards answer one 403 naming both scopes",
      (s, 'scope="a:write b:delete"' in h.get("www-authenticate", "")), (403, True))
check("... while a guard that plainly denies still hides the tool", tool(capp, "refused", None)[0],
      404)

print("a refused version is answered by authentication first (core)")
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                   "clientInfo": {"name": "sdk", "version": "2"}}}


def handshake(app):
    raw = json.dumps(INIT).encode()
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/mcp", "CONTENT_LENGTH": str(len(raw)),
           "CONTENT_TYPE": "application/json", "wsgi.input": io.BytesIO(raw),
           "HTTP_ACCEPT": "application/json, text/event-stream"}
    box = {}
    b"".join(app(env, lambda s, h: box.update(s=s)))
    return int(box["s"].split()[0])


def outage(headers):
    raise Error(-32603, "authorization server unavailable", 503)


def no_token(headers):
    raise Unauthorized(scope="read")


for label, authn, want in [("an outage", outage, 503), ("no token", no_token, 401),
                           ("valid credentials", lambda headers: {"sub": "x"}, 400)]:
    check(f"a refused 2025 handshake with {label} answers {want}",
          handshake(Server(core, path="/mcp", authenticate=authn)), want)

try:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from micromcp.contrib import oauth as oauth_mod
    from micromcp.contrib.oauth import OAuth
except ModuleNotFoundError as exc:
    print(f"skip: the rest needs {exc.name} (the single-file bundle carries only the core)")
    print(f"\n{OK} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


class Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


grab = Grab()
logging.getLogger("micromcp").addHandler(grab)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
JWK.update(kid="k1", alg="RS256", use="sig")
BASIC = "Basic " + base64.b64encode(b"rs:s3cret").decode()


class Quiet(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass                                  # clients that give up break pipes; expected


class FakeAS:
    """An authorization server's public face: metadata, keys, introspection.
    `behave[path]` makes a path misbehave: "garbage", "hang", "trickle", raw
    bytes, or a JSON object to answer instead; `delay[path]` slows it."""

    def __init__(self, **meta):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.hits = collections.Counter()
        doc = {"issuer": self.base, "jwks_uri": f"{self.base}/jwks",
               "introspection_endpoint": f"{self.base}/introspect",
               "code_challenge_methods_supported": ["S256"],
               "client_id_metadata_document_supported": True, **meta}
        self.meta = {k: v for k, v in doc.items() if v is not None}
        self.opaque, self.behave, self.delay = {}, {}, {}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, doc):
                body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def misbehave(self, normal):
                time.sleep(fake.delay.get(self.path, 0))
                how = fake.behave.get(self.path)
                if how is None:
                    return False
                if how == "garbage":
                    self.wfile.write(b"garbage\r\n\r\n")
                    self.close_connection = True
                elif how == "hang":
                    time.sleep(30)
                elif how == "trickle":
                    body = json.dumps(normal).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    for b in body:
                        self.wfile.write(bytes([b]))
                        self.wfile.flush()
                        time.sleep(0.1)
                else:
                    self.reply(200, how)
                return True

            def do_GET(self):
                fake.hits[self.path] += 1
                normal = fake.meta if self.path == META else {"keys": [JWK]}
                if self.misbehave(normal):
                    return
                if self.path == META:
                    return self.reply(200, fake.meta)
                if self.path == "/jwks":
                    return self.reply(200, {"keys": [JWK]})
                self.reply(404, {"error": "not found"})

            def do_POST(self):
                fake.hits[self.path] += 1
                form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
                if self.misbehave({}):
                    return
                if self.headers.get("Authorization") != BASIC:
                    return self.reply(401, {"error": "invalid_client"})
                self.reply(200, fake.opaque.get(form.get("token", [""])[0], {"active": False}))

        server = Quiet(("127.0.0.1", self.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()


AS = FakeAS()


def claims(**over):
    now = int(time.time())
    body = {"iss": AS.base, "aud": RESOURCE, "sub": "alice", "client_id": "claude",
            "exp": now + 300, "scope": "todos:read", **over}
    return {k: v for k, v in body.items() if v is not None}


def token(key=KEY, kid="k1", **over):
    return jwt.encode(claims(**over), key, algorithm="RS256", headers={"kid": kid})


def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def unsigned():
    return f"{b64({'alg': 'none', 'kid': 'k1', 'typ': 'JWT'})}.{b64(claims())}."


def confused():
    """HS256 over our public key's PEM: the classic algorithm-confusion forgery."""
    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    signing = f"{b64({'alg': 'HS256', 'kid': 'k1', 'typ': 'JWT'})}.{b64(claims())}"
    sig = hmac.new(pem, signing.encode(), hashlib.sha256).digest()
    return f"{signing}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def whoami_mcp(auth=None):
    mcp = MCP("oauth-test", "0.1.0")

    @mcp.tool
    def whoami(who: Principal) -> dict:
        """Who the token says."""
        return {"sub": who["sub"], "client_id": who["client_id"], "scopes": who["scopes"]}

    if auth is not None:
        @mcp.tool(guards=[auth.requires("todos:write")])
        def todo_add(text: str) -> dict:
            """Needs todos:write; hidden without it."""
            return {"added": text}

        @mcp.tool
        def todo_clear(who: Principal) -> dict:
            """Listed for everyone; checks inside."""
            auth.check(who, "todos:write", "todos:read")
            return {"cleared": True}
    return mcp


def server(auth, mcp=None):
    return Server(mcp or whoami_mcp(), authenticate=auth, path="/mcp")


auth = OAuth(issuer=AS.base, resource=RESOURCE, scopes=["todos:read"],
             implies={"todos:admin": ["todos:write", "todos:read"]})
app = Server(whoami_mcp(auth), authenticate=auth, path="/mcp", resource_metadata=auth.metadata)


def names(tok):
    return sorted(t["name"] for t in call(app, "tools/list", token=tok)[1]["result"]["tools"])


# ── construction ───────────────────────────────────────────────────────────
print("construction")
check("metadata is the RFC 9728 document", auth.metadata,
      {"resource": RESOURCE, "authorization_servers": [AS.base],
       "bearer_methods_supported": ["header"], "scopes_supported": ["todos:read"]})
check("OAuth is an async authenticate: no pool thread per request",
      inspect.iscoroutinefunction(OAuth.__call__), True)
for label, kw in [("an http issuer that is not localhost", {"issuer": "http://auth.example"}),
                  ("an issuer with a query", {"issuer": AS.base + "/?tenant=x"}),
                  ("a symmetric algorithm", {"algorithms": ["HS256"]}),
                  ("the none algorithm", {"algorithms": "none"}),
                  ("offline_access", {"scopes": ["offline_access"]}),
                  ("a resource with a fragment", {"resource": RESOURCE + "#x"}),
                  ("a scope with a space", {"scopes": ["a b"]}),
                  ("implies that is not a dict", {"implies": ["todos:admin"]}),
                  ("a NaN leeway", {"leeway": float("nan")}),
                  ("a zero timeout", {"timeout": 0}),
                  ("a malformed introspection pair", {"introspection": ("only-id",)})]:
    kwargs = {"issuer": AS.base, "resource": RESOURCE, **kw}
    check(f"OAuth refuses {label}", raises(lambda k=kwargs: OAuth(**k)), "ValueError")
lenient = OAuth(issuer=AS.base, resource=RESOURCE, algorithms="RS256", scopes=None)
check("one algorithm may be a str; scopes may be None", (lenient.algorithms, lenient.scopes),
      (("RS256",), ()))
check("discovery URLs for an issuer with a path", oauth_mod._metadata_urls("https://a.example/t1"),
      ["https://a.example/.well-known/oauth-authorization-server/t1",
       "https://a.example/.well-known/openid-configuration/t1",
       "https://a.example/t1/.well-known/openid-configuration"])
check("... and for one without (trailing slash too)", oauth_mod._metadata_urls("https://a.example/"),
      ["https://a.example/.well-known/oauth-authorization-server",
       "https://a.example/.well-known/openid-configuration"])
check("constructing does no network I/O", sum(AS.hits.values()), 0)

# ── challenges ─────────────────────────────────────────────────────────────
print("challenges")
first = f'Bearer resource_metadata="{WK}", scope="todos:read"'
s, j, h = call(app, "tools/list")
check("no token: 401 with the challenge", (s, h.get("www-authenticate")), (401, first))
s, j, h = call(app, "tools/list", token="Basic dXNlcjpwdw==")
check("another scheme: the same 401", (s, h.get("www-authenticate")), (401, first))
s, j, h = call(app, "tools/list", token="Bearer ")
check("an empty bearer token: the same 401", (s, h.get("www-authenticate")), (401, first))
check("... still no network I/O", sum(AS.hits.values()), 0)

# ── tokens ─────────────────────────────────────────────────────────────────
print("tokens")
check("a valid token reaches the tool as the principal", result(tool(app, "whoami", token())),
      {"sub": "alice", "client_id": "claude", "scopes": ["todos:read"]})
check("the scheme is case-insensitive", tool(app, "whoami", "bearer " + token())[0], 200)
check("scp lists are read too",
      result(tool(app, "whoami", token(scope=None, scp=["todos:read", "x"])))["scopes"],
      ["todos:read", "x"])
check("aud may be a list naming this server", tool(app, "whoami", token(aud=["o", RESOURCE]))[0], 200)
check("exp inside the leeway is accepted", tool(app, "whoami", token(exp=int(time.time()) - 10))[0],
      200)
check("the metadata was fetched once", AS.hits[META], 1)
check("the keys were fetched once", AS.hits["/jwks"], 1)
now = int(time.time())
for label, tok in [("expired past the leeway", token(exp=now - 120)),
                   ("not valid yet", token(nbf=now + 300)),
                   ("for another audience", token(aud="https://other.example/mcp")),
                   ("from another issuer", token(iss="https://evil.example")),
                   ("without exp", token(exp=None)),
                   ("signed by another key under our key id", token(key=OTHER)),
                   ("unsigned (alg none)", unsigned()),
                   ("HMAC-signed with our public key", confused()),
                   ("naming an unknown key", token(kid="nope")),
                   ("that is not a JWT", "abc"),
                   ("of three garbage parts", "a.b.c")]:
    s, j, h = tool(app, "whoami", tok)
    check(f"a token {label} is 401 invalid_token",
          (s, 'error="invalid_token"' in h.get("www-authenticate", "")), (401, True))
for i in range(5):
    tool(app, "whoami", token(kid=f"unknown{i}"))
check("unknown key ids do not hammer the key endpoint", AS.hits["/jwks"], 1)

# ── scopes ─────────────────────────────────────────────────────────────────
print("scopes")
read, write, admin = token(), token(scope="todos:read todos:write"), token(scope="todos:admin")
check("a guarded tool is hidden without its scope", names(read), ["todo_clear", "whoami"])
check("... and listed with it", names(write), ["todo_add", "todo_clear", "whoami"])
s, j, h = tool(app, "todo_add", read, text="x")
check("calling it anyway is 403 insufficient_scope", (s, h.get("www-authenticate")),
      (403, f'Bearer resource_metadata="{WK}", scope="todos:write", '
            f'error="insufficient_scope", error_description="{STEP_UP}"'))
s, j, h = tool(app, "todo_clear", read)
check("check() in a handler names every scope the tool needs",
      (s, 'scope="todos:write todos:read"' in h.get("www-authenticate", "")), (403, True))
check("with the scope the tool runs", result(tool(app, "todo_add", write, text="x")), {"added": "x"})
check("a broader scope implies narrower ones",
      (tool(app, "todo_add", admin, text="y")[0], tool(app, "todo_clear", admin)[0]), (200, 200))
check("the principal lists the implied scopes", result(tool(app, "whoami", admin))["scopes"],
      ["todos:admin", "todos:read", "todos:write"])
check("check() refuses a principal that is not one of ours",
      raises(lambda: auth.check(None, "todos:read"), Unauthorized), "Unauthorized")

# ── slow providers: nothing waits for a thread, nobody waits past timeout ──
print("slow providers")
slow = FakeAS()
slow.delay.update({META: 0.3, "/jwks": 0.3})
sapp = server(OAuth(issuer=slow.base, resource=RESOURCE))
statuses = burst(16, lambda: tool(sapp, "whoami", token(iss=slow.base))[0])
check("16 concurrent first requests to a slow provider all succeed", statuses, [200] * 16)
check("... sharing one metadata fetch and one key fetch", (slow.hits[META], slow.hits["/jwks"]),
      (1, 1))
trickle = FakeAS()
trickle.behave[META] = "trickle"
tapp = server(OAuth(issuer=trickle.base, resource=RESOURCE, timeout=0.5))
with concurrent.futures.ThreadPoolExecutor(8) as ex:
    stuck = [ex.submit(timed, lambda: tool(tapp, "whoami", token(iss=trickle.base))[0])
             for _ in range(4)]
    time.sleep(0.05)
    quick = [ex.submit(timed, lambda: call(tapp, "tools/list")[0]) for _ in range(4)]
    stuck, quick = [f.result() for f in stuck], [f.result() for f in quick]
check("a provider trickling its metadata is 503 within the timeout",
      [(st, d < 2.0) for st, d in stuck], [(503, True)] * 4)
check("... while tokenless requests still get the 401 at once",
      [(st, d < 0.3) for st, d in quick], [(401, True)] * 4)
hang = FakeAS()
happ = server(OAuth(issuer=hang.base, resource=RESOURCE, timeout=0.5))
check("warm-up with a healthy key endpoint", tool(happ, "whoami", token(iss=hang.base))[0], 200)
hang.behave["/jwks"] = "hang"
oauth_mod.KEY_COOLDOWN = 0                     # let an unknown key id force a load at once
with concurrent.futures.ThreadPoolExecutor(2) as ex:
    rotated = ex.submit(timed, lambda: tool(happ, "whoami", token(iss=hang.base, kid="new"))[0])
    time.sleep(0.05)
    cached = ex.submit(timed, lambda: tool(happ, "whoami", token(iss=hang.base))[0])
    rotated, cached = rotated.result(), cached.result()
check("an unknown key id with a hanging key endpoint is 503 within the timeout",
      (rotated[0], rotated[1] < 2.0), (503, True))
check("... while a token with a cached key is answered at once", (cached[0], cached[1] < 0.3),
      (200, True))
time.sleep(1.0)                                # let the hung load give up
hang.behave.pop("/jwks")
hang.delay["/jwks"] = 0.3
oauth_mod.KEYS_TTL = 0                         # every key is due for a refresh
before = hang.hits["/jwks"]
(st, d) = timed(lambda: tool(happ, "whoami", token(iss=hang.base))[0])
check("keys due for a refresh are used while it runs in the background", (st, d < 0.25),
      (200, True))
time.sleep(0.6)
check("... and the refresh happens", hang.hits["/jwks"] > before, True)
oauth_mod.KEYS_TTL, oauth_mod.KEY_COOLDOWN = 300.0, 30.0

# ── broken providers are 503, never 500 or 401 ─────────────────────────────
print("broken providers")
down = server(OAuth(issuer=f"http://127.0.0.1:{free_port()}", resource=RESOURCE))
s, j, h = call(down, "tools/list", token=token())
check("an unreachable provider is 503, not a new sign-in",
      (s, j["error"]["code"], "www-authenticate" in h), (503, -32603, False))
(st, d) = timed(lambda: call(down, "tools/list", token=token())[0])
check("... and is not retried on every request", (st, d < 0.5), (503, True))
for label, path, how in [("a garbage status line", META, "garbage"),
                         ("an HTML page", META, b"<html>down for maintenance</html>"),
                         ("NaN in the JSON", META, b'{"issuer": NaN}')]:
    f = FakeAS()
    f.behave[path] = how
    check(f"discovery answered with {label} is 503",
          tool(server(OAuth(issuer=f.base, resource=RESOURCE)), "whoami", token(iss=f.base))[0], 503)
for label, how in [("a garbage status line", "garbage"), ("an HTML page", b"<html></html>"),
                   ("an empty key set", {"keys": []}),
                   ("only a symmetric key", {"keys": [{"kty": "oct", "k": "c2VjcmV0", "kid": "k1"}]})]:
    f = FakeAS()
    f.behave["/jwks"] = how
    check(f"a key endpoint answering {label} is 503, not 401",
          tool(server(OAuth(issuer=f.base, resource=RESOURCE)), "whoami", token(iss=f.base))[0], 503)
evil = FakeAS(issuer="https://evil.example")
check("metadata naming another issuer is not used",
      (tool(server(OAuth(issuer=evil.base, resource=RESOURCE)), "whoami", token())[0],
       any("names issuer" in m for m in grab.records)), (503, True))
oauth_mod.RETRY_AFTER = 0.2
nokeys = FakeAS(jwks_uri=None)
napp = server(OAuth(issuer=nokeys.base, resource=RESOURCE))
check("metadata without jwks_uri is 503", tool(napp, "whoami", token(iss=nokeys.base))[0], 503)
nokeys.meta["jwks_uri"] = f"{nokeys.base}/jwks"
time.sleep(0.3)
check("... and is not cached: fixed metadata is picked up",
      tool(napp, "whoami", token(iss=nokeys.base))[0], 200)
oauth_mod.RETRY_AFTER = 10.0

# ── warnings ───────────────────────────────────────────────────────────────
print("warnings")
bare = FakeAS(code_challenge_methods_supported=None, client_id_metadata_document_supported=None)
grab.records.clear()
OAuth(issuer=bare.base, resource=RESOURCE).discover()
check("a provider without PKCE S256 is warned about", any("PKCE S256" in m for m in grab.records),
      True)
check("... and one clients cannot register with",
      any("registered with it by hand" in m for m in grab.records), True)
grab.records.clear()
OAuth(issuer=AS.base, resource=RESOURCE).discover()
check("a provider MCP clients can use draws no warning", grab.records, [])
grab.records.clear()
OAuth(issuer=FakeAS(client_id_metadata_document_supported=None,
                    registration_endpoint="https://r.example").base, resource=RESOURCE).discover()
check("a registration endpoint is enough", grab.records, [])

# ── introspection ──────────────────────────────────────────────────────────
print("introspection")
now = int(time.time())
AS.opaque.update({
    "op-good": {"active": True, "iss": AS.base, "aud": RESOURCE, "sub": "bob",
                "scope": "todos:write", "exp": now + 300, "client_id": "claude"},
    "op-noaud": {"active": True, "iss": AS.base, "sub": "bob", "exp": now + 300},
    "op-otheriss": {"active": True, "iss": "https://evil.example", "aud": RESOURCE,
                    "exp": now + 300},
    "op-expired": {"active": True, "iss": AS.base, "aud": RESOURCE, "exp": now - 300},
    "op-off": {"active": False},
    "op-nan": {"active": True, "iss": AS.base, "aud": RESOURCE, "exp": float("nan")},
})
iauth = OAuth(issuer=AS.base, resource=RESOURCE, introspection=("rs", "s3cret"))
iapp = server(iauth)
check("an active opaque token reaches the tool", result(tool(iapp, "whoami", "op-good")),
      {"sub": "bob", "client_id": "claude", "scopes": ["todos:write"]})
before = AS.hits["/introspect"]
tool(iapp, "whoami", "op-good")
check("... and its answer is reused", AS.hits["/introspect"], before)
for label, tok in [("inactive", "op-off"), ("missing its audience", "op-noaud"),
                   ("from another issuer", "op-otheriss"), ("expired", "op-expired"),
                   ("unknown", "op-nope")]:
    check(f"an opaque token {label} is 401", tool(iapp, "whoami", tok)[0], 401)
check("an introspection answer with NaN is refused as not JSON (503)",
      tool(iapp, "whoami", "op-nan")[0], 503)
check("non-finite exp is never in date",
      raises(lambda: iauth._introspected({"active": True, "iss": AS.base, "aud": RESOURCE,
                                          "exp": float("inf")}, time.time()), Unauthorized),
      "Unauthorized")
check("rejected introspection credentials are 503, not the caller's fault",
      tool(server(OAuth(issuer=AS.base, resource=RESOURCE, introspection=("rs", "no"))),
           "whoami", "op-good")[0], 503)
garbled = FakeAS()
garbled.behave["/introspect"] = "garbage"
check("an introspection endpoint answering garbage is 503",
      tool(server(OAuth(issuer=garbled.base, resource=RESOURCE, introspection=("rs", "s3cret"))),
           "whoami", "op-good")[0], 503)
slowi = FakeAS()
slowi.delay["/introspect"] = 0.3
slowi.opaque["op"] = {"active": True, "iss": slowi.base, "aud": RESOURCE, "sub": "c",
                      "exp": now + 300}
siapp = server(OAuth(issuer=slowi.base, resource=RESOURCE, introspection=("rs", "s3cret")))
check("16 concurrent requests with one new opaque token all succeed",
      burst(16, lambda: tool(siapp, "whoami", "op")[0]), [200] * 16)
check("... sharing one introspection", slowi.hits["/introspect"], 1)

# ── static ─────────────────────────────────────────────────────────────────
print("static")
st = OAuth.static({"dev": {"sub": "me", "scope": "todos:write"}}, scopes=["todos:read"])
stapp = server(st)
check("static: metadata is None", st.metadata, None)
check("static: a listed token is its principal", result(tool(stapp, "whoami", "dev")),
      {"sub": "me", "client_id": None, "scopes": ["todos:write"]})
check("static: any other token is 401", tool(stapp, "whoami", "nope")[0], 401)
dev = asyncio.run(st({"authorization": "Bearer dev"}))
check("static: check() works the same", raises(lambda: st.check(dev, "todos:admin"), Unauthorized),
      "Unauthorized")
dev["claims"]["scope"] += " todos:admin"
dev["scopes"].append("todos:admin")
check("a handler changing its principal changes nothing for the next request",
      asyncio.run(st({"authorization": "Bearer dev"}))["scopes"], ["todos:write"])
check("static: there is nothing to discover", raises(st.discover, RuntimeError), "RuntimeError")

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
