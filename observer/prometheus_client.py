"""
observer/prometheus_client.py
Pulls live metrics from Prometheus before, during, and after each experiment.
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
        """Returns HTTP 5xx error rate as a percentage over the last 2 minutes."""
        q = (
            f'sum(rate(http_requests_total{{namespace="{namespace}",'
            f'pod=~"{workload}-.*",status=~"5.."}}[2m])) / '
            f'sum(rate(http_requests_total{{namespace="{namespace}",'
            f'pod=~"{workload}-.*"}}[2m])) * 100'
        )
        return self._scalar(q)

    def query_latency_p99(self, namespace: str, workload: str) -> Optional[float]:
        """Returns p99 latency in milliseconds over the last 2 minutes."""
        q = (
            f'histogram_quantile(0.99, sum(rate('
            f'http_request_duration_seconds_bucket{{namespace="{namespace}",'
            f'pod=~"{workload}-.*"}}[2m])) by (le)) * 1000'
        )
        return self._scalar(q)

    def query_pod_restarts(self, namespace: str, workload: str) -> Optional[float]:
        """Returns total pod restart count in the last 5 minutes."""
        q = (
            f'sum(increase(kube_pod_container_status_restarts_total{{'
            f'namespace="{namespace}",pod=~"{workload}-.*"}}[5m]))'
        )
        return self._scalar(q)

    def query_cpu_usage(self, namespace: str, workload: str) -> Optional[float]:
        """Returns CPU usage in millicores."""
        q = (
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",'
            f'pod=~"{workload}-.*"}}[2m])) * 1000'
        )
        return self._scalar(q)

    def query_memory_usage_mb(self, namespace: str, workload: str) -> Optional[float]:
        """Returns memory usage in MB."""
        q = (
            f'sum(container_memory_working_set_bytes{{namespace="{namespace}",'
            f'pod=~"{workload}-.*"}}) / 1024 / 1024'
        )
        return self._scalar(q)

    def snapshot(self, namespace: str, workload: str) -> dict:
        """Returns a full metrics snapshot for a workload."""
        return {
            "timestamp": time.time(),
            "error_rate_pct": self.query_error_rate(namespace, workload),
            "latency_p99_ms": self.query_latency_p99(namespace, workload),
            "pod_restarts": self.query_pod_restarts(namespace, workload),
            "cpu_millicores": self.query_cpu_usage(namespace, workload),
            "memory_mb": self.query_memory_usage_mb(namespace, workload),
        }

    def range_query(self, promql: str, start: float, end: float, step: str = "15s") -> list[dict]:
        """Raw range query — returns list of {timestamp, value} dicts."""
        resp = self._get("/api/v1/query_range", {
            "query": promql,
            "start": start,
            "end": end,
            "step": step,
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

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = requests.get(
                self.base_url + path,
                params=params,
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}
