"""Skills over MCP: `mcp.skill()`, `skills/list`, `skills/get`,
`resources/directory/read`, skill files through `resources/read`, the
frontmatter subset (cross-checked against PyYAML when it is installed), and
`capabilities.extensions`."""
import base64
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile

# From the top level, so the suite runs against the single-file bundle too.
from micromcp import (DIRECTORY_MIME, MCP, META_CAPS, META_VER, PROTOCOL, SKILLS_EXT,
                      FrontmatterError, Server, parse_frontmatter)

OK = FAIL = 0
SKILL_MD = """---
name: pdf-processing
description: Extract PDF text, fill forms, merge files. Use when handling PDFs.
license: Apache-2.0
metadata:
  author: example-org
  version: "1.0"
---
# PDF processing

See [the forms guide](references/FORMS.md).
"""
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
    except exc:
        return exc.__name__                 # subclasses count: FrontmatterError is a ValueError
    except Exception as e:  # noqa: BLE001 - the test reports the wrong type
        return f"wrong: {type(e).__name__}: {e}"
    return None


def client(app, auth=None):
    def rpc(method, params=None, legacy=False):
        params = dict(params or {})
        if not legacy:
            params["_meta"] = {META_VER: PROTOCOL, META_CAPS: {}}
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        env = {"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw), "CONTENT_TYPE": "application/json"}
        if not legacy:
            env.update(HTTP_MCP_PROTOCOL_VERSION=PROTOCOL, HTTP_MCP_METHOD=method)
            if method == "resources/read":
                env["HTTP_MCP_NAME"] = params["uri"]
        if auth:
            env["HTTP_AUTHORIZATION"] = auth
        box = {}
        out = json.loads(b"".join(app(env, lambda s, h: box.update(s=s))))
        return int(box["s"].split()[0]), out
    return rpc


def code(reply):
    status, body = reply
    return status, body.get("error", {}).get("code")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


tmp = pathlib.Path(tempfile.mkdtemp())
skill_dir = tmp / "pdf-processing"
(skill_dir / "references").mkdir(parents=True)
(skill_dir / "assets").mkdir()
(skill_dir / "SKILL.md").write_text(SKILL_MD)
(skill_dir / "references" / "FORMS.md").write_text("# Forms\n")
(skill_dir / "assets" / "logo.png").write_bytes(PNG)
(skill_dir / ".DS_Store").write_bytes(b"\x00junk")
(skill_dir / "__pycache__").mkdir()
(skill_dir / "__pycache__" / "x.pyc").write_bytes(b"\x00")
FILES = {"SKILL.md": SKILL_MD.encode(), "assets/logo.png": PNG,
         "references/FORMS.md": b"# Forms\n"}
URI = "skill://pdf-processing/SKILL.md"

print("no skills, no extensions: nothing changes")
plain = Server(MCP("plain", "1"))
rpc = client(plain)
check("discover has no extensions", "extensions" in rpc("server/discover")[1]["result"]["capabilities"],
      False)
check("skills/list is an unknown method", code(rpc("skills/list")), (404, -32601))
check("resources/directory/read is unknown too",
      code(rpc("resources/directory/read", {"uri": "skill://x"})), (404, -32601))

print("\nextensions= declares extensions")
m = MCP("ext", "1", extensions={"io.modelcontextprotocol/ui": {}, "com.example/feature": {"v": 2}})
rpc = client(Server(m, legacy="stateless"))
check("published in discover",
      rpc("server/discover")[1]["result"]["capabilities"]["extensions"],
      {"io.modelcontextprotocol/ui": {}, "com.example/feature": {"v": 2}})
init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                          "clientInfo": {"name": "c", "version": "1"}}, legacy=True)[1]
check("and in a legacy initialize", init["result"]["capabilities"]["extensions"],
      {"io.modelcontextprotocol/ui": {}, "com.example/feature": {"v": 2}})
check("a dict is required", raises(lambda: MCP("x", extensions=["a/b"]), TypeError), "TypeError")
check("an identifier needs a prefix", raises(lambda: MCP("x", extensions={"ui": {}})), "ValueError")
check("settings must be a dict", raises(lambda: MCP("x", extensions={"a.b/c": True})), "ValueError")
check("settings must be JSON", raises(lambda: MCP("x", extensions={"a.b/c": {"f": {1, 2}}})),
      "ValueError")
check("the Skills extension is not declared by hand",
      raises(lambda: MCP("x", extensions={SKILLS_EXT: {}})), "ValueError")

