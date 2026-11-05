"""Runs inside the sandbox container. Not imported by the host.

The container has no network. Code reaches the store API only through ``api.call``,
which sends one JSON line to stdout and reads the host's reply from stdin. The host
decides which endpoints are allowed. Whatever the code assigns to ``result`` (or
prints) is sent back as the final answer.
"""

import contextlib
import io
import json
import sys
import traceback

_REAL_STDOUT = sys.stdout


class _Api:
    def call(self, name, **arguments):
        _REAL_STDOUT.write(json.dumps({"type": "call", "name": name, "arguments": arguments}) + "\n")
        _REAL_STDOUT.flush()
        reply = json.loads(sys.stdin.readline())
        if reply.get("error"):
            raise RuntimeError(reply["error"])
        return reply["result"]


def main():
    code = open(sys.argv[1], encoding="utf-8").read()
    scope = {"api": _Api(), "result": None}
    printed = io.StringIO()
    try:
        with contextlib.redirect_stdout(printed):
            exec(compile(code, "<agent-code>", "exec"), scope)
        message = {"type": "done", "result": scope.get("result"), "stdout": printed.getvalue()[-4000:]}
    except Exception:
        message = {"type": "done", "error": traceback.format_exc(limit=3)[-2000:], "stdout": printed.getvalue()[-2000:]}
    _REAL_STDOUT.write(json.dumps(message, default=str) + "\n")
    _REAL_STDOUT.flush()


main()
