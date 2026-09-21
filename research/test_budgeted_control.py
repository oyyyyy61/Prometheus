import unittest

from budgeted_control import (
    ActionSpec,
    BudgetedObservationPolicy,
    Decision,
    Diagnosis,
    ObservationContext,
    TelemetryAwareSafeControl,
    TelemetryState,
    VerificationRule,
    default_observations,
    telemetry_state_from_metrics,
    verify_action,
)


class BudgetedObservationTests(unittest.TestCase):
    def test_low_uncertainty_stays_at_l0(self):
        request = BudgetedObservationPolicy(default_observations()).select(
            4.0,
            ObservationContext(0.1, TelemetryState.HEALTHY),
        )
        self.assertTrue(request.selected)
        self.assertTrue(all(spec.tier.value == "L0" for spec in request.selected))

    def test_high_uncertainty_buys_action_evidence(self):
        request = BudgetedObservationPolicy(default_observations()).select(
            13.0,
            ObservationContext(
                0.9,
                TelemetryState.HEALTHY,
                candidate_actions=frozenset({"reroute"}),
            ),
        )
        names = {spec.name for spec in request.selected}
        self.assertIn("nccl_collective", names)
        self.assertLessEqual(request.total_cost, 13.0)


class SafeControlTests(unittest.TestCase):
    def test_uncertain_telemetry_abstains(self):
        diagnosis = Diagnosis("COMMUNICATION_STALLED", 0.99, TelemetryState.UNCERTAIN)
        decision = TelemetryAwareSafeControl().decide(
            diagnosis,
            [ActionSpec("reroute", 2, True)],
        )
        self.assertEqual(decision.decision, Decision.ABSTAIN)

    def test_partial_telemetry_blocks_medium_risk_action(self):
        diagnosis = Diagnosis(
            "COMMUNICATION_STALLED",
            0.99,
            TelemetryState.PARTIAL,
            supporting_evidence=frozenset({"nccl_collective"}),
        )
        decision = TelemetryAwareSafeControl().decide(
            diagnosis,
            [ActionSpec("reroute", 2, True, frozenset({"nccl_collective"}))],
        )
        self.assertEqual(decision.decision, Decision.ABSTAIN)

    def test_missing_evidence_escalates(self):
        diagnosis = Diagnosis(
            "COMMUNICATION_STALLED",
            0.85,
            TelemetryState.HEALTHY,
            missing_evidence=frozenset({"nccl_collective"}),
        )
        decision = TelemetryAwareSafeControl().decide(
            diagnosis,
            [ActionSpec("reroute", 2, True, frozenset({"nccl_collective"}))],
        )
        self.assertEqual(decision.decision, Decision.ESCALATE)

    def test_low_risk_reversible_action_is_preferred(self):
        diagnosis = Diagnosis(
            "MEMORY_PRESSURE",
            0.82,
            TelemetryState.HEALTHY,
            supporting_evidence=frozenset({"hbm", "kv"}),
        )
        decision = TelemetryAwareSafeControl().decide(
            diagnosis,
            [
                ActionSpec("restart_pod", 3, False, frozenset({"hbm"})),
                ActionSpec("batch_adjustment", 1, True, frozenset({"hbm"})),
            ],
        )
        self.assertEqual(decision.decision, Decision.ACT)
        self.assertEqual(decision.action.name, "batch_adjustment")


class VerificationTests(unittest.TestCase):
    def test_missing_after_metric_fails_closed(self):
        result = verify_action(
            {"ttft_p99": 100.0},
            {},
            [VerificationRule("ttft_p99", "decrease", 10.0)],
        )
        self.assertFalse(result.passed)

    def test_postcondition_passes(self):
        result = verify_action(
            {"ttft_p99": 100.0, "nccl_p99": 20.0},
            {"ttft_p99": 80.0, "nccl_p99": 15.0},
            [
                VerificationRule("ttft_p99", "decrease", 10.0),
                VerificationRule("nccl_p99", "decrease", 3.0),
            ],
        )
        self.assertTrue(result.passed)

    def test_telemetry_integrity_is_first_class(self):
        self.assertEqual(
            telemetry_state_from_metrics({"scrape_freshness": 0.4}),
            TelemetryState.UNCERTAIN,
        )
        self.assertEqual(
            telemetry_state_from_metrics({"scrape_freshness": 0.8, "sample_loss": 0.05}),
            TelemetryState.PARTIAL,
        )
        self.assertEqual(
            telemetry_state_from_metrics({"scrape_freshness": 1.0, "sensor_conflicts": 1}),
            TelemetryState.CONFLICTING,
        )


if __name__ == "__main__":
    unittest.main()
