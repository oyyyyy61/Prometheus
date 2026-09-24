"""Low-cardinality Prometheus exporter for Agent lifecycle and KV memory state.

The runtime keeps agent IDs and block relationships in memory, while exported
metrics aggregate by bounded state/stage/tier labels. This preserves useful
resource accounting without turning every short-lived Agent or Branch into a
long-lived Prometheus time series.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, Mapping

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from prometheus_client.core import CollectorRegistry


AGENT_STATES = frozenset(
    {
        "active",
        "tool_wait",
        "shared",
        "cold",
        "offloading",
        "offloaded",
        "restoring",
        "reclaimable",
        "released",
        "unknown",
    }
)
AGENT_STAGES = frozenset(
    {"planning", "prefill", "decode", "tool_call", "tool_wait", "merge", "unknown"}
)
MEMORY_KINDS = frozenset({"kv_cache", "tool_state", "intermediate", "other", "unknown"})
EVENTS = frozenset(
    {
        "create",
        "stage_change",
        "fork",
        "tool_call",
        "tool_return",
        "offload",
        "restore",
        "release",
        "unknown",
    }
)


def _bounded(value: str, allowed: Iterable[str], fallback: str) -> str:
    value = str(value or fallback)
    return value if value in allowed else fallback


def _non_negative(value: int | float, name: str) -> float:
    value = float(value)
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


@dataclass
class MemoryState:
    logical_bytes: float = 0.0
    physical_bytes: float = 0.0
    shared_bytes: float = 0.0
    reclaimable_bytes: float = 0.0
    block_count: float = 0.0
    state: str = "unknown"
    tier: str = "unknown"


@dataclass
class AgentState:
    state: str = "unknown"
    stage: str = "unknown"
    context_tokens: float = 0.0
    branch_count: float = 1.0
    tool_wait_seconds: float = 0.0
    memories: Dict[str, MemoryState] = field(default_factory=dict)


class AgentMemoryExporter:
    """Collect and expose aggregated Agent and KV lifecycle metrics."""

    def __init__(
        self,
        *,
        registry: CollectorRegistry | None = None,
        runtime: str = "agentrix",
        model: str = "unknown",
        device: str = "unknown",
    ) -> None:
        self.registry = registry or CollectorRegistry()
        self._lock = threading.RLock()
        self._agents: dict[str, AgentState] = {}
        self._last_update = time.time()
        common = {"runtime": runtime, "model": model, "device": device}

        self.agent_count = Gauge(
            "agent_lifecycle_agents",
            "Number of tracked Agents by lifecycle state and workflow stage",
            ["runtime", "model", "device", "state", "stage"],
            registry=self.registry,
        )
        self.context_tokens = Gauge(
            "agent_lifecycle_context_tokens",
            "Logical context tokens grouped by lifecycle state and workflow stage",
            ["runtime", "model", "device", "state", "stage"],
            registry=self.registry,
        )
        self.branch_count = Gauge(
            "agent_lifecycle_branch_count",
            "Logical Agent branches grouped by lifecycle state and workflow stage",
            ["runtime", "model", "device", "state", "stage"],
            registry=self.registry,
        )
        self.tool_wait_seconds = Gauge(
            "agent_lifecycle_tool_wait_seconds",
            "Current accumulated tool wait time by lifecycle state and workflow stage",
            ["runtime", "model", "device", "state", "stage"],
            registry=self.registry,
        )
        self.memory_bytes = Gauge(
            "agent_memory_bytes",
            "Agent-associated memory by kind and lifecycle state",
            ["runtime", "model", "device", "memory_kind", "state", "tier", "view"],
            registry=self.registry,
        )
        self.memory_blocks = Gauge(
            "agent_memory_blocks",
            "Number of tracked physical memory blocks",
            ["runtime", "model", "device", "memory_kind", "state", "tier"],
            registry=self.registry,
        )
        self.kv_hits = Counter(
            "agent_kv_cache_hits_total",
            "KV cache hits",
            ["runtime", "model", "device"],
            registry=self.registry,
        )
        self.kv_misses = Counter(
            "agent_kv_cache_misses_total",
            "KV cache misses",
            ["runtime", "model", "device"],
            registry=self.registry,
        )
        self.kv_hit_ratio = Gauge(
            "agent_kv_cache_hit_ratio",
            "Cumulative KV cache hit ratio from the model runtime",
            ["runtime", "model", "device"],
            registry=self.registry,
        )
        self.recompute_tokens = Counter(
            "agent_kv_recompute_tokens_total",
            "Tokens recomputed because KV state was unavailable",
            ["runtime", "model", "device"],
            registry=self.registry,
        )
        self.offload_bytes = Counter(
            "agent_memory_offload_bytes_total",
            "Bytes moved from NPU to a lower memory tier",
            ["runtime", "model", "device", "source_tier", "target_tier"],
            registry=self.registry,
        )
        self.restore_bytes = Counter(
            "agent_memory_restore_bytes_total",
            "Bytes restored into NPU memory",
            ["runtime", "model", "device", "source_tier", "target_tier"],
            registry=self.registry,
        )
        self.prompt_tokens = Counter(
            "agent_prompt_tokens_total",
            "Prompt tokens observed by an Agent runtime",
            ["runtime", "model", "device", "engine"],
            registry=self.registry,
        )
        self.generation_tokens = Counter(
            "agent_generation_tokens_total",
            "Generation tokens observed by an Agent runtime",
            ["runtime", "model", "device", "engine"],
            registry=self.registry,
        )
        self.runtime_requests_running = Gauge(
            "agent_runtime_requests_running",
            "Requests currently running in the model runtime",
            ["runtime", "model", "device", "engine"],
            registry=self.registry,
        )
        self.runtime_requests_waiting = Gauge(
            "agent_runtime_requests_waiting",
            "Requests waiting in the model runtime scheduler",
            ["runtime", "model", "device", "engine"],
            registry=self.registry,
        )
        self.npu_aicore_utilization = Gauge(
            "agent_npu_aicore_utilization_percent",
            "AICore utilization read from the Ascend npu-smi fallback",
            ["runtime", "model", "device", "id"],
            registry=self.registry,
        )
        self.npu_overall_utilization = Gauge(
            "agent_npu_overall_utilization_percent",
            "Overall NPU utilization read from the Ascend npu-smi fallback",
            ["runtime", "model", "device", "id"],
            registry=self.registry,
        )
        self.npu_vector_utilization = Gauge(
            "agent_npu_vector_utilization_percent",
            "AIVector utilization read from the Ascend npu-smi fallback",
            ["runtime", "model", "device", "id"],
            registry=self.registry,
        )
        self.npu_cube_utilization = Gauge(
            "agent_npu_cube_utilization_percent",
            "AICube utilization read from the Ascend npu-smi fallback",
            ["runtime", "model", "device", "id"],
            registry=self.registry,
        )
        self.npu_hbm_bandwidth_utilization = Gauge(
            "agent_npu_hbm_bandwidth_utilization_percent",
            "HBM bandwidth utilization read from the Ascend npu-smi fallback",
            ["runtime", "model", "device", "id"],
            registry=self.registry,
        )
        self.transfer_duration = Histogram(
            "agent_memory_transfer_duration_seconds",
            "Duration of memory offload and restore operations",
            ["runtime", "model", "device", "operation", "source_tier", "target_tier"],
            registry=self.registry,
        )
        self.lifecycle_events = Counter(
            "agent_lifecycle_events_total",
            "Agent lifecycle events",
            ["runtime", "model", "device", "event"],
            registry=self.registry,
        )
        self.tracked_agents = Gauge(
            "agent_memory_exporter_tracked_agents",
            "Number of Agent records retained by the exporter",
            ["runtime", "model", "device"],
            registry=self.registry,
        )
        self.last_update = Gauge(
            "agent_memory_exporter_last_update_timestamp_seconds",
            "Unix timestamp of the last runtime state update",
            ["runtime", "model", "device"],
            registry=self.registry,
        )
        self._common = common
        self._kv_hits = self.kv_hits.labels(**common)
        self._kv_misses = self.kv_misses.labels(**common)
        self._kv_hit_ratio = self.kv_hit_ratio.labels(**common)
        self._recompute_tokens = self.recompute_tokens.labels(**common)
        self._tracked_agents = self.tracked_agents.labels(**common)
        self._last_update_metric = self.last_update.labels(**common)
        # Keep a zero-valued baseline visible when no lifecycle transfer has
        # happened yet. Prometheus can then show 0 B/s instead of no data.
        self.offload_bytes.labels(**common, source_tier="npu", target_tier="host")
        self.restore_bytes.labels(**common, source_tier="host", target_tier="npu")

    def _touch(self) -> None:
        self._last_update = time.time()
        self._tracked_agents.set(len(self._agents))
        self._last_update_metric.set(self._last_update)

    def upsert_agent(
        self,
        agent_id: str,
        *,
        state: str = "active",
        stage: str = "unknown",
        context_tokens: int | float = 0,
        branch_count: int | float = 1,
        tool_wait_seconds: int | float = 0,
    ) -> None:
        if not agent_id:
            raise ValueError("agent_id is required")
        with self._lock:
            current = self._agents.setdefault(agent_id, AgentState())
            current.state = _bounded(state, AGENT_STATES, "unknown")
            current.stage = _bounded(stage, AGENT_STAGES, "unknown")
            current.context_tokens = _non_negative(context_tokens, "context_tokens")
            current.branch_count = _non_negative(branch_count, "branch_count")
            current.tool_wait_seconds = _non_negative(tool_wait_seconds, "tool_wait_seconds")
            self._touch()
            self._refresh()

    def set_agent_state(self, agent_id: str, *, state: str, stage: str | None = None) -> None:
        with self._lock:
            if agent_id not in self._agents:
                raise KeyError(f"unknown agent: {agent_id}")
            current = self._agents[agent_id]
            current.state = _bounded(state, AGENT_STATES, "unknown")
            if stage is not None:
                current.stage = _bounded(stage, AGENT_STAGES, "unknown")
            for memory in current.memories.values():
                if memory.state == "unknown":
                    memory.state = current.state
            self._touch()
            self._refresh()

    def set_memory(
        self,
        agent_id: str,
        *,
        memory_kind: str = "kv_cache",
        state: str | None = None,
        tier: str = "npu",
        logical_bytes: int | float = 0,
        physical_bytes: int | float = 0,
        shared_bytes: int | float = 0,
        reclaimable_bytes: int | float = 0,
        block_count: int | float = 0,
    ) -> None:
        with self._lock:
            if agent_id not in self._agents:
                raise KeyError(f"unknown agent: {agent_id}")
            values = {
                "logical_bytes": _non_negative(logical_bytes, "logical_bytes"),
                "physical_bytes": _non_negative(physical_bytes, "physical_bytes"),
                "shared_bytes": _non_negative(shared_bytes, "shared_bytes"),
                "reclaimable_bytes": _non_negative(reclaimable_bytes, "reclaimable_bytes"),
                "block_count": _non_negative(block_count, "block_count"),
            }
            kind = _bounded(memory_kind, MEMORY_KINDS, "unknown")
            record = MemoryState(**values)
            record.state = _bounded(state or self._agents[agent_id].state, AGENT_STATES, "unknown")
            record.tier = str(tier or "unknown")
            self._agents[agent_id].memories[kind] = record
            self._touch()
            self._refresh()

    def remove_agent(self, agent_id: str) -> None:
        with self._lock:
            self._agents.pop(agent_id, None)
            self._touch()
            self._refresh()

    def record_event(self, event: str, *, amount: int | float = 1) -> None:
        event = _bounded(event, EVENTS, "unknown")
        amount = _non_negative(amount, "amount")
        self.lifecycle_events.labels(**self._common, event=event).inc(amount)
        with self._lock:
            self._touch()

    def record_kv_hit(self, amount: int | float = 1) -> None:
        self._kv_hits.inc(_non_negative(amount, "amount"))
        with self._lock:
            self._touch()

    def record_kv_miss(self, amount: int | float = 1) -> None:
        self._kv_misses.inc(_non_negative(amount, "amount"))
        with self._lock:
            self._touch()

    def set_kv_cache_totals(self, *, hits: int | float, misses: int | float) -> None:
        """Set a restart-safe cumulative hit ratio from vLLM source counters."""
        hits = _non_negative(hits, "hits")
        misses = _non_negative(misses, "misses")
        total = hits + misses
        self._kv_hit_ratio.set(hits / total if total else 0.0)
        with self._lock:
            self._touch()

    def record_recompute(self, tokens: int | float) -> None:
        self._recompute_tokens.inc(_non_negative(tokens, "tokens"))
        with self._lock:
            self._touch()

    def record_prompt_tokens(self, tokens: int | float, *, engine: str = "unknown") -> None:
        self.prompt_tokens.labels(**self._common, engine=str(engine or "unknown")).inc(
            _non_negative(tokens, "tokens")
        )
        with self._lock:
            self._touch()

    def record_generation_tokens(self, tokens: int | float, *, engine: str = "unknown") -> None:
        self.generation_tokens.labels(**self._common, engine=str(engine or "unknown")).inc(
            _non_negative(tokens, "tokens")
        )
        with self._lock:
            self._touch()

    def set_runtime_requests(
        self,
        *,
        engine: str = "unknown",
        running: int | float = 0,
        waiting: int | float = 0,
    ) -> None:
        labels = {**self._common, "engine": str(engine or "unknown")}
        self.runtime_requests_running.labels(**labels).set(_non_negative(running, "running"))
        self.runtime_requests_waiting.labels(**labels).set(_non_negative(waiting, "waiting"))
        with self._lock:
            self._touch()

    def set_npu_telemetry(
        self,
        npu_id: str,
        *,
        aicore_percent: int | float,
        overall_percent: int | float,
        vector_percent: int | float,
        cube_percent: int | float,
        hbm_bandwidth_percent: int | float,
    ) -> None:
        """Publish npu-smi values when the bundled exporter reports zeros."""
        labels = {**self._common, "id": str(npu_id)}
        values = {
            "aicore_percent": aicore_percent,
            "overall_percent": overall_percent,
            "vector_percent": vector_percent,
            "cube_percent": cube_percent,
            "hbm_bandwidth_percent": hbm_bandwidth_percent,
        }
        bounded = {
            key: min(max(_non_negative(value, key), 0.0), 100.0)
            for key, value in values.items()
        }
        self.npu_aicore_utilization.labels(**labels).set(bounded["aicore_percent"])
        self.npu_overall_utilization.labels(**labels).set(bounded["overall_percent"])
        self.npu_vector_utilization.labels(**labels).set(bounded["vector_percent"])
        self.npu_cube_utilization.labels(**labels).set(bounded["cube_percent"])
        self.npu_hbm_bandwidth_utilization.labels(**labels).set(bounded["hbm_bandwidth_percent"])
        with self._lock:
            self._touch()

    def record_transfer(
        self,
        operation: str,
        *,
        bytes_count: int | float,
        duration_seconds: int | float,
        source_tier: str,
        target_tier: str,
    ) -> None:
        if operation not in {"offload", "restore"}:
            raise ValueError("operation must be offload or restore")
        bytes_count = _non_negative(bytes_count, "bytes_count")
        duration_seconds = _non_negative(duration_seconds, "duration_seconds")
        labels = {
            **self._common,
            "source_tier": str(source_tier or "unknown"),
            "target_tier": str(target_tier or "unknown"),
        }
        if operation == "offload":
            self.offload_bytes.labels(**labels).inc(bytes_count)
        else:
            self.restore_bytes.labels(**labels).inc(bytes_count)
        self.transfer_duration.labels(**labels, operation=operation).observe(duration_seconds)
        self.record_event(operation)

    def _refresh(self) -> None:
        """Rebuild aggregate gauges from the latest runtime state."""
        grouped_agents: dict[tuple[str, str], dict[str, float]] = {}
        grouped_memory: dict[tuple[str, str, str, str], dict[str, float]] = {}
        for agent in self._agents.values():
            key = (agent.state, agent.stage)
            values = grouped_agents.setdefault(
                key,
                {"count": 0.0, "tokens": 0.0, "branches": 0.0, "wait": 0.0},
            )
            values["count"] += 1
            values["tokens"] += agent.context_tokens
            values["branches"] += agent.branch_count
            values["wait"] += agent.tool_wait_seconds
            for kind, memory in agent.memories.items():
                tier = getattr(memory, "tier", "unknown")
                memory_key = (kind, memory.state, tier, "all")
                totals = grouped_memory.setdefault(
                    memory_key,
                    {"logical": 0.0, "physical": 0.0, "shared": 0.0, "reclaimable": 0.0, "blocks": 0.0},
                )
                totals["logical"] += memory.logical_bytes
                totals["physical"] += memory.physical_bytes
                totals["shared"] += memory.shared_bytes
                totals["reclaimable"] += memory.reclaimable_bytes
                totals["blocks"] += memory.block_count

        for metric in (self.agent_count, self.context_tokens, self.branch_count, self.tool_wait_seconds, self.memory_bytes, self.memory_blocks):
            metric.clear()
        for (state, stage), values in grouped_agents.items():
            labels = {**self._common, "state": state, "stage": stage}
            self.agent_count.labels(**labels).set(values["count"])
            self.context_tokens.labels(**labels).set(values["tokens"])
            self.branch_count.labels(**labels).set(values["branches"])
            self.tool_wait_seconds.labels(**labels).set(values["wait"])
        for (kind, state, tier, _), values in grouped_memory.items():
            labels = {**self._common, "memory_kind": kind, "state": state, "tier": tier}
            for view in ("logical", "physical", "shared", "reclaimable"):
                self.memory_bytes.labels(**labels, view=view).set(values[view])
            self.memory_blocks.labels(**labels).set(values["blocks"])

    def start(self, *, port: int = 8001, address: str = "0.0.0.0") -> None:
        """Start the runtime metrics endpoint."""
        start_http_server(port, addr=address, registry=self.registry)

    def snapshot(self) -> Mapping[str, object]:
        """Return a small inspectable snapshot for runtime tests and debugging."""
        with self._lock:
            return {
                "tracked_agents": len(self._agents),
                "last_update_timestamp": self._last_update,
                "agents": {
                    agent_id: {
                        "state": agent.state,
                        "stage": agent.stage,
                        "context_tokens": agent.context_tokens,
                        "branch_count": agent.branch_count,
                        "memory_kinds": tuple(agent.memories),
                    }
                    for agent_id, agent in self._agents.items()
                },
            }


class _AgentEventHandler(BaseHTTPRequestHandler):
    """Small localhost-only bridge for Agent processes in a separate worker."""

    exporter: AgentMemoryExporter

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, status: int, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/agent-events":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "unknown path"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("request body must be between 1 and 65536 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            _apply_agent_event(self.exporter, payload)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._write_json(HTTPStatus.OK, {"status": "accepted"})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/healthz":
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "unknown path"})


def _apply_agent_event(exporter: AgentMemoryExporter, payload: Mapping[str, object]) -> None:
    event = str(payload.get("event", ""))
    agent_id = str(payload.get("agent_id", ""))
    if event == "upsert":
        exporter.upsert_agent(
            agent_id,
            state=str(payload.get("state", "active")),
            stage=str(payload.get("stage", "unknown")),
            context_tokens=float(payload.get("context_tokens", 0)),
            branch_count=float(payload.get("branch_count", 1)),
            tool_wait_seconds=float(payload.get("tool_wait_seconds", 0)),
        )
    elif event == "state":
        exporter.set_agent_state(agent_id, state=str(payload["state"]), stage=payload.get("stage"))
    elif event == "memory":
        exporter.set_memory(
            agent_id,
            memory_kind=str(payload.get("memory_kind", "kv_cache")),
            state=payload.get("state"),
            tier=str(payload.get("tier", "npu")),
            logical_bytes=float(payload.get("logical_bytes", 0)),
            physical_bytes=float(payload.get("physical_bytes", 0)),
            shared_bytes=float(payload.get("shared_bytes", 0)),
            reclaimable_bytes=float(payload.get("reclaimable_bytes", 0)),
            block_count=float(payload.get("block_count", 0)),
        )
    elif event == "kv_totals":
        exporter.set_kv_cache_totals(
            hits=float(payload["hits"]),
            misses=float(payload["misses"]),
        )
    elif event == "recompute":
        exporter.record_recompute(float(payload["tokens"]))
    elif event == "transfer":
        exporter.record_transfer(
            str(payload["operation"]),
            bytes_count=float(payload["bytes_count"]),
            duration_seconds=float(payload["duration_seconds"]),
            source_tier=str(payload["source_tier"]),
            target_tier=str(payload["target_tier"]),
        )
    elif event == "remove":
        exporter.remove_agent(agent_id)
    else:
        raise ValueError("event must be upsert, state, memory, kv_totals, recompute, transfer, or remove")


def start_agent_event_server(
    exporter: AgentMemoryExporter,
    *,
    port: int = 8003,
    address: str = "127.0.0.1",
) -> ThreadingHTTPServer:
    """Start the local Agent event receiver used by a separate Agent process."""

    handler = type("AgentEventHandler", (_AgentEventHandler,), {"exporter": exporter})
    server = ThreadingHTTPServer((address, port), handler)
    threading.Thread(target=server.serve_forever, name="agent-event-server", daemon=True).start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--address", default="0.0.0.0")
    args = parser.parse_args()

    exporter = AgentMemoryExporter()
    exporter.start(port=args.port, address=args.address)
    print(f"agent memory exporter listening on {args.address}:{args.port}", flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
