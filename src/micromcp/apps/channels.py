"""Channels: WebSocket-style messaging between widgets and this server.

A widget cannot open a socket, and the host delivers a tool result only to the
widget whose call it answers, so changes made elsewhere need a way in. A
`Channel` is that way in; the widget sees the WebSocket API (`mcp.channel()`,
`mcp.WebSocket` in the bridge), carried by four app-only tools.
"""

from __future__ import annotations

import asyncio
import collections
import inspect
import json
import logging
import math
import re
import secrets
import threading
import time
import weakref
from urllib.parse import parse_qsl

from micromcp import Principal, result

log = logging.getLogger("micromcp.apps")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # the tool-name grammar micromcp enforces
_CHANNELS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()   # MCP -> {name: Channel}

CHANNEL_WAIT = 20.0      # seconds channel_recv may hold a request open (Claude allows 20)
CHANNEL_IDLE = 90.0      # seconds without a request before a connection is dropped
CHANNEL_QUEUE = 1000     # frames queued for one connection before it is closed as too slow
CHANNEL_BYTES = 16 * 1024 * 1024   # bytes queued for one connection before it is closed
CHANNEL_CONNECTIONS = 1000         # open connections per channel; further opens are refused


def _wake(waiters):
    for loop, event in waiters:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:        # that loop has closed
            pass


async def _callback(fn, *args):
    """Run a channel callback: an async one on the loop, a sync one on a worker thread."""
    if fn is None:
        return
    if inspect.iscoroutinefunction(fn) \
            or inspect.iscoroutinefunction(getattr(fn, "__call__", None)):  # noqa: B004
        await fn(*args)
    else:
        await asyncio.to_thread(fn, *args)


