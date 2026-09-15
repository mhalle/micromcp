"""micromcp.apps — MCP Apps widgets for micromcp servers (experimental).

A widget is a static HTML page the host renders in a sandboxed iframe next to
the conversation; the host proxies its tool calls to your server. This
package builds those pages and the server side they talk to:

    Widget       a page around the bridge (or your own document), published as
                 a `ui://` resource by the tools that show it: `@board.tool(mcp)`
    fragment()   an HTML fragment result for the widget to swap in, optionally
                 with model context
    page()       the bare document builder `Widget` uses
    tool_url()   a `tool:` URL for hypermedia attributes, arguments encoded
    Channel      WebSocket-style messaging between open widgets and server code
    BRIDGE_JS    the widget-side client: MCP Apps handshake, `mcp.callTool`,
                 `mcp.fetch` (hypermedia over tool calls), model context,
                 `mcp.channel` / `mcp.WebSocket`
    micromcp.apps.django
                 Django views served to widgets (`django_routes`,
                 `set_mcp_context`)
    DEVHOST_HTML a development MCP Apps host page (bytes) to serve next to a server

Experimental: it follows the MCP Apps extension (2026-01-26) and the hosts
that implement it, and may change in a minor release. `import micromcp` does
not load it, its names are not in `micromcp.__all__`, it uses only
micromcp's public API, and it is not part of the single-file bundle.

SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import pathlib

from .channels import CHANNEL_IDLE, CHANNEL_QUEUE, CHANNEL_WAIT, Channel, Connection
from .widget import (BRIDGE_JS, CONTEXT_LIMIT, CONTEXT_META, FRAGMENT_META, SupportsHTML,
                     Widget, fragment, page, tool_url)


DEVHOST_HTML = pathlib.Path(__file__).with_name("devhost.html").read_bytes()

__all__ = [
    "Widget", "fragment", "page", "tool_url", "SupportsHTML",
    "Channel", "Connection", "BRIDGE_JS", "DEVHOST_HTML",
    "CONTEXT_META", "FRAGMENT_META", "CONTEXT_LIMIT", "CHANNEL_WAIT", "CHANNEL_IDLE",
    "CHANNEL_QUEUE",
]
