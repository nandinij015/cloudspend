"""Synthetic billing data with injected anomalies + ground truth (swap for BigQuery via bq_source.py)."""
from __future__ import annotations
import json
from pathlib import Path
import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
START, DAYS = pd.Timestamp("2026-08-01"), 105
ZONE = "asia-south1-a"


def d(i: int) -> str:
    return (START + pd.Timedelta(days=int(i))).strftime("%Y-%m-%d")


def _inventory() -> list[dict]:
    inv: list[dict] = []

    def add(rid, project, service, rtype, env, daily, cpu=None, kind="fixed", protected=False,
            created=0, sku="", attached=None, size_gb=None):
        inv.append(dict(resource_id=rid, project=project, service=service, type=rtype, env=env,
                        daily_cost=daily, cpu_base=cpu, kind=kind, protected=protected,
                        created_day=created, sku=sku or rtype, attached=attached, size_gb=size_gb,
                        zone=ZONE, unattached_since=None))

    P, S, A, R = "acme-prod", "acme-staging", "acme-analytics", "acme-research"
    for i in range(1, 5):
        add(f"prod-api-{i}", P, "Compute Engine", "n2-standard-8", "prod", 9.36, 0.62, "vm", sku="N2 Instance Core/RAM")
    add("prod-batch-1", P, "Compute Engine", "n2-standard-16", "prod", 18.72, 0.09, "vm", sku="N2 Instance Core/RAM")
    add("prod-db-main", P, "Cloud SQL", "db-n2-standard-8", "prod", 31.0, 0.41, "db", protected=True, sku="Cloud SQL vCPU/RAM")
    add("prod-assets", P, "Cloud Storage", "bucket", "prod", 14.0, kind="storage", sku="Standard Storage")
    add("prod-egress", P, "Networking", "egress", "prod", 90.0, kind="usage", sku="Network Egress Internet")
    for i in (1, 2):
        add(f"staging-app-{i}", S, "Compute Engine", "e2-standard-4", "staging", 3.22, 0.07, "vm", sku="E2 Instance Core/RAM")
    add("staging-legacy-sftp", S, "Compute Engine", "e2-standard-2", "staging", 1.61, 0.03, "vm", protected=True, sku="E2 Instance Core/RAM")
    add("staging-loadtest-1", S, "Compute Engine", "e2-standard-8", "staging", 6.44, 0.05, "vm", sku="E2 Instance Core/RAM")
    add("bq-analytics-dashboards", A, "BigQuery", "bigquery", "analytics", 120.0, kind="usage", sku="Analysis (on-demand)")
    add("analytics-notebook-1", A, "Compute Engine", "e2-standard-4", "analytics", 3.22, 0.35, "vm", sku="E2 Instance Core/RAM")
    add("analytics-lake", A, "Cloud Storage", "bucket", "analytics", 22.0, kind="storage", sku="Standard Storage")
    add("ml-notebook-1", R, "Compute Engine", "n1-standard-8", "research", 8.0, 0.30, "vm", sku="N1 Instance Core/RAM")
    add("ml-data", R, "Cloud Storage", "bucket", "research", 9.0, kind="storage", sku="Standard Storage")
    # --- injected anomaly resources ---
    for i in (1, 2, 3):  # GT-1: GPU instances launched and forgotten
        add(f"ml-gpu-exp-{i}", R, "Compute Engine", "g2-standard-8", "research", 20.4, 0.02, "vm", created=72, sku="G2 Instance Core/RAM")
    for i in range(1, 13):  # GT-4: orphaned SSD disks after cluster teardown
        add(f"analytics-orphan-disk-{i:02d}", A, "Compute Engine", "pd-ssd", "analytics", 2.83, None, "disk",
            created=85, sku="SSD backed PD Capacity", attached=False, size_gb=500)
        inv[-1]["unattached_since"] = d(85)
    return inv


SPIKES = {("bq-analytics-dashboards", k): 3.5 for k in range(80, 84)}          # GT-2
SPIKES.update({("prod-egress", k): 2.8 for k in range(90, 96)})                # GT-3

EVENTS = [
    (55, "acme-prod", "Deploy api-v3.1 (no cost impact)"),
    (72, "acme-research", "terraform apply: gpu-exp node pool (3x g2-standard-8)"),
    (79, "acme-analytics", "Release dashboards-v2.14 (removed partition filter on events table)"),
    (85, "acme-analytics", "Dataproc cluster 'etl-batch' torn down"),
    (90, "acme-prod", "CDN config change: cache TTL reduced 3600s -> 60s"),
]


