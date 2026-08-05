#!/usr/bin/env python
"""Deploy the RideFlow LakeLogic data mesh into Microsoft Fabric.

No Fabric-specific SDK required — this drives the public Fabric REST API and the
OneLake DFS API directly, authenticating with tokens from the Azure CLI
(`az account get-access-token`). It:

  1. resolves (or creates) the target workspace on a Fabric capacity
  2. creates the schema-enabled Lakehouse that acts as the catalog
  3. uploads the contract registry (domains_rideflow/) into the lakehouse
     OneLake Files under _contracts/
  4. imports the three notebooks (00_setup, test_data_driver, pipeline_driver),
     attaching the lakehouse as their default
  5. runs 00_setup, then per-domain test_data + pipeline, end to end

Prerequisites
-------------
  • Azure CLI logged in (`az login`) as an identity with Fabric workspace admin
    and access to a Fabric capacity.
  • `pip install pyyaml requests`

Usage
-----
  python fabric/deploy/deploy.py                 # full bootstrap
  python fabric/deploy/deploy.py --setup-only    # provision, upload, import; no run
  python fabric/deploy/deploy.py --domain marketplace/rideflow   # one system
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

# The script prints Unicode (box drawing, ✓, ▶). Force UTF-8 so the Windows
# console (cp1252 by default) doesn't crash on them.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parents[2]
FAB_API = "https://api.fabric.microsoft.com/v1"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"


# ── auth ──────────────────────────────────────────────────────────────────────
def _token(resource: str) -> str:
    # shell=True so Windows resolves `az` -> `az.cmd` via PATHEXT (Python's bare
    # subprocess can't). `resource` is a fixed https URL, so no injection surface.
    out = subprocess.run(
        f'az account get-access-token --resource {resource} --query accessToken -o tsv',
        capture_output=True, text=True, shell=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        sys.exit(f"Could not get a token for {resource}. Run `az login` first.\n{out.stderr}")
    return out.stdout.strip()


def _hdr(tok: str, ct: str | None = "application/json") -> dict:
    h = {"Authorization": f"Bearer {tok}"}
    if ct:
        h["Content-Type"] = ct
    return h


def _poll_lro(resp: requests.Response, tok: str) -> dict:
    """Follow a Fabric long-running-operation to completion."""
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"{resp.status_code}: {resp.text[:500]}")
    if resp.status_code != 202:
        return resp.json() if resp.text else {}
    op = resp.headers.get("Location")
    while True:
        time.sleep(3)
        r = requests.get(op, headers=_hdr(tok))
        st = r.json().get("status")
        if st in ("Succeeded", "Completed"):
            res = requests.get(op + "/result", headers=_hdr(tok))
            return res.json() if res.text else {}
        if st in ("Failed", "Undefined"):
            raise RuntimeError(f"Operation failed: {r.text[:500]}")


# ── Fabric items ──────────────────────────────────────────────────────────────
def get_or_create_workspace(tok: str, name: str, capacity_id: str) -> str:
    r = requests.get(f"{FAB_API}/workspaces", headers=_hdr(tok))
    r.raise_for_status()
    for w in r.json().get("value", []):
        if w.get("displayName") == name:
            print(f"  ✓ workspace '{name}' exists ({w['id']})")
            return w["id"]
    if not capacity_id:
        sys.exit(f"Workspace '{name}' not found and no capacity_id given to create it.\n"
                 "Set workspace.capacity_id in fabric/fabric_variables.yaml (a Fabric capacity GUID).")
    body = {"displayName": name, "capacityId": capacity_id}
    r = requests.post(f"{FAB_API}/workspaces", headers=_hdr(tok), json=body)
    r.raise_for_status()
    wid = r.json()["id"]
    print(f"  ✓ created workspace '{name}' ({wid})")
    return wid


def get_or_create_lakehouse(tok: str, wid: str, name: str, schema_enabled: bool) -> str:
    r = requests.get(f"{FAB_API}/workspaces/{wid}/lakehouses", headers=_hdr(tok))
    r.raise_for_status()
    for lh in r.json().get("value", []):
        if lh.get("displayName") == name:
            lid = lh["id"]
            # Verify an EXISTING lakehouse is schema-enabled — the medallion needs
            # one schema per domain. A non-schema lakehouse would fail later on the
            # first CREATE SCHEMA with an opaque error, so stop early and clearly.
            if schema_enabled:
                detail = requests.get(
                    f"{FAB_API}/workspaces/{wid}/lakehouses/{lid}", headers=_hdr(tok)
                )
                props = (detail.json() or {}).get("properties", {}) if detail.ok else {}
                if not props.get("defaultSchema"):
                    sys.exit(
                        f"Lakehouse '{name}' exists but is NOT schema-enabled "
                        f"(no defaultSchema). This demo needs a schema-enabled "
                        f"lakehouse (one schema per domain).\n"
                        f"Fix: delete it and recreate with 'Lakehouse schemas' ON, "
                        f"or set lakehouse.name in fabric_variables.yaml to a NEW "
                        f"name so the deploy creates a schema-enabled one."
                    )
            print(f"  ✓ lakehouse '{name}' exists ({lid})")
            return lid
    body = {"displayName": name}
    if schema_enabled:
        body["creationPayload"] = {"enableSchemas": True}
    res = _poll_lro(requests.post(f"{FAB_API}/workspaces/{wid}/lakehouses",
                                  headers=_hdr(tok), json=body), tok)
    lid = res.get("id")
    print(f"  ✓ created schema-enabled lakehouse '{name}' ({lid})")
    return lid


# ── OneLake upload (contract registry) ────────────────────────────────────────
def upload_contracts(store_tok: str, workspace: str, lakehouse: str, src: Path):
    """Upload domains_rideflow/ into Files/_contracts via the OneLake DFS API."""
    base = f"{ONELAKE}/{workspace}/{lakehouse}.Lakehouse/Files/_contracts"
    n = 0
    for f in sorted(src.rglob("*")):
        if f.is_dir():
            continue
        rel = f.relative_to(src).as_posix()
        url = f"{base}/{rel}"
        data = f.read_bytes()
        # create file (PUT ?resource=file), append, flush
        requests.put(url + "?resource=file", headers=_hdr(store_tok, None)).raise_for_status()
        requests.patch(url + f"?action=append&position=0",
                       headers={**_hdr(store_tok, None), "Content-Type": "application/octet-stream"},
                       data=data).raise_for_status()
        requests.patch(url + f"?action=flush&position={len(data)}",
                       headers=_hdr(store_tok, None)).raise_for_status()
        n += 1
    print(f"  ✓ uploaded {n} contract files into Files/_contracts")


# ── notebook import (.py cell format -> .ipynb) ───────────────────────────────
def py_to_ipynb(py_path: Path, default_lakehouse_id: str, wid: str) -> dict:
    text = py_path.read_text(encoding="utf-8")
    # Normalise cell delimiters: accept both the Fabric marker and any Databricks
    # `# COMMAND ----------` markers left in ported notebooks.
    text = text.replace("# COMMAND ----------", "# CELL ********************")
    chunks = text.split("# CELL ********************")
    cells = []
    for ch in chunks:
        # A Databricks `%md` cell is ALL markdown — dropping only the `# MAGIC %md`
        # marker but unwrapping the `# MAGIC <text>` body lines would emit prose as
        # code (SyntaxError). Skip the whole markdown cell.
        if "# MAGIC %md" in ch:
            continue
        conv = []
        for ln in ch.splitlines():
            s = ln.strip()
            if s == "# Fabric notebook source":
                continue
            # Drop markdown markers (`# MAGIC %md`, `# MAGIC ## heading`, `# MAGIC`)
            # — they carry no executable code.
            if s == "# MAGIC" or s.startswith("# MAGIC %md") or s.startswith("# MAGIC #"):
                continue
            # Unwrap real magics (`# MAGIC %pip ...`, `# MAGIC %sql ...`).
            conv.append(ln[8:] if ln.startswith("# MAGIC ") else ln)
        body = "\n".join(conv).strip("\n")
        if body.strip():
            # Tag the params cell so Fabric injects run-time parameters into it.
            # (The notebooks mark it with a "# Parameters cell" comment.)
            meta = {"tags": ["parameters"]} if "Parameters cell" in body else {}
            cells.append({"cell_type": "code", "source": body.splitlines(keepends=True),
                          "metadata": meta, "outputs": [], "execution_count": None})
    return {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {
            "language_info": {"name": "python"},
            "dependencies": {
                "lakehouse": {
                    "default_lakehouse": default_lakehouse_id,
                    "default_lakehouse_workspace_id": wid,
                }
            },
        },
        "cells": cells,
    }


def ensure_folder(tok: str, wid: str, name: str):
    """Return the id of a top-level workspace folder, creating it if absent. Returns
    None if the folders API is unavailable (deploy still works, items land at root)."""
    if not name:
        return None
    try:
        r = requests.get(f"{FAB_API}/workspaces/{wid}/folders", headers=_hdr(tok))
        if r.ok:
            for f in r.json().get("value", []):
                if f.get("displayName") == name and not f.get("parentFolderId"):
                    print(f"  ✓ folder '{name}' exists")
                    return f["id"]
        c = requests.post(f"{FAB_API}/workspaces/{wid}/folders",
                          headers=_hdr(tok), json={"displayName": name})
        if c.status_code in (200, 201):
            print(f"  ✓ created folder '{name}'")
            return c.json().get("id")
        print(f"  ⚠ could not create folder '{name}' ({c.status_code}); items go to root")
    except Exception as e:
        print(f"  ⚠ folders unavailable ({e}); items go to root")
    return None


def import_notebook(tok: str, wid: str, name: str, ipynb: dict, folder_id: str | None = None) -> str:
    """Idempotently import a notebook: update its definition if one with this name
    already exists (re-running deploy must not duplicate notebooks), else create it
    (in ``folder_id`` if given)."""
    payload = base64.b64encode(json.dumps(ipynb).encode()).decode()
    definition = {
        "format": "ipynb",
        "parts": [{"path": "notebook-content.ipynb", "payload": payload,
                   "payloadType": "InlineBase64"}],
    }
    # Look for an existing notebook of the same name.
    r = requests.get(f"{FAB_API}/workspaces/{wid}/notebooks", headers=_hdr(tok))
    existing = None
    if r.ok:
        existing = next((n for n in r.json().get("value", [])
                         if n.get("displayName") == name), None)
    if existing:
        nb_id = existing["id"]
        _poll_lro(requests.post(
            f"{FAB_API}/workspaces/{wid}/notebooks/{nb_id}/updateDefinition",
            headers=_hdr(tok), json={"definition": definition}), tok)
        print(f"  ✓ updated notebook '{name}'")
        return nb_id
    body = {"displayName": name, "type": "Notebook", "definition": definition}
    if folder_id:
        body["folderId"] = folder_id
    res = _poll_lro(requests.post(
        f"{FAB_API}/workspaces/{wid}/items", headers=_hdr(tok), json=body), tok)
    print(f"  ✓ imported notebook '{name}'" + (" (Notebooks)" if folder_id else ""))
    return res.get("id")


def run_notebook(tok: str, wid: str, nb_id: str, params: dict):
    """Run a notebook on-demand and wait for it to finish."""
    body = {"executionData": {"parameters": {
        k: {"value": v, "type": "string" if isinstance(v, str) else "int"}
        for k, v in params.items()}}}
    r = requests.post(f"{FAB_API}/workspaces/{wid}/items/{nb_id}/jobs/instances?jobType=RunNotebook",
                      headers=_hdr(tok), json=body)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"run failed {r.status_code}: {r.text[:400]}")
    loc = r.headers.get("Location")
    while True:
        time.sleep(5)
        s = requests.get(loc, headers=_hdr(tok)).json()
        st = s.get("status")
        if st in ("Completed", "Succeeded"):
            return
        if st in ("Failed", "Cancelled", "Deduped"):
            raise RuntimeError(f"notebook run {st}: {json.dumps(s)[:400]}")


# ── Fabric Data Pipeline (visual orchestration) ───────────────────────────────
def _nb_activity(name, nb_id, wid, params, depends_on):
    """A Fabric 'TridentNotebook' pipeline activity."""
    act = {
        "name": name,
        "type": "TridentNotebook",
        "policy": {
            "timeout": "0.12:00:00",
            "retry": 0,
            "retryIntervalInSeconds": 30,
            "secureOutput": False,
            "secureInput": False,
        },
        "typeProperties": {
            "notebookId": nb_id,
            "workspaceId": wid,
            "parameters": {k: {"value": v, "type": "string"} for k, v in params.items()},
        },
    }
    if depends_on:
        act["dependsOn"] = [{"activity": depends_on, "dependencyConditions": ["Succeeded"]}]
    else:
        act["dependsOn"] = []
    return act


# Cross-domain run order (marketplace first so downstream FK reads find real ids).
DOMAIN_ORDER = {"marketplace": 0, "reference": 1, "payments": 2, "operations": 3,
                "marketing": 4, "shared": 9}


def _setup_pipeline_definition(nb_ids, wid, lh_name):
    """One-shot provisioning pipeline: schemas, run-log tables, Files folders."""
    return {"properties": {"activities": [
        _nb_activity("Setup", nb_ids["00_setup"], wid, {"lakehouse_name": lh_name}, None)]}}


def _system_pipeline_definition(nb_ids, wid, lh_name, env, d, s):
    """Self-contained per-system orchestrator: 0 Test Data → 1 Bronze → 2 Silver →
    3 Gold, mirroring the Databricks per-layer jobs. Each layer is its own notebook
    activity (pipeline_driver scoped to that layer) for per-layer visibility/re-runs."""
    base = {"lakehouse_name": lh_name, "domain": d, "system": s, "environment": env}
    acts = [_nb_activity("0 Test Data Generation", nb_ids["test_data_driver"], wid, base, None)]
    prev = "0 Test Data Generation"
    for num, layer, title in [("1", "bronze", "Bronze Ingestion"),
                              ("2", "silver", "Silver Processing"),
                              ("3", "gold", "Gold Aggregation")]:
        name = f"{num} {title}"
        acts.append(_nb_activity(name, nb_ids["pipeline_driver"], wid,
                                 {**base, "target_layers": layer, "reset_layers": layer}, prev))
        prev = name
    return {"properties": {"activities": acts}}


def _ordered_systems(systems):
    return sorted(systems, key=lambda ds: (DOMAIN_ORDER.get(ds[0], 5), ds[1]))


# ── Master orchestrator: run every per-system pipeline by cross-domain dependency ──
def _invoke_pipeline_activity(name, target_pid, wid, conn_id, deps):
    """A Fabric 'Invoke Pipeline' activity — runs another Data Pipeline by id and waits.
    References the child by GUID (so its display name may contain any characters) and needs
    a Fabric connection (externalReferences) — the 'Connection' field in the activity UI."""
    return {
        "name": name,
        "type": "InvokePipeline",
        "typeProperties": {
            "operationType": "InvokeFabricPipeline",
            "pipelineId": target_pid,
            "workspaceId": wid,
            "waitOnCompletion": True,
            "parameters": {},
        },
        "externalReferences": {"connection": conn_id},
        "policy": {"timeout": "0.12:00:00", "retry": 0, "retryIntervalInSeconds": 30,
                   "secureOutput": False, "secureInput": False},
        "dependsOn": [{"activity": a, "dependencyConditions": ["Succeeded"]} for a in deps],
    }


def _mesh_act_name(d, s):
    return f"{d} - {s}"   # Fabric activity names disallow '/'


def _mesh_fk_parents(d, s, present):
    """Cross-domain FK edges as a set of parent *activity names* (systems only, not Setup):
    marketplace + reference are roots; payments / marketing / operations read marketplace's
    silver ids; zendesk enriches payments charges; shared marts join every other system."""
    mp = _mesh_act_name("marketplace", "rideflow")
    pay = _mesh_act_name("payments", "stripe")
    if (d, s) == ("shared", "shared"):
        return {n for n in present if n != _mesh_act_name("shared", "shared")}
    if d in ("marketplace", "reference"):
        return set()
    if (d, s) == ("operations", "zendesk"):
        return {pay} if pay in present else ({mp} if mp in present else set())
    return {mp} if mp in present else set()


def _mesh_waves(ordered, present, max_parallel):
    """Pack the FK-ordered systems into WAVES of at most `max_parallel` each. A system joins
    the current wave only if none of its FK parents are in that same wave (parents must land
    in a strictly earlier wave); otherwise it opens the next wave. Each wave depends on the
    whole previous wave, so at most `max_parallel` notebooks run concurrently — the knob that
    keeps a run under the FTL64 trial's Spark-session ceiling. Returns [[ (d,s), ... ], ...]."""
    n = max(1, int(max_parallel))
    waves, cur, cur_names = [], [], set()
    for d, s in ordered:
        name = _mesh_act_name(d, s)
        parents = _mesh_fk_parents(d, s, present)
        if cur and (len(cur) >= n or parents & cur_names):
            waves.append(cur); cur, cur_names = [], set()
        cur.append((d, s)); cur_names.add(name)
    if cur:
        waves.append(cur)
    return waves


