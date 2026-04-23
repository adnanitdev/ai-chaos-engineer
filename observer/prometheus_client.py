"""
observer/prometheus_client.py
Uses metrics that actually capture chaos on fast-recovering EKS clusters.
Key insight: pods recover in <10s so availability never drops in Prometheus.
Instead we measure:
  - pod churn rate (new pod creations during experiment window)
  - CPU spike during stress
  - network drop during partition
  - kube_pod_status_phase transitions
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

    def query_pod_churn(self, namespace: str, workload: str) -> float:
        """
        Counts how many times pods were recreated in last 10 minutes.
        Each pod kill creates a new pod with a new name — this counter goes up.
        Uses kube_pod_created timestamp to detect new pod appearances.
        """
        q = (
            f'count(time() - kube_pod_created{{namespace="{namespace}",'
            f'pod=~"{workload}-.*"}} < 600)'
        )
        result = self._scalar(q)
        return result if result is not None else 0.0

    def query_pod_restarts(self, namespace: str, workload: str) -> float:
        q = (
            f'sum(kube_pod_container_status_restarts_total{{'
            f'namespace="{namespace}",pod=~"{workload}-.*"}})'
        )
        result = self._scalar(q)
        return result if result is not None else 0.0

    def query_cpu_usage(self, namespace: str, workload: str) -> float:
        q = (
            f'sum(rate(container_cpu_usage_seconds_total{{'
            f'namespace="{namespace}",pod=~"{workload}-.*",container!=""}}[2m])) * 1000'
        )
        result = self._scalar(q)
        return result if result is not None else 0.0

    def query_memory_usage_mb(self, namespace: str, workload: str) -> float:
        q = (
            f'sum(container_memory_working_set_bytes{{'
            f'namespace="{namespace}",pod=~"{workload}-.*",container!=""}}) / 1024 / 1024'
        )
        result = self._scalar(q)
        return result if result is not None else 0.0

    def query_network_bytes_per_sec(self, namespace: str, workload: str) -> float:
        q = (
            f'sum(rate(container_network_transmit_bytes_total{{'
            f'namespace="{namespace}",pod=~"{workload}-.*"}}[2m]))'
        )
        result = self._scalar(q)
        return result if result is not None else 0.0

    def query_ready_replicas(self, namespace: str, workload: str) -> float:
        q = (
            f'kube_deployment_status_replicas_available{{'
            f'namespace="{namespace}",deployment="{workload}"}}'
        )
        result = self._scalar(q)
        return result if result is not None else 0.0

    def query_desired_replicas(self, namespace: str, workload: str) -> float:
        q = (
            f'kube_deployment_spec_replicas{{'
            f'namespace="{namespace}",deployment="{workload}"}}'
        )
        result = self._scalar(q)
        return result if result is not None else 1.0

    def query_pods_not_running(self, namespace: str, workload: str) -> float:
        """
        Counts pods NOT in Running phase — catches the brief window when
        pods are Pending/ContainerCreating after being killed.
        """
        q = (
            f'count(kube_pod_status_phase{{namespace="{namespace}",'
            f'pod=~"{workload}-.*",phase!="Running"}} == 1) or vector(0)'
        )
        result = self._scalar(q)
        return result if result is not None else 0.0

    def query_error_rate(self, namespace: str, workload: str) -> float:
        # proxy: restart rate per minute
        q = (
            f'sum(rate(kube_pod_container_status_restarts_total{{'
            f'namespace="{namespace}",pod=~"{workload}-.*"}}[5m])) * 60'
        )
        result = self._scalar(q)
        return result if result is not None else 0.0

    def query_latency_p99(self, namespace: str, workload: str) -> float:
        # proxy: CPU saturation
        return self.query_cpu_usage(namespace, workload)

    def snapshot(self, namespace: str, workload: str) -> dict:
        ready   = self.query_ready_replicas(namespace, workload)
        desired = self.query_desired_replicas(namespace, workload)
        avail_pct = round((ready / desired * 100), 1) if desired > 0 else 100.0

        return {
            "timestamp":          time.time(),
            "error_rate_pct":     self.query_error_rate(namespace, workload),
            "latency_p99_ms":     self.query_latency_p99(namespace, workload),
            "pod_restarts":       self.query_pod_restarts(namespace, workload),
            "pod_churn":          self.query_pod_churn(namespace, workload),
            "pods_not_running":   self.query_pods_not_running(namespace, workload),
            "cpu_millicores":     self.query_cpu_usage(namespace, workload),
            "memory_mb":          self.query_memory_usage_mb(namespace, workload),
            "network_bytes_sec":  self.query_network_bytes_per_sec(namespace, workload),
            "ready_replicas":     ready,
            "desired_replicas":   desired,
            "availability_pct":   avail_pct,
        }

    def range_query(self, promql: str, start: float, end: float, step: str = "15s") -> list[dict]:
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
            r = requests.get(self.base_url + path, params=params, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}
