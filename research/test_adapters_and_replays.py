import json
import unittest

from generate_fault_replays import generate_scenario
from prometheus_adapter import PrometheusAdapter, PrometheusClient
from replay import process_snapshot


class FakeResponse:
    """模拟 urllib 响应对象，避免测试必须启动 Prometheus。"""

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakePrometheus:
    """根据 PromQL 返回当前训练 exporter 的一组固定样本。"""

    VALUES = {
        'up{job="training"}': 1.0,
        "training_current_epoch": 12.0,
        "training_current_loss": 0.03,
        "training_checkpoint_save_timestamp_seconds": 990.0,
        "training_checkpoint_save_duration_seconds": 0.02,
        "training_checkpoint_file_size_bytes": 2048.0,
        "training_resume_total": 1.0,
    }

    def __call__(self, request, timeout):
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(request.full_url).query)["query"][0]
        value = self.VALUES.get(query)
        result = [] if value is None else [{"value": ["1", str(value)]}]
        return FakeResponse({"status": "success", "data": {"result": result}})


class MissingPrometheus(FakePrometheus):
    """模拟 exporter 存活但部分时间序列消失的情况。"""

    def __call__(self, request, timeout):
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(request.full_url).query)["query"][0]
        if query == "training_current_loss":
            return FakeResponse({"status": "success", "data": {"result": []}})
        return super().__call__(request, timeout)


class AdapterTests(unittest.TestCase):
    def test_training_metrics_become_snapshot(self):
        client = PrometheusClient(opener=FakePrometheus())
        snapshot = PrometheusAdapter(client).snapshot(
            budget=8.0,
            action_candidates=["batch_adjustment"],
            timestamp=1000.0,
        )
        self.assertEqual(snapshot["metrics"]["training_current_epoch"], 12.0)
        self.assertEqual(snapshot["metrics"]["training_up"], 1.0)
        self.assertAlmostEqual(snapshot["metrics"]["checkpoint_age_seconds"], 10.0)
        self.assertEqual(snapshot["telemetry"]["sample_loss"], 0.0)
        self.assertEqual(snapshot["adapter_errors"], {})

    def test_missing_prometheus_sample_marks_telemetry_loss(self):
        client = PrometheusClient(opener=MissingPrometheus())
        snapshot = PrometheusAdapter(client).snapshot(budget=8.0, timestamp=1000.0)
        self.assertGreater(snapshot["telemetry"]["sample_loss"], 0.0)
        self.assertIn("training_current_loss", snapshot["adapter_errors"])


class FaultReplayTests(unittest.TestCase):
    def test_all_four_scenarios_have_normal_and_fault_phases(self):
        for name in ("cpu_io", "memory_pressure", "communication", "telemetry_failure"):
            snapshots = generate_scenario(name, steps=5)
            self.assertEqual(len(snapshots), 5)
            self.assertNotIn("diagnosis", snapshots[0])
            self.assertIn("diagnosis", snapshots[-1])
            self.assertEqual(snapshots[-1]["scenario"], name)

    def test_telemetry_failure_abstains_even_with_high_confidence(self):
        snapshot = generate_scenario("telemetry_failure", steps=4)[-1]
        decision = process_snapshot(snapshot)["control"]
        self.assertEqual(decision["decision"], "ABSTAIN")
        self.assertIsNone(decision["action"])

    def test_communication_replay_can_act_when_evidence_is_present(self):
        snapshot = generate_scenario("communication", steps=4, budget=13)[-1]
        decision = process_snapshot(snapshot)["control"]
        self.assertEqual(decision["decision"], "ACT")
        self.assertEqual(decision["action"], "placement_restriction")

    def test_budget_limited_communication_replay_escalates(self):
        # 8 个预算单位足够 L0 和一部分 L1，但不足以购买 NCCL 证据。
        snapshot = generate_scenario("communication", steps=4, budget=8)[-1]
        decision = process_snapshot(snapshot)["control"]
        self.assertEqual(decision["decision"], "ESCALATE")


if __name__ == "__main__":
    unittest.main()