print("\na skill from a directory")
mcp = MCP("docs", "1")
check("mcp.skill returns the SKILL.md URI", mcp.skill(skill_dir), URI)
app = Server(mcp)
rpc = client(app)
caps = rpc("server/discover")[1]["result"]["capabilities"]
check("discover declares the extension with directoryRead",
      caps["extensions"], {SKILLS_EXT: {"directoryRead": True}})
check("... beside the resources capability", "resources" in caps, True)
listed = rpc("skills/list")[1]["result"]
entry = {"uri": URI,
         "frontmatter": {"name": "pdf-processing",
                         "description": "Extract PDF text, fill forms, merge files. "
                                        "Use when handling PDFs.",
                         "license": "Apache-2.0",
                         "metadata": {"author": "example-org", "version": "1.0"}},
         "resources": [{"uri": f"skill://pdf-processing/{rel}", "digest": digest(data),
                        "size": len(data)} for rel, data in sorted(FILES.items())]}
check("skills/list entry: verbatim frontmatter, complete manifest, digests", listed["skills"],
      [entry])
check("dot-files and __pycache__ are left out",
      any(".DS_Store" in r["uri"] or "pyc" in r["uri"] for r in listed["skills"][0]["resources"]),
      False)
check("result is complete and cacheable",
      (listed["resultType"], "ttlMs" in listed, "cacheScope" in listed), ("complete", True, True))
check("skills/get returns the same entry", rpc("skills/get", {"uri": URI})[1]["result"]["skill"],
      entry)
status, body = rpc("skills/get", {"uri": "skill://nope/SKILL.md"})
check("an unknown skill is 400 / -32602 naming it",
      (status, body["error"]["code"], body["error"]["message"]),
      (400, -32602, "No skill is served at skill://nope/SKILL.md"))
check("a supporting file is not a skill",
      code(rpc("skills/get", {"uri": "skill://pdf-processing/references/FORMS.md"})), (400, -32602))
check("uri must be a string", code(rpc("skills/get", {"uri": 3})), (400, -32602))
check("a cursor is refused (none is ever issued)",
      code(rpc("skills/list", {"cursor": "x"})), (400, -32602))

print("\nskill files are resources")
read = rpc("resources/read", {"uri": URI})[1]["result"]["contents"][0]
check("SKILL.md reads as markdown text", (read["mimeType"], read["text"]), ("text/markdown", SKILL_MD))
check("... whose UTF-8 bytes match the manifest digest", digest(read["text"].encode()),
      entry["resources"][0]["digest"])
png = rpc("resources/read", {"uri": "skill://pdf-processing/assets/logo.png"})[1]["result"]
blob = png["contents"][0]
check("a binary file is a blob with its MIME type", (blob["mimeType"], base64.b64decode(blob["blob"])),
      ("image/png", PNG))
check("an unserved file is not found",
      code(rpc("resources/read", {"uri": "skill://pdf-processing/.DS_Store"})), (400, -32602))
res = rpc("resources/list")[1]["result"]["resources"]
check("resources/list lists SKILL.md with its frontmatter",
      res, [{"uri": URI, "name": "pdf-processing", "mimeType": "text/markdown",
             "description": entry["frontmatter"]["description"]}])

print("\nresources/directory/read")
d = lambda uri: rpc("resources/directory/read", {"uri": uri})  # noqa: E731
check("the skill root lists its direct children",
      d("skill://pdf-processing")[1]["result"]["resources"],
      [{"uri": URI, "name": "SKILL.md", "mimeType": "text/markdown"},
       {"uri": "skill://pdf-processing/assets", "name": "assets", "mimeType": DIRECTORY_MIME},
       {"uri": "skill://pdf-processing/references", "name": "references",
        "mimeType": DIRECTORY_MIME}])
check("a subdirectory too", d("skill://pdf-processing/assets")[1]["result"]["resources"],
      [{"uri": "skill://pdf-processing/assets/logo.png", "name": "logo.png",
        "mimeType": "image/png"}])
check("a file is not a directory", code(d(URI)), (400, -32602))
check("a missing directory is -32602", code(d("skill://pdf-processing/nope")), (400, -32602))
check("a trailing slash is not the directory", code(d("skill://pdf-processing/")), (400, -32602))
check("cursor refused", code(rpc("resources/directory/read",
                                 {"uri": "skill://pdf-processing", "cursor": "c"})), (400, -32602))

print("\nprefixes, in-memory skills, and nesting")
m2 = MCP("acme", "1")
refunds = m2.skill({"SKILL.md": "---\nname: refunds\ndescription: Process refunds\n---\nBody\n",
                    "examples/email.md": "Dear customer\n"}, prefix="acme/billing")
