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
and answers with the challenges MCP clients act on. JWT signatures are checked
with PyJWT (`pip install "micromcp[oauth]"`); discovery, introspection, and
the rest use only the standard library.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import math
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
MAX_DOCUMENT = 1024 * 1024    # bytes read from a metadata or introspection response
RETRY_AFTER = 30.0            # seconds before a failed discovery is tried again
KEY_COOLDOWN = 30.0           # seconds between key refetches forced by unknown key ids
INTROSPECTION_TTL = 60.0      # seconds an introspection answer is reused, at most
INTROSPECTION_CACHE = 1024    # introspection answers kept


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Metadata counts only from the URL it was asked for: a 3xx is an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


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


def _fetch(url, timeout, data=None, headers=None) -> dict:
    """A JSON object from `url` (POSTing `data` when given). Raises OSError or
    ValueError; the caller decides what an unreachable provider means."""
    req = urllib.request.Request(url, data=data,
                                 headers={"Accept": "application/json", **(headers or {})})
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read(MAX_DOCUMENT + 1)
    if len(raw) > MAX_DOCUMENT:
        raise ValueError(f"response larger than {MAX_DOCUMENT} bytes")
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("response is not a JSON object")
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
    """PyJWT with its crypto backend, recent enough to rate-limit key refetches."""
    try:
        import jwt
    except ImportError:
        raise ImportError('micromcp.contrib.oauth checks JWTs with PyJWT: '
                          'pip install "micromcp[oauth]"') from None
    if "cooldown_duration" not in inspect.signature(jwt.PyJWKClient).parameters:
        raise ImportError(f"micromcp.contrib.oauth needs PyJWT 2.14 or later (found "
                          f"{jwt.__version__}): earlier ones refetch the provider's keys for "
                          f"every unknown key id")
    if not jwt.algorithms.has_crypto:
        raise ImportError('micromcp.contrib.oauth needs PyJWT\'s crypto backend: '
                          'pip install "micromcp[oauth]"')
    return jwt


