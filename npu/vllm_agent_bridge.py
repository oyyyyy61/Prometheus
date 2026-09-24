"""Bridge remote session-router, vLLM, and Ascend exporter metrics.

This process runs on the NPU host. It gives the Agent/KV dashboard useful
aggregate data before application-level lifecycle hooks are added to the
Agent. Session identity comes from ``/routing-stats``; KV occupancy and token
counters come from vLLM; device capacity comes from the Ascend exporter.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from prometheus_client.parser import text_string_to_metric_families

try:
    from .agent_memory_exporter import AgentMemoryExporter, start_agent_event_server
except ImportError:  # Direct execution from the npu directory.
    from agent_memory_exporter import AgentMemoryExporter, start_agent_event_server


LOG = logging.getLogger("vllm-agent-bridge")


@dataclass(frozen=True)
class Sample:
    name: str
    labels: Mapping[str, str]
    value: float


def fetch_text(url: str, timeout: float) -> str:
    request = Request(url, headers={"User-Agent": "agentrix-vllm-agent-bridge/1.0"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}")


def fetch_json(url: str, timeout: float) -> Mapping[str, object]:
    return json.loads(fetch_text(url, timeout))


def parse_samples(payload: str) -> list[Sample]:
    samples: list[Sample] = []
    for family in text_string_to_metric_families(payload):
        for sample in family.samples:
            samples.append(Sample(sample.name, dict(sample.labels), float(sample.value)))
    return samples


def find_samples(samples: Iterable[Sample], name: str) -> list[Sample]:
    return [sample for sample in samples if sample.name == name]


def value_for(samples: Iterable[Sample], name: str, labels: Mapping[str, str]) -> float:
    for sample in find_samples(samples, name):
        if all(sample.labels.get(key) == value for key, value in labels.items()):
            return sample.value
    return 0.0


def values_by_label(samples: Iterable[Sample], name: str, label: str) -> dict[str, float]:
    return {
        sample.labels[label]: sample.value
        for sample in find_samples(samples, name)
        if label in sample.labels
    }


def parse_npu_smi_usages(payload: str) -> dict[str, dict[str, float]]:
    """Parse ``npu-smi info -t usages`` output by NPU ID."""
    records: dict[str, dict[str, float]] = {}
    blocks = re.finditer(
        r"NPU ID\s*:\s*(\d+)(.*?)(?=\n\s*NPU ID\s*:|\Z)",
        payload,
        flags=re.DOTALL,
    )
    fields = {
        "aicore_percent": "Aicore Usage Rate(%)",
        "vector_percent": "Aivector Usage Rate(%)",
        "hbm_bandwidth_percent": "HBM Bandwidth Usage Rate(%)",
        "overall_percent": "NPU Utilization(%)",
        "cube_percent": "Aicube Usage Rate(%)",
    }
    for block in blocks:
        values: dict[str, float] = {}
        for output_name, display_name in fields.items():
            match = re.search(rf"{re.escape(display_name)}\s*:\s*([0-9]+(?:\.[0-9]+)?)", block.group(2))
            if match:
                values[output_name] = float(match.group(1))
        if values:
            records[block.group(1)] = values
    return records


def fetch_npu_smi_usages(timeout: float, npu_ids: Iterable[str]) -> dict[str, dict[str, float]]:
    """Read hardware utilization when npu-exporter exposes stale zeros."""
    records: dict[str, dict[str, float]] = {}
    for npu_id in sorted(set(npu_ids)):
        try:
            result = subprocess.run(
                ["npu-smi", "info", "-t", "usages", "-i", str(npu_id)],
                check=True,
                capture_output=True,
                text=True,
                timeout=max(timeout, 1.0),
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as error:
            LOG.debug("npu-smi fallback unavailable for %s: %s", npu_id, error)
            continue
        records.update(parse_npu_smi_usages(result.stdout))
    return records


class VllmAgentBridge:
    """Poll the three local endpoints and update AgentMemoryExporter."""

    def __init__(
        self,
        exporter: AgentMemoryExporter,
        *,
        router_url: str = "http://127.0.0.1:8000/routing-stats",
        vllm_url: str = "http://127.0.0.1:8001/metrics",
        npu_url: str = "http://127.0.0.1:8082/metrics",
        timeout: float = 3.0,
    ) -> None:
        self.exporter = exporter
        self.router_url = router_url
        self.vllm_url = vllm_url
        self.npu_url = npu_url
        self.timeout = timeout
        self._known_agents: set[str] = set()
        self._previous_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._current_deltas: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def _counter_delta(self, sample: Sample) -> float:
        key = (sample.name, tuple(sorted(sample.labels.items())))
        previous = self._previous_counters.get(key, sample.value)
        self._previous_counters[key] = sample.value
        delta = max(sample.value - previous, 0.0)
        self._current_deltas[key] = delta
        return delta

    def poll_once(self) -> None:
        routing = fetch_json(self.router_url, self.timeout)
        vllm_samples = parse_samples(fetch_text(self.vllm_url, self.timeout))
        npu_samples = parse_samples(fetch_text(self.npu_url, self.timeout))
        self._current_deltas = {}

        sessions = {str(key): float(value) for key, value in dict(routing.get("sessions", {})).items()}
        requests = {str(key): float(value) for key, value in dict(routing.get("requests", {})).items()}
        engines = set(sessions) | set(requests)
        engines.update(
            sample.labels.get("engine", "")
            for sample in find_samples(vllm_samples, "vllm:kv_cache_usage_perc")
        )
        engines.discard("")

        running_by_engine = values_by_label(vllm_samples, "vllm:num_requests_running", "engine")
        waiting_by_engine = values_by_label(vllm_samples, "vllm:num_requests_waiting", "engine")
        kv_by_engine = values_by_label(vllm_samples, "vllm:kv_cache_usage_perc", "engine")
        total_by_npu = values_by_label(npu_samples, "npu_chip_info_hbm_total_memory", "id")
        npu_ids = sorted(total_by_npu)
        npu_smi = fetch_npu_smi_usages(self.timeout, npu_ids)

        for npu_id, telemetry in npu_smi.items():
            self.exporter.set_npu_telemetry(
                npu_id,
                aicore_percent=telemetry.get("aicore_percent", 0),
                overall_percent=telemetry.get("overall_percent", 0),
                vector_percent=telemetry.get("vector_percent", 0),
                cube_percent=telemetry.get("cube_percent", 0),
                hbm_bandwidth_percent=telemetry.get("hbm_bandwidth_percent", 0),
            )

        current_agents: set[str] = set()
        for engine in sorted(engines):
            agent_id = f"vllm-engine-{engine}"
            current_agents.add(agent_id)
            running = running_by_engine.get(engine, 0.0)
            waiting = waiting_by_engine.get(engine, 0.0)
            session_count = sessions.get(engine, 0.0)
            stage = "decode" if running > 0 else "unknown"
            state = "active" if running > 0 or session_count > 0 else "cold"
            self.exporter.upsert_agent(
                agent_id,
                state=state,
                stage=stage,
                branch_count=max(session_count, 1.0),
            )
            self.exporter.set_runtime_requests(engine=engine, running=running, waiting=waiting)

            npu_id = npu_ids[int(engine)] if engine.isdigit() and int(engine) < len(npu_ids) else None
            total_mib = total_by_npu.get(npu_id, 0.0) if npu_id else 0.0
            usage = kv_by_engine.get(engine, 0.0)
            usage = usage / 100.0 if usage > 1.0 else usage
            usage = min(max(usage, 0.0), 1.0)
            self.exporter.set_memory(
                agent_id,
                memory_kind="kv_cache",
                state=state,
                tier="npu",
                physical_bytes=total_mib * 1024 * 1024 * usage,
            )

        for agent_id in self._known_agents - current_agents:
            self.exporter.remove_agent(agent_id)
        self._known_agents = current_agents

        for sample in find_samples(vllm_samples, "vllm:prefix_cache_hits_total"):
            self.exporter.record_kv_hit(self._counter_delta(sample))
        for sample in find_samples(vllm_samples, "vllm:prefix_cache_queries_total"):
            hit_key = (
                "vllm:prefix_cache_hits_total",
                tuple(sorted(sample.labels.items())),
            )
            self.exporter.record_kv_miss(
                max(self._counter_delta(sample) - self._current_deltas.get(hit_key, 0.0), 0.0)
            )
        hit_total = sum(
            sample.value for sample in find_samples(vllm_samples, "vllm:prefix_cache_hits_total")
        )
        query_total = sum(
            sample.value for sample in find_samples(vllm_samples, "vllm:prefix_cache_queries_total")
        )
        self.exporter.set_kv_cache_totals(
            hits=hit_total,
            misses=max(query_total - hit_total, 0.0),
        )
        for sample in find_samples(vllm_samples, "vllm:prompt_tokens_total"):
            self.exporter.record_prompt_tokens(self._counter_delta(sample), engine=sample.labels.get("engine", "unknown"))
        for sample in find_samples(vllm_samples, "vllm:generation_tokens_total"):
            self.exporter.record_generation_tokens(self._counter_delta(sample), engine=sample.labels.get("engine", "unknown"))

    def run_forever(self, interval: float) -> None:
        while True:
            try:
                self.poll_once()
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
                LOG.warning("poll failed (router=%s vllm=%s npu=%s): %s", self.router_url, self.vllm_url, self.npu_url, error)
            time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--router-url", default="http://127.0.0.1:8000/routing-stats")
    parser.add_argument("--vllm-url", default="http://127.0.0.1:8001/metrics")
    parser.add_argument("--npu-url", default="http://127.0.0.1:8082/metrics")
    parser.add_argument("--model", default="Qwen3.5-9B")
    parser.add_argument("--control-port", type=int, default=8003)
    parser.add_argument("--control-address", default="127.0.0.1")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    exporter = AgentMemoryExporter(
        runtime="vllm-agent-bridge",
        model=args.model,
        device="ascend-910b2",
    )
    exporter.start(port=args.port)
    start_agent_event_server(
        exporter,
        port=args.control_port,
        address=args.control_address,
    )
    LOG.info("Agent event endpoint listening on %s:%s/agent-events", args.control_address, args.control_port)
    VllmAgentBridge(
        exporter,
        router_url=args.router_url,
        vllm_url=args.vllm_url,
        npu_url=args.npu_url,
    ).run_forever(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
