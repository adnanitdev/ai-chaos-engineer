"""
tests/test_blast_predictor.py
Unit tests for blast prediction logic — no cluster connection needed.
"""
import pytest
from unittest.mock import MagicMock
from analyzer.cluster_scanner import ClusterTopology, ServiceNode
from analyzer.ai_planner import ExperimentPlan
from analyzer.blast_predictor import BlastPredictor


def _make_topology(workloads: list[ServiceNode]) -> ClusterTopology:
    return ClusterTopology(
        context="test-context",
        namespace="default",
        nodes=[{"name": "node-1", "ready": True, "roles": ["worker"],
                "capacity": {"cpu": "4", "memory": "8Gi"},
                "instance_type": "t3.medium", "zone": "us-east-1a"}],
        workloads=workloads,
        services=[],
        config_maps=[],
        persistent_volumes=[],
        ingresses=[],
        network_policies=[],
    )


def _make_workload(name: str, replicas: int = 2, ready: int = 2,
                    hpa: bool = True, has_limits: bool = True) -> ServiceNode:
    containers = [{"name": "app", "image": "app:latest",
                   "resources": {"limits": {"cpu": "500m"} if has_limits else {}},
                   "env_count": 3, "volume_mounts": 0}]
    return ServiceNode(
        name=name, namespace="default", kind="Deployment",
        replicas_desired=replicas, replicas_ready=ready,
        containers=containers, labels={"app": name},
        selectors={"app": name}, hpa_enabled=hpa,
    )


def _make_experiment(kind: str, target: str, severity: str = "medium") -> ExperimentPlan:
    return ExperimentPlan(
        name=f"test-{kind}",
        kind=kind,
        target_workload=target,
        target_namespace="default",
        severity=severity,
        predicted_blast_radius="test blast radius",
        predicted_affected_services=[],
        rationale="test",
        parameters={"duration_seconds": 60},
    )


class TestBlastPredictor:

    def test_safe_pod_kill_multi_replica(self):
        topology = _make_topology([_make_workload("api", replicas=3, ready=3)])
        predictor = BlastPredictor(topology)
        exp = _make_experiment("pod_kill", "api")
        result = predictor.predict(exp)
        assert result.safe_to_proceed is True
        assert result.risk_level in ("low", "medium", "high")

    def test_aborts_pod_kill_single_replica(self):
        topology = _make_topology([_make_workload("api", replicas=1, ready=1)])
        predictor = BlastPredictor(topology)
        exp = _make_experiment("pod_kill", "api")
        result = predictor.predict(exp)
        assert result.safe_to_proceed is False
        assert "1 replica" in result.abort_reason

    def test_aborts_on_degraded_workload(self):
        topology = _make_topology([_make_workload("api", replicas=3, ready=1)])
        predictor = BlastPredictor(topology)
        exp = _make_experiment("pod_kill", "api")
        result = predictor.predict(exp)
        assert result.safe_to_proceed is False
        assert "degraded" in result.abort_reason.lower()

    def test_warns_no_hpa(self):
        topology = _make_topology([_make_workload("api", hpa=False)])
        predictor = BlastPredictor(topology)
        exp = _make_experiment("cpu_stress", "api")
        result = predictor.predict(exp)
        assert result.safe_to_proceed is True
        assert any("HPA" in w for w in result.warnings)

    def test_warns_no_resource_limits(self):
        topology = _make_topology([_make_workload("api", has_limits=False)])
        predictor = BlastPredictor(topology)
        exp = _make_experiment("cpu_stress", "api")
        result = predictor.predict(exp)
        assert any("resource limits" in w.lower() for w in result.warnings)

    def test_aborts_unknown_workload(self):
        topology = _make_topology([_make_workload("api")])
        predictor = BlastPredictor(topology)
        exp = _make_experiment("pod_kill", "nonexistent-service")
        result = predictor.predict(exp)
        assert result.safe_to_proceed is False
        assert "not found" in result.abort_reason.lower()

    def test_high_severity_raises_risk_level(self):
        topology = _make_topology([_make_workload("api", hpa=False)])
        predictor = BlastPredictor(topology)
        exp = _make_experiment("network_partition", "api", severity="high")
        result = predictor.predict(exp)
        assert result.risk_level in ("high", "critical")
