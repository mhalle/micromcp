"""Shared bits for the script-style suites: ephemeral ports."""
import socket


def free_port() -> int:
    """A port the OS just handed out and released; good enough for a test that
    binds it immediately afterwards."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
