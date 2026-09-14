"""The one exception the core raises, and the JSON-RPC error envelope."""

from __future__ import annotations


class Error(Exception):
    """A JSON-RPC error with the HTTP status it should travel under."""

    def __init__(self, code, message, status=400, data=None):
        self.code, self.message, self.status, self.data = code, message, status, data


def _err(status, rid, code, message, data=None):
    err = {"code": code, "message": message}
    if data:
        err["data"] = data
    return status, {"jsonrpc": "2.0", "id": rid, "error": err}
