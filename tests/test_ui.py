"""The core's MCP Apps surface: tool visibility and `ui://` resources.
Widgets, fragments, and channels live in micromcp-apps (apps/)."""
import io
import json
import sys

from micromcp import MCP, MCP_APP_MIME, META_CAPS, META_VER, PROTOCOL, Server

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


def client(mcp):
    srv = Server(mcp)

    def rpc(method, params=None):
        params = dict(params or {})
        params["_meta"] = {META_VER: PROTOCOL, META_CAPS: {}}
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw),
               "HTTP_MCP_PROTOCOL_VERSION": PROTOCOL, "HTTP_MCP_METHOD": method}
        if params.get("name") or params.get("uri"):
            env["HTTP_MCP_NAME"] = params.get("name") or params["uri"]
        box = {}
        out = b"".join(srv(env, lambda s, h: box.update(s=s)))
        return json.loads(out)
    return rpc


print("visibility")
mcp = MCP("ui-test")


@mcp.tool(visibility="app")
def app_only() -> str:
    """Widget-only."""
    return "x"


@mcp.tool(visibility=["model", "app"], meta={"ui": {"resourceUri": "ui://w"}})
def both() -> str:
    """Model and widget."""
    return "x"


@mcp.resource("ui://w")
def widget() -> str:
    return "<p>w</p>"


rpc = client(mcp)
tools = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
check("visibility='app' is published as _meta.ui.visibility",
      tools["app_only"]["_meta"]["ui"], {"visibility": ["app"]})
check("visibility merges into meta's ui object",
      tools["both"]["_meta"]["ui"], {"resourceUri": "ui://w", "visibility": ["model", "app"]})
for label, kwargs in [("unknown audience", {"visibility": "user"}),
                      ("empty", {"visibility": []}),
                      ("duplicates", {"visibility": ["app", "app"]}),
                      ("conflicts with meta", {"visibility": "app",
                                               "meta": {"ui": {"visibility": ["model"]}}}),
                      ("meta ui not a dict", {"visibility": "app", "meta": {"ui": "x"}})]:
    check(f"visibility refused: {label}",
          raises(lambda kw=kwargs: mcp.tool(lambda: 1, name="bad", **kw)), "ValueError")
check("a refused tool registers nothing", "bad" in mcp.tools, False)

print("ui:// resources")
listed = rpc("resources/list")["result"]["resources"]
check("a ui:// resource defaults to the MCP App MIME type",
      [(r["uri"], r["mimeType"]) for r in listed], [("ui://w", MCP_APP_MIME)])

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
