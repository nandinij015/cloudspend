# CloudSpend Autopilot
Multi-agent FinOps system (Google ADK + Gemini) that detects cloud cost anomalies, finds the cause, and proposes **safe, approval-gated** savings, each with a cost-to-action evidence chain.

## Run (offline, no API key needed for the pipeline + dashboard)
```bash
pip install -r requirements.txt
python -m cloudspend.data          # generate synthetic billing (4 injected anomalies + ground truth)
python -m eval.evaluate            # precision/recall, root-cause accuracy, savings error, unsafe recs
uvicorn cloudspend.server:app --reload   # dashboard at http://localhost:8000
```
## Gemini agents
```bash
export GOOGLE_API_KEY=...          # or Vertex AI: GOOGLE_GENAI_USE_VERTEXAI=1 + GOOGLE_CLOUD_PROJECT/LOCATION
adk web                            # ADK dev UI, pick `cloudspend_agent`
```
The dashboard "Ask the agents" box also calls the orchestrator when the key is set.

## Deploy to Cloud Run
```bash
gcloud run deploy cloudspend-autopilot --source . --region asia-south1 --allow-unauthenticated \
  --set-env-vars GOOGLE_API_KEY=...   # use Secret Manager + IAM auth for anything real
```
## Real billing data
Set `SOURCE=bigquery BILLING_TABLE=project.dataset.gcp_billing_export_v1_XXXX` (`cloudspend/bq_source.py`). Inventory + Cloud Monitoring metrics loaders are a TODO there.

## Design
| Piece | File |
|---|---|
| Anomaly Agent logic (robust rolling median/MAD, baseline excludes flagged days) | `cloudspend/detect.py` |
| Root-Cause Agent logic (resource/SKU deltas, deploy correlation, utilisation) | `cloudspend/rootcause.py` |
| Policy Agent logic (R1 protected, R2 prod never stopped/deleted, R3 snapshot-before-delete, R4 evidence) | `cloudspend/policy.py` |
| Pipeline, evidence chain, ranking | `cloudspend/pipeline.py` |
| Audit log + approvals (SQLite, dry-run executor) | `cloudspend/audit.py` |
| ADK orchestrator + 3 sub-agents (tools wrap the pipeline) | `cloudspend_agent/agent.py` |
| API + dashboard | `cloudspend/server.py`, `dashboard/index.html` |

Safety: the LLM only explains computed evidence; it cannot approve or execute. Approvals are logged with exact `gcloud` commands but **never run** (dry-run by design).
