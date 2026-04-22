"""
observer/impact_scorer.py
Scores experiments using metrics that actually exist on EKS.

Scoring signals by experiment type:
  pod_kill          → availability_pct drop + pod_restarts spike
  cpu_stress        → cpu_millicores spike
  memory_stress     → memory_mb spike + cpu_millicores spike
  network_latency   → network_bytes_sec drop
  network_partition → network_bytes_sec drop + availability_pct drop
  node_drain        → availability_pct drop across multiple workloads
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
    resilience_score: int        # 0-100 (100 = no measurable impact)
    verdict: str                 # "pass" | "degraded" | "failed"

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
        time.sleep(5)  # let metrics settle
        after = self.prom.snapshot(experiment.target_namespace, experiment.target_workload)

        deltas = self._compute_deltas(baseline, after)
        resilience_score = self._compute_score(deltas, bool(slo_breach_reason), experiment.kind)
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
        metrics = [
            # (metric_key,        slo_config_key,       availability_invert)
            ("availability_pct",  None,                  True),   # lower = worse
            ("pod_restarts",      "pod_restart_limit",   False),
            ("cpu_millicores",    None,                  False),
            ("memory_mb",         None,                  False),
            ("network_bytes_sec", None,                  False),  # lower = disrupted
            ("error_rate_pct",    "error_rate_percent",  False),
        ]
        deltas = []
        for metric_key, slo_key, avail_invert in metrics:
            b = before.get(metric_key)
            a = after.get(metric_key)
            delta = None
            delta_pct = None

            if b is not None and a is not None:
                delta = round(a - b, 3)
                if b != 0:
                    delta_pct = round((delta / abs(b)) * 100, 1)

            breached = False
            # SLO threshold breach (restarts, error rate)
            if slo_key and a is not None:
                breached = a > self.slo.get(slo_key, float("inf"))
            # Availability breach: any drop below 100% is a breach
            if avail_invert and a is not None:
                breached = a < 100.0

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
    def _compute_score(deltas: list[MetricDelta], slo_breached: bool, kind: str) -> int:
        if slo_breached:
            return 30

        score = 100
        dm = {d.metric: d for d in deltas}

        # ── 1. Availability drop — strongest signal for pod_kill ──────
        avail = dm.get("availability_pct")
        if avail and avail.delta is not None:
            drop = -avail.delta  # positive value means availability fell
            if drop >= 50:
                score -= 45      # e.g. 2 replicas → 1 ready = 50% drop
            elif drop >= 25:
                score -= 25
            elif drop >= 1:
                score -= 15

        # ── 2. Pod restarts — direct crash evidence ───────────────────
        restarts = dm.get("pod_restarts")
        if restarts and restarts.delta is not None and restarts.delta > 0:
            score -= min(30, int(restarts.delta * 10))

        # ── 3. CPU spike — stress experiment signal ───────────────────
        cpu = dm.get("cpu_millicores")
        if cpu and cpu.delta_pct is not None and kind in ("cpu_stress", "memory_stress"):
            if cpu.delta_pct > 200:
                score -= 25
            elif cpu.delta_pct > 100:
                score -= 15
            elif cpu.delta_pct > 50:
                score -= 8

        # ── 4. Memory spike — memory stress signal ────────────────────
        mem = dm.get("memory_mb")
        if mem and mem.delta_pct is not None and kind == "memory_stress":
            if mem.delta_pct > 100:
                score -= 20
            elif mem.delta_pct > 50:
                score -= 10

        # ── 5. Network drop — partition/latency signal ────────────────
        net = dm.get("network_bytes_sec")
        if net and net.delta_pct is not None and kind in ("network_latency", "network_partition"):
            if net.delta_pct < -60:
                score -= 30
            elif net.delta_pct < -30:
                score -= 15
            elif net.delta_pct < -10:
                score -= 8

        # ── 6. Any SLO threshold breach ───────────────────────────────
        for d in deltas:
            if d.breached_threshold and d.metric != "availability_pct":
                score -= 15

        return max(0, min(100, score))

    @staticmethod
    def _verdict(score: int, slo_breached: bool) -> str:
        if slo_breached or score < 40:
            return "failed"
        if score < 70:
            return "degraded"
        return "pass"