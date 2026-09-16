"""A kanban board rendered by the server, with no bundler and no app logic.

htmx 4 is vendored from `examples/vendor/`, every click is a `tools/call`, and each app-only
tool answers with `fragment()` — the HTML the widget swaps in. A channel keeps two open
boards in step, so a card the model adds appears in both without a reload; pushed HTML goes
through the toolkit's own swap, or the `hx-*` attributes in it would arrive dead. Note that a
widget cannot submit a `<form>`: the frame is sandboxed, so the button carries `hx-post` and
names its field with `hx-include`.

Needs fastcore for the components (`pip install fastcore`).

    python examples/mcp_app_board.py
    open http://127.0.0.1:8774/devhost?mcp=/mcp
"""
from __future__ import annotations

import os

import itertools
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server


from micromcp import MCP, Server
from micromcp.apps import DEVHOST_HTML, Channel, Widget, fragment, tool_url

HERE = Path(__file__).parent
PORT = int(os.environ.get("PORT", "8774"))
ORIGIN = f"http://127.0.0.1:{PORT}"

COLUMNS = [("todo", "To do"), ("doing", "In progress"), ("done", "Done")]
COLUMN_IDS = [c for c, _ in COLUMNS]


# ---------------------------------------------------------------- board state

class Board:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.cards: list[dict] = []
        for column, text in [("todo", "Vendor htmx 4"), ("todo", "Write the columns"),
                             ("doing", "Wire the channel"), ("done", "Read docs/apps.md")]:
            self.add(column, text)

    def add(self, column: str, text: str) -> dict | None:
        text = (text or "").strip()
        if not text or column not in COLUMN_IDS:
            return None
        card = {"id": f"c{next(self._ids)}", "column": column, "text": text[:200]}
        with self._lock:
            self.cards.append(card)
        return card

    def get(self, card_id: str) -> dict | None:
        return next((c for c in self.cards if c["id"] == card_id), None)

    def move(self, card_id: str, to: str) -> dict | None:
        card = self.get(card_id)
        if card is None or to not in COLUMN_IDS:
            return None
        with self._lock:
            card["column"] = to
        return card

    def step(self, card_id: str, delta: int) -> dict | None:
        card = self.get(card_id)
        if card is None:
            return None
        i = min(len(COLUMN_IDS) - 1, max(0, COLUMN_IDS.index(card["column"]) + delta))
        return self.move(card_id, COLUMN_IDS[i])

    def remove(self, card_id: str) -> dict | None:
        card = self.get(card_id)
        if card is not None:
            with self._lock:
                self.cards.remove(card)
        return card

    def in_column(self, column: str) -> list[dict]:
        return [c for c in self.cards if c["column"] == column]

    def snapshot(self) -> dict:
        return {c: [card["text"] for card in self.in_column(c)] for c in COLUMN_IDS}


board = Board()


# -------------------------------------------------------------------- render

def card_el(card: dict, index: int):
    """One card: its text, and the controls that move or delete it."""
    from fastcore.xml import Button, Div, Li, Span
    i = COLUMN_IDS.index(card["column"])
    return Li(
        Span(card["text"], cls="card-text"),                      # escaped by fastcore
        Div(
            Button("←", cls="ghost", title="Move left", disabled=(i == 0) or None,
                   hx_post=tool_url("card_move", id=card["id"], delta="-1"),
                   hx_target="#app", hx_swap="innerHTML"),
            Button("→", cls="ghost", title="Move right",
                   disabled=(i == len(COLUMN_IDS) - 1) or None,
                   hx_post=tool_url("card_move", id=card["id"], delta="1"),
                   hx_target="#app", hx_swap="innerHTML"),
            Button("✕", cls="ghost danger", title="Delete",
                   hx_post=tool_url("card_delete", id=card["id"]),
                   hx_target="#app", hx_swap="innerHTML"),
            cls="card-actions"),
        cls="card", data_card=card["id"], style=f"--i:{index}")


def column_el(column: str, label: str):
    from fastcore.xml import Button, Div, Input, Section, Span, Ul
    cards = board.in_column(column)
    return Section(
        Div(Span(label, cls="col-name"), Span(str(len(cards)), cls="count"), cls="col-head"),
        Ul(*[card_el(c, i) for i, c in enumerate(cards)], cls="cards", data_column=column),
        # Not a <form>: a widget's iframe is sandboxed without allow-forms, so a
        # native submit is blocked by the browser before htmx ever sees it.
        Div(
            Input(name="text", id=f"add-{column}", placeholder="Add a card…",
                  autocomplete="off", maxlength="200", data_add=column,
                  aria_label=f"Add a card to {label}"),
            Button("+", cls="add", title=f"Add to {label}",
                   hx_post=tool_url("card_add", column=column),
                   hx_include=f"#add-{column}", hx_target="#app", hx_swap="innerHTML"),
            cls="add-form"),
        cls="column", data_column=column)


