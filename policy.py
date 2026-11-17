"""Policy agent logic: turns raw proposals into safe, approval-gated recommendations. enforce=False gives the naive baseline."""
from __future__ import annotations
import pandas as pd

DESTRUCTIVE = {"stop_instance", "delete_disk", "snapshot_and_delete_disk", "rightsize_instance", "schedule_stop"}
HARD = {"stop_instance", "delete_disk", "snapshot_and_delete_disk", "schedule_stop"}  # outage / data loss if wrong
SAVE_FACTOR = {"stop_instance": 1.0, "snapshot_and_delete_disk": 0.75, "delete_disk": 1.0, "rightsize_instance": 0.5, "schedule_stop": 0.65}
TITLES = {"stop_instance": "Stop {n} idle instance(s)", "snapshot_and_delete_disk": "Snapshot, then delete {n} unattached disk(s)",
          "delete_disk": "Delete {n} disk(s)", "rightsize_instance": "Rightsize {n} oversized instance(s)",
          "manual_review": "Manual review needed for {n} resource(s)", "defer": "Re-check {n} resource(s) later",
          "add_guardrail": "Add a cost guardrail for {n} workload(s)", "rollback_review": "Review/rollback change driving {n} workload(s)",
          "investigate": "Investigate unexplained growth ({n})"}


def decide(action: str, r: dict, ctx: dict, enforce: bool):
    """-> (final_action, risk, reasons) for one target."""
    if not enforce:
        return action, "unknown", []
    if action not in DESTRUCTIVE:
        return action, "low", ["Non-destructive: changes limits/alerts only."]
    if r.get("protected"):
        return "manual_review", "none", ["R1: resource is labelled protected - no automated change is proposed."]
    if r["env"] == "prod" and action in HARD:
        if r["kind"] == "vm":
            return "rightsize_instance", "high", ["R2: production resources are never stopped/deleted on utilisation alone; downgraded to a rightsize that needs a change window and owner sign-off."]
        return "manual_review", "none", ["R2: production resource - human review only."]
    if action in ("delete_disk", "snapshot_and_delete_disk"):
        if r.get("attached") or not r.get("unattached_since"):
            return "manual_review", "none", ["R3: disk is attached or its attach history is unknown."]
        days = (ctx["last_date"] - pd.Timestamp(r["unattached_since"])).days
        if days < 7:
            return "defer", "low", [f"R3: unattached for only {days}d (< 7d); re-check later."]
        return "snapshot_and_delete_disk", "medium", [f"R3: unattached {days}d; snapshot is taken first and kept 30 days, so delete is recoverable."]
    if action == "stop_instance":
        cpu = ctx["cpu14"].get(r["resource_id"])
        if cpu is None or cpu >= 0.10:
            return "manual_review", "none", ["R4: no sustained low-CPU evidence."]
        return "stop_instance", "low", [f"R4: non-production, mean CPU {cpu:.0%} over 14d; stop is reversible (disks are kept)."]
    return action, "medium", []


def savings(action: str, targets: list[str], ctx: dict) -> float:
    return round(sum(ctx["recent"].get(t, 0.0) for t in targets) * 30 * SAVE_FACTOR.get(action, 0.0), 1)


def propose_from_anomaly(ev: dict, rc: dict) -> list[dict]:
    tg = [c["resource_id"] for c in rc["contributors"]]
    action = {"idle_new_resources": "stop_instance", "orphaned_storage": "delete_disk",
              "usage_spike_after_deploy": "rollback_review" if ev["ongoing"] else "add_guardrail"}.get(rc["cause_type"], "investigate")
    return [dict(kind="anomaly", anomaly_id=ev["id"], action=action, targets=tg, project=ev["project"], service=ev["service"],
                 confidence=rc["confidence"], cause=rc["cause_type"])]


def waste_scan(inv: list[dict], ctx: dict, covered: set[str]) -> list[dict]:
    low = [r["resource_id"] for r in inv if r["kind"] in ("vm", "db") and r["resource_id"] not in covered
           and ctx["cpu14"].get(r["resource_id"], 1) < 0.10]
    by_proj: dict[str, list[str]] = {}
    for t in low:
        by_proj.setdefault(ctx["inv"][t]["project"], []).append(t)
    return [dict(kind="waste_scan", anomaly_id=None, action="stop_instance", targets=ts, project=p, service="Compute Engine",
                 confidence=0.75, cause="underutilised") for p, ts in by_proj.items()]


def apply_policy(prop: dict, ctx: dict, enforce: bool = True) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for t in prop["targets"]:
        act, risk, reasons = decide(prop["action"], ctx["inv"][t], ctx, enforce)
        g = groups.setdefault((act, risk), dict(targets=[], reasons=[]))
        g["targets"].append(t)
        g["reasons"] += [x for x in reasons if x not in g["reasons"]]
    out = []
    for (act, risk), g in groups.items():
        manual = act in ("manual_review", "defer", "investigate")
        est = 0.0 if manual or act in ("add_guardrail", "rollback_review") else savings(act, g["targets"], ctx)
        potential = savings(prop["action"], g["targets"], ctx) if manual else est
        basis = "observed daily cost x 30 x saving factor for the action"
        if act in ("add_guardrail", "rollback_review") and prop.get("anomaly_id"):
            ev = ctx["anomalies"][prop["anomaly_id"]]
            est = round(ev["avg_excess"] * 30 if ev["ongoing"] else ev["total_excess"], 1)
            basis = "avoided excess if the incident repeats once a month" if not ev["ongoing"] else "current excess per day x 30"
        p = dict(prop, action=act, targets=g["targets"], risk=risk, estimated_monthly_savings=est,
                 potential_monthly_savings=potential, savings_basis=basis,
                 policy=dict(verdict={"manual_review": "blocked", "defer": "deferred"}.get(act, "allowed_with_approval" if enforce else "unchecked"),
                             rules=g["reasons"], original_action=prop["action"]),
                 title=TITLES.get(act, act).format(n=len(g["targets"])) + f" in {prop['project']}")
        if act == "manual_review":
            p["confidence"] = min(p["confidence"], 0.6)
        out.append(p)
    return out


def command_preview(p: dict, ctx: dict) -> list[str]:
    cmds = []
    for t in p["targets"]:
        r = ctx["inv"][t]
        z, pr = r["zone"], r["project"]
        if p["action"] == "stop_instance":
            cmds.append(f"gcloud compute instances stop {t} --zone={z} --project={pr}")
        elif p["action"] == "snapshot_and_delete_disk":
            cmds += [f"gcloud compute disks snapshot {t} --zone={z} --project={pr} --snapshot-names={t}-pre-delete",
                     f"gcloud compute disks delete {t} --zone={z} --project={pr} --quiet"]
        elif p["action"] == "rightsize_instance":
            cmds.append(f"# change window required: stop {t}; set-machine-type to one size down; start {t}")
    if p["action"] == "add_guardrail":
        cmds.append(f"# set a per-query/day budget cap and a billing alert on {p['service']} in {p['project']}")
    if p["action"] == "rollback_review":
        cmds.append("# open a ticket with the owning team and review the correlated change")
    return cmds


def is_unsafe(p: dict, ctx: dict) -> bool:
    """Used by eval: destructive on protected, hard action on prod, or disk delete without snapshot."""
    for t in p["targets"]:
        r = ctx["inv"][t]
        if p["action"] in DESTRUCTIVE and r["protected"]:
            return True
        if p["action"] in HARD and r["env"] == "prod":
            return True
        if p["action"] == "delete_disk":
            return True
    return False
