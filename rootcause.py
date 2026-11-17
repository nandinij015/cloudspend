"""Root-cause analysis: which resources/SKUs drove the delta, correlated deploy events, utilisation evidence."""
from __future__ import annotations
import pandas as pd


def investigate(ev: dict, bill: pd.DataFrame, inv_by: dict, metrics: pd.DataFrame, events: pd.DataFrame) -> dict:
    s, e = pd.Timestamp(ev["start"]), pd.Timestamp(ev["end"])
    scope = bill[(bill.project == ev["project"]) & (bill.service == ev["service"])]
    b0 = max(scope["date"].min(), s - pd.Timedelta(days=28))
    nb, ne = max((s - b0).days, 1), (e - s).days + 1
    base = scope[(scope.date >= b0) & (scope.date < s)].groupby("resource_id")["cost"].sum() / nb
    cur = scope[(scope.date >= s) & (scope.date <= e)].groupby("resource_id")["cost"].sum() / ne
    delta = cur.sub(base, fill_value=0).sort_values(ascending=False)
    pos = delta[delta > 0]
    contributors = []
    for rid, dl in pos.items():
        if dl / pos.sum() < 0.02:
            break
        inf = inv_by.get(rid, {})
        m = metrics[(metrics.resource_id == rid) & (metrics.date >= s) & (metrics.date <= e)]
        cpu = float(m["cpu_util"].mean()) if m["cpu_util"].notna().any() else None
        attached = bool(m["attached"].dropna().iloc[-1]) if m["attached"].notna().any() else None
        contributors.append(dict(resource_id=rid, baseline_daily=round(float(base.get(rid, 0)), 2),
                                 event_daily=round(float(cur.get(rid, 0)), 2), delta_daily=round(float(dl), 2),
                                 share=round(float(dl / pos.sum()), 3), is_new=bool(base.get(rid, 0) < 0.5),
                                 env=inf.get("env"), protected=bool(inf.get("protected")),
                                 cpu_avg=None if cpu is None else round(cpu, 3), attached=attached, type=inf.get("type")))
    sku = scope[(scope.date >= s) & (scope.date <= e)].groupby("sku")["cost"].sum().sort_values(ascending=False)
    dep = events[(events.project == ev["project"]) & (events.date >= s - pd.Timedelta(days=2)) & (events.date <= s + pd.Timedelta(days=1))]
    deploys = [dict(date=r.date.strftime("%Y-%m-%d"), description=r.description) for r in dep.itertuples()]

    new_share = sum(c["share"] for c in contributors if c["is_new"])
    disks = [c for c in contributors if c["type"] == "pd-ssd" or c["attached"] is False]
    idle = [c for c in contributors if c["cpu_avg"] is not None and c["cpu_avg"] < 0.05]
    notes = []
    if contributors and new_share > 0.8 and len(disks) == len(contributors):
        cause, factors = "orphaned_storage", 2
        summary = f"{len(contributors)} newly appeared SSD disks are unattached; they account for {new_share:.0%} of the increase."
        notes.append("Every contributing disk reports attached=false for the whole anomaly window.")
    elif contributors and new_share > 0.8 and len(idle) == len(contributors):
        cause, factors = "idle_new_resources", 2
        summary = f"{len(contributors)} new instance(s) started on {ev['start']} and sit idle (CPU < 5%); they explain {new_share:.0%} of the increase."
        notes.append("Mean CPU of every contributing instance is below 5% for the whole window.")
    elif deploys and contributors and not contributors[0]["is_new"] and ev["avg_daily"] >= 2 * max(ev["baseline_daily"], 1):
        cause, factors = "usage_spike_after_deploy", 2
        summary = f"Usage of {contributors[0]['resource_id']} rose {ev['avg_daily'] / max(ev['baseline_daily'], 1):.1f}x right after: {deploys[0]['description']}."
    else:
        cause, factors = "unexplained_usage_growth", 0
        summary = "Spend rose without a new resource or a correlated deploy; needs human review."
    if deploys:
        notes.append(f"Change event within 2 days of onset: {deploys[0]['date']} - {deploys[0]['description']}.")
    top = contributors[0]["share"] if contributors else 0
    conf = 0.45 + 0.2 * (top > 0.6) + 0.1 * factors + 0.1 * bool(deploys) + 0.1 * (ev["days"] >= 2)
    return dict(anomaly_id=ev["id"], cause_type=cause, summary=summary, confidence=round(min(conf, 0.95), 2),
                contributors=contributors, deploy_events=deploys, evidence=notes,
                sku_breakdown=[dict(sku=k, cost=round(float(v), 2)) for k, v in sku.head(5).items()])
