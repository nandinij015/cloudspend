"""Scores the system against injected ground truth. Run: python -m eval.evaluate"""
import json
from pathlib import Path
import pandas as pd
from cloudspend import data, pipeline, policy as PL


def overlap(a, b):
    return pd.Timestamp(a["start"]) <= pd.Timestamp(b["end"]) + pd.Timedelta(days=1) and pd.Timestamp(b["start"]) <= pd.Timestamp(a["end"]) + pd.Timedelta(days=1)


def main():
    src = data.load(regenerate=True)
    truth = src["truth"]["anomalies"]
    st = pipeline.run(src=src)
    det = st["anomalies"]
    matched, tp = {}, 0
    for a in det:
        m = next((t for t in truth if t["project"] == a["project"] and t["service"] == a["service"] and overlap(a, t)), None)
        if m:
            tp += 1
            matched[m["id"]] = a
    precision, recall = tp / max(len(det), 1), len(matched) / len(truth)
    rc_res = sum(set(t["resources"]) <= {c["resource_id"] for c in matched[t["id"]]["root_cause"]["contributors"]} for t in truth if t["id"] in matched)
    sav = []
    for t in truth:
        a = matched.get(t["id"])
        rec = next((r for r in st["recommendations"] if r.get("anomaly_id") == (a or {}).get("id") and r["estimated_monthly_savings"] > 0), None)
        if rec and t["true_monthly_savings"]:
            sav.append(dict(truth=t["id"], estimated=rec["estimated_monthly_savings"], actual=t["true_monthly_savings"],
                            err_pct=round(abs(rec["estimated_monthly_savings"] - t["true_monthly_savings"]) / t["true_monthly_savings"] * 100, 1)))
    naive = pipeline.run(enforce_policy=False, src=src)
    unsafe_on = sum(PL.is_unsafe(r, st["ctx"]) for r in st["recommendations"])
    unsafe_off = sum(PL.is_unsafe(r, naive["ctx"]) for r in naive["recommendations"])
    rep = dict(anomaly_precision=round(precision, 2), anomaly_recall=round(recall, 2), detected=len(det), injected=len(truth),
               root_cause_type_accuracy=round(sum(matched[t["id"]]["root_cause"]["cause_type"] == t["cause"] for t in truth if t["id"] in matched) / len(truth), 2),
               root_cause_resource_recall=round(rc_res / len(truth), 2), savings_estimates=sav,
               mean_savings_error_pct=round(sum(s["err_pct"] for s in sav) / max(len(sav), 1), 1),
               unsafe_recommendations_with_policy=unsafe_on, unsafe_recommendations_naive_baseline=unsafe_off)
    Path(__file__).with_name("report.json").write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep, indent=2))
    assert unsafe_on == 0, "policy let an unsafe recommendation through"


if __name__ == "__main__":
    main()
