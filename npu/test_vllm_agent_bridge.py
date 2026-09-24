import unittest
from unittest.mock import patch

from prometheus_client import CollectorRegistry, generate_latest

from npu.agent_memory_exporter import AgentMemoryExporter
from npu.vllm_agent_bridge import VllmAgentBridge, parse_npu_smi_usages, parse_samples


VLLM_METRICS = """
# TYPE vllm:prefix_cache_hits_total counter
vllm:prefix_cache_hits_total{engine="0",model_name="Qwen3.5-9B"} 2
# TYPE vllm:prefix_cache_queries_total counter
vllm:prefix_cache_queries_total{engine="0",model_name="Qwen3.5-9B"} 10
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{engine="0",model_name="Qwen3.5-9B"} 100
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{engine="0",model_name="Qwen3.5-9B"} 20
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen3.5-9B"} 1
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0",model_name="Qwen3.5-9B"} 0
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0",model_name="Qwen3.5-9B"} 0.25
"""

NPU_METRICS = """
# TYPE npu_chip_info_hbm_total_memory gauge
npu_chip_info_hbm_total_memory{id="4"} 65536
"""

NPU_SMI = """
\tNPU ID                         : 4
\tAicore Usage Rate(%)           : 83
\tAivector Usage Rate(%)         : 9
\tHBM Bandwidth Usage Rate(%)    : 40
\tNPU Utilization(%)             : 96
\tAicube Usage Rate(%)           : 46
"""


class VllmAgentBridgeTest(unittest.TestCase):
    def test_parse_samples(self):
        samples = parse_samples(VLLM_METRICS)
        self.assertTrue(any(sample.name == "vllm:kv_cache_usage_perc" for sample in samples))

    def test_parse_npu_smi_usages(self):
        usages = parse_npu_smi_usages(NPU_SMI)
        self.assertEqual(usages["4"]["aicore_percent"], 83)
        self.assertEqual(usages["4"]["overall_percent"], 96)

    def test_poll_aggregates_router_vllm_and_npu(self):
        registry = CollectorRegistry()
        exporter = AgentMemoryExporter(
            registry=registry,
            runtime="bridge-test",
            model="Qwen3.5-9B",
            device="ascend-910b2",
        )
        bridge = VllmAgentBridge(exporter)
        routing = {"policy": "sticky", "replicas": 2, "sessions": {"0": 1}, "requests": {"0": 1}}

        with patch("npu.vllm_agent_bridge.fetch_json", return_value=routing), patch(
            "npu.vllm_agent_bridge.fetch_text",
            side_effect=[VLLM_METRICS, NPU_METRICS, VLLM_METRICS.replace(" 2\n", " 3\n", 1).replace(" 10\n", " 12\n", 1).replace(" 100\n", " 125\n", 1).replace(" 20\n", " 24\n", 1), NPU_METRICS],
        ):
            bridge.poll_once()
            bridge.poll_once()

        output = generate_latest(registry).decode()
        self.assertIn('agent_lifecycle_agents{device="ascend-910b2",model="Qwen3.5-9B",runtime="bridge-test",stage="decode",state="active"} 1.0', output)
        self.assertIn('view="physical"', output)
        self.assertIn("agent_kv_cache_hits_total", output)
        self.assertIn("agent_prompt_tokens_total", output)
        self.assertIn("agent_generation_tokens_total", output)
        self.assertIn("agent_kv_cache_hit_ratio", output)


if __name__ == "__main__":
    unittest.main()
