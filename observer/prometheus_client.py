"""
observer/prometheus_client.py
Uses only metrics guaranteed on EKS with kube-prometheus-stack:
  - container_cpu_usage_seconds_total         (cAdvisor)
  - container_memory_working_set_bytes        (cAdvisor)
  - container_network_transmit_bytes_total    (cAdvisor)
  - kube_pod_container_status_restarts_total  (kube-state-metrics)
  - kube_deployment_status_replicas_available (kube-state-metrics)
  - kube_deployment_spec_replicas             (kube-state-metrics)

NOTE: http_requests_total and http_request_duration_seconds_bucket are NOT
used because httpbin/nginx do not export them. All scoring uses cAdvisor
and kube-state-metrics which are always present on kube-prometheus-stack.
"""
from __future__ import annotations
import time
from typing import Optional
import requests
from config_loader import load_config


class PrometheusClient:
    def __init__(self, config_path: str = "config.yaml"):
        cfg = load_config(config_path)
        self.base_url = cfg["prometheus"]["url"].rstrip("/")
        self.timeout = 10

    # ------------------------------------------------------------------
    # Public metric queries
    # ------------------------------------------------------------------

    def query_error_rate(self, namespace: str, workload: str) -> Optional[float]:
        """
        Restart rate per minute — proxy for error rate.
        Spikes immediately when pods crash/OOMKill during experiments.
        Returns 0.0 (not None) so scoring always has a value to compare.
        """
        q = (
            f'sum(rate(kube_pod_container_status_restarts_total{{'
            f'namespace="{namespace}",pod=~"{workload}-.*"}}[5m])) * 60'
        )
        return self._scalar_or_zero(q)

    def query_latency_p99(self, namespace: str, workload: str) -> Optional[float]:
        """
        CPU saturation in millicores — proxy for latency pressure.
        Spikes during cpu_stress experiments.
        """
        q = (
            f'sum(rate(container_cpu_usage_seconds_total{{'
            f'namespace="{namespace}",pod=~"{workload}-.*",container!=""}}[2m])) * 1000'
        )
        return self._scalar_or_zero(q)

    def query_pod_restarts(self, namespace: str, workload: str) -> Optional[float]:
        """Pod restart count over last 5 minutes. Spikes on pod kill."""
        q = (
            f'sum(increase(kube_pod_container_status_restarts_total{{'
            f'namespace="{namespace}",pod=~"{workload}-.*"}}[5m]))'
        )
        return self._scalar_or_zero(q)

    def query_cpu_usage(self, namespace: str, workload: str) -> Optional[float]:
        """CPU usage in millicores."""
        q = (
            f'sum(rate(container_cpu_usage_seconds_total{{'
            f'namespace="{namespace}",pod=~"{workload}-.*",container!=""}}[2m])) * 1000'
        )
        return self._scalar_or_zero(q)

    def query_memory_usage_mb(self, namespace: str, workload: str) -> Optional[float]:
        """Memory usage in MB."""
        q = (
            f'sum(container_memory_working_set_bytes{{'
            f'namespace="{namespace}",pod=~"{workload}-.*",container!=""}}) / 1024 / 1024'
        )
        return self._scalar_or_zero(q)

    def query_network_bytes_per_sec(self, namespace: str, workload: str) -> Optional[float]:
        """
        Network transmit bytes/sec.
        Drops sharply during network_partition and network_latency experiments.
        """
        q = (
            f'sum(rate(container_network_transmit_bytes_total{{'
            f'namespace="{namespace}",pod=~"{workload}-.*"}}[2m]))'
        )
        return self._scalar_or_zero(q)

    def query_ready_replicas(self, namespace: str, workload: str) -> Optional[float]:
        """Ready replicas. Drops to 0 immediately on pod kill."""
        q = (
            f'kube_deployment_status_replicas_available{{'
            f'namespace="{namespace}",deployment="{workload}"}}'
        )
        return self._scalar_or_zero(q)

    def query_desired_replicas(self, namespace: str, workload: str) -> Optional[float]:
        """Desired replica count from deployment spec."""
        q = (
            f'kube_deployment_spec_replicas{{'
            f'namespace="{namespace}",deployment="{workload}"}}'
        )
        result = self._scalar(q)
        return result if result is not None else 1.0

    def snapshot(self, namespace: str, workload: str) -> dict:
        """Full metrics snapshot for a workload. All values default to 0.0, never None."""
        ready   = self.query_ready_replicas(namespace, workload)
        desired = self.query_desired_replicas(namespace, workload)
        avail_pct = round((ready / desired * 100), 1) if desired and desired > 0 else 100.0

        return {
            "timestamp":         time.time(),
            "error_rate_pct":    self.query_error_rate(namespace, workload),
            "latency_p99_ms":    self.query_latency_p99(namespace, workload),
            "pod_restarts":      self.query_pod_restarts(namespace, workload),
            "cpu_millicores":    self.query_cpu_usage(namespace, workload),
            "memory_mb":         self.query_memory_usage_mb(namespace, workload),
            "network_bytes_sec": self.query_network_bytes_per_sec(namespace, workload),
            "ready_replicas":    ready,
            "desired_replicas":  desired,
            "availability_pct":  avail_pct,
        }

    def range_query(self, promql: str, start: float, end: float, step: str = "15s") -> list[dict]:
        """Raw range query — returns list of {timestamp, value} dicts."""
        resp = self._get("/api/v1/query_range", {
            "query": promql, "start": start, "end": end, "step": step,
        })
        results = []
        for series in resp.get("data", {}).get("result", []):
            for ts, val in series.get("values", []):
                try:
                    results.append({"timestamp": float(ts), "value": float(val)})
                except (ValueError, TypeError):
                    pass
        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _scalar(self, query: str) -> Optional[float]:
        resp = self._get("/api/v1/query", {"query": query})
        results = resp.get("data", {}).get("result", [])
        if not results:
            return None
        try:
            return float(results[0]["value"][1])
        except (IndexError, KeyError, ValueError, TypeError):
            return None

    def _scalar_or_zero(self, query: str) -> float:
        """Like _scalar but returns 0.0 instead of None — ensures scoring always has data."""
        result = self._scalar(query)
        return result if result is not None else 0.0

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = requests.get(self.base_url + path, params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}