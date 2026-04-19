"""
observer/impact_scorer.py
Computes the before/after metric delta for each experiment and produces
a structured ImpactReport with a numeric resilience score.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import json
import time
from observer.prometheus_client import PrometheusClient
from analyzer.ai_planner import ExperimentPlan


@dataclass
class MetricDelta:
    metric: str
    before: float | None
    after: float | None
    delta: float | None
    delta_pct: float | None
    breached_threshold: bool = False


@dataclass
class ImpactReport:
    experiment_name: str
    target_workload: str
    start_time: float
    end_time: float
    duration_seconds: float
    before_snapshot: dict
    after_snapshot: dict
    deltas: list[MetricDelta]
    slo_breached: bool
    slo_breach_reason: str
    resilience_score: int           # 0-100 (100 = no impact detected)
    verdict: str                    # "pass" | "degraded" | "failed"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class ImpactScorer:
    def __init__(self, config_path: str = "config.yaml"):
        self.prom = PrometheusClient(config_path)
        from config_loader import load_config
        cfg = load_config(config_path)
        self.slo = cfg["slo"]

    def take_baseline(self, experiment: ExperimentPlan) -> dict:
        """Call this BEFORE the experiment starts."""
        return self.prom.snapshot(experiment.target_namespace, experiment.target_workload)

    def score(
        self,
        experiment: ExperimentPlan,
        baseline: dict,
        start_time: float,
        end_time: float,
        slo_breach_reason: str = "",
    ) -> ImpactReport:
        """Call this AFTER the experiment ends."""
        # Small wait for metrics to settle
        time.sleep(5)
        after = self.prom.snapshot(experiment.target_namespace, experiment.target_workload)

        deltas = self._compute_deltas(baseline, after)
        resilience_score = self._compute_score(deltas, bool(slo_breach_reason))
        verdict = self._verdict(resilience_score, bool(slo_breach_reason))

        return ImpactReport(
            experiment_name=experiment.name,
            target_workload=experiment.target_workload,
            start_time=start_time,
            end_time=end_time,
            duration_seconds=round(end_time - start_time, 1),
            before_snapshot=baseline,
            after_snapshot=after,
            deltas=deltas,
            slo_breached=bool(slo_breach_reason),
            slo_breach_reason=slo_breach_reason,
            resilience_score=resilience_score,
            verdict=verdict,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_deltas(self, before: dict, after: dict) -> list[MetricDelta]:
        pairs = [
            ("error_rate_pct", "error_rate_percent"),
            ("latency_p99_ms", "latency_p99_ms"),
            ("pod_restarts", "pod_restart_limit"),
            ("cpu_millicores", None),
            ("memory_mb", None),
        ]
        deltas = []
        for metric_key, slo_key in pairs:
            b = before.get(metric_key)
            a = after.get(metric_key)
            delta = None
            delta_pct = None
            if b is not None and a is not None:
                delta = round(a - b, 3)
                if b != 0:
                    delta_pct = round((delta / abs(b)) * 100, 1)

            breached = False
            if slo_key and a is not None:
                breached = a > self.slo.get(slo_key, float("inf"))

            deltas.append(MetricDelta(
                metric=metric_key,
                before=round(b, 3) if b is not None else None,
                after=round(a, 3) if a is not None else None,
                delta=delta,
                delta_pct=delta_pct,
                breached_threshold=breached,
            ))
        return deltas

    @staticmethod
    def _compute_score(deltas: list[MetricDelta], slo_breached: bool) -> int:
        if slo_breached:
            return max(0, 30)

        score = 100
        for d in deltas:
            if d.breached_threshold:
                score -= 25
            elif d.delta_pct is not None and d.delta_pct > 50:
                score -= 10
            elif d.delta_pct is not None and d.delta_pct > 20:
                score -= 5
        return max(0, min(100, score))

    @staticmethod
    def _verdict(score: int, slo_breached: bool) -> str:
        if slo_breached or score < 40:
            return "failed"
        if score < 70:
            return "degraded"
        return "pass"
