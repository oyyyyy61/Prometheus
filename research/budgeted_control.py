"""Reference policies for budgeted active observability and safe control.

The module is deliberately independent from Prometheus and Kubernetes. It is a
small, deterministic baseline that can be driven by replayed metric snapshots
before it is connected to a live control plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping


class ObservationTier(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class TelemetryState(str, Enum):
    HEALTHY = "HEALTHY"
    PARTIAL = "PARTIAL"
    CONFLICTING = "CONFLICTING"
    UNCERTAIN = "UNCERTAIN"


class Decision(str, Enum):
    ACT = "ACT"
    ESCALATE = "ESCALATE"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True)
class ObservationSpec:
    name: str
    tier: ObservationTier
    cost: float
    value: float
    signals: frozenset[str] = frozenset()
    required_for: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.cost <= 0:
            raise ValueError("observation cost must be positive")
        if self.value < 0:
            raise ValueError("observation value cannot be negative")


@dataclass(frozen=True)
class ObservationRequest:
    selected: tuple[ObservationSpec, ...]
    total_cost: float
    remaining_budget: float


@dataclass(frozen=True)
class ObservationContext:
    uncertainty: float
    telemetry_state: TelemetryState
    hypotheses: frozenset[str] = frozenset()
    candidate_actions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not 0 <= self.uncertainty <= 1:
            raise ValueError("uncertainty must be between 0 and 1")


class BudgetedObservationPolicy:
    """Greedy active-observation baseline.

    L0 observations are always preferred. When uncertainty is high, the policy
    buys additional evidence using value/cost, while prioritising observations
    required to verify candidate actions.
    """

    def __init__(self, observations: Iterable[ObservationSpec]) -> None:
        self._observations = tuple(observations)

    def select(self, budget: float, context: ObservationContext) -> ObservationRequest:
        if budget < 0:
            raise ValueError("budget cannot be negative")

        effective_uncertainty = context.uncertainty
        if context.telemetry_state in {
            TelemetryState.UNCERTAIN,
            TelemetryState.CONFLICTING,
        }:
            effective_uncertainty = max(effective_uncertainty, 1.0)

        def priority(spec: ObservationSpec) -> tuple[int, float, str]:
            required = bool(
                spec.required_for.intersection(context.candidate_actions)
            )
            # Required verification evidence outranks generic signals. High
            # uncertainty makes deeper observations more valuable.
            action_bonus = 2 if required else 0
            uncertainty_bonus = effective_uncertainty if spec.tier != ObservationTier.L0 else 0.0
            score = (spec.value + action_bonus + uncertainty_bonus) / spec.cost
            # Keep L0 coverage first, then reserve budget for evidence that
            # can verify a candidate action before buying generic L1/L2 data.
            phase_rank = 0 if spec.tier == ObservationTier.L0 else (1 if required else 2)
            return (phase_rank, -score, spec.name)

        candidates = sorted(self._observations, key=priority)
        selected: list[ObservationSpec] = []
        spent = 0.0
        for spec in candidates:
            if spec.tier != ObservationTier.L0 and effective_uncertainty < 0.2:
                continue
            if spent + spec.cost > budget:
                continue
            selected.append(spec)
            spent += spec.cost

        return ObservationRequest(
            selected=tuple(selected),
            total_cost=spent,
            remaining_budget=budget - spent,
        )


@dataclass(frozen=True)
class Diagnosis:
    hypothesis: str
    confidence: float
    telemetry_state: TelemetryState
    supporting_evidence: frozenset[str] = frozenset()
    missing_evidence: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class ActionSpec:
    name: str
    risk: int
    reversible: bool
    required_evidence: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.risk not in (1, 2, 3):
            raise ValueError("risk must be 1 (low), 2 (medium), or 3 (high)")


@dataclass(frozen=True)
class ControlDecision:
    decision: Decision
    action: ActionSpec | None
    reason: str


class TelemetryAwareSafeControl:
    """Conservative action gate for a diagnosis.

    Risk thresholds intentionally remain explicit so experiments can sweep them
    and measure the safety/availability trade-off.
    """

    CONFIDENCE_BY_RISK = {1: 0.60, 2: 0.75, 3: 0.90}

    def decide(
        self,
        diagnosis: Diagnosis,
        actions: Iterable[ActionSpec],
    ) -> ControlDecision:
        if diagnosis.telemetry_state in {
            TelemetryState.UNCERTAIN,
            TelemetryState.CONFLICTING,
        }:
            return ControlDecision(
                Decision.ABSTAIN,
                None,
                "telemetry is not trustworthy; restore or escalate observation first",
            )

        actions = [action for action in actions if action.name != "telemetry_escalation"]
        if diagnosis.telemetry_state == TelemetryState.PARTIAL:
            actions = [action for action in actions if action.risk == 1]

        eligible = [
            action
            for action in actions
            if action.required_evidence.issubset(diagnosis.supporting_evidence)
            and diagnosis.confidence >= self.CONFIDENCE_BY_RISK[action.risk]
        ]
        if not eligible:
            if diagnosis.missing_evidence:
                return ControlDecision(
                    Decision.ESCALATE,
                    None,
                    "missing evidence prevents a risk-appropriate action",
                )
            return ControlDecision(
                Decision.ABSTAIN,
                None,
                "diagnostic confidence is below every available action threshold",
            )

        # Prefer the least risky reversible action. This creates a stable
        # baseline for comparing aggressive policies in later experiments.
        action = min(eligible, key=lambda candidate: (candidate.risk, not candidate.reversible, candidate.name))
        return ControlDecision(Decision.ACT, action, "evidence and confidence satisfy the action gate")


@dataclass(frozen=True)
class VerificationRule:
    metric: str
    direction: str
    minimum_delta: float

    def __post_init__(self) -> None:
        if self.direction not in {"increase", "decrease"}:
            raise ValueError("direction must be increase or decrease")
        if self.minimum_delta < 0:
            raise ValueError("minimum_delta cannot be negative")


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    observed_delta: float | None
    reason: str


def verify_action(
    before: Mapping[str, float],
    after: Mapping[str, float],
    rules: Iterable[VerificationRule],
) -> VerificationResult:
    """Check post-action metrics without treating missing data as success."""

    observed: list[float] = []
    for rule in rules:
        if rule.metric not in before or rule.metric not in after:
            return VerificationResult(False, None, f"missing metric: {rule.metric}")
        delta = after[rule.metric] - before[rule.metric]
        observed.append(delta)
        if rule.direction == "increase" and delta < rule.minimum_delta:
            return VerificationResult(False, delta, f"{rule.metric} did not increase enough")
        if rule.direction == "decrease" and delta > -rule.minimum_delta:
            return VerificationResult(False, delta, f"{rule.metric} did not decrease enough")

    aggregate = sum(observed) / len(observed) if observed else 0.0
    return VerificationResult(True, aggregate, "all post-action conditions passed")


def default_observations() -> tuple[ObservationSpec, ...]:
    """A small signal catalog aligned with the current Prometheus prototype."""

    return (
        ObservationSpec("gpu_utilization", ObservationTier.L0, 1.0, 1.0, frozenset({"compute"})),
        ObservationSpec("hbm_occupancy", ObservationTier.L0, 1.0, 1.0, frozenset({"memory"})),
        ObservationSpec("queue_latency", ObservationTier.L0, 1.0, 1.2, frozenset({"slo"})),
        ObservationSpec("scrape_freshness", ObservationTier.L0, 0.5, 1.5, frozenset({"telemetry"})),
        ObservationSpec("cpu_network", ObservationTier.L1, 2.0, 2.5, frozenset({"cpu", "io"})),
        ObservationSpec("workflow_phase", ObservationTier.L1, 1.0, 2.0, frozenset({"workflow"})),
        ObservationSpec("kv_recompute", ObservationTier.L1, 1.5, 2.5, frozenset({"memory"}), frozenset({"batch_adjustment"})),
        ObservationSpec("nccl_collective", ObservationTier.L2, 5.0, 5.0, frozenset({"communication"}), frozenset({"reroute"})),
        ObservationSpec("pcie_nvlink", ObservationTier.L2, 4.0, 4.0, frozenset({"communication"}), frozenset({"reroute"})),
        ObservationSpec("short_trace", ObservationTier.L2, 4.0, 3.5, frozenset({"causal"})),
    )


def telemetry_state_from_metrics(metrics: Mapping[str, float]) -> TelemetryState:
    """Classify telemetry integrity from a small, replay-friendly signal set."""

    freshness = metrics.get("scrape_freshness")
    sample_loss = metrics.get("sample_loss", 0.0)
    gaps = metrics.get("time_series_gaps", 0.0)
    conflicts = metrics.get("sensor_conflicts", 0.0)

    if conflicts > 0:
        return TelemetryState.CONFLICTING
    if freshness is None or freshness < 0.5 or sample_loss >= 0.2 or gaps > 0:
        return TelemetryState.UNCERTAIN
    if freshness < 0.9 or sample_loss > 0:
        return TelemetryState.PARTIAL
    return TelemetryState.HEALTHY
