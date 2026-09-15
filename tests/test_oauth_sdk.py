"""OAuth end to end with the official mcp SDK client: an ASGIServer using
micromcp.contrib.oauth, served by uvicorn, and a local fake authorization
server that registers clients, takes the user's consent, and issues tokens.
Sign-in from a 401; 403 step-up through a guard, through check() in a
streaming tool, and through two guards in one step; the 2025 (legacy) mode;
and what a user sees during an outage.
"""
import asyncio, http.server, json, logging, secrets, sys, threading, time
from urllib.parse import parse_qs, urlsplit
from _helpers import free_port

try:
    import httpx2
    import jwt
    import uvicorn
    from cryptography.hazmat.primitives.asymmetric import rsa
    from mcp import Client
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata, OAuthToken
    from micromcp import MCP, ASGIServer, Context, Principal
    from micromcp.contrib.oauth import OAuth
except ModuleNotFoundError as exc:
    print(f"skip: needs {exc.name} (the single-file bundle carries only the core)")
    sys.exit(0)

logging.basicConfig(level=logging.CRITICAL)
OK = FAIL = 0


def check(label, got, want):
    global OK, FAIL
    good = got == want
    OK, FAIL = OK + good, FAIL + (not good)
    print(f"  {'PASS' if good else 'FAIL'}  {label}")
    if not good:
        print(f"        got  {got!r}\n        want {want!r}")


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
JWK.update(kid="k1", alg="RS256", use="sig")


def mint(iss, aud, **over):
    now = int(time.time())
    body = {"iss": iss, "aud": aud, "sub": "alice", "client_id": "sdk", "exp": now + 300,
            "iat": now, "scope": "todos:read", **over}
    return jwt.encode(body, KEY, algorithm="RS256", headers={"kid": "k1"})


class Provider:
    """A fake authorization server with what an MCP client signs in through:
    metadata, dynamic registration, an authorization step (`consent` plays
    the user saying yes), a token endpoint minting JWTs for the requested
    resource, and keys. `consents` records every authorization request as
    (scope, resource)."""

    def __init__(self):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.consents, self.codes, self.jwks_status = [], {}, 200
        self.meta = {"issuer": self.base, "jwks_uri": f"{self.base}/jwks",
                     "authorization_endpoint": f"{self.base}/authorize",
                     "token_endpoint": f"{self.base}/token",
                     "registration_endpoint": f"{self.base}/register",
                     "response_types_supported": ["code"],
                     "grant_types_supported": ["authorization_code", "refresh_token"],
                     "code_challenge_methods_supported": ["S256"],
                     "scopes_supported": ["todos:read", "todos:write", "todos:delete"]}
        prov = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, status, doc):
                body = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/.well-known/oauth-authorization-server":
                    return self.reply(200, prov.meta)
                if path == "/jwks":
                    return self.reply(prov.jwks_status, {"keys": [JWK]})
                self.reply(404, {"error": "not found"})

            def do_POST(self):
                path = urlsplit(self.path).path
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if path == "/register":
                    out = {**json.loads(raw), "client_id": "c-" + secrets.token_hex(4),
                           "client_id_issued_at": int(time.time())}
                    out.setdefault("token_endpoint_auth_method", "none")
                    return self.reply(201, out)
                if path == "/token":
                    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    grant = prov.codes.pop(form.get("code"), None)
                    if grant is None:
                        return self.reply(400, {"error": "invalid_grant"})
                    token = mint(prov.base, form.get("resource") or grant["resource"],
                                 scope=grant["scope"] or "")
                    return self.reply(200, {"access_token": token, "token_type": "Bearer",
                                            "expires_in": 300, "scope": grant["scope"]})
                self.reply(404, {"error": "not found"})

        server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def consent(self, url):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        code = secrets.token_urlsafe(8)
        self.codes[code] = {"scope": q.get("scope"), "resource": q.get("resource")}
        self.consents.append((q.get("scope"), q.get("resource")))
        return code, q.get("state")


def build(auth):
    mcp = MCP("sdk-oauth", "0.1.0")

    @mcp.tool
    def whoami(who: Principal) -> dict:
        """Who the token says."""
        return {"sub": who["sub"], "scopes": who["scopes"]}

    @mcp.tool(guards=[auth.requires("todos:write")])
    def todo_add(text: str) -> dict:
        """Needs todos:write."""
        return {"added": text}

    @mcp.tool
    async def purge(who: Principal, ctx: Context) -> dict:
        """A streaming tool that checks todos:delete first thing."""
        auth.check(who, "todos:delete")
        return {"purged": True}

    @mcp.tool(guards=[auth.requires("todos:write"), auth.requires("todos:delete")])
    def nuke() -> dict:
        """Needs two scopes, from two guards."""
        return {"nuked": True}
    return mcp


def serve(auth, port, **options):
    app = ASGIServer(build(auth), path="/mcp", authenticate=auth,
                     resource_metadata=auth.metadata, **options)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical",
                                        lifespan="off"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            return
        time.sleep(0.05)
    raise RuntimeError("uvicorn did not start")


class Memory:
    """Token storage for the SDK's OAuth provider."""

    def __init__(self, tokens=None):
        self.tokens, self.info = tokens, None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.info

    async def set_client_info(self, info):
        self.info = info