def generate(seed: int = 42) -> dict:
    rng = np.random.default_rng(seed)
    inv = _inventory()
    rows, met = [], []
    for r in inv:
        for day in range(r["created_day"], DAYS):
            wk = (START + pd.Timedelta(days=day)).weekday()
            if r["kind"] == "usage":
                cost = r["daily_cost"] * (1.0 if wk < 5 else 0.65) * rng.normal(1, 0.04) * SPIKES.get((r["resource_id"], day), 1.0)
            elif r["kind"] == "storage":
                cost = r["daily_cost"] * (1 + 0.001 * day) * rng.normal(1, 0.004)
            else:
                cost = r["daily_cost"] * rng.normal(1, 0.004)
            unit_cost = {"usage": 6.25, "storage": 0.02}.get(r["kind"], 0.04)
            rows.append((d(day), r["project"], r["service"], r["sku"], r["resource_id"], round(cost / unit_cost, 3), round(cost, 4)))
            if r["kind"] in ("vm", "db"):
                met.append((d(day), r["resource_id"], float(np.clip(r["cpu_base"] + rng.normal(0, 0.02), 0.005, 1)), None))
            elif r["kind"] == "disk":
                met.append((d(day), r["resource_id"], None, False))
    bill = pd.DataFrame(rows, columns=["date", "project", "service", "sku", "resource_id", "usage_amount", "cost"])
    metrics = pd.DataFrame(met, columns=["date", "resource_id", "cpu_util", "attached"])
    events = pd.DataFrame([(d(i), p, t) for i, p, t in EVENTS], columns=["date", "project", "description"])
    gpu = sum(r["daily_cost"] for r in inv if r["resource_id"].startswith("ml-gpu"))
    disks = sum(r["daily_cost"] for r in inv if "orphan-disk" in r["resource_id"])
    truth = dict(anomalies=[
        dict(id="GT-1", project="acme-research", service="Compute Engine", start=d(72), end=d(DAYS - 1), cause="idle_new_resources",
             resources=[f"ml-gpu-exp-{i}" for i in (1, 2, 3)], true_monthly_savings=round(gpu * 30, 1)),
        dict(id="GT-2", project="acme-analytics", service="BigQuery", start=d(80), end=d(83), cause="usage_spike_after_deploy",
             resources=["bq-analytics-dashboards"], true_monthly_savings=None),
        dict(id="GT-3", project="acme-prod", service="Networking", start=d(90), end=d(95), cause="usage_spike_after_deploy",
             resources=["prod-egress"], true_monthly_savings=None),
        dict(id="GT-4", project="acme-analytics", service="Compute Engine", start=d(85), end=d(DAYS - 1), cause="orphaned_storage",
             resources=[f"analytics-orphan-disk-{i:02d}" for i in range(1, 13)], true_monthly_savings=round(disks * 30 * 0.75, 1)),
    ], protected=[r["resource_id"] for r in inv if r["protected"] or r["env"] == "prod"])
    # spike incidents: true avoided cost = injected excess (basis: one recurrence avoided per month)
    for a in truth["anomalies"]:
        if a["true_monthly_savings"] is None:
            r = next(x for x in inv if x["resource_id"] == a["resources"][0])
            tot = 0.0
            for (res, day), m in SPIKES.items():
                if res == r["resource_id"]:
                    wk = (START + pd.Timedelta(days=day)).weekday()
                    tot += r["daily_cost"] * (1.0 if wk < 5 else 0.65) * (m - 1)
            a["true_monthly_savings"] = round(tot, 1)
    return dict(bill=bill, inventory=inv, metrics=metrics, events=events, truth=truth)


def load(regenerate: bool = False) -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    f = DATA_DIR / "billing.csv"
    if regenerate or not f.exists():
        g = generate()
        g["bill"].to_csv(f, index=False)
        g["metrics"].to_csv(DATA_DIR / "metrics.csv", index=False)
        g["events"].to_csv(DATA_DIR / "events.csv", index=False)
        (DATA_DIR / "inventory.json").write_text(json.dumps(g["inventory"], indent=1))
        (DATA_DIR / "truth.json").write_text(json.dumps(g["truth"], indent=1))
    rd = lambda n: pd.read_csv(DATA_DIR / n, parse_dates=["date"])
    return dict(bill=rd("billing.csv"), metrics=rd("metrics.csv"), events=rd("events.csv"),
                inventory=json.loads((DATA_DIR / "inventory.json").read_text()),
                truth=json.loads((DATA_DIR / "truth.json").read_text()))


if __name__ == "__main__":
    s = load(regenerate=True)
    print(f"billing rows={len(s['bill'])} resources={len(s['inventory'])} -> {DATA_DIR}")
