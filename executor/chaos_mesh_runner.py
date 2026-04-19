"""
executor/chaos_mesh_runner.py
Uses Chaos Mesh CRDs for network latency, network partition,
CPU stress, and memory stress experiments.
"""
from __future__ import annotations
import time
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException
from config_loader import load_config
from analyzer.ai_planner import ExperimentPlan


CHAOS_MESH_GROUP = "chaos-mesh.org"
CHAOS_MESH_VERSION = "v1alpha1"


class ChaosMeshRunner:
    def __init__(self, config_path: str = "config.yaml"):
        cfg = load_config(config_path)
        self.dry_run = cfg["chaos"]["dry_run"]
        self.max_duration = cfg["chaos"]["max_duration_seconds"]

        try:
            k8s_config.load_kube_config()
        except Exception:
            k8s_config.load_incluster_config()

        self.custom = client.CustomObjectsApi()

    # ------------------------------------------------------------------
    # Public dispatch
    # ------------------------------------------------------------------

    def run(self, experiment: ExperimentPlan) -> dict:
        dispatch = {
            "network_latency":   self._run_network_latency,
            "network_partition": self._run_network_partition,
            "cpu_stress":        self._run_cpu_stress,
            "memory_stress":     self._run_memory_stress,
        }
        handler = dispatch.get(experiment.kind)
        if handler is None:
            raise ValueError(f"ChaosMeshRunner does not handle experiment kind: {experiment.kind}")
        return handler(experiment)

    # ------------------------------------------------------------------
    # Network latency
    # ------------------------------------------------------------------

    def _run_network_latency(self, exp: ExperimentPlan) -> dict:
        latency_ms = exp.parameters.get("latency_ms", 200)
        jitter_ms  = exp.parameters.get("jitter_ms", 50)
        duration   = self._duration_str(exp)
        name       = f"net-latency-{exp.target_workload[:20]}"

        manifest = {
            "apiVersion": f"{CHAOS_MESH_GROUP}/{CHAOS_MESH_VERSION}",
            "kind": "NetworkChaos",
            "metadata": {"name": name, "namespace": exp.target_namespace},
            "spec": {
                "action": "delay",
                "mode": "all",
                "selector": {
                    "namespaces": [exp.target_namespace],
                    "labelSelectors": {"app": exp.target_workload},
                },
                "delay": {
                    "latency": f"{latency_ms}ms",
                    "jitter": f"{jitter_ms}ms",
                    "correlation": "25",
                },
                "duration": duration,
            },
        }
        return self._apply(manifest, name, exp.target_namespace, "NetworkChaos")

    # ------------------------------------------------------------------
    # Network partition
    # ------------------------------------------------------------------

    def _run_network_partition(self, exp: ExperimentPlan) -> dict:
        direction = exp.parameters.get("direction", "both")
        duration  = self._duration_str(exp)
        name      = f"net-partition-{exp.target_workload[:18]}"

        manifest = {
            "apiVersion": f"{CHAOS_MESH_GROUP}/{CHAOS_MESH_VERSION}",
            "kind": "NetworkChaos",
            "metadata": {"name": name, "namespace": exp.target_namespace},
            "spec": {
                "action": "partition",
                "mode": "all",
                "selector": {
                    "namespaces": [exp.target_namespace],
                    "labelSelectors": {"app": exp.target_workload},
                },
                "direction": direction,
                "duration": duration,
            },
        }
        return self._apply(manifest, name, exp.target_namespace, "NetworkChaos")

    # ------------------------------------------------------------------
    # CPU stress
    # ------------------------------------------------------------------

    def _run_cpu_stress(self, exp: ExperimentPlan) -> dict:
        cpu_load = exp.parameters.get("cpu_load_percent", 80)
        workers  = exp.parameters.get("workers", 2)
        duration = self._duration_str(exp)
        name     = f"cpu-stress-{exp.target_workload[:20]}"

        manifest = {
            "apiVersion": f"{CHAOS_MESH_GROUP}/{CHAOS_MESH_VERSION}",
            "kind": "StressChaos",
            "metadata": {"name": name, "namespace": exp.target_namespace},
            "spec": {
                "mode": "all",
                "selector": {
                    "namespaces": [exp.target_namespace],
                    "labelSelectors": {"app": exp.target_workload},
                },
                "stressors": {
                    "cpu": {"workers": workers, "load": cpu_load},
                },
                "duration": duration,
            },
        }
        return self._apply(manifest, name, exp.target_namespace, "StressChaos")

    # ------------------------------------------------------------------
    # Memory stress
    # ------------------------------------------------------------------

    def _run_memory_stress(self, exp: ExperimentPlan) -> dict:
        memory_mb = exp.parameters.get("memory_mb", 512)
        duration  = self._duration_str(exp)
        name      = f"mem-stress-{exp.target_workload[:20]}"

        manifest = {
            "apiVersion": f"{CHAOS_MESH_GROUP}/{CHAOS_MESH_VERSION}",
            "kind": "StressChaos",
            "metadata": {"name": name, "namespace": exp.target_namespace},
            "spec": {
                "mode": "all",
                "selector": {
                    "namespaces": [exp.target_namespace],
                    "labelSelectors": {"app": exp.target_workload},
                },
                "stressors": {
                    "memory": {"workers": 1, "size": f"{memory_mb}MB"},
                },
                "duration": duration,
            },
        }
        return self._apply(manifest, name, exp.target_namespace, "StressChaos")

    # ------------------------------------------------------------------
    # Shared apply + wait + cleanup
    # ------------------------------------------------------------------

    def _apply(self, manifest: dict, name: str, namespace: str, kind: str) -> dict:
        plural = self._kind_to_plural(kind)

        if self.dry_run:
            import yaml
            return {"status": "dry_run", "name": name, "manifest": yaml.dump(manifest)}

        try:
            self.custom.create_namespaced_custom_object(
                group=CHAOS_MESH_GROUP,
                version=CHAOS_MESH_VERSION,
                namespace=namespace,
                plural=plural,
                body=manifest,
            )
        except ApiException as e:
            if e.status == 409:
                self.custom.delete_namespaced_custom_object(
                    group=CHAOS_MESH_GROUP,
                    version=CHAOS_MESH_VERSION,
                    namespace=namespace,
                    plural=plural,
                    name=name,
                )
                time.sleep(3)
                self.custom.create_namespaced_custom_object(
                    group=CHAOS_MESH_GROUP,
                    version=CHAOS_MESH_VERSION,
                    namespace=namespace,
                    plural=plural,
                    body=manifest,
                )
            else:
                raise

        # Wait for duration + buffer
        duration_s = manifest["spec"].get("duration", "120s")
        wait_s = int(duration_s.replace("s", "")) + 15
        time.sleep(wait_s)

        # Cleanup
        try:
            self.custom.delete_namespaced_custom_object(
                group=CHAOS_MESH_GROUP,
                version=CHAOS_MESH_VERSION,
                namespace=namespace,
                plural=plural,
                name=name,
            )
        except ApiException:
            pass

        return {"status": "completed", "name": name, "kind": kind}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _duration_str(self, exp: ExperimentPlan) -> str:
        d = min(exp.parameters.get("duration_seconds", 120), self.max_duration)
        return f"{d}s"

    @staticmethod
    def _kind_to_plural(kind: str) -> str:
        return {"NetworkChaos": "networkchaos", "StressChaos": "stresschaos"}.get(kind, kind.lower())
