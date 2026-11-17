"""Anomaly detection: robust (median/MAD) rolling baseline per project x service; baseline excludes flagged days."""
from __future__ import annotations
import numpy as np
import pandas as pd


def detect(bill: pd.DataFrame, window=28, z=4.0, min_abs=15.0, min_pct=0.25, min_base_days=14) -> list[dict]:
    daily = bill.groupby(["project", "service", "date"], as_index=False)["cost"].sum()
    dates = pd.date_range(bill["date"].min(), bill["date"].max())
    out = []
    for (proj, svc), g in daily.groupby(["project", "service"]):
        v = g.set_index("date")["cost"].reindex(dates, fill_value=0.0).to_numpy()
        flag = np.zeros(len(v), bool)
        zs = np.zeros(len(v))
        base_hist = np.zeros(len(v))
        for t in range(min_base_days, len(v)):
            idx, i = [], t - 1
            while i >= 0 and len(idx) < window:
                if not flag[i]:
                    idx.append(i)
                i -= 1
            if len(idx) < 7:
                continue
            b = v[idx]
            med = float(np.median(b))
            sigma = max(float(np.median(np.abs(b - med))) * 1.4826, 0.03 * med, 1.0)
            ex = v[t] - med
            zs[t], base_hist[t] = ex / sigma, med
            if ex / sigma >= z and ex >= min_abs and ex >= min_pct * max(med, 1.0):
                flag[t] = True
        runs, cur = [], []
        for i in np.flatnonzero(flag):
            if cur and i - cur[-1] > 2:
                runs.append(cur); cur = []
            cur.append(i)
        if cur:
            runs.append(cur)
        for r in runs:
            s, e = r[0], r[-1]
            base = float(base_hist[s])
            seg = v[s:e + 1]
            out.append(dict(project=proj, service=svc, start=dates[s].strftime("%Y-%m-%d"), end=dates[e].strftime("%Y-%m-%d"),
                            days=int(e - s + 1), ongoing=bool(e == len(v) - 1), baseline_daily=round(base, 2),
                            avg_daily=round(float(seg.mean()), 2), avg_excess=round(float(seg.mean() - base), 2),
                            total_excess=round(float((seg - base).sum()), 2), peak_z=round(float(zs[s:e + 1].max()), 1),
                            pct_over_baseline=round(float((seg.mean() / max(base, 1e-9) - 1) * 100), 1)))
    out.sort(key=lambda a: (a["start"], -a["total_excess"]))
    for n, a in enumerate(out, 1):
        a["id"] = f"AN-{n:03d}"
    return out
