"""Hugging Face Space entrypoint for the Impact agent.

Starts a tiny health server on $PORT (7860) so the Space stays "running", then
runs the real agent (agent.py) unchanged in a background thread. The agent's
collaboration logic is NOT modified.
"""

import os
import runpy
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"hazardmind-impact: alive")

    def log_message(self, *args):
        return


def _run_agent():
    runpy.run_path(os.path.join(os.path.dirname(__file__), "agent.py"), run_name="__main__")


if __name__ == "__main__":
    threading.Thread(target=_run_agent, daemon=True).start()
    port = int(os.getenv("PORT", "7860"))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()
