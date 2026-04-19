"""
analyzer/ai_planner.py
Sends cluster topology to Claude or OpenAI and gets back a ranked,
structured chaos experiment plan with predicted blast radius per experiment.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from typing import Literal
import anthropic
import openai
from config_loader import load_config, get_ai_provider, get_anthropic_key, get_openai_key
from analyzer.cluster_scanner import ClusterTopology


@dataclass
class ExperimentPlan:
    name: str
    kind: str                # pod_kill | network_latency | network_partition | cpu_stress | memory_stress | node_drain
    target_workload: str
    target_namespace: str
    severity: Literal["low", "medium", "high"]
    predicted_blast_radius: str
    predicted_affected_services: list[str]
    rationale: str
    parameters: dict = field(default_factory=dict)
    estimated_recovery_seconds: int = 60


@dataclass
class ChaosPlan:
    summary: str
    cluster_health_assessment: str
    experiments: list[ExperimentPlan]
    overall_risk: Literal["low", "medium", "high"]
    recommendations: list[str]
    ai_provider: str = "anthropic"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


SYSTEM_PROMPT = """
You are an expert Site Reliability Engineer and Chaos Engineering specialist.
You analyze Kubernetes cluster topology and design targeted chaos experiments
to test resilience. You always reason about blast radius before recommending an experiment.

You must respond ONLY with valid JSON — no markdown, no explanation outside the JSON.
""".strip()


def _build_user_prompt(topology: ClusterTopology, enabled_experiments: list[str]) -> str:
    return f"""
Analyze this Kubernetes cluster and design a chaos experiment plan.

CLUSTER TOPOLOGY:
{topology.to_json()}

ENABLED EXPERIMENT TYPES: {json.dumps(enabled_experiments)}

Return a JSON object with this exact schema:
{{
  "summary": "2-3 sentence summary of cluster resilience posture",
  "cluster_health_assessment": "detailed assessment of risks found",
  "overall_risk": "low|medium|high",
  "recommendations": ["list of general resilience recommendations"],
  "experiments": [
    {{
      "name": "descriptive experiment name",
      "kind": "pod_kill|network_latency|network_partition|cpu_stress|memory_stress|node_drain",
      "target_workload": "workload name from the topology",
      "target_namespace": "namespace",
      "severity": "low|medium|high",
      "predicted_blast_radius": "description of what could break",
      "predicted_affected_services": ["list of service names likely impacted"],
      "rationale": "why this experiment is valuable for this specific cluster",
      "parameters": {{
        "duration_seconds": 120,
        "kill_count": 1
      }},
      "estimated_recovery_seconds": 60
    }}
  ]
}}

Rules:
- Rank experiments from highest learning value to lowest
- For pod_kill: include kill_count in parameters
- For network_latency: include latency_ms and jitter_ms in parameters
- For network_partition: include direction (ingress|egress|both) in parameters
- For cpu_stress: include cpu_load_percent and workers in parameters
- For memory_stress: include memory_mb in parameters
- For node_drain: include eviction_timeout_seconds in parameters
- Prioritise single-replica workloads and workloads without HPA as high-value targets
- Do NOT target kube-system workloads unless they are explicitly in the topology
- Generate 3-6 experiments maximum
""".strip()


class AIPlanner:
    def __init__(self, config_path: str = "config.yaml"):
        self.cfg = load_config(config_path)
        self.provider = get_ai_provider()
        self.enabled_experiments = self.cfg["chaos"]["experiments_enabled"]

        if self.provider == "anthropic":
            self._client = anthropic.Anthropic(api_key=get_anthropic_key())
            self._model = self.cfg["ai"]["anthropic_model"]
        else:
            self._client = openai.OpenAI(api_key=get_openai_key())
            self._model = self.cfg["ai"]["openai_model"]

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def plan(self, topology: ClusterTopology) -> ChaosPlan:
        prompt = _build_user_prompt(topology, self.enabled_experiments)
        raw = self._call_ai(prompt)
        data = self._parse_json(raw)
        return self._build_plan(data)

    # ------------------------------------------------------------------
    # AI calls
    # ------------------------------------------------------------------

    def _call_ai(self, prompt: str) -> str:
        if self.provider == "anthropic":
            return self._call_anthropic(prompt)
        return self._call_openai(prompt)

    def _call_anthropic(self, prompt: str) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=self.cfg["ai"]["max_tokens"],
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    def _call_openai(self, prompt: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self.cfg["ai"]["max_tokens"],
            temperature=self.cfg["ai"]["temperature"],
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: str) -> dict:
        text = raw.strip()
        # Strip markdown fences if model added them
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return json.loads(text)

    def _build_plan(self, data: dict) -> ChaosPlan:
        experiments = []
        for e in data.get("experiments", []):
            experiments.append(ExperimentPlan(
                name=e["name"],
                kind=e["kind"],
                target_workload=e["target_workload"],
                target_namespace=e["target_namespace"],
                severity=e.get("severity", "medium"),
                predicted_blast_radius=e.get("predicted_blast_radius", "unknown"),
                predicted_affected_services=e.get("predicted_affected_services", []),
                rationale=e.get("rationale", ""),
                parameters=e.get("parameters", {}),
                estimated_recovery_seconds=e.get("estimated_recovery_seconds", 60),
            ))

        return ChaosPlan(
            summary=data.get("summary", ""),
            cluster_health_assessment=data.get("cluster_health_assessment", ""),
            experiments=experiments,
            overall_risk=data.get("overall_risk", "medium"),
            recommendations=data.get("recommendations", []),
            ai_provider=self.provider,
        )
