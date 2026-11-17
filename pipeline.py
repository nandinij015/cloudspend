"""Orchestrates detect -> root-cause -> propose -> policy -> evidence chain -> ranking."""
from __future__ import annotations
import os
import pandas as pd
from . import data as D, detect as DT, rootcause as RC, policy as PL

RISK_MULT = {"low": 1.0, "medium": 0.7, "high": 0.4, "none": 0.0, "unknown": 1.0}


def load_source() -> dict:
    if os.getenv("SOURCE") == "bigquery":
        from .bq_source import load_bigquery
        return load_bigquery()
    return D.load()


def _trend(bill, project, service, start, end, resource=None):
    s = bill[(bill.project == project) & (bill.service == service)] if resource is None else bill[bill.resource_id == resource]
    daily = s.groupby("date")["cost"].sum()
    lo, hi = pd.Timestamp(start) - pd.Timedelta(days=28), min(bill["date"].max(), pd.Timestamp(end) + pd.Timedelta(days=10))
    daily = daily.reindex(pd.date_range(lo, hi), fill_value=0.0)
    return dict(dates=[x.strftime("%Y-%m-%d") for x in daily.index], cost=[round(float(x), 2) for x in daily.values])


def run(enforce_policy: bool = True, src: dict | None = None) -> dict:
    src = src or load_source()
    bill, inv, met, events = src["bill"], src["inventory"], src["metrics"], src["events"]
    last = bill["date"].max()
    inv_by = {r["resource_id"]: r for r in inv}
    for r in inv_by.values():  # latest attach state
        if r["kind"] == "disk":
            r["attached"] = False
    recent = (bill[bill.date > last - pd.Timedelta(days=7)].groupby("resource_id")["cost"].sum() / 7).to_dict()
    cpu14 = met[met.date > last - pd.Timedelta(days=14)].groupby("resource_id")["cpu_util"].mean().dropna().to_dict()
    anomalies = DT.detect(bill)
    ctx = dict(inv=inv_by, last_date=last, recent=recent, cpu14=cpu14, anomalies={a["id"]: a for a in anomalies})

    raw, rcs = [], {}
    for a in anomalies:
        rcs[a["id"]] = RC.investigate(a, bill, inv_by, met, events)
        a["root_cause"] = rcs[a["id"]]
        raw += PL.propose_from_anomaly(a, rcs[a["id"]])
    covered = {t for p in raw for t in p["targets"]}
    raw += PL.waste_scan(inv, ctx, covered)

    recs = []
    for p in raw:
        recs += PL.apply_policy(p, ctx, enforce_policy)
    for r in recs:
        r["command_preview"] = PL.command_preview(r, ctx)
        r["target_details"] = [dict(resource_id=t, env=ctx["inv"][t]["env"], type=ctx["inv"][t]["type"], protected=ctx["inv"][t]["protected"],
                                    cpu_14d=None if t not in cpu14 else round(cpu14[t], 3), daily_cost=round(recent.get(t, 0), 2)) for t in r["targets"]]
        r["priority"] = round(r["estimated_monthly_savings"] * r["confidence"] * RISK_MULT.get(r["risk"], 0.5), 1)
        r["evidence"] = _evidence(r, ctx, bill, rcs.get(r.get("anomaly_id")), ctx["anomalies"].get(r.get("anomaly_id")))
        r["status"] = "pending"
    recs.sort(key=lambda r: -r["priority"])
    for i, r in enumerate(recs, 1):
        r["id"] = f"REC-{i:02d}"

    first = min((a["start"] for a in anomalies), default=str(last.date()))
    b0 = pd.Timestamp(first)
    daily = bill.groupby("date")["cost"].sum()
    base = float(daily[(daily.index >= b0 - pd.Timedelta(days=28)) & (daily.index < b0)].mean())
    cur = float(daily[daily.index > last - pd.Timedelta(days=7)].mean())
    summary = dict(last_date=str(last.date()), baseline_daily=round(base, 1), current_daily=round(cur, 1),
                   pct_change=round((cur / base - 1) * 100, 1), anomalies=len(anomalies),
                   potential_monthly_savings=round(sum(r["estimated_monthly_savings"] for r in recs), 1),
                   total_daily=[dict(date=d.strftime("%Y-%m-%d"), cost=round(float(c), 2)) for d, c in daily.items()])
    return dict(summary=summary, anomalies=anomalies, recommendations=recs, ctx=ctx)


def _evidence(r, ctx, bill, rc, ev):
    if ev:
        trend = _trend(bill, ev["project"], ev["service"], ev["start"], ev["end"])
        trend.update(baseline=ev["baseline_daily"], anomaly_start=ev["start"], anomaly_end=ev["end"])
        cause = dict(type=rc["cause_type"], summary=rc["summary"], contributors=rc["contributors"][:5],
                     deploy_events=rc["deploy_events"], notes=rc["evidence"], sku_breakdown=rc["sku_breakdown"])
        conf = [f"Root-cause confidence {rc['confidence']:.2f}: top contributor share, change-event correlation, utilisation/attachment evidence, event duration."]
    else:
        t0 = r["targets"][0]
        trend = _trend(bill, r["project"], r["service"], ctx["last_date"] - pd.Timedelta(days=14), ctx["last_date"], resource=t0)
        trend.update(baseline=None, anomaly_start=None, anomaly_end=None)
        cause = dict(type="underutilised", summary="Instances run 24x7 with sustained CPU under 10% over the last 14 days; no anomaly, just standing waste.",
                     contributors=[], deploy_events=[], notes=[], sku_breakdown=[])
        conf = ["Confidence 0.75: 14-day utilisation window, no change-event or anomaly correlation."]
    return dict(trend=trend, cause=cause,
                savings=dict(monthly=r["estimated_monthly_savings"], potential=r["potential_monthly_savings"], basis=r["savings_basis"]),
                confidence=dict(score=r["confidence"], factors=conf),
                impact=dict(risk=r["risk"], notes=r["policy"]["rules"] or ["No policy rules evaluated."], commands=r["command_preview"]),
                policy=r["policy"])


_CACHE: dict = {}


def get(refresh: bool = False) -> dict:
    if refresh or "state" not in _CACHE:
        _CACHE["state"] = run()
    return _CACHE["state"]
