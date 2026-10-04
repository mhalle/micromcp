"""Content-block helpers (text_content, image_content, audio_content,
resource_link, embedded_resource), annotations, and the checks on
hand-written blocks."""
import base64
import datetime
import io
import json
import sys

from micromcp import (MCP, META_CAPS, META_VER, PROTOCOL, Server, audio_content,
                      embedded_resource, image_content, resource_link, result, text_content)

OK = FAIL = 0
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


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
        out = b"".join(srv(env, lambda s, h: None))
        return json.loads(out)
    return rpc


print("helpers build spec-shaped blocks")
check("text_content", text_content("hi"), {"type": "text", "text": "hi"})
img = image_content(PNG, "image/png")
check("image_content keys", sorted(img), ["data", "mimeType", "type"])
check("image_content round-trips the bytes", base64.b64decode(img["data"]), PNG)
check("image_content accepts bytearray", base64.b64decode(image_content(bytearray(PNG), "image/png")["data"]), PNG)
check("audio_content", audio_content(b"RIFF", "audio/wav")["type"], "audio")
check("resource_link minimal", resource_link("file:///a.csv", "a.csv"),
      {"type": "resource_link", "uri": "file:///a.csv", "name": "a.csv"})
check("resource_link full", resource_link("file:///a.csv", "a.csv", title="A", description="d",
                                          mime_type="text/csv", size=10),
      {"type": "resource_link", "uri": "file:///a.csv", "name": "a.csv", "title": "A",
       "description": "d", "mimeType": "text/csv", "size": 10})

print("helpers refuse what hosts would")
check("image_content wants bytes, not base64 text", raises(lambda: image_content("iVBOR", "image/png"), TypeError), "TypeError")
check("image_content wants an image/* type", raises(lambda: image_content(PNG, "audio/wav")), "ValueError")
check("audio_content wants an audio/* type", raises(lambda: audio_content(b"x", "image/png")), "ValueError")
check("text_content wants a str", raises(lambda: text_content(3), TypeError), "TypeError")
check("resource_link wants a name", raises(lambda: resource_link("file:///a", ""), TypeError), "TypeError")
check("resource_link size is a non-negative int", raises(lambda: resource_link("file:///a", "a", size=-1), TypeError), "TypeError")
check("resource_link size is not a bool", raises(lambda: resource_link("file:///a", "a", size=True), TypeError), "TypeError")

print("annotations")
ann = {"audience": ["user"], "priority": 0.3, "lastModified": "2026-10-04T15:00:00Z"}
check("passed through", text_content("x", annotations=ann)["annotations"], ann)
check("detached from the caller's dict", text_content("x", annotations=ann)["annotations"] is ann, False)
check("tuple audience becomes a list", image_content(PNG, "image/png", annotations={"audience": ("user", "assistant")})["annotations"],
      {"audience": ["user", "assistant"]})
dt = datetime.datetime(2026, 10, 4, 15, 0, tzinfo=datetime.timezone.utc)
check("datetime lastModified is ISO 8601", text_content("x", annotations={"lastModified": dt})["annotations"],
      {"lastModified": "2026-10-04T15:00:00+00:00"})
check("on embedded_resource", embedded_resource("ui://w", text="<p/>", annotations={"priority": 1})["annotations"], {"priority": 1})
check("on resource_link", resource_link("file:///a", "a", annotations={"priority": 0})["annotations"], {"priority": 0})
for label, bad in [("unknown key (typo)", {"audiance": ["user"]}),
                   ("bare-string audience", {"audience": "user"}),
                   ("unknown role", {"audience": ["model"]}),
                   ("priority above 1", {"priority": 1.5}),
                   ("priority NaN", {"priority": float("nan")}),
                   ("priority as bool", {"priority": True}),
                   ("lastModified not a date", {"lastModified": "yesterday"}),
                   ("not a dict", ["user"])]:
    check(f"refused: {label}", raises(lambda: text_content("x", annotations=bad)), "ValueError")

print("tools return them")
mcp = MCP("content-test")


@mcp.tool
def pic() -> dict:
    """Image plus a caption for the model."""
    return result([image_content(PNG, "image/png", annotations={"audience": ["user"]}),
                   text_content("a red dot", annotations={"audience": ["assistant"]})])