class OAuth:
    """The `authenticate` callback, and the protected-resource metadata, for a
    server whose tokens a provider issues. Constructing one does no network
    I/O: the provider's metadata is fetched on the first token (or by
    `discover()`).

    issuer         the provider's issuer identifier, exactly as its metadata
                   and its tokens write it (a trailing slash matters)
    resource       this server's canonical URL, the tokens' expected audience
    scopes         `scopes_supported`, and the scope a 401 asks for
    audience       the expected `aud` when the provider does not use `resource`
    algorithms     accepted JWT algorithms, a subset of ALGORITHMS
    leeway         seconds of clock skew allowed on `exp` and `nbf`
    implies        a scope hierarchy: {"todos:admin": ["todos:write"]}
    introspection  (client_id, client_secret): validate opaque tokens at the
                   provider's introspection endpoint instead of as JWTs
    timeout        seconds for each request to the provider

    The principal is `{"sub", "client_id", "scopes", "claims"}`, `scopes`
    sorted and including the ones `implies` adds.
    """

    def __init__(self, *, issuer: str, resource: str, scopes=(), audience=None,
                 algorithms=ALGORITHMS, leeway: float = 30, implies=None,
                 introspection=None, timeout: float = 10.0):
        self._setup(scopes, implies)
        self.issuer = _secure(issuer, "issuer")
        u = urlsplit(resource) if isinstance(resource, str) else None
        if not u or u.scheme not in ("http", "https") or not u.netloc or u.query or u.fragment:
            raise ValueError(f"resource {resource!r} must be an absolute URL without query or "
                             f"fragment: this server's canonical URL")
        self.resource = resource
        aud = resource if audience is None else audience
        self.audiences = (aud,) if isinstance(aud, str) else tuple(aud)
        if not self.audiences or not all(isinstance(a, str) and a for a in self.audiences):
            raise ValueError("audience must be a non-empty str or a list of them")
        self.algorithms = tuple(algorithms)
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
        self.scopes = _words(scopes, "scopes")
        if "offline_access" in self.scopes:
            raise ValueError("scopes: offline_access asks for refresh tokens, which are the "
                             "client's business, not a requirement of this server")
        self._implies = {_words(broad, "implies")[0]: _words(narrower, "implies")
                         for broad, narrower in (implies or {}).items()}
        self._lock = threading.Lock()
        self._as = self._keys = self._failed_at = None
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

    def __call__(self, headers) -> dict:
        """`authenticate`: the principal for a valid bearer token; `Unauthorized`
        (401) for a missing or invalid one; `Error` 503 when the provider
        cannot be reached."""
        scheme, _, token = headers.get("authorization", "").strip().partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise Unauthorized(scope=" ".join(self.scopes) or None)
        if self._tokens is not None:
            claims = self._tokens.get(token)
            if claims is None:
                raise Unauthorized.invalid()
        elif self.introspection is not None:
            claims = self._introspect(token)
        else:
            claims = self._verify(token)
        return self._principal(claims)

    def _principal(self, claims) -> dict:
        scopes, todo = set(), _granted(claims)
        while todo:
            s = todo.pop()
            if s not in scopes:
                scopes.add(s)
                todo.extend(self._implies.get(s, ()))
        return {"sub": claims.get("sub"), "client_id": claims.get("client_id", claims.get("azp")),
                "scopes": sorted(scopes), "claims": claims}

    def _verify(self, token) -> dict:
        jwt = self._jwt
        if self._keys is None:
            keys = jwt.PyJWKClient(self._endpoint("jwks_uri"), cache_jwk_set=True, lifespan=300,
                                   timeout=self.timeout, cooldown_duration=KEY_COOLDOWN)
            with self._lock:
                self._keys = self._keys or keys
        try:
            key = self._keys.get_signing_key_from_jwt(token)
            return jwt.decode(token, key, algorithms=list(self.algorithms),
                              audience=list(self.audiences), issuer=self.issuer,
                              leeway=self.leeway, options={"require": ["exp", "iss", "aud"]})
        except jwt.exceptions.PyJWKClientConnectionError as e:
            log.error("OAuth: cannot fetch the signing keys of %s: %s", self.issuer, e)
            raise _unavailable() from None
        except jwt.exceptions.PyJWTError:
            raise Unauthorized.invalid() from None

    def _introspect(self, token) -> dict:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now:
                return hit[0]
            self._cache.pop(key, None)
        url = self._endpoint("introspection_endpoint")
        client_id, secret = self.introspection
        basic = base64.b64encode(f"{quote_plus(client_id)}:{quote_plus(secret)}".encode()).decode()
        try:
            info = _fetch(url, self.timeout,
                          data=urlencode({"token": token,
                                          "token_type_hint": "access_token"}).encode(),
                          headers={"Authorization": f"Basic {basic}",
                                   "Content-Type": "application/x-www-form-urlencoded"})
        except (OSError, ValueError) as e:
            log.error("OAuth: introspection at %s failed: %s", url, e)
            raise _unavailable() from None
        claims = self._introspected(info, now)
        exp = claims.get("exp")
        until = now + min(INTROSPECTION_TTL, exp - now if exp is not None else INTROSPECTION_TTL)
        with self._lock:
            self._cache[key] = (claims, until)
            while len(self._cache) > INTROSPECTION_CACHE:
                self._cache.popitem(last=False)
        return claims

    def _introspected(self, info, now) -> dict:
        """An introspection answer, accepted only when it is active, in date,
        from our provider, and names this server in `aud` (RFC 8707: a token
        for another service must not work here, and without `aud` we cannot
        tell)."""
        def when(k):
            v = info.get(k)
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
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

    # ── the provider ───────────────────────────────────────────────────────

    def discover(self) -> dict:
        """The provider's metadata, fetched now if it has not been (otherwise it
        is fetched on the first token), with a warning logged for anything that
        would stop MCP clients from signing in. Raises `Error` 503 when there is
        no usable metadata; a failure is retried after RETRY_AFTER seconds."""
        if self._tokens is not None:
            raise RuntimeError("OAuth.static() has no provider to discover")
        with self._lock:
            if self._as is not None:
                return self._as
            if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_AFTER:
                raise _unavailable()
            problems = []
            for url in _metadata_urls(self.issuer):
                try:
                    doc = _fetch(url, self.timeout)
                except (OSError, ValueError) as e:
                    problems.append(f"{url}: {e}")
                    continue
                if doc.get("issuer") != self.issuer:      # RFC 8414 §3.3: must not be used
                    problems.append(f"{url} names issuer {doc.get('issuer')!r}, "
                                    f"not {self.issuer!r}")
                    continue
                self._as = doc
                break
            else:
                self._failed_at = time.monotonic()
                log.error("OAuth: no usable metadata for issuer %s: %s", self.issuer,
                          "; ".join(problems))
                raise _unavailable()
        self._warn(self._as)
        return self._as

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

    def _endpoint(self, name):
        url = self.discover().get(name)
        try:
            return _secure(url, name)
        except ValueError as e:
            log.error("OAuth: the metadata of %s: %s", self.issuer, e)
            raise _unavailable() from None

    # ── scopes ─────────────────────────────────────────────────────────────

    def requires(self, *scopes):
        """A guard: the token must carry every one of `scopes` (a broader scope
        that `implies` one counts). Without them the tool is left out of
        listings, and a direct call answers 403 `insufficient_scope` naming all
        of them, which lets the client step up its authorization."""
        need = _words(scopes, "requires")

        def guard(principal):
            self._require(principal, need)
            return True
        return guard

    def check(self, principal, *scopes):
        """The same test inside a handler, for a tool that stays listed: raises
        `Unauthorized` 403 `insufficient_scope` naming every one of `scopes`.
        Call it before a streaming tool's first notification."""
        self._require(principal, _words(scopes, "check"))

    def _require(self, principal, need):
        if not isinstance(principal, dict) or not isinstance(principal.get("scopes"), list):
            raise Unauthorized(scope=" ".join(need))
        if not set(need) <= set(principal["scopes"]):
            raise Unauthorized.insufficient_scope(" ".join(need))
