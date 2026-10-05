"""Small read-only HTTP API for Pilot statistics.

Public read-only API for PAPER Pilot statistics.
No authentication and no write/order endpoints exist in this server.
"""
import json
import logging
import os
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
from statistics import median

from database import get_connection
from pilot_engine_v2 import VERSION as PILOT_V2_VERSION

from pilot_engine_v1 import get_pilot_engine_v1_report
from pilot_engine_v2 import get_pilot_engine_v2_report
from pilot_engine_v3 import get_pilot_engine_v3_report
from pilot_engine_audit_v1 import get_pilot_engine_audit_v1_report
from pilot_engine_audit_v2 import get_pilot_engine_audit_v2_report
from pilot_exit_audit_v1 import get_pilot_exit_audit_v1_report
from pilot_oos_review_v1 import get_pilot_oos_review_v1_report
from pilot_oos_review_v2 import get_pilot_oos_review_v2_report
from pilot_oos_review_v3 import get_pilot_oos_review_v2_report as get_pilot_oos_review_v3_report
from pilot_oos_summary_v1 import get_pilot_oos_summary_v1_report

logger = logging.getLogger(__name__)
DASHBOARD_PATH = Path(__file__).with_name("dashboard.html")


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


def _pilot_v2_snapshot_readonly():
    """Read the already-recorded Pilot v2 state without processing candidates or writing DB rows."""
    def stats(rows):
        vals = [(float(stake), float(roi)) for stake, roi in rows]
        if not vals:
            return {"n": 0, "roi": None, "pf": None, "win": None, "median": None, "pnl": 0.0}
        rois = [roi for _, roi in vals]
        pnls = [stake * roi / 100.0 for stake, roi in vals]
        gp = sum(max(x, 0.0) for x in pnls)
        gl = -sum(min(x, 0.0) for x in pnls)
        return {
            "n": len(vals),
            "roi": sum(rois) / len(rois),
            "pf": gp / gl if gl else (float("inf") if gp else None),
            "win": 100.0 * sum(roi > 0 for roi in rois) / len(rois),
            "median": median(rois),
            "pnl": sum(pnls),
        }

    with closing(get_connection()) as c:
        counts = dict(c.execute(
            "SELECT action,COUNT(*) FROM pilot_engine_v2_decisions WHERE version=? GROUP BY action",
            (PILOT_V2_VERSION,),
        ).fetchall())
        reasons = c.execute(
            "SELECT reason,COUNT(*) FROM pilot_engine_v2_decisions WHERE version=? AND action='SKIP' GROUP BY reason ORDER BY COUNT(*) DESC",
            (PILOT_V2_VERSION,),
        ).fetchall()
        horizons = {}
        for h in (180, 360, 720, 1440):
            rows = c.execute(
                """SELECT d.stake,o.roi FROM pilot_engine_v2_decisions d
                   JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
                   WHERE d.version=? AND d.action='TRADE' AND o.checkpoint_minutes=?
                   ORDER BY d.decided_at,d.candidate_id""",
                (PILOT_V2_VERSION, h),
            ).fetchall()
            horizons[h] = stats(rows)
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=1440)).isoformat()
        open_n = int(c.execute(
            """SELECT COUNT(*) FROM pilot_engine_v2_decisions d
               LEFT JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
               WHERE d.version=? AND d.action='TRADE' AND d.decided_at>=? AND d.decided_at<=? AND o.candidate_id IS NULL""",
            (PILOT_V2_VERSION, cutoff, now.isoformat()),
        ).fetchone()[0] or 0)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "READ-ONLY",
        "counts": counts,
        "open": open_n,
        "reasons": reasons,
        "horizons": horizons,
    }