def _mesh_orchestrator_definition(setup_pid, sys_pids, wid, conn_id, max_parallel=2):
    """Master 'Data Mesh Orchestrator' pipeline: invokes the Setup pipeline, then every
    per-system 'Full Pipeline Orchestrator' pipeline in FK-dependency order, capped at
    `max_parallel` concurrent systems via waves (see _mesh_waves). Fabric TRIAL capacity
    (FTL64) throttles concurrent Spark/Livy sessions and rejects a wide fan-out with
    'TooManyRequestsForCapacity', so the default cap is 2; raise it on a purchased F-SKU,
    or set 1 for a fully sequential chain. Each wave's activities depend on ALL of the
    previous wave, which bounds concurrency and preserves every cross-domain FK edge
    (marketplace before its consumers, zendesk after payments, shared last).
    sys_pids = [(domain, system, pipeline_id), ...]; conn_id = Invoke-Pipeline connection GUID."""
    pid_by = {(d, s): pid for d, s, pid in sys_pids}
    ordered = _ordered_systems([(d, s) for d, s, _ in sys_pids])
    present = {_mesh_act_name(d, s) for d, s in ordered}
    acts = [_invoke_pipeline_activity("Setup", setup_pid, wid, conn_id, [])]
    if max_parallel is not None and max_parallel <= 0:
        # MAX parallelism — natural FK DAG: each system depends only on its real FK
        # parents (or Setup if it's a root). Fabric runs independent branches at once,
        # so after marketplace ~7 systems fan out. WARNING: on the FTL64 TRIAL this
        # wide fan-out throttles Spark/Livy sessions ('TooManyRequestsForCapacity');
        # it is intended for a purchased F-SKU that can schedule the concurrency.
        for d, s in ordered:
            parents = _mesh_fk_parents(d, s, present)
            deps = sorted(parents) if parents else ["Setup"]
            acts.append(_invoke_pipeline_activity(_mesh_act_name(d, s), pid_by[(d, s)], wid, conn_id, deps))
        return {"properties": {"activities": acts}}
    prev_wave = ["Setup"]
    for wave in _mesh_waves(ordered, present, max_parallel):
        for d, s in wave:
            acts.append(_invoke_pipeline_activity(
                _mesh_act_name(d, s), pid_by[(d, s)], wid, conn_id, prev_wave))
        prev_wave = [_mesh_act_name(d, s) for d, s in wave]
    return {"properties": {"activities": acts}}


