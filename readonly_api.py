"""Small read-only HTTP API for Pilot statistics.

Public read-only API for PAPER Pilot statistics.
No authentication and no write/order endpoints exist in this server.
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pilot_engine_v1 import get_pilot_engine_v1_report
from pilot_engine_v2 import get_pilot_engine_v2_report
from pilot_engine_audit_v1 import get_pilot_engine_audit_v1_report

logger = logging.getLogger(__name__)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        if value != value:
            return None
        if value == float("inf"):
            return "inf"
        if value == float("-inf"):
            return "-inf"
    return value


class ReadOnlyAPIHandler(BaseHTTPRequestHandler):
    server_version = "PolymarketReadOnlyAPI/1.0"

    def log_message(self, fmt, *args):
        logger.info("read-only-api: " + fmt, *args)

    def _send(self, status, payload):
        raw = json.dumps(_json_safe(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path not in {"/api/status", "/api/pilot/v1", "/api/pilot/v2", "/api/pilot/audit"}:
            self._send(404, {"ok": False, "error": "not_found"})
            return
        try:
            if path == "/api/status":
                payload = {
                    "ok": True,
                    "service": "polymarket-bot-read-only",
                    "time_utc": datetime.now(timezone.utc).isoformat(),
                    "endpoints": ["/api/status", "/api/pilot/v1", "/api/pilot/v2", "/api/pilot/audit"],
                }
            elif path == "/api/pilot/v1":
                payload = {"ok": True, "report": get_pilot_engine_v1_report()}
            elif path == "/api/pilot/v2":
                payload = {"ok": True, "report": get_pilot_engine_v2_report()}
            else:
                payload = {"ok": True, "report": get_pilot_engine_audit_v1_report()}
            self._send(200, payload)
        except Exception as exc:
            logger.exception("Read-only API request failed: %s", path)
            self._send(500, {"ok": False, "error": type(exc).__name__, "message": str(exc)})


def start_readonly_api():
    """Start API in a daemon thread. Returns the server, or None if disabled."""
    enabled = os.getenv("READ_ONLY_API_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    if not enabled:
        logger.info("Read-only API disabled")
        return None
    port = int(os.getenv("PORT", os.getenv("READ_ONLY_API_PORT", "8080")))
    server = ThreadingHTTPServer(("0.0.0.0", port), ReadOnlyAPIHandler)
    thread = threading.Thread(target=server.serve_forever, name="readonly-api", daemon=True)
    thread.start()
    logger.info("Read-only API listening on 0.0.0.0:%s", port)
    return server