check("a prefix goes before the name", refunds, "skill://acme/billing/refunds/SKILL.md")
rpc2 = client(Server(m2))
check("prefix directories are directory resources",
      rpc2("resources/directory/read", {"uri": "skill://acme"})[1]["result"]["resources"],
      [{"uri": "skill://acme/billing", "name": "billing", "mimeType": DIRECTORY_MIME}])
outer = {"SKILL.md": "---\nname: outer\ndescription: Outer\n---\n",
         "inner/SKILL.md": "---\nname: inner\ndescription: Inner\n---\n"}
m2.skill(outer)
check("a nested skill registers when its files match",
      m2.skill({"SKILL.md": outer["inner/SKILL.md"]}, prefix="outer"), "skill://outer/inner/SKILL.md")
check("... and its SKILL.md is in both manifests",
      [len(s["resources"]) for s in rpc2("skills/list")[1]["result"]["skills"]], [2, 2, 1])
check("a nested skill with other bytes is refused",
      raises(lambda: m2.skill({"SKILL.md": "---\nname: inner\ndescription: Changed\n---\n"},
                              prefix="outer")), "ValueError")
check("a duplicate skill is refused", raises(lambda: m2.skill(outer)), "ValueError")
m2.skill({**outer, "notes.md": "new"}, replace=True)
check("replace=True swaps the snapshot",
      [r["uri"] for r in rpc2("skills/get", {"uri": "skill://outer/SKILL.md"})[1]["result"]["skill"]
       ["resources"]],
      ["skill://outer/SKILL.md", "skill://outer/inner/SKILL.md", "skill://outer/notes.md"])
check("a resource cannot take a skill file's URI",
      raises(lambda: m2.resource("skill://outer/notes.md")(lambda: "x")), "ValueError")
m3 = MCP("x")
m3.resource("skill://solo/SKILL.md")(lambda: "x")
check("nor a skill a resource's",
      raises(lambda: m3.skill({"SKILL.md": "---\nname: solo\ndescription: d\n---\n"})), "ValueError")

print("\nguards hide a skill and its files")
mg = MCP("g", "1")
mg.skill({"SKILL.md": "---\nname: secret\ndescription: Hidden\n---\n", "a.md": "a"},
         guards=[lambda who: who is not None])
gapp = Server(mg, authenticate=lambda h: {"sub": "u"} if h.get("authorization") else None)
anon, user = client(gapp), client(gapp, auth="Bearer t")
check("anonymous: not listed", anon("skills/list")[1]["result"]["skills"], [])
check("anonymous: skills/get -32602",
      code(anon("skills/get", {"uri": "skill://secret/SKILL.md"})), (400, -32602))
check("anonymous: file not found", code(anon("resources/read", {"uri": "skill://secret/a.md"})),
      (400, -32602))
hidden = anon("resources/read", {"uri": "skill://secret/a.md"})[1]["error"]
missing = anon("resources/read", {"uri": "skill://secret/b.md"})[1]["error"]
check("a hidden skill file answers exactly like a missing one, uri aside",
      ({**hidden, "data": None}, hidden["data"]["uri"]),
      ({**missing, "data": None}, "skill://secret/a.md"))
check("anonymous: directory hidden", code(anon("resources/directory/read", {"uri": "skill://secret"})),
      (400, -32602))
check("anonymous: not in resources/list", anon("resources/list")[1]["result"]["resources"], [])
check("signed in: listed", len(user("skills/list")[1]["result"]["skills"]), 1)
check("signed in: readable", user("resources/read", {"uri": "skill://secret/a.md"})[1]["result"]
      ["contents"][0]["text"], "a")
check("the extension is declared either way",
      SKILLS_EXT in anon("server/discover")[1]["result"]["capabilities"]["extensions"], True)

