"""
executor/__init__.py
Unified dispatcher — routes experiment to the right runner.
"""
from __future__ import annotations
from analyzer.ai_planner import ExperimentPlan

LITMUS_KINDS = {"pod_kill", "node_drain"}
CHAOS_MESH_KINDS = {"network_latency", "network_partition", "cpu_stress", "memory_stress"}


def run_experiment(experiment: ExperimentPlan, config_path: str = "config.yaml") -> dict:
    if experiment.kind in LITMUS_KINDS:
        from executor.litmus_runner import LitmusRunner
        return LitmusRunner(config_path).run(experiment)
    if experiment.kind in CHAOS_MESH_KINDS:
        from executor.chaos_mesh_runner import ChaosMeshRunner
        return ChaosMeshRunner(config_path).run(experiment)
    raise ValueError(f"Unknown experiment kind: {experiment.kind}")