def create_or_update_pipeline(tok, wid, name, definition, folder_id=None):
    payload = base64.b64encode(json.dumps(definition).encode()).decode()
    part = {"parts": [
        {"path": "pipeline-content.json", "payload": payload, "payloadType": "InlineBase64"}]}
    r = requests.get(f"{FAB_API}/workspaces/{wid}/dataPipelines", headers=_hdr(tok))
    existing = None
    if r.ok:
        existing = next((p for p in r.json().get("value", []) if p.get("displayName") == name), None)
    if existing:
        pid = existing["id"]
        _poll_lro(requests.post(
            f"{FAB_API}/workspaces/{wid}/dataPipelines/{pid}/updateDefinition",
            headers=_hdr(tok), json={"definition": part}), tok)
        print(f"  ✓ updated data pipeline '{name}'")
        return pid
    body = {"displayName": name, "type": "DataPipeline", "definition": part}
    if folder_id:
        body["folderId"] = folder_id
    res = _poll_lro(requests.post(
        f"{FAB_API}/workspaces/{wid}/items", headers=_hdr(tok), json=body), tok)
    print(f"  ✓ created data pipeline '{name}'" + (" (pipelines)" if folder_id else ""))
    return res.get("id")


def run_pipeline_wait(tok, wid, pid, label):
    """Trigger a Data Pipeline run and poll to completion. Returns final status.

    Fetches a FRESH token for every call — Fabric jobs can run for hours, well past
    a single access token's ~1h lifetime, so reusing one token silently breaks the
    poll loop. `_token` shells out to `az` (cached, near-instant)."""
    r = requests.post(f"{FAB_API}/workspaces/{wid}/items/{pid}/jobs/instances?jobType=Pipeline",
                      headers=_hdr(_token("https://api.fabric.microsoft.com")), json={})
    if r.status_code not in (200, 202):
        print(f"  ✗ {label}: could not start ({r.status_code}) {r.text[:150]}")
        return "StartFailed"
    print(f"  ▶ {label}")
    while True:
        time.sleep(20)
        t = _token("https://api.fabric.microsoft.com")
        insts = requests.get(f"{FAB_API}/workspaces/{wid}/items/{pid}/jobs/instances",
                             headers=_hdr(t)).json().get("value", [])
        s = sorted(insts, key=lambda x: x.get("startTimeUtc", ""), reverse=True)[0] if insts else {}
        st = s.get("status")
        if st in ("Completed", "Succeeded"):
            print(f"    ✓ {st}")
            return "Completed"
        if st in ("Failed", "Cancelled", "Deduped"):
            print(f"    ✗ {st}: {json.dumps(s.get('failureReason') or {})[:160]}")
            return st


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup-only", action="store_true")
    ap.add_argument("--skip-upload", action="store_true",
                    help="skip re-uploading contracts (already staged in OneLake)")
    ap.add_argument("--domain", default="", help="only run one <domain>/<system>")
    ap.add_argument("--fail-fast", action="store_true", help="stop at the first failing system")
    ap.add_argument("--pipeline", action="store_true",
                    help="create/update a Fabric Data Pipeline instead of running notebooks directly")
    ap.add_argument("--no-run", action="store_true",
                    help="with --pipeline, create it but don't trigger a run")
    ap.add_argument("--invoke-connection", default="",
                    help="Fabric connection GUID for the Data Mesh Orchestrator's Invoke "
                         "Pipeline activities (create it once in the workspace; required to "
                         "build the orchestrator)")
    ap.add_argument("--mesh-max-parallel", type=int, default=2,
                    help="Max per-system pipelines the Data Mesh Orchestrator runs at once "
                         "(FK-aware waves). Keep at 2 on the FTL64 TRIAL capacity (a wider "
                         "fan-out throttles Spark/Livy sessions → TooManyRequestsForCapacity). "
                         "Bump to 3+ on a purchased F-SKU. 1 = fully sequential.")
    args = ap.parse_args()

    cfg = yaml.safe_load((REPO / "fabric" / "fabric_variables.yaml").read_text(encoding="utf-8"))
    ws_name = cfg["workspace"]["name"]
    cap = cfg["workspace"].get("capacity_id") or ""
    lh_name = cfg["lakehouse"]["name"]
    env = cfg["bootstrap"]["environment"]

    fab_tok = _token("https://api.fabric.microsoft.com")
    store_tok = _token("https://storage.azure.com")

    print("── Provision ────────────────────────────────────────────────")
    wid = get_or_create_workspace(fab_tok, ws_name, cap)
    lid = get_or_create_lakehouse(fab_tok, wid, lh_name, cfg["lakehouse"].get("schema_enabled", True))

    if args.skip_upload:
        print("── Upload contracts ── skipped (--skip-upload) ──────────────")
    else:
        print("── Upload contracts ─────────────────────────────────────────")
        upload_contracts(store_tok, ws_name, lh_name, REPO / "domains_rideflow")

    print("── Folders ──────────────────────────────────────────────────")
    nb_folder = ensure_folder(fab_tok, wid, "Notebooks")
    pl_folder = ensure_folder(fab_tok, wid, "pipelines")

    print("── Import notebooks ─────────────────────────────────────────")
    nb_ids = {}
    for rel in cfg["notebooks"]:
        p = REPO / rel
        nb_ids[p.stem] = import_notebook(fab_tok, wid, p.stem, py_to_ipynb(p, lid, wid), nb_folder)

    # Discover systems (domain/system) from the registry — used by both the direct
    # runner and the Data Pipeline builder.
    systems = []
    for sysyaml in sorted((REPO / "domains_rideflow").rglob("_system.yaml")):
        d0 = yaml.safe_load(sysyaml.read_text(encoding="utf-8"))
        if d0.get("domain") and d0.get("system"):
            systems.append((d0["domain"], d0["system"]))
    # De-duplicate (the ported tree has both shared/_system.yaml and
    # shared/shared/_system.yaml → same (shared, shared)). Duplicate (domain,
    # system) would create duplicate pipeline activity names, which Fabric rejects.
    systems = list(dict.fromkeys(systems))

    if args.pipeline:
        print("── Data Pipelines (per-system orchestrators) ────────────────")
        tag = f"[{env}]"
        setup_name = f"{tag} RideFlow — Setup"
        setup_pid = create_or_update_pipeline(
            fab_tok, wid, setup_name,
            _setup_pipeline_definition(nb_ids, wid, lh_name), pl_folder)
        sys_pids = []
        for d, s in _ordered_systems(systems):
            name = f"{tag} {d} / {s} — Full Pipeline Orchestrator"
            pid = create_or_update_pipeline(
                fab_tok, wid, name,
                _system_pipeline_definition(nb_ids, wid, lh_name, env, d, s), pl_folder)
            sys_pids.append((d, s, pid))

        # Master orchestrator — invokes Setup + every system pipeline by FK dependency.
        # Invoke Pipeline needs a Fabric connection (create it once, pass --invoke-connection).
        mesh_name = f"{tag} RideFlow — Data Mesh Orchestrator"
        mesh_pid = None
        if args.invoke_connection:
            print("── Data Mesh Orchestrator (runs all pipelines by dependency) ──")
            mesh_pid = create_or_update_pipeline(
                fab_tok, wid, mesh_name,
                _mesh_orchestrator_definition(setup_pid, sys_pids, wid, args.invoke_connection,
                                              args.mesh_max_parallel),
                pl_folder)
        else:
            print("── Data Mesh Orchestrator ── skipped (pass --invoke-connection <GUID>) ──")

        if mesh_pid and not args.no_run:
            print("── Run the Data Mesh Orchestrator (Setup → systems by dependency) ─")
            st = run_pipeline_wait(fab_tok, wid, mesh_pid, mesh_name)
            print("\n" + "═" * 60)
            print(f"  Data Mesh Orchestrator: {st}")
            print("═" * 60)
        if mesh_pid:
            print(f"✅ Data Pipelines ready. Run '{mesh_name}' to run the whole mesh in "
                  "dependency order (one click).")
        else:
            print("✅ Per-system Data Pipelines ready. To add the master orchestrator, create a "
                  "Fabric connection for Invoke Pipeline and re-run with --invoke-connection <GUID>.")
        return

    print("── Setup ────────────────────────────────────────────────────")
    run_notebook(fab_tok, wid, nb_ids["00_setup"], {"lakehouse_name": lh_name})

    if args.setup_only:
        print("✅ setup-only complete."); return

    # discover systems (domain/system) from the registry
    systems = []
    for sysyaml in sorted((REPO / "domains_rideflow").rglob("_system.yaml")):
        d = yaml.safe_load(sysyaml.read_text(encoding="utf-8"))
        if d.get("domain") and d.get("system"):
            systems.append((d["domain"], d["system"]))
    if args.domain:
        want = tuple(args.domain.split("/"))
        systems = [s for s in systems if s == want]

    print("── Generate + process each system ───────────────────────────")
    # Resilient: run EVERY system even if one fails, then summarise. --fail-fast
    # restores stop-on-first-error.
    results = []
    for d, s in systems:
        base = {"lakehouse_name": lh_name, "domain": d, "system": s, "environment": env}
        for step, params in (("test-data", base),
                             ("medallion", {**base, "target_layers": "bronze, silver, gold"})):
            nb = "test_data_driver" if step == "test-data" else "pipeline_driver"
            print(f"  ▶ {d}/{s}: {step}")
            try:
                run_notebook(fab_tok, wid, nb_ids[nb], params)
                results.append((d, s, step, "OK", ""))
            except Exception as e:
                results.append((d, s, step, "FAIL", str(e)[:120]))
                print(f"    ✗ {d}/{s} {step} FAILED: {str(e)[:160]}")
                if args.fail_fast:
                    raise
                break  # skip this system's medallion if its test-data failed

    print("\n── Summary ──────────────────────────────────────────────────")
    ok = sum(1 for r in results if r[3] == "OK")
    for d, s, step, st, err in results:
        print(f"  {st:4}  {d}/{s} · {step}" + (f"  — {err}" if err else ""))
    print(f"\n{'✅' if ok == len(results) else '⚠'} {ok}/{len(results)} steps OK across {len(systems)} systems.")


if __name__ == "__main__":
    main()