def render_board():
    from fastcore.xml import Div
    return Div(*[column_el(c, label) for c, label in COLUMNS], cls="board")


def board_html() -> str:
    from fastcore.xml import to_xml
    return to_xml(render_board())


def context_text() -> str:
    snap = board.snapshot()
    parts = [f"{label}: " + (", ".join(snap[c]) if snap[c] else "(empty)")
             for c, label in COLUMNS]
    return "Kanban board — " + " | ".join(parts)


def model_context(source: str) -> dict:
    """What the model is told. `source` says which path carried it: the tool
    result's context=, or mcp.setContext() after a channel push."""
    return {"text": context_text(), "data": {"columns": board.snapshot(), "source": source}}


# -------------------------------------------------------------- server + widget

mcp = MCP("kanban", "1.0.0")

STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f6f7f9; --panel: #ffffff; --ink: #14181f; --muted: #5d6674;
  --line: #dfe3e9; --accent: #3b6cf6; --danger: #c0392b; --shadow: 0 1px 2px rgba(16,20,28,.10);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171c; --panel: #1c2027; --ink: #e8ebf0; --muted: #98a1b0;
    --line: #2c323c; --accent: #7aa2ff; --danger: #ff7a6b; --shadow: 0 1px 2px rgba(0,0,0,.45);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 14px; background: var(--bg); color: var(--ink);
  font: 15px/1.45 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
h1 { font-size: 15px; font-weight: 650; margin: 0 0 12px; letter-spacing: .01em; }
h1 .hint { font-weight: 400; color: var(--muted); margin-left: 8px; font-size: 13px; }
/* A widget panel is narrow and its width is the host's choice, so let the
   columns reflow rather than pinning three of them. */
