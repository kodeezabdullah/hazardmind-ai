"""Hugging Face Space entrypoint for the Report agent.

Starts a tiny health server on $PORT (7860) so the Space stays "running". The
report pipeline itself now runs as a LangGraph node (see node.py) driven by
the backend orchestrator, not as a standalone long-lived process here.
"""

import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"hazardmind-report: alive")

    def log_message(self, *args):
        return


if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()
