// TypeScript declarations for `window.mcp`, the widget-side bridge of micromcp.apps
// (bridge.js), which every page built by `Widget(body=...)` or `Widget(html=..., bridge=True)`
// carries. Copy into a bundled widget's project:
//
//   python -c "from micromcp.apps import BRIDGE_TYPES; print(BRIDGE_TYPES)" > src/mcp-bridge.d.ts

/** A content block of a tool result. */
export interface ContentBlock {
  type: string;
  text?: string;
  [key: string]: unknown;
}

/** What `tools/call` answers. */
export interface ToolResult {
  content?: ContentBlock[];
  structuredContent?: Record<string, unknown>;
  isError?: boolean;
  _meta?: Record<string, unknown>;
  [key: string]: unknown;
}

/** The host's context: theme, style variables, container size. */
export interface HostContext {
  theme?: string;
  styles?: { variables?: Record<string, string> };
  containerDimensions?: { maxHeight?: number | string; [key: string]: unknown };
  [key: string]: unknown;
}

/** The host's answer to the MCP Apps handshake (`ui/initialize`). */
export interface InitializeResult {
  hostCapabilities?: Record<string, unknown>;
  hostInfo?: { name?: string; version?: string; [key: string]: unknown };
  hostContext?: HostContext;
  [key: string]: unknown;
}

/** How a message to the model went. */
export type Outcome = "ok" | `error: ${string}`;

/** A WebSocket-compatible socket over a server Channel. Frames are text. */
export interface MCPWebSocket extends EventTarget {
  readonly url: string;
  readonly channel: string;
  readonly protocol: string;
  readonly extensions: string;
  binaryType: string;
  readonly bufferedAmount: number;
  readonly readyState: 0 | 1 | 2 | 3;
  readonly CONNECTING: 0;
  readonly OPEN: 1;
  readonly CLOSING: 2;
  readonly CLOSED: 3;
  onopen: ((this: MCPWebSocket, ev: Event) => unknown) | null;
  onmessage: ((this: MCPWebSocket, ev: MessageEvent<string>) => unknown) | null;
  onerror: ((this: MCPWebSocket, ev: Event) => unknown) | null;
  onclose: ((this: MCPWebSocket, ev: CloseEvent) => unknown) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export interface MCPWebSocketConstructor {
  /** `url` is `mcp:<channel>[?query]`. */
  new (url: string): MCPWebSocket;
  readonly CONNECTING: 0;
  readonly OPEN: 1;
  readonly CLOSING: 2;
  readonly CLOSED: 3;
}

export interface MCPBridge {
  /** Resolves once the handshake with the host has completed. */
  readonly ready: Promise<InitializeResult>;
  hostCapabilities: Record<string, unknown> | null;
  hostInfo: InitializeResult["hostInfo"] | null;
  hostContext: HostContext | null;
  /** Call a tool on this widget's server (after `ready`). */
  /** Rejects if the call itself fails (no such tool, bad arguments, timeout); a tool
   *  that ran and failed resolves with `isError: true`. */
  callTool(name: string, args?: Record<string, unknown>): Promise<ToolResult>;
  /** fetch()-shaped: `tool:name?a=1` calls that tool; other URLs go to the `route=` tool. */
  fetch(input: string | URL | Request, init?: RequestInit): Promise<Response>;
  /** Tell the model what the user sees (debounced; each update replaces the last). */
  setContext(text: string, data?: Record<string, unknown>, delay?: number): Promise<Outcome>;
  /** Post a message into the conversation as the user (starts a turn). */
  say(text: string): Promise<Outcome>;
  /** Open a socket on a server Channel. */
  channel(name: string, params?: Record<string, string>): MCPWebSocket;
  /** The socket constructor, for libraries that take a WebSocket implementation. */
  WebSocket: MCPWebSocketConstructor;
  on(method: "mcp:context", fn: (event: { outcome: Outcome; text: string; data?: unknown }) => void): void;
  on(method: "mcp:say", fn: (event: { outcome: Outcome; text: string }) => void): void;
  /** Host notifications and requests, by JSON-RPC method (e.g. "ui/notifications/tool-result"). */
  on(method: string, fn: (params: any) => void): void;
  /** A raw JSON-RPC request to the host. */
  request(method: string, params?: Record<string, unknown>, timeoutMs?: number): Promise<any>;
  /** A raw JSON-RPC notification to the host. */
  notify(method: string, params?: Record<string, unknown>): void;
  /** Show text in the page's `[data-mcp-status]` element, if it has one. */
  status(text: string): void;
}

declare global {
  var mcp: MCPBridge;
}
