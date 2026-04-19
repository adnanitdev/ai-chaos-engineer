"""
main.py — AI Chaos Engineer CLI
Usage:
  python main.py run                    # full automated run
  python main.py scan                   # scan cluster only
  python main.py plan                   # scan + AI plan (no execution)
  python main.py run --dry-run          # plan + dry-run execution
  python main.py run --provider openai  # override AI provider
"""
from __future__ import annotations
import time
import sys
import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

console = Console()


def _banner():
    console.print(Panel.fit(
        "[bold purple]⚡ AI Chaos Engineer[/bold purple]\n"
        "[dim]Powered by Claude / GPT-4o + LitmusChaos + Chaos Mesh[/dim]",
        border_style="purple",
    ))


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """AI-powered chaos engineering for Kubernetes."""
    pass


@cli.command()
@click.option("--config", default="config.yaml", help="Path to config file")
def scan(config: str):
    """Scan the Kubernetes cluster and print topology."""
    _banner()
    from analyzer.cluster_scanner import ClusterScanner

    with console.status("[bold green]Scanning cluster..."):
        scanner = ClusterScanner(config)
        topology = scanner.scan()

    console.print(f"\n[bold]Context:[/bold] {topology.context}")
    console.print(f"[bold]Namespace:[/bold] {topology.namespace}")

    table = Table(title="Workloads", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Namespace")
    table.add_column("Replicas")
    table.add_column("HPA")
    for w in topology.workloads:
        ready_str = f"{w.replicas_ready}/{w.replicas_desired}"
        color = "green" if w.replicas_ready == w.replicas_desired else "red"
        table.add_row(
            w.name, w.kind, w.namespace,
            f"[{color}]{ready_str}[/{color}]",
            "✓" if w.hpa_enabled else "✗",
        )
    console.print(table)

    risk = topology.risk_summary
    console.print(f"\n[bold yellow]Risk Summary[/bold yellow]")
    console.print(f"  Single-replica workloads : {risk['single_replica_workloads']}")
    console.print(f"  No HPA                   : {risk['workloads_without_hpa']}")
    console.print(f"  No resource limits        : {risk['workloads_without_resource_limits']}")
    console.print(f"  Degraded                  : {risk['degraded_workloads']}")


@cli.command()
@click.option("--config", default="config.yaml", help="Path to config file")
@click.option("--provider", default=None, help="AI provider override: anthropic|openai")
def plan(config: str, provider: str | None):
    """Scan cluster and generate AI chaos plan without executing."""
    _banner()
    import os
    if provider:
        os.environ["AI_PROVIDER"] = provider

    from analyzer.cluster_scanner import ClusterScanner
    from analyzer.ai_planner import AIPlanner

    with console.status("[bold green]Scanning cluster..."):
        topology = ClusterScanner(config).scan()

    console.print(f"[green]✓[/green] Found {len(topology.workloads)} workloads on {len(topology.nodes)} nodes")

    with console.status("[bold purple]Asking AI to plan experiments..."):
        chaos_plan = AIPlanner(config).plan(topology)

    console.print(f"\n[bold]AI Provider:[/bold] {chaos_plan.ai_provider}")
    console.print(f"[bold]Overall Risk:[/bold] {chaos_plan.overall_risk.upper()}")
    console.print(f"\n{chaos_plan.summary}\n")

    table = Table(title="Experiment Plan", header_style="bold magenta")
    table.add_column("#", width=3)
    table.add_column("Experiment")
    table.add_column("Kind")
    table.add_column("Target")
    table.add_column("Severity")
    for i, exp in enumerate(chaos_plan.experiments, 1):
        sev_color = {"low": "green", "medium": "yellow", "high": "red"}.get(exp.severity, "white")
        table.add_row(
            str(i), exp.name, exp.kind, exp.target_workload,
            f"[{sev_color}]{exp.severity}[/{sev_color}]",
        )
    console.print(table)

    for exp in chaos_plan.experiments:
        console.print(f"\n[bold cyan]{exp.name}[/bold cyan]")
        console.print(f"  Rationale    : {exp.rationale}")
        console.print(f"  Blast radius : {exp.predicted_blast_radius}")
        console.print(f"  Parameters   : {exp.parameters}")


@cli.command()
@click.option("--config", default="config.yaml", help="Path to config file")
@click.option("--provider", default=None, help="AI provider override: anthropic|openai")
@click.option("--dry-run", is_flag=True, default=False, help="Plan only, skip execution")
@click.option("--skip-confirm", is_flag=True, default=False, help="Skip confirmation prompt")
def run(config: str, provider: str | None, dry_run: bool, skip_confirm: bool):
    """Full automated chaos run: scan → plan → execute → report."""
    _banner()
    import os
    if provider:
        os.environ["AI_PROVIDER"] = provider
    if dry_run:
        console.print("[yellow]DRY RUN mode — experiments will not be executed[/yellow]\n")

    from analyzer.cluster_scanner import ClusterScanner
    from analyzer.ai_planner import AIPlanner
    from analyzer.blast_predictor import BlastPredictor
    from executor import run_experiment
    from observer.impact_scorer import ImpactScorer
    from reporter.ai_analyzer import AIAnalyzer
    from reporter.report_generator import ReportGenerator
    from reporter.slack_notifier import SlackNotifier

    # ── 1. Scan ───────────────────────────────────────────────────────────────
    with console.status("[bold green]Scanning cluster..."):
        topology = ClusterScanner(config).scan()
    console.print(f"[green]✓[/green] {len(topology.workloads)} workloads, {len(topology.nodes)} nodes — context: {topology.context}")

    # ── 2. Plan ───────────────────────────────────────────────────────────────
    with console.status("[bold purple]Generating AI chaos plan..."):
        chaos_plan = AIPlanner(config).plan(topology)
    console.print(f"[green]✓[/green] {len(chaos_plan.experiments)} experiments planned (risk: {chaos_plan.overall_risk})")

    if not chaos_plan.experiments:
        console.print("[yellow]No experiments generated. Exiting.[/yellow]")
        sys.exit(0)

    # ── 3. Confirm ────────────────────────────────────────────────────────────
    if not dry_run and not skip_confirm:
        console.print(f"\n[bold red]About to execute {len(chaos_plan.experiments)} chaos experiments.[/bold red]")
        console.print("Targets: " + ", ".join(e.target_workload for e in chaos_plan.experiments))
        if not click.confirm("\nProceed?"):
            console.print("Aborted.")
            sys.exit(0)

    # ── 4. Execute ────────────────────────────────────────────────────────────
    predictor = BlastPredictor(topology)
    scorer = ImpactScorer(config)
    impact_reports = []

    for i, exp in enumerate(chaos_plan.experiments, 1):
        console.rule(f"[bold]Experiment {i}/{len(chaos_plan.experiments)}: {exp.name}[/bold]")

        # Pre-flight
        prediction = predictor.predict(exp)
        for w in prediction.warnings:
            console.print(f"  [yellow]⚠ {w}[/yellow]")

        if not prediction.safe_to_proceed:
            console.print(f"  [red]⛔ Skipped: {prediction.abort_reason}[/red]")
            continue

        if dry_run:
            console.print(f"  [dim]DRY RUN — would execute: {exp.kind} on {exp.target_workload}[/dim]")
            continue

        # Baseline
        baseline = scorer.take_baseline(exp)
        start_ts = time.time()

        console.print(f"  [bold]Executing {exp.kind} on {exp.target_workload}...[/bold]")

        slo_breach = ""
        try:
            from executor.rollback import RollbackWatcher

            def on_breach(reason: str):
                nonlocal slo_breach
                slo_breach = reason
                console.print(f"  [red bold]SLO BREACH: {reason}[/red bold]")

            watcher = RollbackWatcher(abort_fn=on_breach, config_path=config)
            watcher.start(exp.target_namespace, exp.target_workload)

            result = run_experiment(exp, config)

            watcher.stop()
        except Exception as e:
            console.print(f"  [red]Execution error: {e}[/red]")
            continue

        end_ts = time.time()
        console.print(f"  [dim]Execution result: {result.get('status')}[/dim]")

        # Score
        impact = scorer.score(exp, baseline, start_ts, end_ts, slo_breach)
        impact_reports.append(impact)

        verdict_color = {"pass": "green", "degraded": "yellow", "failed": "red"}.get(impact.verdict, "white")
        console.print(
            f"  [{verdict_color}]Verdict: {impact.verdict.upper()} — "
            f"Score: {impact.resilience_score}/100[/{verdict_color}]"
        )

        # Cooldown
        from config_loader import load_config
        cooldown = load_config(config)["chaos"]["cooldown_seconds"]
        if i < len(chaos_plan.experiments) and not dry_run:
            console.print(f"  [dim]Cooldown {cooldown}s...[/dim]")
            time.sleep(cooldown)

    # ── 5. Report ─────────────────────────────────────────────────────────────
    if not impact_reports:
        console.print("\n[yellow]No experiments completed — no report generated.[/yellow]")
        sys.exit(0)

    console.rule("[bold purple]Generating Resilience Report[/bold purple]")

    with console.status("[bold purple]AI is analyzing results..."):
        analyzer = AIAnalyzer(config)
        resilience = analyzer.analyze(chaos_plan, impact_reports)

    score_color = "green" if resilience.overall_resilience_score >= 80 else \
                  "yellow" if resilience.overall_resilience_score >= 50 else "red"

    console.print(Panel(
        f"[bold {score_color}]Resilience Score: {resilience.overall_resilience_score}/100 — "
        f"{resilience.overall_verdict.upper()}[/bold {score_color}]\n\n"
        f"{resilience.executive_summary}",
        title="Overall Result",
        border_style=score_color,
    ))

    if resilience.critical_findings:
        console.print("\n[bold red]Critical Findings:[/bold red]")
        for f in resilience.critical_findings:
            console.print(f"  • {f}")

    if resilience.recommendations:
        console.print("\n[bold yellow]Top Recommendations:[/bold yellow]")
        for r in resilience.recommendations[:5]:
            console.print(f"  [{r['priority']}] {r['action']} (effort: {r['effort']})")

    # Save reports
    generator = ReportGenerator(config)
    paths = generator.generate(chaos_plan, impact_reports, resilience, topology.context)
    console.print(f"\n[green]✓[/green] Reports saved:")
    for fmt, path in paths.items():
        console.print(f"  {fmt}: {path}")

    # Slack
    notifier = SlackNotifier(config)
    if notifier.enabled:
        sent = notifier.notify(resilience, topology.context, paths.get("markdown", ""))
        console.print(f"[green]✓[/green] Slack notification {'sent' if sent else 'failed'}")

    console.print("\n[bold green]Done![/bold green]")


if __name__ == "__main__":
    cli()
