"""Load real Google Cloud billing export from BigQuery into the same schema as data.load(). Set SOURCE=bigquery."""
import os
import pandas as pd


def load_bigquery() -> dict:
    from google.cloud import bigquery
    table = os.environ["BILLING_TABLE"]          # e.g. my-proj.billing.gcp_billing_export_v1_XXXX
    days = int(os.getenv("LOOKBACK_DAYS", "120"))
    sql = f"""
      SELECT DATE(usage_start_time) AS date, project.id AS project, service.description AS service,
             sku.description AS sku, COALESCE(resource.name, resource.global_name, sku.id) AS resource_id,
             SUM(usage.amount) AS usage_amount,
             SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS cost
      FROM `{table}`
      WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
      GROUP BY 1,2,3,4,5"""
    bill = bigquery.Client().query(sql).to_dataframe()
    bill["date"] = pd.to_datetime(bill["date"])
    # TODO: fill inventory/metrics from Cloud Asset Inventory + Cloud Monitoring (compute.googleapis.com/instance/cpu/utilization)
    return dict(bill=bill, inventory=[], metrics=pd.DataFrame(columns=["date", "resource_id", "cpu_util", "attached"]),
                events=pd.DataFrame(columns=["date", "project", "description"]), truth={})
