#!/usr/bin/env python
"""Organize the Fabric workspace: put notebooks in a 'Notebooks' folder and the
Data Pipeline in a 'pipelines' folder (creating the folders if they don't exist),
and delete the throwaway '_diag' notebook.

Idempotent — safe to re-run. Auth via the Azure CLI (`az login`).

Usage:  python fabric/deploy/organize_workspace.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import requests
import yaml

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parents[2]
FAB = "https://api.fabric.microsoft.com/v1"

# Which items go where, and which to delete.
NOTEBOOKS_FOLDER = "Notebooks"
PIPELINES_FOLDER = "Pipelines"
NOTEBOOK_NAMES = {"00_setup", "test_data_driver", "pipeline_driver"}
PIPELINE_NAMES = {"RideFlow Medallion"}
DELETE_NAMES = {"_diag"}


def _token() -> str:
    out = subprocess.run(
        "az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv",
        capture_output=True, text=True, shell=True)
    if out.returncode != 0 or not out.stdout.strip():
        sys.exit(f"Could not get a Fabric token. Run `az login`.\n{out.stderr}")
    return out.stdout.strip()


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def resolve_workspace(tok, name) -> str:
    r = requests.get(f"{FAB}/workspaces", headers=_hdr(tok)); r.raise_for_status()
    for w in r.json().get("value", []):
        if w.get("displayName") == name:
            return w["id"]
    sys.exit(f"Workspace '{name}' not found.")


def ensure_folder(tok, wid, name) -> str:
    """Return the id of the folder named `name`, creating it if absent."""
    r = requests.get(f"{FAB}/workspaces/{wid}/folders", headers=_hdr(tok))
    r.raise_for_status()
    for f in r.json().get("value", []):
        if f.get("displayName") == name and not f.get("parentFolderId"):
            print(f"  ✓ folder '{name}' exists ({f['id']})")
            return f["id"]
    r = requests.post(f"{FAB}/workspaces/{wid}/folders", headers=_hdr(tok),
                      json={"displayName": name})
    r.raise_for_status()
    fid = r.json()["id"]
    print(f"  ✓ created folder '{name}' ({fid})")
    return fid


def move_item(tok, wid, item_id, folder_id, label):
    """Move an item into a folder. Tries the dedicated move endpoint, then falls
    back to updateItem with folderId."""
    r = requests.post(f"{FAB}/workspaces/{wid}/items/{item_id}/move",
                      headers=_hdr(tok), json={"targetFolderId": folder_id})
    if r.status_code in (200, 202):
        print(f"  ✓ moved {label}")
        return
    r2 = requests.patch(f"{FAB}/workspaces/{wid}/items/{item_id}",
                        headers=_hdr(tok), json={"folderId": folder_id})
    if r2.status_code in (200, 202):
        print(f"  ✓ moved {label} (via update)")
        return
    print(f"  ⚠ could not move {label}: move={r.status_code} update={r2.status_code} {r2.text[:120]}")


def main():
    cfg = yaml.safe_load((REPO / "fabric" / "fabric_variables.yaml").read_text(encoding="utf-8"))
    ws_name = cfg["workspace"]["name"]
    tok = _token()
    wid = resolve_workspace(tok, ws_name)
    print(f"Workspace: {ws_name} ({wid})")

    print("── Ensure folders ───────────────────────────────────────────")
    nb_folder = ensure_folder(tok, wid, NOTEBOOKS_FOLDER)
    pl_folder = ensure_folder(tok, wid, PIPELINES_FOLDER)

    r = requests.get(f"{FAB}/workspaces/{wid}/items", headers=_hdr(tok)); r.raise_for_status()
    items = r.json().get("value", [])

    print("── Delete throwaway items ───────────────────────────────────")
    for it in items:
        if it.get("displayName") in DELETE_NAMES and it.get("type") == "Notebook":
            d = requests.delete(f"{FAB}/workspaces/{wid}/items/{it['id']}", headers=_hdr(tok))
            print(f"  {'✓ deleted' if d.status_code in (200, 204) else '⚠ delete '+str(d.status_code)} {it['displayName']}")

    print("── Move items into folders ──────────────────────────────────")
    for it in items:
        name, typ, iid = it.get("displayName"), it.get("type"), it["id"]
        if name in DELETE_NAMES:
            continue
        if typ == "Notebook" and name in NOTEBOOK_NAMES:
            move_item(tok, wid, iid, nb_folder, f"notebook '{name}' → {NOTEBOOKS_FOLDER}")
        elif typ == "DataPipeline" and name in PIPELINE_NAMES:
            move_item(tok, wid, iid, pl_folder, f"pipeline '{name}' → {PIPELINES_FOLDER}")

    print("✅ Workspace organized.")


if __name__ == "__main__":
    main()