.board { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; align-items: start; }
.column {
  background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 10px; box-shadow: var(--shadow); min-height: 120px;
}
.col-head { display: flex; align-items: center; justify-content: space-between; margin: 2px 4px 10px; }
.col-name { font-weight: 640; font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.count {
  font-size: 12px; font-variant-numeric: tabular-nums; color: var(--muted);
  background: var(--bg); border: 1px solid var(--line); border-radius: 999px; padding: 1px 8px;
}
.cards { list-style: none; margin: 0 0 10px; padding: 0; display: flex; flex-direction: column; gap: 8px; min-height: 8px; }
.card {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 8px;
  background: var(--bg); border: 1px solid var(--line); border-left: 3px solid var(--accent);
  border-radius: 9px; padding: 8px 8px 8px 10px;
}
.card-text { overflow-wrap: break-word; hyphens: auto; }
.card-actions { display: flex; gap: 2px; flex: none; }
button { font: inherit; cursor: pointer; border-radius: 7px; border: 1px solid transparent; }
.ghost {
  background: transparent; color: var(--muted); line-height: 1; padding: 3px 6px; font-size: 14px;
}
.ghost:hover:not(:disabled) { background: var(--panel); border-color: var(--line); color: var(--ink); }
.ghost:disabled { opacity: .3; cursor: default; }
.ghost.danger:hover:not(:disabled) { color: var(--danger); border-color: var(--danger); }
.add-form { display: flex; gap: 6px; }
.add-form input {
  flex: 1; min-width: 0; font: inherit; color: var(--ink); background: var(--bg);
  border: 1px solid var(--line); border-radius: 7px; padding: 6px 8px;
}
.add-form input:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.add { background: var(--accent); color: #fff; border: none; padding: 6px 12px; font-weight: 600; }
.add:hover { filter: brightness(1.08); }
.htmx-request .card, .htmx-request.card { opacity: .75; }
"""

SYNC_JS = """
// Enter-to-add. htmx's own trigger filters (hx-trigger="keyup[key=='Enter']")
// compile the expression with eval, which the MCP Apps policy forbids:
// "Evaluating a string as JavaScript violates the following Content Security
// Policy directive because 'unsafe-eval' is not an allowed source of script".
// A delegated listener that clicks the column's + button is eval-free.
let lastAdd = null;
document.addEventListener("keydown", (e) => {
  const input = e.target.closest?.("input[data-add]");
  if (!input || e.key !== "Enter") return;
  lastAdd = input.dataset.add;
  input.parentElement.querySelector("button.add").click();
});
document.addEventListener("click", (e) => {
  const b = e.target.closest?.("button.add");
  if (b) lastAdd = b.parentElement.querySelector("input[data-add]").dataset.add;
});
// The whole board is swapped, so put the caret back where the user was typing.
document.addEventListener("htmx:after:swap", () => {
  if (!lastAdd) return;
  document.getElementById("add-" + lastAdd)?.focus();
});

(async () => {
  await mcp.ready;                       // the handshake first, then the channel
  const ws = mcp.channel("board");
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    // htmx.swap() rather than innerHTML: it processes the hx-* attributes in
    // the HTML that arrives, so pushed cards stay clickable.
    htmx.swap({text: msg.html, target: document.getElementById("app"), swap: "innerHTML"});
    if (msg.context) mcp.setContext(msg.context.text, msg.context.data);
  };
})();
"""

widget = Widget(
    "kanban",
    title="Kanban board",
    body=('<h1>Kanban <span class="hint">every click is a tool call</span></h1>'
          '<div id="app" hx-post="tool:board_view" hx-trigger="mcp:ready" '
          'hx-target="#app" hx-swap="innerHTML">Loading…</div>'),
    styles=[STYLE],
    scripts=[HERE / "vendor" / "htmx.min.js", SYNC_JS],
)

sync = Channel(mcp, "board")


@sync.on_connect
def joined(conn):
    conn.send_json({"html": board_html(), "context": model_context("channel")})


def push() -> None:
    """Tell every open widget what the board looks like now."""
    sync.broadcast_json({"html": board_html(), "context": model_context("channel")})


# ------------------------------------------------------- tools the widget calls

@mcp.tool(visibility="app")
def board_view():
    """Render the whole board."""
    return fragment(render_board(), context=model_context("tool"))


@mcp.tool(visibility="app")
def card_add(column: str = "todo", text: str = ""):
    """Add a card the user typed."""
    board.add(column, text)
    push()
    return fragment(render_board(), context=model_context("tool"))


@mcp.tool(visibility="app")
def card_move(id: str = "", delta: str = "1"):
    """Move a card one column left or right."""
    board.step(id, int(delta))
    push()
    return fragment(render_board(), context=model_context("tool"))


@mcp.tool(visibility="app")
def card_delete(id: str = ""):
    """Delete a card."""
    board.remove(id)
    push()
    return fragment(render_board(), context=model_context("tool"))


# --------------------------------------------------------- tools the model calls

@widget.tool(mcp, read_only=True)
def show_board() -> str:
    """Open the kanban board."""
    return context_text()


@mcp.tool
def board_add_card(column: str = "todo", text: str = "") -> dict:
    """Add a card to a column of the kanban board (todo, doing, done)."""
    card = board.add(column, text)
    if card is None:
        return {"ok": False, "error": f"empty text, or unknown column {column!r}"}
    push()
    return {"ok": True, "card": card, "board": board.snapshot()}


@mcp.tool
def board_move_card(id: str = "", to: str = "doing") -> dict:
    """Move a kanban card to a column (todo, doing, done)."""
    card = board.move(id, to)
    if card is None:
        return {"ok": False, "error": f"no card {id!r}, or unknown column {to!r}"}
    push()
    return {"ok": True, "card": card, "board": board.snapshot()}


@mcp.tool
def board_delete_card(id: str = "") -> dict:
    """Delete a kanban card by id."""
    card = board.remove(id)
    if card is None:
        return {"ok": False, "error": f"no card {id!r}"}
    push()
    return {"ok": True, "deleted": card, "board": board.snapshot()}


@mcp.tool(read_only=True)
def board_state() -> dict:
    """The current cards, by column, with their ids."""
    return {"cards": board.cards, "columns": board.snapshot()}


# ------------------------------------------------------------------ the server

endpoint = Server(mcp, path="/mcp", allowed_origins={ORIGIN})


def app(environ, start_response):
    if environ.get("PATH_INFO") == "/devhost":
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(DEVHOST_HTML)))])
        return [DEVHOST_HTML]
    return endpoint(environ, start_response)


class Threaded(ThreadingMixIn, WSGIServer):
    daemon_threads = True


if __name__ == "__main__":
    print(f"kanban board: {ORIGIN}/devhost?mcp=/mcp", flush=True)
    make_server("127.0.0.1", PORT, app, server_class=Threaded).serve_forever()
