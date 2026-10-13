# Fabric notebook source
# ═══════════════════════════════════════════════════════════════════════════════
# Notebook  : 00_setup — Provision the RideFlow demo in a Fabric Lakehouse
# Purpose   : Idempotent bootstrap — one schema per domain + a `quarantine` schema
#             + OneLake Files folders + per-domain run-log tables, all inside the
#             attached schema-enabled Lakehouse (addressed 2-part: schema.table).
#
# Requires  : the schema-enabled lakehouse attached as this notebook's DEFAULT.
# On any failure the full traceback is written to Files/_logs/setup_error.txt so
# it can be inspected (the Fabric job API only returns a generic error).
# ═══════════════════════════════════════════════════════════════════════════════

# CELL ********************
# Parameters cell — overridden by the deploy when the cell is tagged 'parameters';
# only used for the final print, so the default is harmless.
lakehouse_name = "rideflow_dev_demo"

# CELL ********************
import os
import traceback

try:
    import notebookutils  # Fabric runtime
except Exception:
    notebookutils = None

FILES = "/lakehouse/default/Files"


def _put_log(rel_path, text):
    """Best-effort write to OneLake Files (notebookutils first, FUSE fallback)."""
    try:
        notebookutils.fs.put(f"Files/{rel_path}", text, True)
        return
    except Exception:
        pass
    try:
        p = f"{FILES}/{rel_path}"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write(text)
    except Exception:
        pass


def _list_dir(rel):
    """List child dir names under Files/<rel> — notebookutils first (works in job
    runs), FUSE os.listdir fallback."""
    names = []
    try:
        for f in notebookutils.fs.ls(f"Files/{rel}"):
            if getattr(f, "isDir", False):
                names.append(f.name.rstrip("/"))
        if names:
            return sorted(names)
    except Exception:
        pass
    try:
        base = f"{FILES}/{rel}"
        names = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
    except Exception:
        pass
    return sorted(names)


def _sql(stmt):
    print(f"  → {stmt}")
    spark.sql(stmt)


_RUN_LOG_COLS = """(
  pipeline_run_id STRING, run_id STRING, timestamp STRING, start_time STRING,
  end_time STRING, run_duration_seconds DOUBLE, engine STRING, contract STRING,
  contract_version STRING, stage STRING, dataset STRING, domain STRING,
  system STRING, environment STRING, data_layer STRING, status STRING,
  error_message STRING, source_path STRING, counts_source BIGINT,
  counts_total BIGINT, counts_good BIGINT, counts_quarantined BIGINT,
  quarantine_ratio DOUBLE, estimated_cost DOUBLE, cost_currency STRING,
  cost_confidence STRING, max_source_mtime DOUBLE, max_watermark_value STRING,
  dlt_state_json STRING, slo_json STRING, report_json STRING
)"""

try:
    domains = _list_dir("_contracts")
    print(f"🌐 Domains discovered: {domains}")
    if not domains:
        raise RuntimeError(
            "No domains under Files/_contracts — the deploy upload step must run "
            "before setup (or the lakehouse isn't attached as default)."
        )

    # Schemas — 2-part (created in the attached default lakehouse).
    _sql("CREATE SCHEMA IF NOT EXISTS quarantine")
    for domain in domains:
        _sql(f"CREATE SCHEMA IF NOT EXISTS `{domain}`")
        _sql(f"CREATE TABLE IF NOT EXISTS `{domain}`._pipeline_run_log {_RUN_LOG_COLS} USING DELTA")

    # OneLake Files folders for landing + logs.
    for sub in ["_logs"] + [f"landing_{d}" for d in domains]:
        try:
            notebookutils.fs.mkdirs(f"Files/{sub}")
        except Exception:
            os.makedirs(f"{FILES}/{sub}", exist_ok=True)
        print(f"  ✓ Files/{sub}")

    print("═" * 60)
    print(f"  Provisioned lakehouse '{lakehouse_name}':")
    print(f"  Schemas : quarantine, {', '.join(domains)}")
    print("═" * 60)
except Exception:
    tb = traceback.format_exc()
    print(tb)
    _put_log("_logs/setup_error.txt", tb)
    raise
