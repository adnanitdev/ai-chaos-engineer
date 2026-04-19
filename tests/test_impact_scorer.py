"""
tests/test_impact_scorer.py
Unit tests for impact scoring logic — mocks Prometheus.
"""
import time
import pytest
from unittest.mock import patch, MagicMock
from observer.impact_scorer import ImpactScorer, MetricDelta


BASELINE = {
    "timestamp": time.time(),
    "error_rate_pct": 0.5,
    "latency_p99_ms": 120.0,
    "pod_restarts": 0.0,
    "cpu_millicores": 200.0,
    "memory_mb": 256.0,
}

AFTER_HEALTHY = {
    "timestamp": time.time(),
    "error_rate_pct": 0.6,
    "latency_p99_ms": 130.0,
    "pod_restarts": 0.0,
    "cpu_millicores": 210.0,
    "memory_mb": 260.0,
}

AFTER_DEGRADED = {
    "timestamp": time.time(),
    "error_rate_pct": 3.5,
    "latency_p99_ms": 950.0,
    "pod_restarts": 1.0,
    "cpu_millicores": 800.0,
    "memory_mb": 700.0,
}

AFTER_FAILED = {
    "timestamp": time.time(),
    "error_rate_pct": 12.0,
    "latency_p99_ms": 5000.0,
    "pod_restarts": 5.0,
    "cpu_millicores": 1000.0,
    "memory_mb": 900.0,
}


def _make_exp():
    from analyzer.ai_planner import ExperimentPlan
    return ExperimentPlan(
        name="test-pod-kill",
        kind="pod_kill",
        target_workload="api",
        target_namespace="default",
        severity="medium",
        predicted_blast_radius="minor",
        predicted_affected_services=[],
        rationale="test",
        parameters={"duration_seconds": 60},
    )


@patch("observer.impact_scorer.time.sleep")
@patch("observer.impact_scorer.PrometheusClient")
class TestImpactScorer:

    def _scorer(self, MockProm, snap):
        mock_prom = MagicMock()
        mock_prom.snapshot.return_value = snap
        MockProm.return_value = mock_prom
        return ImpactScorer()

    def test_pass_verdict_healthy_after(self, MockProm, mock_sleep):
        scorer = self._scorer(MockProm, AFTER_HEALTHY)
        exp = _make_exp()
        report = scorer.score(exp, BASELINE, time.time() - 60, time.time())
        assert report.verdict == "pass"
        assert report.resilience_score >= 80

    def test_degraded_verdict(self, MockProm, mock_sleep):
        scorer = self._scorer(MockProm, AFTER_DEGRADED)
        exp = _make_exp()
        report = scorer.score(exp, BASELINE, time.time() - 60, time.time())
        assert report.verdict in ("degraded", "failed")
        assert report.resilience_score < 80

    def test_failed_verdict_slo_breach(self, MockProm, mock_sleep):
        scorer = self._scorer(MockProm, AFTER_FAILED)
        exp = _make_exp()
        report = scorer.score(exp, BASELINE, time.time() - 60, time.time(),
                              slo_breach_reason="error rate 12% > 5%")
        assert report.verdict == "failed"
        assert report.slo_breached is True
        assert report.resilience_score <= 30

    def test_deltas_computed_correctly(self, MockProm, mock_sleep):
        scorer = self._scorer(MockProm, AFTER_HEALTHY)
        exp = _make_exp()
        report = scorer.score(exp, BASELINE, time.time() - 60, time.time())
        err_delta = next(d for d in report.deltas if d.metric == "error_rate_pct")
        assert err_delta.before == pytest.approx(0.5, abs=0.01)
        assert err_delta.after == pytest.approx(0.6, abs=0.01)
        assert err_delta.delta == pytest.approx(0.1, abs=0.01)

    def test_score_100_when_no_degradation(self, MockProm, mock_sleep):
        # After identical to before — no degradation
        scorer = self._scorer(MockProm, BASELINE)
        exp = _make_exp()
        report = scorer.score(exp, BASELINE, time.time() - 60, time.time())
        assert report.resilience_score == 100
