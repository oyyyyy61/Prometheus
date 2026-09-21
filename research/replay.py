"""Replay JSONL snapshots through the research policies.

Usage:
    python replay.py samples.jsonl
    python replay.py samples.jsonl --output decisions.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from budgeted_control import (
    ActionSpec,
    BudgetedObservationPolicy,
    Diagnosis,
    ObservationContext,
    TelemetryAwareSafeControl,
    TelemetryState,
    default_observations,
    telemetry_state_from_metrics,
)


ACTION_CATALOG = {
    # CPU/IO 瓶颈的恢复动作只影响 CPU worker 或工具并发，风险较低。
    "increase_cpu_workers": ActionSpec("increase_cpu_workers", 1, True, frozenset({"cpu_network"})),
    "batch_adjustment": ActionSpec("batch_adjustment", 1, True, frozenset({"hbm"})),
    "reroute": ActionSpec("reroute", 2, True, frozenset({"nccl_collective"})),
    "placement_restriction": ActionSpec("placement_restriction", 2, True, frozenset({"nccl_collective"})),
    "restart_pod": ActionSpec("restart_pod", 3, False, frozenset({"node_health"})),
}


# 诊断器使用的证据名与观测目录中的信号名不完全相同。
# 这里把两套名字显式对齐，避免“没有采集证据却执行动作”。
OBSERVATION_EVIDENCE = {
    "gpu_utilization": "gpu_utilization",
    "hbm_occupancy": "hbm",
    "queue_latency": "queue_latency",
    "scrape_freshness": "telemetry",
    "cpu_network": "cpu_network",
    "workflow_phase": "workflow_phase",
    "kv_recompute": "kv",
    "nccl_collective": "nccl_collective",
    "pcie_nvlink": "pcie_nvlink",
    "short_trace": "causal",
}


def process_snapshot(snapshot: dict) -> dict:
    metrics = {key: float(value) for key, value in snapshot.get("metrics", {}).items()}
    metrics.update(
        {key: float(value) for key, value in snapshot.get("telemetry", {}).items()}
    )
    telemetry = telemetry_state_from_metrics(metrics)
    uncertainty = float(snapshot.get("uncertainty", 1.0 if telemetry != TelemetryState.HEALTHY else 0.1))
    actions = frozenset(snapshot.get("action_candidates", []))
    context = ObservationContext(
        uncertainty=uncertainty,
        telemetry_state=telemetry,
        hypotheses=frozenset(snapshot.get("hypotheses", [])),
        candidate_actions=actions,
    )
    request = BudgetedObservationPolicy(default_observations()).select(
        float(snapshot.get("budget", 0.0)), context
    )
    selected_evidence = {
        OBSERVATION_EVIDENCE[spec.name]
        for spec in request.selected
        if spec.name in OBSERVATION_EVIDENCE
    }

    diagnosis_data = snapshot.get("diagnosis")
    decision = None
    if diagnosis_data:
        supplied_evidence = frozenset(diagnosis_data.get("supporting_evidence", []))
        # 只把本轮实际选中的观测对应的证据交给控制门控。
        # 诊断器可以保留更多原始线索，但控制器不能使用预算外证据。
        supporting_evidence = supplied_evidence.intersection(selected_evidence)
        missing_evidence = frozenset(diagnosis_data.get("missing_evidence", []))
        missing_evidence = missing_evidence.union(supplied_evidence - selected_evidence)
        diagnosis = Diagnosis(
            hypothesis=str(diagnosis_data["hypothesis"]),
            confidence=float(diagnosis_data["confidence"]),
            telemetry_state=telemetry,
            supporting_evidence=supporting_evidence,
            missing_evidence=missing_evidence,
        )
        selected_actions = [ACTION_CATALOG[name] for name in actions if name in ACTION_CATALOG]
        control = TelemetryAwareSafeControl().decide(diagnosis, selected_actions)
        decision = {
            "decision": control.decision.value,
            "action": control.action.name if control.action else None,
            "reason": control.reason,
        }

    return {
        "timestamp": snapshot.get("timestamp"),
        "telemetry_state": telemetry.value,
        "selected_observations": [spec.name for spec in request.selected],
        "observation_cost": request.total_cost,
        "remaining_budget": request.remaining_budget,
        "control": decision,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    output = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
    try:
        with args.input.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    snapshot = json.loads(line)
                    result = process_snapshot(snapshot)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                    parser.error(f"invalid snapshot at line {line_number}: {error}")
                output.write(json.dumps(result, ensure_ascii=False) + "\n")
    finally:
        if args.output:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
