"""
executor/rollback.py
Auto-rollback and safety circuit breaker.
Watches SLO metrics during an experiment and aborts if thresholds are breached.
"""
from __future__ import annotations
import threading
import time
from config_loader import load_config
from observer.prometheus_client import PrometheusClient


class SLOBreachError(Exception):
    pass


class RollbackWatcher:
    """
    Runs in a background thread during each experiment.
    Calls the abort_fn if any SLO threshold is breached.
    """

    def __init__(self, abort_fn, config_path: str = "config.yaml"):
        cfg = load_config(config_path)
        self.slo = cfg["slo"]
        self.interval = cfg["prometheus"]["scrape_interval_seconds"]
        self.abort_fn = abort_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.breach_reason: str = ""

        try:
            self.prom = PrometheusClient(config_path)
            self.prom_available = True
        except Exception:
            self.prom_available = False

    def start(self, namespace: str, workload: str) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            args=(namespace, workload),
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _watch_loop(self, namespace: str, workload: str) -> None:
        while not self._stop_event.is_set():
            time.sleep(self.interval)
            if not self.prom_available:
                continue
            try:
                self._check_slos(namespace, workload)
            except SLOBreachError as e:
                self.breach_reason = str(e)
                self.abort_fn(str(e))
                return

    def _check_slos(self, namespace: str, workload: str) -> None:
        # Error rate
        err_rate = self.prom.query_error_rate(namespace, workload)
        if err_rate is not None and err_rate > self.slo["error_rate_percent"]:
            raise SLOBreachError(
                f"SLO breach: error rate {err_rate:.1f}% > threshold {self.slo['error_rate_percent']}%"
            )

        # Latency p99
        latency = self.prom.query_latency_p99(namespace, workload)
        if latency is not None and latency > self.slo["latency_p99_ms"]:
            raise SLOBreachError(
                f"SLO breach: p99 latency {latency:.0f}ms > threshold {self.slo['latency_p99_ms']}ms"
            )

        # Pod restarts
        restarts = self.prom.query_pod_restarts(namespace, workload)
        if restarts is not None and restarts > self.slo["pod_restart_limit"]:
            raise SLOBreachError(
                f"SLO breach: {int(restarts)} pod restarts > limit {self.slo['pod_restart_limit']}"
            )
