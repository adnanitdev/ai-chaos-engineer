"""
reporter/report_generator.py
Renders the final resilience report as Markdown and PDF.
"""
from __future__ import annotations
import os
import time
from pathlib import Path
from jinja2 import Environment, BaseLoader
from analyzer.ai_planner import ChaosPlan
from observer.impact_scorer import ImpactReport
from reporter.ai_analyzer import ResilienceReport

MARKDOWN_TEMPLATE = """
# Resilience Report
**Generated:** {{ timestamp }}  
**Cluster:** {{ cluster_context }}  
**AI Provider:** {{ report.ai_provider }}

---

## Overall Score: {{ report.overall_resilience_score }}/100 — {{ report.overall_verdict | upper }}

{{ report.executive_summary }}

---

## Experiments Run

| Experiment | Target | Verdict | Score |
|---|---|---|---|
{% for s in report.experiment_summaries -%}
| {{ s.experiment_name }} | {{ s.get('target', '—') }} | {{ s.verdict }} | {{ s.resilience_score }}/100 |
{% endfor %}

---

## Findings

### Strengths
{% for s in report.strengths %}
- {{ s }}
{% endfor %}

### Critical Findings
{% for f in report.critical_findings %}
- {{ f }}
{% endfor %}

---

## Recommendations

{% for r in report.recommendations %}
### [{{ r.priority }}] {{ r.action }}
- **Impact:** {{ r.impact }}
- **Effort:** {{ r.effort }}

{% endfor %}

---

## Detailed Experiment Results

{% for exp_report in impact_reports %}
### {{ exp_report.experiment_name }}

**Target:** `{{ exp_report.target_workload }}`  
**Duration:** {{ exp_report.duration_seconds }}s  
**Verdict:** {{ exp_report.verdict }}  
**Score:** {{ exp_report.resilience_score }}/100  
{% if exp_report.slo_breached %}
**⚠ SLO Breach:** {{ exp_report.slo_breach_reason }}
{% endif %}

| Metric | Before | After | Delta |
|---|---|---|---|
{% for d in exp_report.deltas -%}
| {{ d.metric }} | {{ d.before }} | {{ d.after }} | {{ d.delta }}{% if d.breached_threshold %} ⚠{% endif %} |
{% endfor %}

{% endfor %}

---

## Cluster Health Assessment

{{ plan.cluster_health_assessment }}

### General Recommendations
{% for r in plan.recommendations %}
- {{ r }}
{% endfor %}
""".strip()


class ReportGenerator:
    def __init__(self, config_path: str = "config.yaml"):
        from config_loader import load_config
        cfg = load_config(config_path)
        self.output_dir = Path(cfg["reporting"]["output_dir"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.formats = cfg["reporting"]["formats"]

    def generate(
        self,
        plan: ChaosPlan,
        impact_reports: list[ImpactReport],
        resilience_report: ResilienceReport,
        cluster_context: str = "unknown",
    ) -> dict[str, str]:
        timestamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        slug = time.strftime("%Y%m%d-%H%M%S")
        output_paths: dict[str, str] = {}

        env = Environment(loader=BaseLoader())
        template = env.from_string(MARKDOWN_TEMPLATE)
        markdown = template.render(
            timestamp=timestamp,
            cluster_context=cluster_context,
            report=resilience_report,
            plan=plan,
            impact_reports=impact_reports,
        )

        if "markdown" in self.formats:
            md_path = self.output_dir / f"resilience-report-{slug}.md"
            md_path.write_text(markdown)
            output_paths["markdown"] = str(md_path)

        if "pdf" in self.formats:
            pdf_path = self._render_pdf(markdown, slug)
            if pdf_path:
                output_paths["pdf"] = pdf_path

        return output_paths

    def _render_pdf(self, markdown_text: str, slug: str) -> str | None:
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import cm
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
            from reportlab.lib import colors

            pdf_path = str(self.output_dir / f"resilience-report-{slug}.pdf")
            doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                                    leftMargin=2*cm, rightMargin=2*cm,
                                    topMargin=2*cm, bottomMargin=2*cm)
            styles = getSampleStyleSheet()
            story = []

            for line in markdown_text.split("\n"):
                line = line.strip()
                if not line:
                    story.append(Spacer(1, 6))
                elif line.startswith("# "):
                    story.append(Paragraph(line[2:], styles["Title"]))
                    story.append(HRFlowable(width="100%", color=colors.grey))
                elif line.startswith("## "):
                    story.append(Spacer(1, 10))
                    story.append(Paragraph(line[3:], styles["Heading2"]))
                elif line.startswith("### "):
                    story.append(Paragraph(line[4:], styles["Heading3"]))
                elif line.startswith("- ") or line.startswith("* "):
                    story.append(Paragraph("• " + line[2:], styles["Normal"]))
                elif line.startswith("|"):
                    pass  # skip table lines in PDF (markdown tables)
                elif line.startswith("---"):
                    story.append(HRFlowable(width="100%", color=colors.lightgrey))
                else:
                    clean = line.replace("**", "").replace("`", "")
                    if clean:
                        story.append(Paragraph(clean, styles["Normal"]))

            doc.build(story)
            return pdf_path
        except Exception as e:
            print(f"[warn] PDF generation failed: {e}")
            return None