@mcp.tool
def raw() -> bytes:
    """Returns bytes directly: refused with a pointer to the helpers."""
    return PNG


@mcp.tool
def handmade_bytes() -> dict:
    """An image block with bytes where base64 text belongs."""
    return result([{"type": "image", "data": PNG, "mimeType": "image/png"}])


@mcp.tool
def handmade_bad_ann() -> dict:
    """A hand-written block whose annotations have a typo."""
    return result([{"type": "text", "text": "x", "annotations": {"priorty": 1}}])


@mcp.tool
def handmade_dt() -> dict:
    """A hand-written block with a datetime lastModified."""
    return result([{"type": "text", "text": "x", "annotations": {"lastModified": dt}}])


@mcp.resource("data://dot", mime_type="image/png", annotations={"audience": ["user"], "priority": 0.5})
def dot() -> bytes:
    return PNG


@mcp.resource("data://row/{n}", annotations={"priority": 0.1})
def row(n: int) -> str:
    return str(n)


rpc = client(mcp)
r = rpc("tools/call", {"name": "pic", "arguments": {}})["result"]
check("image + text blocks sent", [b["type"] for b in r["content"]], ["image", "text"])
check("image bytes intact on the wire", base64.b64decode(r["content"][0]["data"]), PNG)
check("block annotations on the wire", [b["annotations"] for b in r["content"]],
      [{"audience": ["user"]}, {"audience": ["assistant"]}])
check("not an error", r.get("isError"), None)

r = rpc("tools/call", {"name": "raw", "arguments": {}})["result"]
check("bare bytes -> isError", r.get("isError"), True)
check("... naming image_content", "image_content" in r["content"][0]["text"], True)
check("... never the Python repr", "\\x89PNG" in r["content"][0]["text"], False)

r = rpc("tools/call", {"name": "handmade_bytes", "arguments": {}})["result"]
check("hand-written image with bytes data -> isError", r.get("isError"), True)
check("... pointing at image_content", "image_content" in r["content"][0]["text"], True)
r = rpc("tools/call", {"name": "handmade_bad_ann", "arguments": {}})["result"]
check("hand-written annotations typo -> isError", (r.get("isError"), "priorty" in r["content"][0]["text"]), (True, True))
r = rpc("tools/call", {"name": "handmade_dt", "arguments": {}})["result"]
check("hand-written datetime lastModified normalized", r["content"][0]["annotations"],
      {"lastModified": "2026-10-04T15:00:00+00:00"})

print("resource annotations")
r = rpc("resources/list")["result"]["resources"]
check("listed on the resource", [x.get("annotations") for x in r], [{"audience": ["user"], "priority": 0.5}])
r = rpc("resources/templates/list")["result"]["resourceTemplates"]
check("listed on the template", [x.get("annotations") for x in r], [{"priority": 0.1}])
r = rpc("resources/read", {"uri": "data://dot"})["result"]["contents"][0]
check("not on read contents (the spec has no field there)", "annotations" in r, False)
check("bad resource annotations refused at registration",
      raises(lambda: mcp.resource("data://x", annotations={"audience": "user"})(lambda: "x")), "ValueError")

try:
    from mcp_types._v2026_07_28 import CallToolResult, ListResourcesResult
except ImportError:
    print("  skip  (mcp_types not installed)")
else:
    print("the SDK's types accept them")
    r = rpc("tools/call", {"name": "pic", "arguments": {}})["result"]
    parsed = CallToolResult.model_validate(r)
    check("CallToolResult parses image + text", [type(b).__name__ for b in parsed.content],
          ["ImageContent", "TextContent"])
    check("annotations parsed", parsed.content[0].annotations.audience, ["user"])
    blocks = [resource_link("file:///a.csv", "a.csv", mime_type="text/csv", size=3,
                            annotations={"priority": 0.2}),
              audio_content(b"RIFF", "audio/wav"),
              embedded_resource("data://dot", mime_type="image/png", blob=PNG)]
    parsed = CallToolResult.model_validate({**r, "content": blocks})
    check("resource_link / audio / embedded parse", [type(b).__name__ for b in parsed.content],
          ["ResourceLink", "AudioContent", "EmbeddedResource"])
    check("ListResourcesResult parses annotations",
          ListResourcesResult.model_validate(rpc("resources/list")["result"]).resources[0].annotations.priority, 0.5)

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
