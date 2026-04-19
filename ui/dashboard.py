"""
ui/dashboard.py
Optional Streamlit dashboard — run with: streamlit run ui/dashboard.py
Lets you trigger chaos runs, watch live metrics, and browse past reports.
"""
import json
import os
import time
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="AI Chaos Engineer",
    page_icon="⚡",
    layout="wide",
)

# ── Sidebar ─────────────────────────────────────────────────────────────────

st.sidebar.title("⚡ AI Chaos Engineer")
page = st.sidebar.radio("Navigate", ["Run Chaos", "Live Metrics", "Past Reports", "Config"])

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_config_safe():
    try:
        from config_loader import load_config
        return load_config()
    except Exception as e:
        st.error(f"Config error: {e}")
        return {}


def color_verdict(verdict: str) -> str:
    return {"pass": "🟢", "degraded": "🟡", "failed": "🔴", "resilient": "🟢",
            "needs_work": "🟡", "fragile": "🔴"}.get(verdict, "⚪")


def score_color(score: int) -> str:
    if score >= 80:
        return "normal"
    if score >= 50:
        return "off"
    return "inverse"

# ── Run Chaos page ────────────────────────────────────────────────────────────

if page == "Run Chaos":
    st.title("⚡ Run Chaos Experiment")

    cfg = load_config_safe()

    col1, col2 = st.columns(2)
    with col1:
        provider = st.selectbox("AI Provider", ["anthropic", "openai"],
                                index=0 if cfg.get("ai", {}).get("provider") == "anthropic" else 1)
        namespace = st.text_input("Namespace", value=cfg.get("kubernetes", {}).get("namespace", "default"))
        dry_run = st.checkbox("Dry Run (plan only, no execution)",
                              value=cfg.get("chaos", {}).get("dry_run", False))

    with col2:
        exp_types = cfg.get("chaos", {}).get("experiments_enabled", [
            "pod_kill", "network_latency", "cpu_stress", "memory_stress"
        ])
        selected_exps = st.multiselect("Experiment Types", exp_types, default=exp_types)

    st.divider()

    if st.button("🚀 Scan Cluster & Plan", type="primary"):
        with st.spinner("Scanning cluster topology..."):
            try:
                os.environ["AI_PROVIDER"] = provider
                from analyzer.cluster_scanner import ClusterScanner
                scanner = ClusterScanner()
                topology = scanner.scan()
                st.session_state["topology"] = topology
                st.success(f"Found {len(topology.workloads)} workloads across {len(topology.nodes)} nodes")

                with st.expander("Cluster Risk Summary"):
                    st.json(topology.risk_summary)

            except Exception as e:
                st.error(f"Cluster scan failed: {e}")

    if "topology" in st.session_state:
        topology = st.session_state["topology"]

        if st.button("🤖 Generate Chaos Plan with AI"):
            with st.spinner(f"Asking {provider} to plan experiments..."):
                try:
                    from analyzer.ai_planner import AIPlanner
                    planner = AIPlanner()
                    plan = planner.plan(topology)
                    st.session_state["plan"] = plan

                    st.subheader(f"Plan: {color_verdict(plan.overall_risk)} {plan.overall_risk.upper()} risk")
                    st.write(plan.summary)

                    for i, exp in enumerate(plan.experiments):
                        with st.expander(f"{i+1}. {exp.name} [{exp.severity}]"):
                            col1, col2 = st.columns(2)
                            col1.metric("Target", exp.target_workload)
                            col2.metric("Kind", exp.kind)
                            st.write(f"**Rationale:** {exp.rationale}")
                            st.write(f"**Predicted blast radius:** {exp.predicted_blast_radius}")
                            st.json(exp.parameters)

                except Exception as e:
                    st.error(f"Planning failed: {e}")

    if "plan" in st.session_state:
        plan = st.session_state["plan"]
        topology = st.session_state["topology"]

        st.divider()
        if dry_run:
            st.info("Dry run mode — experiments will be planned but not executed.")

        if st.button("💥 Execute Chaos Plan", type="primary", disabled=dry_run and False):
            impact_reports = []
            progress = st.progress(0)
            status_area = st.empty()

            from executor import run_experiment
            from analyzer.blast_predictor import BlastPredictor
            from observer.impact_scorer import ImpactScorer
            from reporter.ai_analyzer import AIAnalyzer
            from reporter.report_generator import ReportGenerator
            from reporter.slack_notifier import SlackNotifier

            predictor = BlastPredictor(topology)
            scorer = ImpactScorer()

            for i, exp in enumerate(plan.experiments):
                status_area.info(f"Running: {exp.name}")
                prediction = predictor.predict(exp)

                if not prediction.safe_to_proceed:
                    st.warning(f"⛔ Skipped '{exp.name}': {prediction.abort_reason}")
                    progress.progress((i + 1) / len(plan.experiments))
                    continue

                baseline = scorer.take_baseline(exp)
                start = time.time()
                try:
                    run_experiment(exp)
                except Exception as e:
                    st.error(f"Experiment failed: {e}")
                    continue

                report = scorer.score(exp, baseline, start, time.time())
                impact_reports.append(report)
                st.write(f"{color_verdict(report.verdict)} **{exp.name}** — Score: {report.resilience_score}/100")
                progress.progress((i + 1) / len(plan.experiments))

            if impact_reports:
                status_area.info("Generating AI resilience analysis...")
                analyzer = AIAnalyzer()
                resilience = analyzer.analyze(plan, impact_reports)

                st.subheader(f"Overall: {color_verdict(resilience.overall_verdict)} {resilience.overall_score}/100")
                st.write(resilience.executive_summary)

                generator = ReportGenerator()
                paths = generator.generate(plan, impact_reports, resilience, topology.context)
                st.session_state["last_report"] = paths

                SlackNotifier().notify(resilience, topology.context,
                                       paths.get("markdown", ""))

                st.success(f"Reports saved: {paths}")

