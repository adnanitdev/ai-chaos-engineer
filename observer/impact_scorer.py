"""
observer/impact_scorer.py

Uses min_over_time range queries to capture disruption that recovers
faster than the Prometheus scrape interval (15s).
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
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
    resilience_score: int
    verdict: str

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
        return self.prom.snapshot(experiment.target_namespace, experiment.target_workload)

    def score(
        self,
        experiment: ExperimentPlan,
        baseline: dict,
        start_time: float,
        end_time: float,
        slo_breach_reason: str = "",
    ) -> ImpactReport:
        time.sleep(5)
        after = self.prom.snapshot(experiment.target_namespace, experiment.target_workload)
        deltas = self._compute_deltas(baseline, after)
        resilience_score = self._compute_score(
            deltas, bool(slo_breach_reason), experiment.kind,
            experiment.target_namespace, experiment.target_workload,
            start_time, end_time,
        )
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

    def _compute_deltas(self, before: dict, after: dict) -> list[MetricDelta]:
        metrics = [
            ("availability_pct",  "availability_pct",    None),
            ("pod_restarts",      "pod_restart_limit",   None),
            ("cpu_millicores",    None,                  None),
            ("memory_mb",         None,                  None),
            ("network_bytes_sec", None,                  None),
            ("error_rate_pct",    "error_rate_percent",  None),
        ]
        deltas = []
        for key, slo_key, _ in metrics:
            b = before.get(key)
            a = after.get(key)
            delta, delta_pct = None, None
            if b is not None and a is not None:
                delta = round(a - b, 3)
                if b != 0:
                    delta_pct = round((delta / abs(b)) * 100, 1)
            breached = False
            if slo_key and a is not None:
                breached = a > self.slo.get(slo_key, float("inf"))
            deltas.append(MetricDelta(
                metric=key,
                before=round(b, 3) if b is not None else None,
                after=round(a, 3) if a is not None else None,
                delta=delta,
                delta_pct=delta_pct,
                breached_threshold=breached,
            ))
        return deltas

    def _compute_score(
        self,
        deltas: list[MetricDelta],
        slo_breached: bool,
        kind: str,
        namespace: str,
        workload: str,
        start_time: float,
        end_time: float,
    ) -> int:
        if slo_breached:
            return 30

        score = 100
        dm = {d.metric: d for d in deltas}
        window = f"{int(end_time - start_time) + 30}s"

        # ── pod_kill: use min_over_time to catch fast recovery ────────
        if kind in ("pod_kill", "node_drain"):
            min_replicas = self.prom._scalar(
                f'min_over_time(kube_deployment_status_replicas_available{{'
                f'namespace="{namespace}",deployment="{workload}"}}[{window}])'
            )
            desired = self.prom.query_desired_replicas(namespace, workload)
            if min_replicas is not None and desired > 0:
                min_pct = round(min_replicas / desired * 100, 1)
                drop = 100 - min_pct
                if drop >= 50:
                    score -= 40
                elif drop >= 25:
                    score -= 25
                elif drop >= 1:
                    score -= 15

        # ── cpu_stress: use max_over_time for CPU spike ───────────────
        if kind in ("cpu_stress", "memory_stress"):
            max_cpu = self.prom._scalar(
                f'max_over_time(sum(rate(container_cpu_usage_seconds_total{{'
                f'namespace="{namespace}",pod=~"{workload}-.*",container!=""}}[2m]))[{window}:15s]) * 1000'
            )
            baseline_cpu = dm.get("cpu_millicores")
            if max_cpu and baseline_cpu and baseline_cpu.before and baseline_cpu.before > 0:
                spike_pct = round((max_cpu - baseline_cpu.before) / baseline_cpu.before * 100, 1)
                if spike_pct > 200: score -= 25
                elif spike_pct > 100: score -= 15
                elif spike_pct > 50: score -= 8

        # ── memory_stress: use max_over_time for memory spike ─────────
        if kind == "memory_stress":
            max_mem = self.prom._scalar(
                f'max_over_time(sum(container_memory_working_set_bytes{{'
                f'namespace="{namespace}",pod=~"{workload}-.*",container!=""}})[{window}:15s]) / 1024 / 1024'
            )
            baseline_mem = dm.get("memory_mb")
            if max_mem and baseline_mem and baseline_mem.before and baseline_mem.before > 0:
                spike_pct = round((max_mem - baseline_mem.before) / baseline_mem.before * 100, 1)
                if spike_pct > 100: score -= 20
                elif spike_pct > 50: score -= 10

        # ── network chaos: use min_over_time for traffic drop ─────────
        if kind in ("network_latency", "network_partition"):
            min_net = self.prom._scalar(
                f'min_over_time(sum(rate(container_network_transmit_bytes_total{{'
                f'namespace="{namespace}",pod=~"{workload}-.*"}}[2m]))[{window}:15s])'
            )
            baseline_net = dm.get("network_bytes_sec")
            if min_net is not None and baseline_net and baseline_net.before and baseline_net.before > 0:
                drop_pct = round((baseline_net.before - min_net) / baseline_net.before * 100, 1)
                if drop_pct > 50: score -= 30
                elif drop_pct > 20: score -= 15

        # ── SLO threshold breaches ────────────────────────────────────
        for d in deltas:
            if d.breached_threshold:
                score -= 15

        return max(0, min(100, score))

    @staticmethod
    def _verdict(score: int, slo_breached: bool) -> str:
        if slo_breached or score < 40:
            return "failed"
        if score < 70:
            return "degraded"
        return "pass"