def _pilot_v2_text(snapshot):
    counts = snapshot["counts"]
    lines = [
        "Polymarket Pilot v2 public read-only feed",
        f"generated_at_utc={snapshot['generated_at_utc']}",
        f"TRADE={int(counts.get('TRADE', 0))}",
        f"SKIP={int(counts.get('SKIP', 0))}",
        f"OPEN={int(snapshot['open'])}",
    ]
    for h, label in ((180, "3h"), (360, "6h"), (720, "12h"), (1440, "24h")):
        x = snapshot["horizons"][h]
        def val(v, digits=2):
            if v is None: return "NA"
            if v == float("inf"): return "INF"
            return f"{v:.{digits}f}"
        lines.append(
            f"{label}: n={x['n']} ROI={val(x['roi'])}% PF={val(x['pf'])} "
            f"Win={val(x['win'])}% median={val(x['median'])}% PnL=${val(x['pnl'])}"
        )
    if snapshot["reasons"]:
        lines.append("SKIP_REASONS=" + ", ".join(f"{reason}:{n}" for reason, n in snapshot["reasons"]))
    return "\n".join(lines) + "\n"


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

    def _send_text(self, status, text):
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in {"/", "/dashboard"}:
            raw = DASHBOARD_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path in {"/pilot-v2.txt", "/public/pilot-v2.txt"}:
            try:
                self._send_text(200, _pilot_v2_text(_pilot_v2_snapshot_readonly()))
            except Exception as exc:
                logger.exception("Public Pilot v2 text feed failed")
                self._send_text(500, f"ERROR={type(exc).__name__}: {exc}\n")
            return
        if path in {"/pilot-v2.json", "/public/pilot-v2.json"}:
            try:
                self._send(200, {"ok": True, "report": _pilot_v2_snapshot_readonly()})
            except Exception as exc:
                logger.exception("Public Pilot v2 JSON feed failed")
                self._send(500, {"ok": False, "error": type(exc).__name__, "message": str(exc)})
            return
        if path not in {"/api/status", "/api/pilot/v1", "/api/pilot/v2", "/api/pilot/audit", "/api/pilot/v2/audit", "/api/pilot/v2/exit-audit", "/api/pilot/v2/oos-review", "/api/pilot/v2/oos-review-2", "/api/pilot/v2/oos-review-3", "/api/pilot/v2/oos-summary", "/api/pilot/v3"}:
            self._send(404, {"ok": False, "error": "not_found"})
            return
        try:
            if path == "/api/status":
                payload = {
                    "ok": True,
                    "service": "polymarket-bot-read-only",
                    "time_utc": datetime.now(timezone.utc).isoformat(),
                    "endpoints": ["/dashboard", "/pilot-v2.txt", "/pilot-v2.json", "/api/status", "/api/pilot/v1", "/api/pilot/v2", "/api/pilot/audit", "/api/pilot/v2/audit", "/api/pilot/v2/exit-audit", "/api/pilot/v2/oos-review", "/api/pilot/v2/oos-review-2", "/api/pilot/v2/oos-review-3", "/api/pilot/v2/oos-summary", "/api/pilot/v3"],
                }
            elif path == "/api/pilot/v1":
                payload = {"ok": True, "report": get_pilot_engine_v1_report()}
            elif path == "/api/pilot/v2":
                payload = {"ok": True, "report": get_pilot_engine_v2_report()}
            elif path == "/api/pilot/v2/audit":
                payload = {"ok": True, "report": get_pilot_engine_audit_v2_report()}
            elif path == "/api/pilot/v2/exit-audit":
                payload = {"ok": True, "report": get_pilot_exit_audit_v1_report()}
            elif path == "/api/pilot/v2/oos-review":
                payload = {"ok": True, "report": get_pilot_oos_review_v1_report()}
            elif path == "/api/pilot/v2/oos-review-2":
                payload = {"ok": True, "report": get_pilot_oos_review_v2_report()}
            elif path == "/api/pilot/v2/oos-review-3":
                payload = {"ok": True, "report": get_pilot_oos_review_v3_report()}
            elif path == "/api/pilot/v2/oos-summary":
                payload = {"ok": True, "report": get_pilot_oos_summary_v1_report()}
            elif path == "/api/pilot/v3":
                payload = {"ok": True, "report": get_pilot_engine_v3_report()}
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
