"""Protocol constants, error codes, and default tunables."""

from __future__ import annotations

import logging
import re

log = logging.getLogger("micromcp")

PROTOCOL = "2026-07-28"
META_VER = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"
META_SUB = "io.modelcontextprotocol/subscriptionId"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
# Handshake-era revisions served, per request and without sessions, when a
# server is built with legacy="stateless". Newest first: it is the answer to
# an initialize naming a version we do not know.
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
WELL_KNOWN = "/.well-known/oauth-protected-resource"   # RFC 9728

# JSON-RPC / MCP error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020         # spec-allocated
UNSUPPORTED_VERSION = -32022     # spec-allocated; must carry data.supported + data.requested
UNAUTHORIZED = -32001            # implementation-defined range; travels under HTTP 401

# Methods that exist only in the initialize-handshake era (2024-11-05 .. 2025-11-25).
# Refused precisely by default; answered per request under legacy="stateless".
HANDSHAKE_METHODS = frozenset({"initialize", "notifications/initialized", "ping"})
LIST_METHODS = frozenset({"tools/list", "resources/list", "resources/templates/list",
                          "prompts/list"})
ROUTING_HEADERS = ("mcp-protocol-version", "mcp-method", "mcp-name")
# Headers whose two copies would make this server and a proxy disagree about
# who is asking; rejected outright when they repeat (ASGI sees the raw pairs).
SINGLETON_HEADERS = ROUTING_HEADERS + ("origin", "host", "authorization",
                                       "content-type", "content-length")
CORS_HEADERS = "Content-Type, Accept, Authorization, MCP-Protocol-Version, Mcp-Method, Mcp-Name"

# Defaults for the per-server tunables (all overridable in the constructor).
MAX_BODY = 4 * 1024 * 1024   # request body cap
MAX_URI = 2048               # longest resource URI we will try to match
MAX_DEPTH = 64               # deepest JSON nesting validated or converted
OFFLOAD_BYTES = 64 * 1024    # bodies larger than this are parsed/validated off the loop
QUEUE_SIZE = 64              # SSE frames buffered ahead of the client before the handler blocks
STREAM_BUDGET = 64 * 1024 * 1024   # notification bytes one call may emit
KEEPALIVE = 15.0             # seconds of SSE silence before a comment frame is sent
CANCEL_GRACE = 2.0           # seconds a cancelled handler gets to unwind before we log it
WORKERS = 32                 # thread-pool size for sync handlers
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # the LLM-side tool-name grammar
