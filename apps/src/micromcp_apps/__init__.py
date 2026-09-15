"""micromcp-apps — MCP Apps widgets for micromcp servers.

A widget is a static HTML page the host renders in a sandboxed iframe next to
the conversation; the host proxies its tool calls to your server. This
package builds those pages and the server side they talk to:

    Widget       a page around the bridge (or your own document), published as
                 a `ui://` resource by the tools that show it: `@board.tool(mcp)`
    fragment()   an HTML fragment result for the widget to swap in, optionally
                 with model context
    page()       the bare document builder `Widget` uses
    Channel      WebSocket-style messaging between open widgets and server code
    BRIDGE_JS    the widget-side client: MCP Apps handshake, `mcp.callTool`,
                 `mcp.fetch` (hypermedia over tool calls), model context,
                 `mcp.channel` / `mcp.WebSocket`
    micromcp_apps.django
                 Django views served to widgets (`django_routes`,
                 `set_mcp_context`)
    DEVHOST_HTML a development MCP Apps host page (bytes) to serve next to a server

Everything here uses only micromcp's public API (`MCP.tool(meta=,
visibility=)`, `MCP.resource`, `result`).

SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import pathlib

from .channels import CHANNEL_IDLE, CHANNEL_QUEUE, CHANNEL_WAIT, Channel, Connection
from .widget import BRIDGE_JS, CONTEXT_LIMIT, CONTEXT_META, FRAGMENT_META, Widget, fragment, page

__version__ = "0.1.0"

DEVHOST_HTML = pathlib.Path(__file__).with_name("devhost.html").read_bytes()

__all__ = [
    "Widget", "fragment", "page", "Channel", "Connection", "BRIDGE_JS", "DEVHOST_HTML",
    "CONTEXT_META", "FRAGMENT_META", "CONTEXT_LIMIT", "CHANNEL_WAIT", "CHANNEL_IDLE",
    "CHANNEL_QUEUE", "__version__",
]
