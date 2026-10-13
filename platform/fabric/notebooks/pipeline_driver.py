# Fabric notebook source
# ═══════════════════════════════════════════════════════════════════════════════
# Notebook  : pipeline_driver — Registry-driven Medallion Pipeline (Fabric)
# Purpose   : Thin, parameterisable runner. Loads a system's contract registry from
#             OneLake Files and runs bronze → silver → gold in DAG order with the
#             LakeLogic Spark engine, writing Delta tables into the domain schema of
#             the attached schema-enabled Lakehouse.
#
# Requires  : the schema-enabled `rideflow_demo` lakehouse attached as DEFAULT.
# ═══════════════════════════════════════════════════════════════════════════════

# CELL ********************
# Install LakeLogic + deps for a Fabric JOB run. `%pip` magic cancels the session
# in job runs, and the base runtime ships an older typing_extensions than lakelogic
# needs — so install with --target, prepend it to sys.path, and drop stale copies.
import subprocess, sys
_TARGET = "/tmp/lakelogic_libs"
# Install lakelogic + its deps (pip resolves polars/deltalake at lakelogic's pinned
# versions — do NOT --upgrade them explicitly, that pulls incompatible native
# builds and crashes the session). pdfplumber is pure-python (checkr PDF extract).
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "--target", _TARGET,
                       "lakelogic", "typing_extensions", "pyyaml", "pdfplumber"])
if _TARGET not in sys.path:
    sys.path.insert(0, _TARGET)
for _m in list(sys.modules):
    if _m.split(".")[0] in ("lakelogic", "pydantic", "pydantic_core", "typing_extensions"):
        del sys.modules[_m]

def _mark(step):
    try:
        import notebookutils
        notebookutils.fs.put("Files/_logs/pd_progress.txt", step + "\n", True)
    except Exception:
        pass
_mark("1_install_done")

# CELL ********************
# Parameters cell (overridden by the deploy pipeline / per-domain run).
# Fabric injects ALL parameters as STRINGS; numeric/boolean ones are coerced below.
# ── Target ──────────────────────────────────────────────────────────────────
lakehouse_name = "rideflow_demo"
domain = "marketplace"
system = "rideflow"
environment = "dev"                     # dev | staging | prod
engine = "spark"                        # spark | polars | duckdb
storage_mode = "uc"                     # uc (schema-enabled lakehouse) | direct
# ── Layers & scope ──────────────────────────────────────────────────────────
target_layers = "bronze, silver, gold"  # layers to process
entity_filter = ""                      # comma-separated entities to restrict to
reset_layers = "bronze, silver, gold"   # DROP these layers before processing
reload_layers = ""                      # TRUNCATE these layers before processing
dry_run = "false"                       # validate contracts, write nothing
ddl_only = "false"                      # create tables only, no data
# ── Reprocessing ────────────────────────────────────────────────────────────
reprocess_from = ""                     # window start (inclusive)
reprocess_to = ""                       # window end (inclusive)
reprocess_column = ""                   # column to reprocess on
reprocess_values = ""                   # comma-separated ids to reprocess
# ── Lineage / display ───────────────────────────────────────────────────────
created_by = ""                         # stamped into lineage metadata
show_dag = "false"                      # displayHTML isn't available in job runs
# ── Execution / resilience ──────────────────────────────────────────────────
parallel = "false"                      # run independent contracts concurrently
max_workers = "4"                       # max parallel threads
retry_attempts = "1"                    # per-contract retries
retry_wait_sec = "30"                   # base wait between retries (seconds)
timeout_min = "0"                       # per-entity timeout (0 = none)
max_failures = "0"                      # stop after N consecutive failures (0 = unlimited)
resume_run = ""                         # resume a previous pipeline run id

# CELL ********************
import os

# LakeLogic resolves {catalog} from FABRIC_<ENV>_LAKEHOUSE. On Fabric the "catalog"
# is the schema-enabled lakehouse; contracts then resolve {catalog}.<domain>.<table>
# to Spark tables in the attached lakehouse (the OneLake-backed catalog).
os.environ[f"FABRIC_{environment.upper()}_LAKEHOUSE"] = lakehouse_name

REGISTRY_PATH = f"/lakehouse/default/Files/_contracts/{domain}/{system}/_system.yaml"
print(f"Registry: {REGISTRY_PATH}  (env: {environment})")

