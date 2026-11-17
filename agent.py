"""Google ADK multi-agent system: orchestrator + Anomaly / Root-Cause / Policy agents. Tools wrap the deterministic pandas pipeline,
so every number the LLM reports comes from computed evidence, never from the model's imagination."""
import os
from google.adk.agents import Agent
from cloudspend import audit, pipeline

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def get_spend_summary() -> dict:
    """Overall daily spend now vs baseline, percent change, and number of detected anomalies."""
    s = pipeline.get()["summary"]
    return {k: v for k, v in s.items() if k != "total_daily"}


def list_anomalies() -> list:
    """List detected cost anomalies (project, service, dates, excess per day, total excess)."""
    return [{k: v for k, v in a.items() if k != "root_cause"} for a in pipeline.get()["anomalies"]]


def investigate_anomaly(anomaly_id: str) -> dict:
    """Root cause for one anomaly id (e.g. 'AN-001'): contributing resources, SKUs, correlated deploys, utilisation evidence, confidence."""
    for a in pipeline.get()["anomalies"]:
        if a["id"] == anomaly_id:
            return a["root_cause"]
    return {"error": f"unknown anomaly {anomaly_id}"}


def list_recommendations() -> list:
    """Ranked, policy-checked savings recommendations with savings, confidence, risk and policy verdict."""
    keep = ("id", "title", "action", "risk", "estimated_monthly_savings", "potential_monthly_savings", "confidence", "policy", "targets", "anomaly_id")
    return [{k: r[k] for k in keep} for r in pipeline.get()["recommendations"]]


def explain_recommendation(rec_id: str) -> dict:
    """Full cost-to-action evidence chain for one recommendation id (e.g. 'REC-01')."""
    for r in pipeline.get()["recommendations"]:
        if r["id"] == rec_id:
            e = dict(r["evidence"])
            e["trend"] = {k: v for k, v in e["trend"].items() if k not in ("dates", "cost")}
            return e
    return {"error": f"unknown recommendation {rec_id}"}


def request_approval(rec_id: str) -> dict:
    """Record that a human approval is requested. The agent can NEVER approve or execute; only the dashboard user can."""
    audit.log("agent", "approval_requested", rec_id, {})
    return {"status": "approval_requested", "note": "A human must approve this in the dashboard."}


anomaly_agent = Agent(name="anomaly_agent", model=MODEL, description="Detects and describes cloud cost anomalies.",
                      instruction="Use get_spend_summary and list_anomalies. Report only numbers returned by tools.",
                      tools=[get_spend_summary, list_anomalies])
root_cause_agent = Agent(name="root_cause_agent", model=MODEL, description="Explains why a cost anomaly happened.",
                         instruction="Use investigate_anomaly. State the cause, the evidence (resources, deploys, utilisation) and the confidence. Never guess beyond the evidence.",
                         tools=[investigate_anomaly])
policy_agent = Agent(name="policy_agent", model=MODEL, description="Explains and gates savings recommendations against safety policy.",
                     instruction=("Use list_recommendations and explain_recommendation. Always include savings, confidence, risk and the policy verdict. "
                                  "Never suggest deleting or stopping production or protected resources. You cannot approve or execute anything; "
                                  "use request_approval and tell the user to approve in the dashboard."),
                     tools=[list_recommendations, explain_recommendation, request_approval])

root_agent = Agent(
    name="cloudspend_orchestrator", model=MODEL,
    description="CloudSpend Autopilot: investigates cloud cost spikes and recommends safe savings.",
    instruction=("You are a FinOps investigation orchestrator. For a question about a bill increase: delegate to anomaly_agent (what changed), "
                 "then root_cause_agent (why), then policy_agent (what to do safely). Answer concisely with the evidence chain: trend, cause, "
                 "estimated savings, confidence, operational risk. Every change needs human approval."),
    sub_agents=[anomaly_agent, root_cause_agent, policy_agent])