class Connection:
    """One widget's end of a `Channel`, the way a WebSocket server sees a client.

    `send(text)` / `send_json(obj)` queue a frame for the widget and `close()` ends the
    connection (the widget's socket closes on its next receive); all three are safe from any
    thread and from sync or async code. `params` is the query of the URL the widget opened
    (`mcp:scene?partial=1` gives `{"partial": "1"}`), `principal` is who opened it: a
    connection is usable only by that principal."""

    def __init__(self, channel: Channel, principal, params: dict[str, str]):
        self.id = secrets.token_urlsafe(18)
        self.channel, self.principal, self.params = channel, principal, params
        self._frames: collections.deque[str] = collections.deque()
        self._bytes = 0                     # UTF-8 size of the queued frames
        self._lock = threading.Lock()
        self._waiters: set = set()
        self._closed = self._finished = False
        self._seen = time.monotonic()

    def __repr__(self):
        state = ", closed" if self._closed else ""
        return f"Connection({self.channel.name!r}, {self.id[:8]}…{state})"

    @property
    def closed(self) -> bool:
        return self._closed

    def send(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("Connection.send takes text; use send_json for other values")
        size = len(text.encode("utf-8"))
        with self._lock:
            if self._closed:
                return
            full = (len(self._frames) >= self.channel.max_queue
                    or self._bytes + size > self.channel.max_bytes)
            if not full:
                self._frames.append(text)
                self._bytes += size
            waiters = list(self._waiters)
        if full:
            log.warning("channel %r: a connection fell too far behind (max_queue=%d frames, "
                        "max_bytes=%d); closing it", self.channel.name, self.channel.max_queue,
                        self.channel.max_bytes)
            self.close()
        else:
            _wake(waiters)

    def send_json(self, value) -> None:
        self.send(json.dumps(value, allow_nan=False, separators=(",", ":")))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            waiters = list(self._waiters)
        _wake(waiters)

    def _drain(self) -> list[str]:
        with self._lock:
            frames = list(self._frames)
            self._frames.clear()
            self._bytes = 0
        return frames

    async def _wait(self, seconds: float) -> None:
        if not seconds > 0:                 # also NaN
            return
        entry = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            if self._frames or self._closed:
                return
            self._waiters.add(entry)
        try:
            await asyncio.wait_for(entry[1].wait(), seconds)
        except TimeoutError:
            pass
        finally:
            with self._lock:
                self._waiters.discard(entry)


class Channel:
    """A named, WebSocket-style message channel between this server's widgets and its code.

        scene = Channel(mcp, "scene")

        @scene.on_connect
        def joined(conn):                      # conn.params: the URL query the widget opened with
            conn.send_json(snapshot())

        @scene.on_message
        def received(conn, text): ...          # text frames, as on a WebSocket

        scene.broadcast_json(change)           # from any tool, sync or async

    In the widget, `mcp.channel("scene")` returns a WebSocket-compatible object (and
    `mcp.WebSocket` is its constructor, for libraries that take one). Underneath are four
    app-only tools shared by every channel of `mcp`, registered with its first channel:
    `channel_open`, `channel_send`, `channel_recv` (a long poll that returns as soon as a
    frame is queued, or after `wait` seconds) and `channel_close`. Callbacks may be sync
    (run on a worker thread) or async. A callback that raises is logged and its connection
    closed; the widget sees the close, never the exception.

    guards      run on open with the principal; a connection stays bound to its principal
    wait        longest a receive may wait for a frame, in seconds
    idle        a connection unheard from this long is dropped (there is no socket to notice a
                widget going away); `on_disconnect` runs on a later channel request
    max_queue   frames queued for one connection before it is closed as too slow
    max_bytes   bytes (UTF-8) queued for one connection before it is closed as too slow
    max_connections
                open connections on this channel; further opens are refused until some close
                or time out, so one client cannot grow the server's memory without bound
    """

    def __init__(self, mcp, name: str, *, guards=(), wait: float = CHANNEL_WAIT,
                 idle: float = CHANNEL_IDLE, max_queue: int = CHANNEL_QUEUE,
                 max_bytes: int = CHANNEL_BYTES, max_connections: int = CHANNEL_CONNECTIONS):
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"channel name {name!r} must match {_NAME_RE.pattern}")
        if not (math.isfinite(wait) and wait >= 0 and math.isfinite(idle) and idle > 0
                and max_queue >= 1 and max_bytes >= 1 and max_connections >= 1):
            raise ValueError("wait must be >= 0 and idle > 0 (finite seconds); max_queue, "
                             "max_bytes, and max_connections must be >= 1")
        channels = _CHANNELS.get(mcp)
        if channels is not None and name in channels:
            raise ValueError(f"channel {name!r} is already registered")
        if channels is None:
            channels = {}
            _channel_tools(mcp, channels)      # raises, recording nothing, if the names are taken
            _CHANNELS[mcp] = channels
        channels[name] = self
        self.name, self.guards = name, tuple(guards)
        self.wait, self.idle, self.max_queue = float(wait), float(idle), int(max_queue)
        self.max_bytes, self.max_connections = int(max_bytes), int(max_connections)
        self._conns: dict[str, Connection] = {}
        self._swept = 0.0                   # when idle connections were last looked for
        self._lock = threading.Lock()
        self._on_connect = self._on_message = self._on_disconnect = None

    def __repr__(self):
        return f"Channel({self.name!r}, {len(self.connections)} open)"

    def on_connect(self, fn):
        """Decorator: `fn(conn)` runs when a widget connects; frames it sends arrive first."""
        self._on_connect = fn
        return fn

    def on_message(self, fn):
        """Decorator: `fn(conn, text)` runs for each frame a widget sends, in order."""
        self._on_message = fn
        return fn

    def on_disconnect(self, fn):
        """Decorator: `fn(conn)` runs once a connection has closed or timed out."""
        self._on_disconnect = fn
        return fn

    @property
    def connections(self) -> list[Connection]:
        with self._lock:
            return [c for c in self._conns.values() if not c.closed]

    def broadcast(self, text: str, *, exclude=None) -> None:
        """Queue a text frame for every open connection. `exclude` leaves one
        out, by `Connection` or by id: the widget whose own action caused the
        broadcast has usually rendered the change already, and its page knows
        its id as `mcp.channel(...).id`."""
        skip = getattr(exclude, "id", exclude)
        for conn in self.connections:
            if conn.id != skip:
                conn.send(text)

    def broadcast_json(self, value, *, exclude=None) -> None:
        self.broadcast(json.dumps(value, allow_nan=False, separators=(",", ":")),
                       exclude=exclude)

    def _admits(self, principal) -> bool:
        try:
            return all(g(principal) for g in self.guards)
        except Exception:
            log.exception("guard for channel %r raised; refusing", self.name)
            return False

    def _get(self, conn_id: str, principal) -> Connection | None:
        with self._lock:
            conn = self._conns.get(conn_id)
        if conn is None or conn._finished or conn.principal != principal:
            return None
        conn._seen = time.monotonic()
        return conn

    async def _open(self, principal, params: dict[str, str]) -> Connection | None:
        await self._sweep()
        if not self._admits(principal):
            return None
        with self._lock:
            full = len(self._conns) >= self.max_connections
        if full:
            await self._sweep(force=True)
        conn = Connection(self, principal, params)
        with self._lock:
            if len(self._conns) >= self.max_connections:
                conn = None
            else:
                self._conns[conn.id] = conn
        if conn is None:
            log.warning("channel %r has max_connections=%d open; refusing another",
                        self.name, self.max_connections)
            return None
        try:
            await _callback(self._on_connect, conn)
        except Exception:
            log.exception("on_connect for channel %r raised; closing the connection", self.name)
            await self._finish(conn)
            return None
        return conn

    async def _finish(self, conn: Connection) -> None:
        with self._lock:
            if conn._finished:
                return
            conn._finished = True
            self._conns.pop(conn.id, None)
        conn.close()
        try:
            await _callback(self._on_disconnect, conn)
        except Exception:
            log.exception("on_disconnect for channel %r raised", self.name)

    async def _sweep(self, force: bool = False) -> None:
        """Finish closed and idle connections. Rate-limited, so a busy channel does not pay
        a scan of every connection on every request."""
        now = time.monotonic()
        with self._lock:
            if not force and now - self._swept < min(1.0, self.idle / 4):
                return
            self._swept = now
            stale = [c for c in self._conns.values()
                     if (c.closed and not c._frames)
                     or (now - c._seen > self.idle and not c._waiters)]
        for conn in stale:
            await self._finish(conn)


