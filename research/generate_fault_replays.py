"""生成四类故障的研究回放数据。

这些数据用于验证“主动观测 + Telemetry-aware Safe Control”的策略行为。
它们模拟的是观测结果，不会启动训练进程，也不会调用 Kubernetes 或执行
恢复动作。每条 JSONL 记录都可以直接交给 replay.py。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCENARIOS = ("cpu_io", "memory_pressure", "communication", "telemetry_failure")


def _base_snapshot(step: int, scenario: str, budget: float) -> dict:
    """构造所有场景都共享的基本字段。"""

    return {
        "timestamp": step,
        "scenario": scenario,
        "source": "synthetic_fault_replay",
        "budget": budget,
        "telemetry": {
            "scrape_freshness": 1.0,
            "sample_loss": 0.0,
            "time_series_gaps": 0.0,
            "sensor_conflicts": 0.0,
        },
        "action_candidates": [],
    }


def generate_scenario(name: str, steps: int = 8, budget: float = 8.0) -> list[dict]:
    """按时间顺序生成一个场景，前两步正常，后续步骤出现故障。"""

    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario: {name}")
    if steps < 3:
        raise ValueError("steps must be at least 3 so that normal and fault phases exist")

    snapshots: list[dict] = []
    for step in range(steps):
        snapshot = _base_snapshot(step, name, budget)
        fault_active = step >= 2

        # 这些 L0 字段模拟 serving 层常见的低利用率表象。
        snapshot["metrics"] = {
            "gpu_utilization": 0.72 if not fault_active else 0.20,
            "hbm_occupancy": 0.55 if not fault_active else 0.60,
            "queue_latency": 1.0 if not fault_active else 3.0,
        }

        if name == "cpu_io":
            # GPU 看起来空闲，CPU 或外部 IO 却变慢。这是最容易被错误
            # 解释成“GPU 不够用”的场景。
            snapshot["metrics"].update({
                "cpu_utilization": 0.92 if fault_active else 0.45,
                "io_wait": 0.80 if fault_active else 0.08,
                "tool_wait_seconds": 4.0 if fault_active else 0.1,
            })
            snapshot["action_candidates"] = ["increase_cpu_workers", "batch_adjustment"]
            if fault_active:
                snapshot["uncertainty"] = 0.72
                snapshot["diagnosis"] = {
                    "hypothesis": "CPU_OR_IO_BOTTLENECK",
                    "confidence": 0.82,
                    "supporting_evidence": ["cpu_network", "workflow_phase"],
                }

        elif name == "memory_pressure":
            # HBM 接近满载，KV cache 命中率下降，重算增加。GPU kernel
            # 仍然活跃，所以单看 GPU utilization 无法完成归因。
            snapshot["metrics"].update({
                "hbm_occupancy": 0.96 if fault_active else 0.55,
                "kv_recompute": 0.38 if fault_active else 0.02,
                "cache_hit_ratio": 0.48 if fault_active else 0.91,
            })
            snapshot["action_candidates"] = ["batch_adjustment"]
            if fault_active:
                snapshot["uncertainty"] = 0.68
                snapshot["diagnosis"] = {
                    "hypothesis": "MEMORY_PRESSURE",
                    "confidence": 0.86,
                    "supporting_evidence": ["hbm", "kv"],
                }

        elif name == "communication":
            # 所有 rank 的 GPU 利用率平均值可能仍然不低，但 collective
            # p99 和 rank skew 已经升高，适合测试 reroute 的证据门控。
            snapshot["metrics"].update({
                "nccl_collective_p99": 8.0 if fault_active else 1.2,
                "rank_skew": 0.42 if fault_active else 0.04,
                "pcie_nvlink_pressure": 0.88 if fault_active else 0.20,
            })
            snapshot["action_candidates"] = ["reroute", "placement_restriction"]
            if fault_active:
                snapshot["uncertainty"] = 0.84
                snapshot["diagnosis"] = {
                    "hypothesis": "COMMUNICATION_STALLED",
                    "confidence": 0.91,
                    "supporting_evidence": ["nccl_collective"],
                }

        elif name == "telemetry_failure":
            # 故障发生后，设备指标和 scrape payload 消失。即使其他旧
            # 指标看起来平稳，也不能据此执行 scale-down 或重启。
            if fault_active:
                snapshot["telemetry"].update({
                    "scrape_freshness": 0.1,
                    "sample_loss": 0.65,
                    "time_series_gaps": 1.0,
                })
                snapshot["metrics"].pop("gpu_utilization")
                snapshot["metrics"].pop("hbm_occupancy")
                snapshot["uncertainty"] = 0.99
                snapshot["diagnosis"] = {
                    "hypothesis": "TELEMETRY_UNCERTAIN",
                    "confidence": 0.99,
                    "supporting_evidence": [],
                    "missing_evidence": ["gpu_utilization", "hbm_occupancy"],
                }
            snapshot["action_candidates"] = ["reroute", "restart_pod"]

        snapshots.append(snapshot)
    return snapshots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="cpu_io")
    parser.add_argument("--all", action="store_true", help="依次生成四类场景")
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--budget", type=float, default=8.0)
    parser.add_argument("--output", type=Path, help="输出 JSONL 文件；省略时输出到标准输出")
    args = parser.parse_args(argv)

    if args.steps < 3 or args.budget < 0:
        parser.error("steps 至少为 3，budget 不能为负数")

    names = SCENARIOS if args.all else (args.scenario,)
    output = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
    try:
        for name in names:
            for snapshot in generate_scenario(name, args.steps, args.budget):
                output.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    finally:
        if args.output:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