print("\nregistration checks")
bad_dir = tmp / "wrong-name"
bad_dir.mkdir()
(bad_dir / "SKILL.md").write_text("---\nname: other\ndescription: d\n---\n")
check("name must match the directory", raises(lambda: MCP("x").skill(bad_dir)), "ValueError")
check("a directory without SKILL.md", raises(lambda: MCP("x").skill(tmp)), "ValueError")
fm = lambda body: {"SKILL.md": f"---\n{body}\n---\n"}  # noqa: E731
for label, src in [
    ("no SKILL.md", {"a.md": "x"}),
    ("no frontmatter", {"SKILL.md": "# just markdown\n"}),
    ("unclosed frontmatter", {"SKILL.md": "---\nname: a\ndescription: d\n"}),
    ("uppercase name", fm("name: PDF\ndescription: d")),
    ("leading hyphen", fm("name: -pdf\ndescription: d")),
    ("double hyphen", fm("name: pdf--x\ndescription: d")),
    ("65-character name", fm(f"name: {'a' * 65}\ndescription: d")),
    ("missing description", fm("name: a")),
    ("1025-character description", fm(f"name: a\ndescription: {'d' * 1025}")),
    ("unquoted metadata number", fm("name: a\ndescription: d\nmetadata:\n  version: 1.0")),
    ("non-string license", fm("name: a\ndescription: d\nlicense: 3")),
    ("a file name with a space", {**fm("name: a\ndescription: d"), "my file.md": "x"}),
    ("a .. segment", {**fm("name: a\ndescription: d"), "../x.md": "x"}),
    ("an absolute path", {**fm("name: a\ndescription: d"), "/x.md": "x"}),
    ("non-UTF-8 SKILL.md", {"SKILL.md": b"---\nname: a\ndescription: \xff\n---\n"}),
    ("too many files", {**fm("name: a\ndescription: d"), **{f"f{i}.md": "x" for i in range(512)}}),
]:
    check(f"refused: {label}", raises(lambda src=src: MCP("x").skill(src)), "ValueError")
check("refused: a bad prefix", raises(lambda: MCP("x").skill(fm("name: a\ndescription: d"),
                                                          prefix="a b")), "ValueError")
check("refused: neither a path nor a dict", raises(lambda: MCP("x").skill(3), TypeError),
      "TypeError")
link_dir = tmp / "linky"
link_dir.mkdir()
(link_dir / "SKILL.md").write_text("---\nname: linky\ndescription: d\n---\n")
outside = tmp / "secret.txt"
outside.write_text("do not serve")
try:
    os.symlink(outside, link_dir / "leak.txt")
except OSError:
    print("  SKIP  symlinks are not available")
else:
    check("refused: a symlink out of the skill directory",
          raises(lambda: MCP("x").skill(link_dir)), "ValueError")

print("\nthe frontmatter subset")
CORPUS = [
    "name: a\ndescription: plain words, with commas? and punctuation!",
    "name: a\ndescription: 'It''s quoted: really'\nlicense: \"Apache-2.0\"",
    "name: a\ndescription: x  # a comment\nmetadata:\n  author: me\n  version: \"1.0\"",
    "name: a\ndescription: >\n  folded one\n  folded two\n\n  second paragraph\n",
    "name: a\ndescription: >-\n  strip\n  this\n",
    "name: a\ndescription: |\n  literal\n    indented\n  end\n",
    "name: a\ndescription: |-\n  keep\n  lines\n",
    "# lead comment\nname: a\n\ndescription: \"tab\\there \\u00e9\"\nempty:\nint: 3\nfloat: 1.5\n"
    "flag: true\nnone: ~\nversion: 1.0.1\nnegative: -3",
    "name: a\ndescription: d\nallowed-tools: Bash(git:*) Read\nmetadata:\n\n  # c\n  k: v\nafter: x",
    "name: a\ndescription: mail me@example.com\nurl: https://example.com/a#frag",
]
REFUSED = ["description: yes", "x: 012", "x: 1e3", "x: 2024-01-01", "x: 1:30", "x: -.5",
           "x: 1_000", "x: [a, b]", "x: {a: 1}", "x: a: b", "x: &a v", "x: *a", "- a",
           "x: 'open", "x: \"\\x41\"", "x: .inf", "x: on", "a:\n  b:\n    c: 1", "x: |+\n  a",
           "x: >\n  a\n    b", "x: plain\n  continued", "x: 0x1F", "'q': 1", "x: 1\nx: 2",
           "\tx: 1"]
try:
    import yaml
except ImportError:
    yaml = None
    print("  SKIP  PyYAML is not installed: the corpus is checked for parsing only")
for text in CORPUS:
    mine, body = parse_frontmatter(f"---\n{text}\n---\nBODY")
    check(f"{text.splitlines()[-1][:40]!r}: "
          + ("same as PyYAML" if yaml else "parses"),
          mine if yaml else True, yaml.safe_load(text) if yaml else True)
check("the body follows the frontmatter", parse_frontmatter("---\nname: a\n---\nBODY\n")[1], "BODY\n")
check("CRLF line endings", parse_frontmatter("---\r\nname: a\r\n---\r\n")[0], {"name": "a"})
for text in REFUSED:
    check(f"refused: {text!r}", raises(lambda t=text: parse_frontmatter(f"---\n{t}\n---\n"),
                                       FrontmatterError), "FrontmatterError")

print(f"\n{OK} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
