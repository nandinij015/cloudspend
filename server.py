"""FastAPI app: JSON API + static dashboard. Run: uvicorn cloudspend.server:app --reload"""
from __future__ import annotations
import os
from pathlib import Path
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from . import audit, pipeline

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="CloudSpend Autopilot")


def _state():
    s = pipeline.get()
    dec = audit.statuses()
    recs = [dict(r, status=dec.get(r["id"], {}).get("status", "pending")) for r in s["recommendations"]]
    return s, recs


@app.on_event("startup")
def _start():
    s = pipeline.get()
    audit.log("system", "analysis_completed", None, dict(anomalies=len(s["anomalies"]), recommendations=len(s["recommendations"])))


@app.get("/api/state")
def state():
    s, recs = _state()
    return dict(summary=s["summary"], anomalies=[{k: v for k, v in a.items() if k != "root_cause"} for a in s["anomalies"]], recommendations=recs)


@app.post("/api/recommendations/{rec_id}/{decision}")
def decide(rec_id: str, decision: str, body: dict = Body(default={})):
    s, recs = _state()
    rec = next((r for r in recs if r["id"] == rec_id), None)
    if not rec:
        raise HTTPException(404, "unknown recommendation")
    status = {"approve": "approved", "reject": "rejected"}.get(decision)
    if not status:
        raise HTTPException(400, "decision must be approve or reject")
    try:
        return audit.decide(rec, status, body.get("user", "engineer"), body.get("note", ""))
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.get("/api/audit")
def audit_log():
    return audit.entries()


@app.post("/api/rerun")
def rerun():
    s = pipeline.get(refresh=True)
    audit.log("system", "analysis_completed", None, dict(anomalies=len(s["anomalies"])))
    return {"ok": True}


_runner = {}


@app.post("/api/chat")
async def chat(body: dict = Body(...)):
    q = body.get("question", "")
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_USE_VERTEXAI")):
        s, recs = _state()
        top = recs[:3]
        return dict(mode="offline", answer=f"Bill is {s['summary']['pct_change']}% above baseline ({s['summary']['anomalies']} anomalies). Top actions: "
                    + "; ".join(f"{r['id']} {r['title']} (~${r['estimated_monthly_savings']:,.0f}/mo, risk {r['risk']})" for r in top)
                    + ". Set GOOGLE_API_KEY to enable the Gemini agents.")
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types
    from cloudspend_agent.agent import root_agent
    if not _runner:
        _runner["svc"] = InMemorySessionService()
        _runner["run"] = Runner(agent=root_agent, app_name="cloudspend", session_service=_runner["svc"])
        await _runner["svc"].create_session(app_name="cloudspend", user_id="u", session_id="s")
    text = ""
    async for ev in _runner["run"].run_async(user_id="u", session_id="s", new_message=types.Content(role="user", parts=[types.Part(text=q)])):
        if ev.is_final_response() and ev.content and ev.content.parts:
            text = "".join(p.text or "" for p in ev.content.parts)
    return dict(mode="gemini", answer=text)


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")
