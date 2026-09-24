"""Alertmanager relay for the remote Ascend recovery agent."""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen

from prometheus_client import Counter, start_http_server


LOG = logging.getLogger("recovery-relay")
REMOTE_RECOVERY_URL = os.environ.get("REMOTE_RECOVERY_URL", "http://127.0.0.1:18004")
REMOTE_RECOVERY_TOKEN = os.environ.get("REMOTE_RECOVERY_TOKEN", "")
REMOTE_RECOVERY_TIMEOUT = float(os.environ.get("REMOTE_RECOVERY_TIMEOUT", "10"))
ALLOWED_ACTIONS = frozenset({"restart_bridge", "restart_vllm", "restore_checkpoint"})

forwarded_total = Counter(
    "recovery_relay_actions_total",
    "Actions forwarded to the remote recovery agent",
    ["action", "result"],
)


def dispatch_remote_action(action: str) -> bool:
    if action not in ALLOWED_ACTIONS:
        forwarded_total.labels(action="unknown", result="rejected").inc()
        LOG.warning("ignoring unsupported recovery action %r", action)
        return False
    if not REMOTE_RECOVERY_TOKEN:
        forwarded_total.labels(action=action, result="unconfigured").inc()
        LOG.error("REMOTE_RECOVERY_TOKEN is empty; action %s was skipped", action)
        return False
    request = Request(
        f"{REMOTE_RECOVERY_URL.rstrip('/')}/v1/actions",
        data=json.dumps({"action": action}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {REMOTE_RECOVERY_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=REMOTE_RECOVERY_TIMEOUT) as response:
            if response.status >= 300:
                raise URLError(f"remote agent returned HTTP {response.status}")
            response.read()
    except (URLError, TimeoutError, OSError) as error:
        forwarded_total.labels(action=action, result="failure").inc()
        LOG.error("remote recovery action %s failed: %s", action, error)
        return False
    forwarded_total.labels(action=action, result="success").inc()
    LOG.info("remote recovery action %s completed", action)
    return True


class AlertHandler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: bytes = b"received") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._reply(200, b"ok")
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/alert":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2 * 1024 * 1024:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as error:
            self._reply(400, str(error).encode("utf-8"))
            return

        if payload.get("status") == "firing":
            for alert in payload.get("alerts", []):
                labels = alert.get("labels", {})
                if labels.get("host") != "rental-910b2":
                    continue
                action = labels.get("recovery_action") or alert.get("annotations", {}).get("recovery_action")
                if action:
                    dispatch_remote_action(action)
        self._reply(200)

    def log_message(self, format: str, *args: object) -> None:
        LOG.info("%s - %s", self.address_string(), format % args)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    address = os.environ.get("RECOVERY_RELAY_ADDRESS", "0.0.0.0")
    port = int(os.environ.get("RECOVERY_RELAY_PORT", "9000"))
    metrics_port = int(os.environ.get("RECOVERY_RELAY_METRICS_PORT", "9001"))
    start_http_server(metrics_port)
    server = ThreadingHTTPServer((address, port), AlertHandler)
    LOG.info("recovery relay listening on %s:%s; remote agent=%s", address, port, REMOTE_RECOVERY_URL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
