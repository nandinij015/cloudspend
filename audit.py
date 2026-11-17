"""Audit log + approval workflow (SQLite). Execution is DRY-RUN: approved actions are logged with the exact commands, never run."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parent / "data" / "audit.db"


def _c():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts REAL, actor TEXT, event TEXT, rec_id TEXT, detail TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS decisions(rec_id TEXT PRIMARY KEY, status TEXT, decided_by TEXT, ts REAL, note TEXT)")
    return c


def log(actor: str, event: str, rec_id: str | None = None, detail: dict | None = None):
    with _c() as c:
        c.execute("INSERT INTO audit(ts,actor,event,rec_id,detail) VALUES(?,?,?,?,?)", (time.time(), actor, event, rec_id, json.dumps(detail or {})))


def decide(rec: dict, status: str, user: str = "engineer", note: str = "") -> dict:
    if status not in ("approved", "rejected"):
        raise ValueError("status must be approved or rejected")
    if rec["policy"]["verdict"] in ("blocked", "deferred") and status == "approved":
        log("policy-agent", "approval_refused", rec["id"], {"verdict": rec["policy"]["verdict"]})
        raise PermissionError(f"Policy verdict is '{rec['policy']['verdict']}': nothing executable to approve.")
    with _c() as c:
        c.execute("INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,?)", (rec["id"], status, user, time.time(), note))
    log(user, status, rec["id"], {"note": note, "title": rec["title"]})
    if status == "approved":  # executor stub: real execution intentionally not implemented
        log("executor", "dry_run_executed", rec["id"], {"commands": rec["command_preview"], "real_execution": False})
    return dict(rec_id=rec["id"], status=status)


def statuses() -> dict:
    with _c() as c:
        return {r[0]: dict(status=r[1], by=r[2], ts=r[3], note=r[4]) for r in c.execute("SELECT * FROM decisions")}


def entries(limit: int = 100) -> list[dict]:
    with _c() as c:
        rows = c.execute("SELECT ts,actor,event,rec_id,detail FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(ts=r[0], actor=r[1], event=r[2], rec_id=r[3], detail=json.loads(r[4])) for r in rows]
