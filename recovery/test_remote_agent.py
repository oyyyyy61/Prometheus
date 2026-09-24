import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from prometheus_client import CollectorRegistry

try:
    from .remote_agent import AgentConfig, RecoveryAgent
except ImportError:
    from remote_agent import AgentConfig, RecoveryAgent


class RemoteRecoveryAgentTest(unittest.TestCase):
    def test_only_allowlisted_systemd_actions_are_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
            agent = RecoveryAgent(
                AgentConfig(
                    token="secret",
                    bridge_unit="bridge.service",
                    vllm_unit="vllm.service",
                    checkpoint_source=root / "source.pt",
                    checkpoint_target=root / "target.pt",
                    cooldown_seconds=0,
                    use_sudo=False,
                ),
                runner=runner,
                registry=CollectorRegistry(),
            )
            agent.execute("restart_bridge")
            agent.execute("restart_vllm")
            self.assertEqual(runner.call_args_list[0].args[0], ["systemctl", "restart", "bridge.service"])
            self.assertEqual(runner.call_args_list[1].args[0], ["systemctl", "restart", "vllm.service"])
            with self.assertRaises(ValueError):
                agent.execute("sh -c id")

    def test_restore_checkpoint_is_atomic_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pt"
            target = root / "nested" / "target.pt"
            source.write_bytes(b"checkpoint-v2")
            agent = RecoveryAgent(
                AgentConfig(
                    token="secret",
                    bridge_unit="bridge.service",
                    vllm_unit="vllm.service",
                    checkpoint_source=source,
                    checkpoint_target=target,
                    cooldown_seconds=0,
                ),
                registry=CollectorRegistry(),
            )
            result = agent.execute("restore_checkpoint")
            self.assertEqual(result["status"], "completed")
            self.assertEqual(target.read_bytes(), b"checkpoint-v2")
            self.assertFalse(any(target.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
