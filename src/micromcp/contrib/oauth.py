"""Token validation for a server whose tokens come from an OAuth authorization
server: an identity provider such as Auth0, WorkOS, Keycloak, or Okta.

    from micromcp.contrib.oauth import OAuth

    auth = OAuth(issuer="https://auth.example.com/",
                 resource="https://todos.example.com/mcp", scopes=["todos:read"])
    app = ASGIServer(mcp, path="/mcp", authenticate=auth, resource_metadata=auth.metadata)

    @mcp.tool(guards=[auth.requires("todos:write")])
    def todo_add(who: Principal, text: str) -> dict: ...

The server stays a resource server: the provider signs users in and issues
tokens; this module finds the provider's metadata and keys, checks each token,
and answers with the challenges MCP clients act on.

`OAuth` is an async `authenticate`, awaited on the server's event loop, so a
request that needs nothing from the provider (no token, a cached key, a cached
introspection answer) never waits for a thread. Fetches from the provider run
on a few threads of their own, one at a time per document, and no caller waits
for one longer than `timeout`. JWT signatures are checked with PyJWT
(`pip install "micromcp[oauth]"`); the fetching uses only the standard library.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import copy
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.request
from collections import OrderedDict
from urllib.parse import quote_plus, urlencode, urlsplit

from .._constants import INTERNAL_ERROR, log
from ..errors import Error, Unauthorized

__all__ = ["OAuth", "ALGORITHMS"]

# Asymmetric only. A JWKS publishes public keys: accepting HS* would let anyone
# "sign" a token with that public material (algorithm confusion).
ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
              "ES256", "ES384", "ES512", "EdDSA")
MAX_DOCUMENT = 1024 * 1024    # bytes read from any answer of the provider
RETRY_AFTER = 10.0            # seconds a failed discovery answers 503 before it is retried
KEYS_TTL = 300.0              # seconds before the keys are refreshed (in the background)
KEY_COOLDOWN = 30.0           # seconds between key loads that unknown key ids may force
INTROSPECTION_TTL = 60.0      # seconds an introspection answer is reused, at most
INTROSPECTION_CACHE = 1024    # introspection answers kept
FETCH_THREADS = 4             # threads fetching from the provider
FETCH_BACKLOG = 64            # fetches that may wait for one; beyond that, 503 at once


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Metadata counts only from the URL it was asked for: a 3xx is an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class _ProviderError(Exception):
    """The provider could not be reached, was too slow, or answered something unusable."""


def _unavailable():
    """503: an outage is not the caller's fault, and a 401 would send every client
    into a new sign-in."""
    return Error(INTERNAL_ERROR, "authorization server unavailable", 503)


def _secure(url, what):
    """`url` when it is https, or http to this machine (development and tests)."""
    u = urlsplit(url) if isinstance(url, str) else None
    local = u is not None and u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1",
                                                                     "::1")
    if u is None or not u.netloc or not (u.scheme == "https" or local):
        raise ValueError(f"{what} {url!r} must be an https URL (http only for localhost)")
    return url


def _metadata_urls(issuer):
    """Where the provider's metadata may be, in the order MCP clients try:
    RFC 8414 with path insertion, then OpenID Connect Discovery."""
    u = urlsplit(issuer)
    base, path = f"{u.scheme}://{u.netloc}", u.path.rstrip("/")
    if not path:
        return [f"{base}/.well-known/oauth-authorization-server",
                f"{base}/.well-known/openid-configuration"]
    return [f"{base}/.well-known/oauth-authorization-server{path}",
            f"{base}/.well-known/openid-configuration{path}",
            f"{base}{path}/.well-known/openid-configuration"]


def _not_json(name):
    raise ValueError(f"{name} is not a JSON number")


def _fetch(url, deadline, data=None, headers=None) -> dict:
    """A JSON object from `url` (POSTing `data` when given), read in full before
    `deadline` (a time.monotonic() value), so a provider that trickles its
    answer cannot hold a thread past it. Every failure is a _ProviderError."""
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("no time left")
        req = urllib.request.Request(url, data=data,
                                     headers={"Accept": "application/json", **(headers or {})})
        chunks, size = [], 0
        with _OPENER.open(req, timeout=remaining) as resp:
            while chunk := resp.read1(65536):
                size += len(chunk)
                if size > MAX_DOCUMENT:
                    raise ValueError(f"answer larger than {MAX_DOCUMENT} bytes")
                if time.monotonic() > deadline:
                    raise TimeoutError("answer too slow")
                chunks.append(chunk)
        doc = json.loads(b"".join(chunks), parse_constant=_not_json)
    except Exception as e:     # socket, HTTP, protocol, JSON, and recursion errors alike
        raise _ProviderError(f"{url}: {type(e).__name__}: {e}") from None
    if not isinstance(doc, dict):
        raise _ProviderError(f"{url}: not a JSON object")
    return doc


def _words(value, what) -> tuple:
    """Scope names: non-empty strings without whitespace."""
    out = (value,) if isinstance(value, str) else tuple(value)
    if not all(isinstance(s, str) and s and not any(c.isspace() for c in s) for s in out):
        raise ValueError(f"{what} must be scope names without whitespace, got {out!r}")
    return out


def _granted(claims) -> list:
    """The scopes a token carries: `scope` (space-separated, RFC 9068) or `scp` (a list)."""
    scope = claims.get("scope", claims.get("scp"))
    if isinstance(scope, str):
        return scope.split()
    if isinstance(scope, list):
        return [s for s in scope if isinstance(s, str)]
    return []


def _pyjwt():
    """PyJWT 2.14 or later (the 2.x security fixes), with its crypto backend."""
    try:
        import jwt
    except ImportError:
        raise ImportError('micromcp.contrib.oauth checks JWTs with PyJWT: '
                          'pip install "micromcp[oauth]"') from None
    if tuple(int(n) for n in re.findall(r"\d+", jwt.__version__)[:2]) < (2, 14):
        raise ImportError(f"micromcp.contrib.oauth needs PyJWT 2.14 or later "
                          f"(found {jwt.__version__})")
    if not jwt.algorithms.has_crypto:
        raise ImportError('micromcp.contrib.oauth needs PyJWT\'s crypto backend: '
                          'pip install "micromcp[oauth]"')
    return jwt


class OAuth:
    """The async `authenticate` callback, and the protected-resource metadata,
    for a server whose tokens a provider issues. Constructing one does no
    network I/O: the provider's metadata is fetched on the first token (or by
    `discover()`).

    issuer         the provider's issuer identifier, exactly as its metadata
                   and its tokens write it (a trailing slash matters)
    resource       this server's canonical URL, the tokens' expected audience
    scopes         `scopes_supported`, and the scope a 401 asks for
    audience       the expected `aud` when the provider does not use `resource`
    algorithms     accepted JWT algorithms, a subset of ALGORITHMS
    leeway         seconds of clock skew allowed on `exp` and `nbf` (30)
    implies        a scope hierarchy: {"todos:admin": ["todos:write"]}
    introspection  (client_id, client_secret): validate opaque tokens at the
                   provider's introspection endpoint instead of as JWTs
    timeout        the most seconds a request waits for the provider (10)

    The principal is `{"sub", "client_id", "scopes", "claims"}` (a copy per
    request), `scopes` sorted and including the ones `implies` adds.
    """

    def __init__(self, *, issuer: str, resource: str, scopes=(), audience=None,
                 algorithms=ALGORITHMS, leeway: float = 30, implies=None,
                 introspection=None, timeout: float = 10.0):
        self._setup(scopes, implies)
        self.issuer = _secure(issuer, "issuer")
        if urlsplit(issuer).query or urlsplit(issuer).fragment:
            raise ValueError(f"issuer {issuer!r} must have no query or fragment (RFC 8414)")
        u = urlsplit(resource) if isinstance(resource, str) else None
        if not u or u.scheme not in ("http", "https") or not u.netloc or u.query or u.fragment:
            raise ValueError(f"resource {resource!r} must be an absolute URL without query or "
                             f"fragment: this server's canonical URL")
        self.resource = resource
        aud = resource if audience is None else audience
        self.audiences = (aud,) if isinstance(aud, str) else tuple(aud)
        if not self.audiences or not all(isinstance(a, str) and a for a in self.audiences):
            raise ValueError("audience must be a non-empty str or a list of them")
        self.algorithms = (algorithms,) if isinstance(algorithms, str) else tuple(algorithms)
        bad = [a for a in self.algorithms if a not in ALGORITHMS]
        if bad or not self.algorithms:
            raise ValueError(f"algorithms {bad!r}: only asymmetric JWT algorithms are accepted "
                             f"({', '.join(ALGORITHMS)})")
        for name, value, low_ok in (("leeway", leeway, True), ("timeout", timeout, False)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not (0 <= value if low_ok else 0 < value) or not value < math.inf:
                raise ValueError(f"{name} must be a {'non-negative' if low_ok else 'positive'} "
                                 f"number of seconds")
        self.leeway, self.timeout = leeway, timeout
        if introspection is not None and not (
                isinstance(introspection, tuple) and len(introspection) == 2
                and all(isinstance(x, str) and x for x in introspection)):
            raise ValueError("introspection must be (client_id, client_secret)")
        self.introspection = introspection
        self._jwt = _pyjwt() if introspection is None else None
        self._tokens = None

    @classmethod
    def static(cls, tokens: dict, *, scopes=(), implies=None) -> OAuth:
        """No provider, for development and tests: bearer tokens mapped to their
        claims, `{"dev-token": {"sub": "me", "scope": "todos:write"}}`. The
        principal has the same shape as for a validated token. `metadata` is
        None: there is no authorization server to name."""
        self = cls.__new__(cls)
        self._setup(scopes, implies)
        self.issuer = self.resource = self.introspection = self._jwt = None
        if not isinstance(tokens, dict) or not all(
                isinstance(t, str) and t and isinstance(c, dict) for t, c in tokens.items()):
            raise ValueError("tokens must map bearer token strings to claim dicts")
        self._tokens = dict(tokens)
        return self

    def _setup(self, scopes, implies):
        self.scopes = _words(scopes or (), "scopes")
        if "offline_access" in self.scopes:
            raise ValueError("scopes: offline_access asks for refresh tokens, which are the "
                             "client's business, not a requirement of this server")
        if implies is not None and not isinstance(implies, dict):
            raise ValueError("implies must map a scope to the scopes it implies")
        self._implies = {_words(broad, "implies")[0]: _words(narrower, "implies")
                         for broad, narrower in (implies or {}).items()}
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._flights, self._pending, self._executor = {}, 0, None
        self._as = self._keys = self._failed_at = self._key_try = None
        self._complained = {}
        self._cache = OrderedDict()

    @property
    def metadata(self) -> dict | None:
        """The RFC 9728 document to pass as `resource_metadata=`."""
        if self._tokens is not None:
            return None
        doc = {"resource": self.resource, "authorization_servers": [self.issuer],
               "bearer_methods_supported": ["header"]}
        if self.scopes:
            doc["scopes_supported"] = list(self.scopes)
        return doc

    # ── authenticate ───────────────────────────────────────────────────────

    async def __call__(self, headers) -> dict:
        """`authenticate`: the principal for a valid bearer token; `Unauthorized`
        (401) for a missing or invalid one; `Error` 503 when the provider is
        unreachable, too slow, or answers something unusable."""
        scheme, _, token = headers.get("authorization", "").strip().partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise Unauthorized(scope=" ".join(self.scopes) or None)
        if self._tokens is not None:
            claims = self._tokens.get(token)
            if claims is None:
                raise Unauthorized.invalid()
        elif self.introspection is not None:
            claims = await self._introspect(token)
        else:
            claims = await self._verify(token)
        return self._principal(claims)

    def _principal(self, claims) -> dict:
        claims = copy.deepcopy(claims)       # a handler must not change the next request's
        scopes, todo = set(), _granted(claims)
        while todo:
            s = todo.pop()
            if s not in scopes:
                scopes.add(s)
                todo.extend(self._implies.get(s, ()))
        return {"sub": claims.get("sub"), "client_id": claims.get("client_id", claims.get("azp")),
                "scopes": sorted(scopes), "claims": claims}

    async def _verify(self, token) -> dict:
        jwt = self._jwt
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except Exception:                    # never a 500 for bytes a caller chose
            raise Unauthorized.invalid() from None
        key = await self._signing_key(kid)
        try:
            return jwt.decode(token, key, algorithms=list(self.algorithms),
                              audience=list(self.audiences), issuer=self.issuer,
                              leeway=self.leeway, options={"require": ["exp", "iss", "aud"]})
        except jwt.exceptions.PyJWTError:
            raise Unauthorized.invalid() from None
        except Exception:
            log.exception("OAuth: unexpected error while checking a token; refusing it")
            raise Unauthorized.invalid() from None

    # ── fetches from the provider ──────────────────────────────────────────

    def _start(self, key, fn):
        """A future for `fn()` on a fetch thread, shared with every caller that
        asks for `key` while it runs."""
        if self._pid != os.getpid():         # forked: the threads and the lock stayed behind
            self._pid, self._lock = os.getpid(), threading.Lock()
            self._flights, self._pending, self._executor = {}, 0, None
        with self._lock:
            fut = self._flights.get(key)
            if fut is not None:
                return fut
            if self._pending >= FETCH_BACKLOG:
                raise _ProviderError("too many fetches from the provider are waiting")
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    FETCH_THREADS, thread_name_prefix="micromcp-oauth")
            try:
                fut = self._executor.submit(fn)
            except RuntimeError as e:        # the interpreter is shutting down
                raise _ProviderError(str(e)) from None
            self._flights[key] = fut
            self._pending += 1
        fut.add_done_callback(lambda f: self._landed(key, f))
        return fut

    def _landed(self, key, fut):
        with self._lock:
            self._pending -= 1
            if self._flights.get(key) is fut:
                del self._flights[key]

    async def _fetched(self, key, fn):
        """`fn()` on a fetch thread (shared, see `_start`), waited for at most
        `timeout` seconds. Every failure is a _ProviderError."""
        inner = asyncio.wrap_future(self._start(key, fn))
        try:
            return await asyncio.wait_for(asyncio.shield(inner), self.timeout)
        except TimeoutError:
            # The fetch goes on for whoever else waits; if it fails later, its
            # error is read here, not reported as "never retrieved".
            inner.add_done_callback(lambda f: f.cancelled() or f.exception())
            raise _ProviderError(f"no answer within {self.timeout} s") from None

    def _complain(self, what, detail):
        """Log a provider failure, once per RETRY_AFTER per kind: an outage would
        otherwise log once per request."""
        now = time.monotonic()
        with self._lock:
            last = self._complained.get(what)
            if last is not None and now - last < RETRY_AFTER:
                return
            self._complained[what] = now
        log.error("OAuth: %s: %s", what, detail)

    # ── the provider's metadata ────────────────────────────────────────────

    def _discover_now(self) -> dict:
        """Fetch and check the metadata, within one `timeout` for all the URLs.
        The document must name our issuer and carry the endpoint this mode
        needs; otherwise it is not used (and not cached)."""
        deadline = time.monotonic() + self.timeout
        need = "introspection_endpoint" if self.introspection else "jwks_uri"
        problems = []
        for url in _metadata_urls(self.issuer):
            try:
                doc = _fetch(url, deadline)
            except _ProviderError as e:
                problems.append(str(e))
                continue
            if doc.get("issuer") != self.issuer:      # RFC 8414 §3.3: must not be used
                problems.append(f"{url} names issuer {doc.get('issuer')!r}, "
                                f"not {self.issuer!r}")
                continue
            try:
                _secure(doc.get(need), need)
            except ValueError as e:
                problems.append(f"{url}: {e}")
                continue
            return doc
        raise _ProviderError("; ".join(problems))

    def _found(self, doc) -> dict:
        with self._lock:
            first, self._as, self._failed_at = self._as is None, doc, None
        if first:
            self._warn(doc)
        return doc

    def _lost(self, e):
        with self._lock:
            self._failed_at = time.monotonic()
        self._complain(f"no usable metadata for issuer {self.issuer}", e)
        return _unavailable()

    async def _metadata(self) -> dict:
        if self._as is not None:
            return self._as
        if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_AFTER:
            raise _unavailable()
        try:
            return self._found(await self._fetched("metadata", self._discover_now))
        except _ProviderError as e:
            raise self._lost(e) from None

    def discover(self) -> dict:
        """The provider's metadata, fetched now (blocking) if it has not been; it
        is otherwise fetched on the first token. Logs a warning for anything
        that would stop MCP clients from signing in. Raises `Error` 503 when
        there is no usable metadata."""
        if self._tokens is not None:
            raise RuntimeError("OAuth.static() has no provider to discover")
        if self._as is not None:
            return self._as
        try:
            return self._found(self._discover_now())
        except _ProviderError as e:
            raise self._lost(e) from None

    def _warn(self, doc):
        methods = doc.get("code_challenge_methods_supported")
        if not isinstance(methods, list) or "S256" not in methods:
            log.warning("OAuth: %s does not advertise PKCE S256 "
                        "(code_challenge_methods_supported); MCP clients refuse to sign in "
                        "with it", self.issuer)
        if doc.get("client_id_metadata_document_supported") is not True \
                and not doc.get("registration_endpoint"):
            log.warning("OAuth: %s supports neither Client ID Metadata Documents nor dynamic "
                        "client registration; each MCP client must be registered with it by "
                        "hand", self.issuer)

    # ── signing keys ───────────────────────────────────────────────────────

    def _load_keys(self) -> dict:
        """Fetch the key set and keep the keys usable with `algorithms`, by id.
        An empty or unusable set is the provider's fault, not the token's."""
        uri = self._as["jwks_uri"]
        doc = _fetch(uri, time.monotonic() + self.timeout)
        try:
            found = self._jwt.PyJWKSet.from_dict(doc).keys
        except Exception as e:               # PyJWKSetError, malformed entries
            raise _ProviderError(f"{uri}: {type(e).__name__}: {e}") from None
        keys = {k.key_id: k for k in found if k.algorithm_name in self.algorithms}
        if not keys:
            raise _ProviderError(f"{uri}: no signing key for {', '.join(self.algorithms)}")
        self._keys = (keys, time.monotonic())
        return keys

    @staticmethod
    def _pick(keys, kid):
        if kid is None and len(keys) == 1:
            return next(iter(keys.values()))
        return keys.get(kid) if kid is None or isinstance(kid, str) else None

    def _may_load(self, gap) -> bool:
        """Claim the next key load, unless one was tried within `gap` seconds."""
        now = time.monotonic()
        with self._lock:
            if self._key_try is not None and now - self._key_try < gap:
                return False
            self._key_try = now
            return True

    async def _signing_key(self, kid):
        """The key for `kid`. Keys due for a refresh are still used while one
        refresh runs in the background. An unknown kid may force a load at most
        every KEY_COOLDOWN seconds, or joins the one already running."""
        await self._metadata()
        keys, loaded = self._keys or ({}, None)
        key = self._pick(keys, kid)
        if key is not None:
            if time.monotonic() - loaded > KEYS_TTL and self._may_load(KEY_COOLDOWN):
                self._load_in_background()
            return key
        with self._lock:
            running = "keys" in self._flights
        if not running and not self._may_load(KEY_COOLDOWN if keys else RETRY_AFTER):
            if keys:
                raise Unauthorized.invalid()     # an unknown key id; the keys are recent
            raise _unavailable()                 # still no keys since the last failed load
        try:
            keys = await self._fetched("keys", self._load_keys)
        except _ProviderError as e:
            self._complain(f"cannot load the signing keys of {self.issuer}", e)
            raise _unavailable() from None
        key = self._pick(keys, kid)
        if key is None:
            raise Unauthorized.invalid()
        return key

    def _load_in_background(self):
        try:
            fut = self._start("keys", self._load_keys)
        except _ProviderError:
            return

        def landed(f):
            if f.exception() is not None:
                self._complain(f"cannot refresh the signing keys of {self.issuer}",
                               f.exception())
        fut.add_done_callback(landed)

    # ── introspection ──────────────────────────────────────────────────────

    def _ask(self, token) -> dict:
        """Ask the introspection endpoint about `token`."""
        url = self._as["introspection_endpoint"]
        client_id, secret = self.introspection
        basic = base64.b64encode(f"{quote_plus(client_id)}:{quote_plus(secret)}".encode()).decode()
        return _fetch(url, time.monotonic() + self.timeout,
                      data=urlencode({"token": token, "token_type_hint": "access_token"}).encode(),
                      headers={"Authorization": f"Basic {basic}",
                               "Content-Type": "application/x-www-form-urlencoded"})

    async def _introspect(self, token) -> dict:
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and hit[1] > time.time():
            return hit[0]
        await self._metadata()
        try:
            info = await self._fetched(("introspect", key), lambda: self._ask(token))
        except _ProviderError as e:
            self._complain(f"introspection at {self.issuer} failed", e)
            raise _unavailable() from None
        now = time.time()
        claims = self._introspected(info, now)
        exp = claims.get("exp")
        until = now + min(INTROSPECTION_TTL, exp - now if exp is not None else INTROSPECTION_TTL)
        with self._lock:
            self._cache[key] = (claims, until)
            self._cache.move_to_end(key)
            while len(self._cache) > INTROSPECTION_CACHE:
                self._cache.popitem(last=False)
        return claims

    def _introspected(self, info, now) -> dict:
        """An introspection answer, accepted only when it is active, in date
        (finite `exp`/`nbf`), from our provider, and names this server in
        `aud` (RFC 8707: a token for another service must not work here, and
        without `aud` we cannot tell)."""
        def when(k):
            v = info.get(k)
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            return v if ok else None
        aud = info.get("aud")
        auds = {aud} if isinstance(aud, str) else \
            {a for a in aud if isinstance(a, str)} if isinstance(aud, list) else set()
        exp, nbf = when("exp"), when("nbf")
        if info.get("active") is not True \
                or ("exp" in info and (exp is None or exp + self.leeway < now)) \
                or ("nbf" in info and (nbf is None or nbf - self.leeway > now)) \
                or ("iss" in info and info["iss"] != self.issuer) \
                or not auds & set(self.audiences):
            raise Unauthorized.invalid()
        return info

    # ── scopes ─────────────────────────────────────────────────────────────

    def requires(self, *scopes):
        """A guard: the token must carry every one of `scopes` (a broader scope
        that `implies` one counts). Without them the tool is left out of
        listings, and a direct call answers 403 `insufficient_scope` naming all
        of them, which lets the client step up its authorization. Several
        `requires()` on one tool answer with one challenge naming all their
        scopes."""
        need = _words(scopes, "requires")

        def guard(principal):
            self._require(principal, need)
            return True
        return guard

    def check(self, principal, *scopes):
        """The same test inside a handler, for a tool that stays listed: raises
        `Unauthorized` 403 `insufficient_scope` naming every one of `scopes`.
        In a streaming (`Context`) tool, call it first thing: once the stream
        is open the failure can only travel in-band, without the 403."""
        self._require(principal, _words(scopes, "check"))

    def _require(self, principal, need):
        if not isinstance(principal, dict) or not isinstance(principal.get("scopes"), list):
            raise Unauthorized(scope=" ".join(need))
        if not set(need) <= set(principal["scopes"]):
            raise Unauthorized.insufficient_scope(" ".join(need))
