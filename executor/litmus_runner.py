"""
executor/litmus_runner.py
Applies LitmusChaos ChaosEngine CRDs for pod-kill and node-drain experiments.
Polls until the experiment completes or times out, then cleans up.
"""
from __future__ import annotations
import time
import yaml
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException
from config_loader import load_config
from analyzer.ai_planner import ExperimentPlan


LITMUS_GROUP = "litmuschaos.io"
LITMUS_VERSION = "v1alpha1"
CHAOS_ENGINE = "chaosengines"
CHAOS_RESULT = "chaosresults"


class LitmusRunner:
    def __init__(self, config_path: str = "config.yaml"):
        cfg = load_config(config_path)
        self.dry_run = cfg["chaos"]["dry_run"]
        self.max_duration = cfg["chaos"]["max_duration_seconds"]
        self.auto_rollback = cfg["chaos"]["auto_rollback"]

        try:
            k8s_config.load_kube_config()
        except Exception:
            k8s_config.load_incluster_config()

        self.custom = client.CustomObjectsApi()
        self.core = client.CoreV1Api()

    # ------------------------------------------------------------------
    # Public dispatch
    # ------------------------------------------------------------------

    def run(self, experiment: ExperimentPlan) -> dict:
        if experiment.kind == "pod_kill":
            return self._run_pod_kill(experiment)
        if experiment.kind == "node_drain":
            return self._run_node_drain(experiment)
        raise ValueError(f"LitmusRunner does not handle experiment kind: {experiment.kind}")

    # ------------------------------------------------------------------
    # Pod Kill
    # ------------------------------------------------------------------

    def _run_pod_kill(self, exp: ExperimentPlan) -> dict:
        kill_count = exp.parameters.get("kill_count", 1)
        duration = min(exp.parameters.get("duration_seconds", 120), self.max_duration)
        engine_name = f"chaos-pod-kill-{exp.target_workload[:20]}"

        engine_manifest = {
            "apiVersion": f"{LITMUS_GROUP}/{LITMUS_VERSION}",
            "kind": "ChaosEngine",
            "metadata": {
                "name": engine_name,
                "namespace": exp.target_namespace,
            },
            "spec": {
                "appinfo": {
                    "appns": exp.target_namespace,
                    "applabel": f"app={exp.target_workload}",
                    "appkind": "deployment",
                },
                "chaosServiceAccount": "litmus-admin",
                "experiments": [
                    {
                        "name": "pod-delete",
                        "spec": {
                            "components": {
                                "env": [
                                    {"name": "TOTAL_CHAOS_DURATION", "value": str(duration)},
                                    {"name": "CHAOS_INTERVAL", "value": "10"},
                                    {"name": "FORCE", "value": "false"},
                                    {"name": "PODS_AFFECTED_PERC", "value": str(min(kill_count * 33, 100))},
                                ]
                            }
                        },
                    }
                ],
            },
        }

        return self._apply_and_wait(engine_manifest, engine_name, exp.target_namespace, duration)

    # ------------------------------------------------------------------
    # Node Drain
    # ------------------------------------------------------------------

    def _run_node_drain(self, exp: ExperimentPlan) -> dict:
        duration = min(exp.parameters.get("duration_seconds", 180), self.max_duration)
        timeout = exp.parameters.get("eviction_timeout_seconds", 30)
        engine_name = f"chaos-node-drain-{exp.target_workload[:15]}"

        engine_manifest = {
            "apiVersion": f"{LITMUS_GROUP}/{LITMUS_VERSION}",
            "kind": "ChaosEngine",
            "metadata": {
                "name": engine_name,
                "namespace": exp.target_namespace,
            },
            "spec": {
                "appinfo": {
                    "appns": exp.target_namespace,
                    "applabel": f"app={exp.target_workload}",
                    "appkind": "deployment",
                },
                "chaosServiceAccount": "litmus-admin",
                "experiments": [
                    {
                        "name": "node-drain",
                        "spec": {
                            "components": {
                                "env": [
                                    {"name": "TOTAL_CHAOS_DURATION", "value": str(duration)},
                                    {"name": "EVICTION_TIMEOUT", "value": str(timeout)},
                                ]
                            }
                        },
                    }
                ],
            },
        }

        return self._apply_and_wait(engine_manifest, engine_name, exp.target_namespace, duration)

    # ------------------------------------------------------------------
    # Apply CRD + poll result
    # ------------------------------------------------------------------

    def _apply_and_wait(
        self, manifest: dict, engine_name: str, namespace: str, duration_seconds: int
    ) -> dict:
        if self.dry_run:
            return {
                "status": "dry_run",
                "engine": engine_name,
                "manifest": yaml.dump(manifest),
            }

        # Create ChaosEngine
        try:
            self.custom.create_namespaced_custom_object(
                group=LITMUS_GROUP,
                version=LITMUS_VERSION,
                namespace=namespace,
                plural=CHAOS_ENGINE,
                body=manifest,
            )
        except ApiException as e:
            if e.status == 409:  # already exists — patch it
                self.custom.patch_namespaced_custom_object(
                    group=LITMUS_GROUP,
                    version=LITMUS_VERSION,
                    namespace=namespace,
                    plural=CHAOS_ENGINE,
                    name=engine_name,
                    body=manifest,
                )
            else:
                raise

        # Poll for completion
        deadline = time.time() + duration_seconds + 60
        result_name = f"{engine_name}-pod-delete"

        while time.time() < deadline:
            time.sleep(10)
            try:
                result = self.custom.get_namespaced_custom_object(
                    group=LITMUS_GROUP,
                    version=LITMUS_VERSION,
                    namespace=namespace,
                    plural=CHAOS_RESULT,
                    name=result_name,
                )
                verdict = (
                    result.get("status", {})
                    .get("experimentStatus", {})
                    .get("verdict", "Awaited")
                )
                if verdict != "Awaited":
                    self._cleanup(engine_name, namespace)
                    return {
                        "status": "completed",
                        "verdict": verdict,
                        "engine": engine_name,
                        "raw_result": result.get("status", {}),
                    }
            except ApiException:
                pass  # result not created yet

        # Timeout — cleanup
        self._cleanup(engine_name, namespace)
        return {"status": "timeout", "engine": engine_name}

    def _cleanup(self, engine_name: str, namespace: str) -> None:
        try:
            self.custom.delete_namespaced_custom_object(
                group=LITMUS_GROUP,
                version=LITMUS_VERSION,
                namespace=namespace,
                plural=CHAOS_ENGINE,
                name=engine_name,
            )
        except ApiException:
            pass
