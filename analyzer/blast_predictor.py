"""
analyzer/blast_predictor.py
Pre-flight safety check — estimates blast radius before executing an experiment.
Aborts if the cluster is already degraded or the target has only 1 replica.
"""
from __future__ import annotations
from dataclasses import dataclass
from analyzer.cluster_scanner import ClusterTopology
from analyzer.ai_planner import ExperimentPlan


@dataclass
class BlastPrediction:
    safe_to_proceed: bool
    risk_level: str          # low | medium | high | critical
    warnings: list[str]
    estimated_impact: str
    abort_reason: str = ""   # non-empty if safe_to_proceed=False


class BlastPredictor:
    def __init__(self, topology: ClusterTopology):
        self.topology = topology
        self._workload_map = {
            f"{w.namespace}/{w.name}": w for w in topology.workloads
        }

    def predict(self, experiment: ExperimentPlan) -> BlastPrediction:
        warnings: list[str] = []
        abort_reason = ""

        key = f"{experiment.target_namespace}/{experiment.target_workload}"
        workload = self._workload_map.get(key)

        if workload is None:
            return BlastPrediction(
                safe_to_proceed=False,
                risk_level="critical",
                warnings=[],
                estimated_impact="unknown",
                abort_reason=f"Target workload '{key}' not found in cluster topology.",
            )

        # Abort: already degraded
        if workload.replicas_ready < workload.replicas_desired:
            abort_reason = (
                f"Workload '{workload.name}' is already degraded "
                f"({workload.replicas_ready}/{workload.replicas_desired} replicas ready). "
                "Aborting to avoid worsening existing incident."
            )

        # Abort: single replica + pod kill = guaranteed outage
        if (
            experiment.kind == "pod_kill"
            and workload.replicas_desired == 1
            and not abort_reason
        ):
            abort_reason = (
                f"Workload '{workload.name}' has only 1 replica. "
                "A pod kill will cause a full outage. "
                "Set dry_run=true or increase replicas before proceeding."
            )

        # Warning: no HPA
        if not workload.hpa_enabled:
            warnings.append(
                f"'{workload.name}' has no HPA — it cannot auto-scale to recover from load stress."
            )

        # Warning: no resource limits
        no_limits = [
            c["name"] for c in workload.containers
            if not c.get("resources", {}).get("limits")
        ]
        if no_limits:
            warnings.append(
                f"Containers {no_limits} have no resource limits — "
                "CPU/memory stress may cascade to the node."
            )

        # Warning: node drain on single-AZ cluster
        if experiment.kind == "node_drain":
            zones = {n.get("zone", "unknown") for n in self.topology.nodes}
            if len(zones) == 1:
                warnings.append(
                    "All nodes are in a single AZ — node drain may cause widespread disruption."
                )

        # Estimate impact string
        affected = experiment.predicted_affected_services or []
        impact = (
            f"Direct impact on '{experiment.target_workload}'. "
            f"Downstream services potentially affected: {', '.join(affected) if affected else 'none identified'}."
        )

        # Risk level
        if abort_reason:
            risk = "critical"
        elif experiment.severity == "high" or len(warnings) >= 2:
            risk = "high"
        elif experiment.severity == "medium" or len(warnings) == 1:
            risk = "medium"
        else:
            risk = "low"

        return BlastPrediction(
            safe_to_proceed=not bool(abort_reason),
            risk_level=risk,
            warnings=warnings,
            estimated_impact=impact,
            abort_reason=abort_reason,
        )
