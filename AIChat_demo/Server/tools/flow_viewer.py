"""Read-only HTTP server for the local Echo-Mate flow console."""

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from tools import flow_log
except ModuleNotFoundError:  # Running directly from Server/tools.
    import flow_log


ASSET_DIR = Path(__file__).with_name("flowview")
ASSETS = {
    "/assets/flowview.css": ("flowview.css", "text/css; charset=utf-8"),
    "/assets/flowview.js": ("flowview.js", "text/javascript; charset=utf-8"),
}


class FlowHandler(BaseHTTPRequestHandler):
    server_version = "EchoMateFlow/1.0"

    def _send_body(self, body, content_type, status=HTTPStatus.OK):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status=HTTPStatus.OK):
        self._send_body(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            health = flow_log.health()
            self._send_json(
                health,
                HTTPStatus.OK if health.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if parsed.path in ("/api/events", "/api/flow"):
            self._handle_events(parse_qs(parsed.query))
            return
        if parsed.path == "/":
            self._send_file(ASSET_DIR / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path in ASSETS:
            filename, content_type = ASSETS[parsed.path]
            self._send_file(ASSET_DIR / filename, content_type)
            return
        self._send_json(
            {"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND
        )

    def _handle_events(self, query):
        try:
            notify_id = (
                int(query["notify_id"][0])
                if query.get("notify_id")
                else None
            )
            limit = max(1, min(int(query.get("limit", ["1000"])[0]), 5000))
        except (TypeError, ValueError):
            self._send_json(
                {"ok": False, "error": "invalid query"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        events = flow_log.read_events(
            limit=limit,
            trace_id=(query.get("trace_id") or [None])[0],
            notify_id=notify_id,
            component=(query.get("component") or [None])[0],
            status=(query.get("status") or [None])[0],
            after_id=(query.get("after_id") or [None])[0],
        )
        self._send_json({
            "ok": True,
            "events": events,
            "cursor": events[-1]["id"] if events else None,
        })

    def _send_file(self, path, content_type):
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(
                {"ok": False, "error": "asset unavailable"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._send_body(body, content_type)

    def log_message(self, format_string, *args):
        return


def serve(host="127.0.0.1", port=8787):
    server = ThreadingHTTPServer((host, port), FlowHandler)
    print(f"flow viewer listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    serve(host=args.host, port=args.port)