# ── Live Metrics page ─────────────────────────────────────────────────────────

elif page == "Live Metrics":
    st.title("📊 Live Cluster Metrics")

    cfg = load_config_safe()
    namespace = st.text_input("Namespace", value=cfg.get("kubernetes", {}).get("namespace", "default"))
    workload = st.text_input("Workload name", placeholder="e.g. my-api")
    auto_refresh = st.checkbox("Auto-refresh every 10s", value=False)

    if workload and st.button("Fetch Metrics"):
        try:
            from observer.prometheus_client import PrometheusClient
            prom = PrometheusClient()
            snap = prom.snapshot(namespace, workload)

            c1, c2, c3 = st.columns(3)
            c1.metric("Error Rate", f"{snap.get('error_rate_pct') or 0:.2f}%")
            c2.metric("p99 Latency", f"{snap.get('latency_p99_ms') or 0:.0f}ms")
            c3.metric("Pod Restarts", f"{snap.get('pod_restarts') or 0:.0f}")

            c4, c5 = st.columns(2)
            c4.metric("CPU", f"{snap.get('cpu_millicores') or 0:.0f}m")
            c5.metric("Memory", f"{snap.get('memory_mb') or 0:.0f}MB")

        except Exception as e:
            st.error(f"Metrics fetch failed: {e}")

# ── Past Reports page ─────────────────────────────────────────────────────────

elif page == "Past Reports":
    st.title("📁 Past Reports")

    reports_dir = Path("./reports")
    if not reports_dir.exists():
        st.info("No reports yet. Run a chaos experiment first.")
    else:
        md_files = sorted(reports_dir.glob("*.md"), reverse=True)
        if not md_files:
            st.info("No markdown reports found.")
        else:
            selected = st.selectbox("Select report", [f.name for f in md_files])
            if selected:
                content = (reports_dir / selected).read_text()
                st.markdown(content)

# ── Config page ───────────────────────────────────────────────────────────────

elif page == "Config":
    st.title("⚙️ Configuration")
    cfg = load_config_safe()
    st.json(cfg)
    st.info("Edit `config.yaml` and `.env` to change settings. Restart the dashboard to apply.")
