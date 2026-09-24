import unittest
from urllib.request import Request, urlopen
import json
import socket

from prometheus_client import CollectorRegistry, generate_latest

try:
    from .agent_memory_exporter import AgentMemoryExporter, start_agent_event_server
except ImportError:  # Direct execution from the npu directory.
    from agent_memory_exporter import AgentMemoryExporter, start_agent_event_server


class AgentMemoryExporterTest(unittest.TestCase):
    def setUp(self):
        self.registry = CollectorRegistry()
        self.exporter = AgentMemoryExporter(
            registry=self.registry,
            runtime="test-runtime",
            model="test-model",
            device="npu-0",
        )

    def test_aggregates_agent_and_memory_state(self):
        self.exporter.upsert_agent(
            "agent-a",
            state="tool_wait",
            stage="tool_wait",
            context_tokens=100,
            branch_count=2,
            tool_wait_seconds=3.5,
        )
        self.exporter.set_memory(
            "agent-a",
            memory_kind="kv_cache",
            tier="npu",
            logical_bytes=1000,
            physical_bytes=700,
            shared_bytes=300,
            reclaimable_bytes=200,
            block_count=7,
        )
        output = generate_latest(self.registry).decode()

        self.assertIn(
            'agent_lifecycle_agents{device="npu-0",model="test-model",runtime="test-runtime",stage="tool_wait",state="tool_wait"} 1.0',
            output,
        )
        self.assertIn(
            'agent_memory_bytes{device="npu-0",memory_kind="kv_cache",model="test-model",runtime="test-runtime",state="tool_wait",tier="npu",view="reclaimable"} 200.0',
            output,
        )
        self.assertIn(
            'agent_memory_blocks{device="npu-0",memory_kind="kv_cache",model="test-model",runtime="test-runtime",state="tool_wait",tier="npu"} 7.0',
            output,
        )

    def test_events_and_transfers_are_counters(self):
        self.exporter.upsert_agent("agent-a")
        self.exporter.record_kv_hit(2)
        self.exporter.record_kv_miss()
        self.exporter.set_kv_cache_totals(hits=8, misses=2)
        self.exporter.record_recompute(128)
        self.exporter.record_transfer(
            "offload",
            bytes_count=4096,
            duration_seconds=0.25,
            source_tier="npu",
            target_tier="cpu",
        )
        output = generate_latest(self.registry).decode()

        self.assertIn("agent_kv_cache_hits_total", output)
        self.assertIn("agent_kv_cache_misses_total", output)
        self.assertIn('agent_kv_cache_hit_ratio{device="npu-0",model="test-model",runtime="test-runtime"} 0.8', output)
        self.assertIn("agent_kv_recompute_tokens_total", output)
        self.assertIn("agent_memory_offload_bytes_total", output)
        self.assertIn('event="offload"', output)

    def test_unknown_labels_are_bounded(self):
        self.exporter.upsert_agent("agent-a", state="unbounded-state", stage="unbounded-stage")
        self.exporter.set_memory("agent-a", memory_kind="unbounded-memory")
        snapshot = self.exporter.snapshot()

        self.assertEqual(snapshot["tracked_agents"], 1)
        output = generate_latest(self.registry).decode()
        self.assertIn('stage="unknown"', output)
        self.assertIn('state="unknown"', output)
        self.assertIn('memory_kind="unknown"', output)

    def test_agent_event_endpoint_updates_exporter(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = start_agent_event_server(self.exporter, port=port)
        try:
            body = json.dumps(
                {
                    "event": "upsert",
                    "agent_id": "remote-agent",
                    "state": "tool_wait",
                    "stage": "tool_wait",
                    "tool_wait_seconds": 2.5,
                }
            ).encode()
            request = Request(
                f"http://127.0.0.1:{port}/agent-events",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
            output = generate_latest(self.registry).decode()
            self.assertIn('state="tool_wait"', output)
            self.assertIn('stage="tool_wait"', output)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
