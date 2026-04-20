# ⚡ AI Chaos Engineer

An AI-powered chaos engineering tool for Kubernetes that **intelligently plans, executes, and analyzes** failure experiments — then tells you exactly how to improve resilience.

Supports **Claude (Anthropic)** and **GPT-4o (OpenAI)** switchable via config.  
Works with **EKS, GKE, AKS** out of the box.

---

## How It Works

```
Kubernetes Cluster
       │
       ▼
  Cluster Scanner ──► AI Planner (Claude / GPT-4o)
                             │
                    Ranked Experiment Plan
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         Pod Kill     Network Chaos    CPU/Mem Stress
       (LitmusChaos)  (Chaos Mesh)    (Chaos Mesh)
              │              │              │
              └──────────────┴──────────────┘
                             │
                    Prometheus Metrics
                             │
                      AI Result Analyzer
                             │
                    Resilience Report (MD + PDF + Slack)
```

---

## Prerequisites

| Tool | Purpose |
|---|---|
| Python 3.11+ | Runtime |
| kubectl + kubeconfig | Cluster access (EKS/GKE/AKS) |
| [LitmusChaos](https://litmuschaos.io) | Pod kill, node drain |
| [Chaos Mesh](https://chaos-mesh.org) | Network, CPU, memory chaos |
| Prometheus | Metrics collection |
| Anthropic or OpenAI API key | AI reasoning |

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/ai-chaos-engineer
cd ai-chaos-engineer

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# Fill in your API keys in .env
```

---

## Cluster Setup

### 1. Install LitmusChaos

```bash
kubectl apply -f https://litmuschaos.github.io/litmus/litmus-operator-v3.28.0.yaml
kubectl apply -f k8s/litmus/rbac.yaml
kubectl apply -f k8s/litmus/experiments.yaml
```

### 2. Install Chaos Mesh

```bash
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos-mesh --create-namespace \
  --version 2.6.3
```

### 3. Verify Prometheus is running

```bash
kubectl get svc -n monitoring | grep prometheus
# Port-forward if needed:
kubectl port-forward svc/prometheus-operated 9090:9090 -n monitoring
```

---

## Configuration

Edit `config.yaml`:

```yaml
ai:
  provider: "anthropic"   # or "openai"

kubernetes:
  namespace: "production"  # or "all"

chaos:
  dry_run: false           # true = plan only, no execution

slo:
  error_rate_percent: 5.0
  latency_p99_ms: 2000

slack:
  enabled: true
  webhook_url: "https://hooks.slack.com/..."
```

---

## Usage

### Scan cluster only
```bash
python main.py scan
```

### Plan experiments (AI only, no execution)
```bash
python main.py plan
python main.py plan --provider openai   # switch AI provider
```

### Full automated run
```bash
python main.py run
python main.py run --dry-run            # safe mode
python main.py run --provider openai --skip-confirm
```

### Streamlit dashboard
```bash
streamlit run ui/dashboard.py
```

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Output

Each run produces:
- `reports/resilience-report-YYYYMMDD-HHMMSS.md` — full markdown report
- `reports/resilience-report-YYYYMMDD-HHMMSS.pdf` — PDF version
- Slack message (if configured)

### Sample Report Structure
```
Overall Score: 74/100 — NEEDS_WORK

Executive Summary: ...

Experiments:
  ✓ Pod kill — api-service        PASS     92/100
  ⚠ Network latency — checkout   DEGRADED  61/100
  ✗ CPU stress — payment-svc     FAILED    34/100

Recommendations:
  [P1] Add HPA to payment-svc (effort: low)
  [P1] Set resource limits on checkout container (effort: low)
  [P2] Increase replica count for api-service (effort: medium)
```

---

## Project Structure

```
ai-chaos-engineer/
├── main.py                    # CLI entrypoint
├── config.yaml                # configuration
├── config_loader.py           # shared config + env loader
├── requirements.txt
├── analyzer/
│   ├── cluster_scanner.py     # reads K8s topology
│   ├── ai_planner.py          # Claude/GPT-4o experiment planner
│   └── blast_predictor.py     # pre-flight safety check
├── executor/
│   ├── litmus_runner.py       # pod kill, node drain
│   ├── chaos_mesh_runner.py   # network, CPU, memory chaos
│   └── rollback.py            # SLO watcher + auto-abort
├── observer/
│   ├── prometheus_client.py   # metric queries
│   └── impact_scorer.py       # before/after delta + resilience score
├── reporter/
│   ├── ai_analyzer.py         # AI result analysis
│   ├── report_generator.py    # Markdown + PDF output
│   └── slack_notifier.py      # Slack webhook
├── ui/
│   └── dashboard.py           # Streamlit dashboard
├── k8s/litmus/
│   ├── rbac.yaml              # LitmusChaos RBAC
│   └── experiments.yaml       # ChaosExperiment CRDs
└── tests/
    ├── test_blast_predictor.py
    └── test_impact_scorer.py
```

---

## Safety

- **Dry run mode** — plan without executing anything
- **Pre-flight checks** — aborts if workload is already degraded or has 1 replica
- **SLO watcher** — monitors error rate, latency, restarts during execution; auto-aborts on breach
- **Auto-cleanup** — removes all ChaosEngine/NetworkChaos CRDs after each experiment
- **Cooldown** — configurable wait between experiments

---