def client(url, provider, mode="auto", tokens=None, redirected=None):
    """An SDK client whose sign-in consents at `provider`. With `redirected`,
    any attempt to send the user to sign in is recorded and refused."""
    storage, box = Memory(tokens), {}

    async def redirect(u):
        if redirected is not None:
            redirected.append(u)
            raise RuntimeError("the client sent the user to sign in")
        box["code"], box["state"] = provider.consent(u)

    async def callback():
        return AuthorizationCodeResult(code=box["code"], state=box["state"])

    metadata = OAuthClientMetadata(redirect_uris=["http://localhost:9/cb"],
                                   client_name="micromcp tests", grant_types=["authorization_code"],
                                   response_types=["code"], token_endpoint_auth_method="none")
    oauth = OAuthClientProvider(server_url=url, client_metadata=metadata, storage=storage,
                                redirect_handler=redirect, callback_handler=callback)
    http = httpx2.AsyncClient(auth=oauth, timeout=20)
    return Client(streamable_http_client(url, http_client=http), mode=mode), storage


def leaves(e):
    if isinstance(e, BaseExceptionGroup):
        for sub in e.exceptions:
            yield from leaves(sub)
    else:
        yield e


def stepped_up_to(consents, *scopes):
    """Each consent in `consents` asked for all of `scopes` (and there was one)."""
    return [set(s.split()) >= set(scopes) for s, _ in consents]


async def main():
    prov = Provider()
    port = free_port()
    res = f"http://127.0.0.1:{port}/mcp"
    auth = OAuth(issuer=prov.base, resource=res, scopes=["todos:read"])
    serve(auth, port, legacy="stateless")

    print("sign-in and step-up (mode='auto')")
    c, _ = client(res, prov)
    async with c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        check("a 401 starts the sign-in; the listing shows what todos:read may use", tools,
              ["purge", "whoami"])
        check("... asking for the challenge's scope, for this resource", prov.consents,
              [("todos:read", res)])
        check("the signed-in user reaches the tool", (await c.call_tool("whoami", {})).structured_content,
              {"sub": "alice", "scopes": ["todos:read"]})
        n = len(prov.consents)
        r = await c.call_tool("purge", {})
        check("check() in a streaming tool: 403, step-up, and the call succeeds",
              (r.is_error, r.structured_content), (False, {"purged": True}))
        check("... after one consent that adds todos:delete",
              stepped_up_to(prov.consents[n:], "todos:read", "todos:delete"), [True])
        r = await c.call_tool("todo_add", {"text": "x"})
        check("a requires() guard: 403, step-up, and the call succeeds", r.structured_content,
              {"added": "x"})

    print("two guards, one step-up")
    c, _ = client(res, prov)
    async with c:
        await c.list_tools()
        n = len(prov.consents)
        r = await c.call_tool("nuke", {})
        check("a tool with two requires() guards: the call succeeds", r.structured_content,
              {"nuked": True})
        check("... after a single consent naming both scopes",
              stepped_up_to(prov.consents[n:], "todos:write", "todos:delete"), [True])

    print("the 2025 handshake (mode='legacy')")
    c, _ = client(res, prov, mode="legacy")
    async with c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        check("legacy mode: sign-in from the 401", tools, ["purge", "whoami"])
        r = await c.call_tool("todo_add", {"text": "y"})
        check("legacy mode: 403 step-up", r.structured_content, {"added": "y"})

    print("outages")
    dead = f"http://127.0.0.1:{free_port()}"          # nothing listens here
    port2 = free_port()
    res2 = f"http://127.0.0.1:{port2}/mcp"
    serve(OAuth(issuer=dead, resource=res2), port2)
    redirected = []
    token = OAuthToken(access_token=mint(dead, res2), expires_in=300, scope="todos:read")
    c, storage = client(res2, prov, tokens=token, redirected=redirected)
    try:
        async with c:
            await c.list_tools()
        seen = "connected"
    except BaseException as e:  # noqa: BLE001 - the SDK wraps its errors in groups
        seen = " | ".join(str(x) for x in leaves(e))
    check("connecting during an outage reads as one, not as a version error",
          "authorization server unavailable" in seen, True)
    check("... sends nobody to sign in, and keeps the token",
          (redirected, storage.tokens is not None), ([], True))

    prov3 = Provider()
    port3 = free_port()
    res3 = f"http://127.0.0.1:{port3}/mcp"
    auth3 = OAuth(issuer=prov3.base, resource=res3)
    serve(auth3, port3)
    c, _ = client(res3, prov3)
    async with c:
        await c.list_tools()                           # signs in
        n = len(prov3.consents)
        auth3._keys = None                             # the keys must be loaded again ...
        prov3.jwks_status = 503                        # ... while the provider fails
        try:
            r = await c.call_tool("whoami", {})
            seen = f"result, is_error={r.is_error}"
        except BaseException as e:  # noqa: BLE001
            seen = " | ".join(str(x) for x in leaves(e))
        check("an outage mid-session reads as one", "authorization server unavailable" in seen,
              True)
        check("... and starts no new sign-in", prov3.consents[n:], [])


asyncio.run(main())
print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
