"""Only test_scripts.py is a pytest module. The other files here are runnable
suites that execute at import time; naming one on the command line must not
make pytest import it."""
import pathlib

collect_ignore = [p.name for p in pathlib.Path(__file__).parent.glob("*.py")
                  if p.name not in ("test_scripts.py", "conftest.py")]
