"""Restricted recovery agent for the remote Ascend host.

The agent is intentionally small and binds to loopback by default.  It accepts
only three action names and never evaluates a command supplied by a request.
Run it as a dedicated unprivileged user; grant that user only the two
allowlisted ``systemctl restart`` operations through sudoers when needed.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from prometheus_client import Counter, Gauge, REGISTRY, start_http_server
from prometheus_client.core import CollectorRegistry


LOG = logging.getLogger("remote-recovery-agent")
MAX_BODY_BYTES = 16 * 1024
ALLOWED_ACTIONS = frozenset({"restart_bridge", "restart_vllm", "restore_checkpoint"})


@dataclass(frozen=True)
class AgentConfig:
    token: str
    bridge_unit: str
    vllm_unit: str
    checkpoint_source: Path
    checkpoint_target: Path
    cooldown_seconds: float = 300.0
    command_timeout_seconds: float = 90.0
    use_sudo: bool = True


class RecoveryAgent:
    """Execute validated recovery operations with one action in flight."""

    def __init__(self, config: AgentConfig, runner: Callable[..., subprocess.CompletedProcess] | None = None, registry: CollectorRegistry | None = None) -> None:
        if not config.token:
            raise ValueError("a recovery token is required")
        if not config.bridge_unit or not config.vllm_unit:
            raise ValueError("systemd unit names are required")
        self.config = config
        self._runner = runner or subprocess.run
        registry = registry or REGISTRY
        self._lock = threading.Lock()
        self._last_action: dict[str, float] = {}
        self.actions_total = Counter("recovery_agent_actions_total", "Recovery actions received", ["action", "result"], registry=registry)
        self.action_duration = Gauge("recovery_agent_action_duration_seconds", "Duration of the most recent recovery action", ["action"], registry=registry)
        self.in_flight = Gauge("recovery_agent_action_in_flight", "Whether a recovery action is currently running", registry=registry)
        self.checkpoint_available = Gauge("recovery_agent_checkpoint_available", "Whether the configured checkpoint source exists", registry=registry)
        self.checkpoint_mtime = Gauge("recovery_agent_checkpoint_mtime_seconds", "Modification time of the configured checkpoint source", registry=registry)
        self.checkpoint_target_available = Gauge("recovery_agent_checkpoint_target_available", "Whether the active checkpoint target exists", registry=registry)
        self.checkpoint_target_mtime = Gauge("recovery_agent_checkpoint_target_mtime_seconds", "Modification time of the active checkpoint target", registry=registry)
        self.refresh_checkpoint_metrics()

    def refresh_checkpoint_metrics(self) -> None:
        try:
            stat = self.config.checkpoint_source.stat()
        except OSError:
            self.checkpoint_available.set(0)
            self.checkpoint_mtime.set(0)
        else:
            self.checkpoint_available.set(1)
            self.checkpoint_mtime.set(stat.st_mtime)
        try:
            target_stat = self.config.checkpoint_target.stat()
        except OSError:
            self.checkpoint_target_available.set(0)
            self.checkpoint_target_mtime.set(0)
        else:
            self.checkpoint_target_available.set(1)
            self.checkpoint_target_mtime.set(target_stat.st_mtime)

    def _run_systemctl(self, unit: str) -> None:
        # Unit names are configured by the operator and never come from HTTP.
        command = (["sudo", "-n"] if self.config.use_sudo else []) + ["systemctl", "restart", unit]
        result = self._runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.config.command_timeout_seconds,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "systemctl failed").strip()
            raise RuntimeError(detail[:500])

    def _restore_checkpoint(self) -> None:
        source = self.config.checkpoint_source
        target = self.config.checkpoint_target
        source_stat = source.stat()
        if not source.is_file():
            raise RuntimeError("checkpoint source is not a regular file")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.recovery-{os.getpid()}-{threading.get_ident()}.tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            os.chmod(target, source_stat.st_mode & 0o777)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        self.refresh_checkpoint_metrics()

    def execute(self, action: str) -> dict[str, object]:
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"unsupported action: {action}")
        now = time.monotonic()
        with self._lock:
            previous = self._last_action.get(action, 0.0)
            remaining = self.config.cooldown_seconds - (now - previous)
            if remaining > 0:
                raise RuntimeError(f"action is cooling down ({remaining:.1f}s remaining)")
            self._last_action[action] = now
            self.in_flight.set(1)
            started = time.monotonic()
            try:
                if action == "restart_bridge":
                    self._run_systemctl(self.config.bridge_unit)
                elif action == "restart_vllm":
                    self._run_systemctl(self.config.vllm_unit)
                else:
                    self._restore_checkpoint()
                self.actions_total.labels(action=action, result="success").inc()
                return {"action": action, "status": "completed"}
            except Exception:
                self.actions_total.labels(action=action, result="failure").inc()
                raise
            finally:
                self.action_duration.labels(action=action).set(time.monotonic() - started)
                self.in_flight.set(0)


def _authorized(request: BaseHTTPRequestHandler, token: str) -> bool:
    value = request.headers.get("Authorization", "")
    supplied = value[7:] if value.startswith("Bearer ") else ""
    return bool(supplied) and hmac.compare_digest(
        hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(token.encode()).digest()
    )


def make_handler(agent: RecoveryAgent):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._json(200, {"status": "ok"})
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/actions":
                self.send_error(404)
                return
            if not _authorized(self, agent.config.token):
                self._json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                action = payload.get("action")
                if not isinstance(action, str):
                    raise ValueError("action must be a string")
                result = agent.execute(action)
            except (ValueError, json.JSONDecodeError) as error:
                self._json(400, {"error": str(error)})
                return
            except RuntimeError as error:
                self._json(409, {"error": str(error)})
                return
            except Exception as error:  # pragma: no cover - defensive HTTP boundary
                LOG.exception("recovery action failed")
                self._json(500, {"error": str(error)})
                return
            self._json(200, result)

        def log_message(self, format: str, *args: object) -> None:
            LOG.info("%s - %s", self.address_string(), format % args)

    return Handler


def build_config(args: argparse.Namespace) -> AgentConfig:
    token = args.token or os.environ.get("RECOVERY_AGENT_TOKEN", "")
    return AgentConfig(
        token=token,
        bridge_unit=args.bridge_unit,
        vllm_unit=args.vllm_unit,
        checkpoint_source=Path(args.checkpoint_source).resolve(),
        checkpoint_target=Path(args.checkpoint_target).resolve(),
        cooldown_seconds=args.cooldown,
        command_timeout_seconds=args.command_timeout,
        use_sudo=not args.no_sudo,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8004)
    parser.add_argument("--metrics-port", type=int, default=9004)
    parser.add_argument("--token")
    parser.add_argument("--bridge-unit", default=os.environ.get("RECOVERY_BRIDGE_UNIT", "agentrix-vllm-bridge.service"))
    parser.add_argument("--vllm-unit", default=os.environ.get("RECOVERY_VLLM_UNIT", "vllm.service"))
    parser.add_argument("--checkpoint-source", default=os.environ.get("RECOVERY_CHECKPOINT_SOURCE", "/data/Agentrix/checkpoints/latest.pt"))
    parser.add_argument("--checkpoint-target", default=os.environ.get("RECOVERY_CHECKPOINT_TARGET", "/data/Agentrix/checkpoints/active.pt"))
    parser.add_argument("--cooldown", type=float, default=300.0)
    parser.add_argument("--command-timeout", type=float, default=90.0)
    parser.add_argument("--no-sudo", action="store_true", help="run systemctl directly for local development")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    agent = RecoveryAgent(build_config(args))
    start_http_server(args.metrics_port, addr=args.address)
    server = ThreadingHTTPServer((args.address, args.port), make_handler(agent))
    LOG.info("remote recovery agent listening on %s:%s", args.address, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
