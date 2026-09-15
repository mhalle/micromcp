"""pytest entry point for the micromcp-apps script-style suites (see ../../tests).

Each suite is a self-contained script that prints PASS/FAIL lines and exits
non-zero on any failure; here each one becomes a pytest case, run in its own
process from this directory.
"""
import os
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).parent

SUITES = [
    ("test_apps.py", ["unit"], 60),
]


def _summarize(stdout: str, stderr: str) -> str:
    """The lines a reader needs: every FAIL/REJECTED with its got/want, the
    final tally, and the tail of stderr — not 40 lines of PASS."""
    lines = stdout.splitlines()
    keep = []
    for i, line in enumerate(lines):
        if "FAIL" in line or "REJECTED" in line or "Error" in line:
            keep.extend(lines[i:i + 3])
    keep.extend(lines[-3:])
    return "\n".join(keep) + ("\n--- stderr ---\n" + stderr[-2000:] if stderr else "")


@pytest.mark.parametrize(
    "script",
    [pytest.param(name, marks=[getattr(pytest.mark, m) for m in marks], id=name.removesuffix(".py"))
     for name, marks, _ in SUITES],
)
def test_suite(script):
    timeout = next(t for n, _, t in SUITES if n == script)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.run([sys.executable, str(HERE / script)], cwd=HERE, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        pytest.fail(f"{script} hung for {timeout}s\n{_summarize(out, err)}", pytrace=False)
    assert proc.returncode == 0, \
        f"{script} exited {proc.returncode}\n{_summarize(proc.stdout, proc.stderr)}"