def _channel_tools(mcp, channels: dict) -> None:
    """Register the app-only tools every channel of `mcp` (the `channels` dict) shares."""

    def find(conn_id, principal):
        for channel in list(channels.values()):
            conn = channel._get(conn_id, principal)
            if conn is not None:
                return channel, conn
        return None, None

    def reply(**fields):
        frames = fields.get("frames", [])
        return result([{"type": "text", "text": f"{len(frames)} frame(s)"}], structured=fields)

    @mcp.tool(name="channel_open", visibility="app")
    async def channel_open(channel: str, who: Principal, params: str = ""):
        """Open a connection to a channel (widgets only)."""
        target = channels.get(channel)
        conn = await target._open(who, dict(parse_qsl(params))) if target else None
        if conn is None:
            return reply(conn=None, frames=[], closed=True)
        return reply(conn=conn.id, frames=conn._drain(), closed=conn.closed)

    @mcp.tool(name="channel_send", visibility="app")
    async def channel_send(conn: str, data: str, who: Principal):
        """Send one text frame on a connection (widgets only)."""
        channel, c = find(conn, who)
        if c is None or c.closed:
            return reply(closed=True)
        try:
            await _callback(channel._on_message, c, data)
        except Exception:
            log.exception("on_message for channel %r raised; closing the connection",
                          channel.name)
            await channel._finish(c)
            return reply(closed=True)
        return reply(closed=c.closed)

    @mcp.tool(name="channel_recv", visibility="app")
    async def channel_recv(conn: str, who: Principal, wait: float = 0):
        """Receive queued frames, waiting up to `wait` seconds for one (widgets only)."""
        channel, c = find(conn, who)
        if c is None:
            return reply(frames=[], closed=True)
        await channel._sweep()
        wait = float(wait)
        await c._wait(min(wait, channel.wait) if wait > 0 else 0.0)   # NaN and negatives: 0
        c._seen = time.monotonic()
        frames = c._drain()
        if c.closed:
            await channel._finish(c)
        return reply(frames=frames, closed=c.closed)

    @mcp.tool(name="channel_close", visibility="app")
    async def channel_close(conn: str, who: Principal):
        """Close a connection (widgets only)."""
        channel, c = find(conn, who)
        if c is not None:
            await channel._finish(c)
        return reply(closed=True)