# Fabric passes parameters as strings — coerce the numeric/boolean ones.
DRY_RUN = str(dry_run).strip().lower() == "true"
DDL_ONLY = str(ddl_only).strip().lower() == "true"
SHOW_DAG = str(show_dag).strip().lower() == "true"
PARALLEL = str(parallel).strip().lower() == "true"
MAX_WORKERS = int(str(max_workers).strip() or "4")
RETRY_ATTEMPTS = int(str(retry_attempts).strip() or "1")
RETRY_WAIT = int(str(retry_wait_sec).strip() or "30")
TIMEOUT_MIN = int(str(timeout_min).strip() or "0")
MAX_FAILURES = int(str(max_failures).strip() or "0")
REPROCESS_VALUES = [v.strip() for v in str(reprocess_values).split(",") if v.strip()] or None
_mark("2_env_done")

# CELL ********************
import traceback as _tb0


def _cap0(name, text):
    print(text)
    try:
        import notebookutils as _nu
        _nu.fs.put(f"Files/_logs/{name}.txt", text, True)
    except Exception:
        pass


try:
    from lakelogic.core.registry import DomainRegistry
    from lakelogic.pipeline import LakehousePipeline
    registry = DomainRegistry.from_yaml(REGISTRY_PATH, environment=environment, storage_mode=storage_mode)
    pipeline = LakehousePipeline(registry, engine=engine, spark=spark)
    if SHOW_DAG:
        try:
            displayHTML(pipeline.visualize_dag())  # noqa: F821 (Fabric global)
        except Exception:
            pass
    _mark("3_init_done")
except Exception:
    _cap0(f"pipeline_init_{domain}_{system}", _tb0.format_exc())
    raise

# CELL ********************
import traceback as _traceback


def _capture(name, text):
    print(text)
    try:
        import notebookutils as _nu
        _nu.fs.put(f"Files/_logs/{name}_error.txt", text, True)
        return
    except Exception:
        pass
    try:
        import os as _os
        _os.makedirs("/lakehouse/default/Files/_logs", exist_ok=True)
        open(f"/lakehouse/default/Files/_logs/{name}_error.txt", "w", encoding="utf-8").write(text)
    except Exception:
        pass


try:
    summary = pipeline.run(
        target_layers=target_layers,
        reset_layers=reset_layers,
        reload_layers=reload_layers,
        dry_run=DRY_RUN,
        entity_filter=entity_filter,
        environment=environment,
        reprocess_from=reprocess_from or None,
        reprocess_to=reprocess_to or None,
        reprocess_column=reprocess_column or None,
        reprocess_values=REPROCESS_VALUES,
        parallel=PARALLEL,
        max_workers=MAX_WORKERS,
        retry_attempts=RETRY_ATTEMPTS,
        retry_base_wait_seconds=RETRY_WAIT,
        entity_timeout_minutes=TIMEOUT_MIN if TIMEOUT_MIN > 0 else None,
        max_consecutive_failures=MAX_FAILURES,
        resume_from_run=resume_run or None,
        ddl_only=DDL_ONLY,
        created_by=created_by or None,
    )
except Exception:
    _capture(f"pipeline_{domain}_{system}", _traceback.format_exc())
    raise

print("\n" + "=" * 70)
print("PIPELINE RUN SUMMARY")
print("=" * 70)
print(f"  Run     : {summary.run_id}")
print(f"  Env     : {summary.environment}")
print(f"  {'Contract':<45} {'Layer':<8} {'Status':<14} Rows")
print(f"  {'-'*45} {'-'*8} {'-'*14} ----")
for r in summary.results:
    print(f"  {r.get('contract','?'):<45} {r.get('layer','?'):<8} {r.get('status','?'):<14} {r.get('rows','-')}")
    if r.get("error"):
        print(f"    ↳ {r['error']}")
print("=" * 70)

# Surface real per-contract errors so the Fabric pipeline activity fails loudly.
_failed = [r for r in summary.results if r.get("status") == "failed"]
if _failed:
    _details = " | ".join(
        f"{r.get('contract') or r.get('table_name')} [{r.get('layer')}]: "
        f"{(str(r.get('error') or 'see logs').strip().splitlines() or ['see logs'])[0][:300]}"
        for r in _failed
    )
    raise RuntimeError(f"{len(_failed)} contract(s) failed — {_details}")
