"""Tiny stdlib health endpoint so a host/uptime monitor can tell the bot is alive.

Enabled by setting HEALTH_PORT. Runs in a daemon thread; GET /healthz -> 200 ok.
"""
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger("brainrotgpt.health")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib API name)
        if self.path in ("/healthz", "/health", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence default request logging
        pass


def start_health_server(port: int):
    """Start the health server in a background thread. Returns the server or None."""
    if not port:
        return None
    server = HTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("health endpoint listening on :%d/healthz", port)
    return server
