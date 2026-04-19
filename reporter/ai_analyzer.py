"""
reporter/ai_analyzer.py
Feeds all experiment impact reports back to the AI and gets a final
resilience report with prioritised fix recommendations.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
import anthropic
import openai
from config_loader import load_config, get_ai_provider, get_anthropic_key, get_openai_key
from analyzer.ai_planner import ChaosPlan
from observer.impact_scorer import ImpactReport


@dataclass
class ResilienceReport:
    overall_resilience_score: int           # 0-100
    overall_verdict: str                    # "resilient" | "needs_work" | "fragile"
    executive_summary: str
    strengths: list[str]
    critical_findings: list[str]
    recommendations: list[dict]             # [{priority, action, impact, effort}]
    experiment_summaries: list[dict]
    ai_provider: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


SYSTEM_PROMPT = """
You are a principal SRE analyzing chaos experiment results to produce
an executive resilience report for an engineering team.
You respond ONLY with valid JSON — no markdown, no preamble.
""".strip()


def _build_prompt(plan: ChaosPlan, reports: list[ImpactReport]) -> str:
    reports_data = [r.to_dict() for r in reports]
    return f"""
Analyze these chaos experiment results and produce a resilience report.

ORIGINAL PLAN:
{plan.to_json()}

EXPERIMENT RESULTS:
{json.dumps(reports_data, indent=2, default=str)}

Return JSON with this exact schema:
{{
  "overall_resilience_score": 0-100,
  "overall_verdict": "resilient|needs_work|fragile",
  "executive_summary": "3-4 sentences suitable for engineering management",
  "strengths": ["what the system did well"],
  "critical_findings": ["what failed or degraded significantly"],
  "recommendations": [
    {{
      "priority": "P1|P2|P3",
      "action": "specific actionable fix",
      "impact": "what this fixes",
      "effort": "low|medium|high"
    }}
  ],
  "experiment_summaries": [
    {{
      "experiment_name": "...",
      "verdict": "pass|degraded|failed",
      "resilience_score": 0-100,
      "key_finding": "one sentence",
      "fix": "specific fix if failed"
    }}
  ]
}}

Rules:
- overall_resilience_score = weighted average of individual experiment scores
- Prioritise P1 recommendations by business impact
- Be specific — name exact workloads, metrics, and thresholds
- Strengths are just as important as findings
""".strip()


class AIAnalyzer:
    def __init__(self, config_path: str = "config.yaml"):
        self.cfg = load_config(config_path)
        self.provider = get_ai_provider()

        if self.provider == "anthropic":
            self._client = anthropic.Anthropic(api_key=get_anthropic_key())
            self._model = self.cfg["ai"]["anthropic_model"]
        else:
            self._client = openai.OpenAI(api_key=get_openai_key())
            self._model = self.cfg["ai"]["openai_model"]

    def analyze(self, plan: ChaosPlan, reports: list[ImpactReport]) -> ResilienceReport:
        prompt = _build_prompt(plan, reports)
        raw = self._call_ai(prompt)
        data = self._parse_json(raw)
        return ResilienceReport(
            overall_resilience_score=data.get("overall_resilience_score", 50),
            overall_verdict=data.get("overall_verdict", "needs_work"),
            executive_summary=data.get("executive_summary", ""),
            strengths=data.get("strengths", []),
            critical_findings=data.get("critical_findings", []),
            recommendations=data.get("recommendations", []),
            experiment_summaries=data.get("experiment_summaries", []),
            ai_provider=self.provider,
        )

    def _call_ai(self, prompt: str) -> str:
        if self.provider == "anthropic":
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=self.cfg["ai"]["max_tokens"],
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        else:
            resp = self._client.chat.completions.create(
                model=self._model,
                max_tokens=self.cfg["ai"]["max_tokens"],
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content

    @staticmethod
    def _parse_json(raw: str) -> dict:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return json.loads(text)
