"""OAuth: `Unauthorized`'s 403 step-up form (core; runs in the single-file
bundle too), then `micromcp.contrib.oauth.OAuth` against a local fake
authorization server: discovery, JWT checks, scopes, introspection, static
tokens, and the warnings that predict a failed sign-in.
"""
import base64, collections, hashlib, hmac, http.server, io, json, logging, sys, threading, time
from urllib.parse import parse_qs
from micromcp import MCP, Server, Principal, Unauthorized, PROTOCOL, META_VER, META_CAPS, WELL_KNOWN
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
check("refresh() recomputes the status with the header", changed.refresh().status, 403)
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


class FakeAS:
    """An authorization server's public face: metadata, keys, introspection."""

    def __init__(self, **meta):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.hits = collections.Counter()
        doc = {"issuer": self.base, "jwks_uri": f"{self.base}/jwks",
               "introspection_endpoint": f"{self.base}/introspect",
               "code_challenge_methods_supported": ["S256"],
               "client_id_metadata_document_supported": True, **meta}
        self.meta = {k: v for k, v in doc.items() if v is not None}
        self.opaque = {}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, doc):
                body = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.hits[self.path] += 1
                if self.path == "/.well-known/oauth-authorization-server":
                    return self.reply(200, fake.meta)
                if self.path == "/jwks":
                    return self.reply(200, {"keys": [JWK]})
                self.reply(404, {"error": "not found"})

            def do_POST(self):
                fake.hits[self.path] += 1
                form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
                if self.headers.get("Authorization") != BASIC:
                    return self.reply(401, {"error": "invalid_client"})
                self.reply(200, fake.opaque.get(form.get("token", [""])[0], {"active": False}))

        server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
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
for label, kw in [("an http issuer that is not localhost", {"issuer": "http://auth.example"}),
                  ("a symmetric algorithm", {"algorithms": ["HS256"]}),
                  ("the none algorithm", {"algorithms": ["none"]}),
                  ("offline_access", {"scopes": ["offline_access"]}),
                  ("a resource with a fragment", {"resource": RESOURCE + "#x"}),
                  ("a scope with a space", {"scopes": ["a b"]}),
                  ("a NaN leeway", {"leeway": float("nan")}),
                  ("a zero timeout", {"timeout": 0}),
                  ("a malformed introspection pair", {"introspection": ("only-id",)})]:
    kwargs = {"issuer": AS.base, "resource": RESOURCE, **kw}
    check(f"OAuth refuses {label}", raises(lambda k=kwargs: OAuth(**k)), "ValueError")
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
check("the metadata was fetched once", AS.hits["/.well-known/oauth-authorization-server"], 1)
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
check("unknown key ids do not hammer the key endpoint", AS.hits["/jwks"] <= 2, True)

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

# ── provider failures ──────────────────────────────────────────────────────
print("provider failures")
down = OAuth(issuer=f"http://127.0.0.1:{free_port()}", resource=RESOURCE)
dapp = Server(whoami_mcp(), authenticate=down, path="/mcp")
s, j, h = call(dapp, "tools/list", token=token())
check("an unreachable provider is 503, not a new sign-in",
      (s, j["error"]["code"], "www-authenticate" in h), (503, -32603, False))
t0 = time.monotonic()
s = call(dapp, "tools/list", token=token())[0]
check("... and is not retried on every request", (s, time.monotonic() - t0 < 0.5), (503, True))
evil = FakeAS(issuer="https://evil.example")
eapp = Server(whoami_mcp(), authenticate=OAuth(issuer=evil.base, resource=RESOURCE), path="/mcp")
check("metadata naming another issuer is not used",
      (call(eapp, "tools/list", token=token())[0], any("names issuer" in m for m in grab.records)),
      (503, True))

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
check("a registration endpoint is enough",
      (grab.records.clear(), OAuth(issuer=FakeAS(client_id_metadata_document_supported=None,
                                                 registration_endpoint="https://r").base,
                                   resource=RESOURCE).discover(), grab.records)[2], [])

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
})
iapp = Server(whoami_mcp(), path="/mcp",
              authenticate=OAuth(issuer=AS.base, resource=RESOURCE, introspection=("rs", "s3cret")))
check("an active opaque token reaches the tool", result(tool(iapp, "whoami", "op-good")),
      {"sub": "bob", "client_id": "claude", "scopes": ["todos:write"]})
before = AS.hits["/introspect"]
tool(iapp, "whoami", "op-good")
check("... and its answer is reused", AS.hits["/introspect"], before)
for label, tok in [("inactive", "op-off"), ("missing its audience", "op-noaud"),
                   ("from another issuer", "op-otheriss"), ("expired", "op-expired"),
                   ("unknown", "op-nope")]:
    check(f"an opaque token {label} is 401", tool(iapp, "whoami", tok)[0], 401)
wapp = Server(whoami_mcp(), path="/mcp",
              authenticate=OAuth(issuer=AS.base, resource=RESOURCE, introspection=("rs", "no")))
check("rejected introspection credentials are 503, not the caller's fault",
      tool(wapp, "whoami", "op-good")[0], 503)

# ── static ─────────────────────────────────────────────────────────────────
print("static")
st = OAuth.static({"dev": {"sub": "me", "scope": "todos:write"}}, scopes=["todos:read"])
sapp = Server(whoami_mcp(), authenticate=st, path="/mcp")
check("static: metadata is None", st.metadata, None)
check("static: a listed token is its principal", result(tool(sapp, "whoami", "dev")),
      {"sub": "me", "client_id": None, "scopes": ["todos:write"]})
check("static: any other token is 401", tool(sapp, "whoami", "nope")[0], 401)
check("static: check() works the same",
      raises(lambda: st.check(st({"authorization": "Bearer dev"}), "todos:admin"), Unauthorized),
      "Unauthorized")
check("static: there is nothing to discover", raises(st.discover, RuntimeError), "RuntimeError")

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
