"""Hugging Face Space entrypoint for the Satellite agent.

HF Spaces require a process listening on $PORT (7860). The agent itself only
connects to Band (it does not serve HTTP), so this wrapper:

  1. starts a tiny health server on $PORT so the Space stays "running", and
  2. runs the real agent (agent.py) unchanged in a background thread.

The agent's collaboration logic is NOT modified — this only satisfies HF's
port requirement.
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
        self.wfile.write(b"hazardmind-satellite: alive")

    def log_message(self, *args):  # silence access logs
        return


def _run_agent():
    # Execute agent.py exactly as `python agent.py` would.
    runpy.run_path(os.path.join(os.path.dirname(__file__), "agent.py"), run_name="__main__")


if __name__ == "__main__":
    threading.Thread(target=_run_agent, daemon=True).start()
    port = int(os.getenv("PORT", "7860"))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()
