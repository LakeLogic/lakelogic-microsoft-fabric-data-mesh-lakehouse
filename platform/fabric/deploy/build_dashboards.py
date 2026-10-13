#!/usr/bin/env python
"""Build a Direct Lake Power BI semantic model over the RideFlow Gold tables and a
starter report, in the 'Dashboards' folder of the Fabric workspace.

Direct Lake = live over OneLake (no import/refresh). Columns are taken from the
gold contracts (model.fields), so the model always matches the published schema.
Only Gold tables that actually exist in the lakehouse are included.

Auth via the Azure CLI (`az login`). Idempotent — safe to re-run.

Usage:  python platform/fabric/deploy/build_dashboards.py [--model-only]
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

import requests
import yaml

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parents[3]
FAB = "https://api.fabric.microsoft.com/v1"
MODEL_NAME = "RideFlow Gold"
REPORT_NAME = "RideFlow Executive"

# lakelogic contract type -> Power BI/TMDL dataType
TYPE_MAP = {
    "date": "dateTime", "timestamp": "dateTime", "datetime": "dateTime",
    "string": "string", "integer": "int64", "int": "int64", "long": "int64",
    "float": "double", "double": "double", "decimal": "double",
    "boolean": "boolean", "bool": "boolean",
}


def _token(resource="https://api.fabric.microsoft.com") -> str:
    out = subprocess.run(
        f"az account get-access-token --resource {resource} --query accessToken -o tsv",
        capture_output=True, text=True, shell=True)
    if out.returncode != 0 or not out.stdout.strip():
        sys.exit(f"Could not get a token for {resource}. Run `az login`.\n{out.stderr}")
    return out.stdout.strip()


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _poll_lro(resp, tok):
    if resp.status_code == 202 and resp.headers.get("Location"):
        import time
        loc = resp.headers["Location"]
        while True:
            time.sleep(3)
            s = requests.get(loc, headers=_hdr(tok))
            st = s.json().get("status") if s.headers.get("content-type", "").startswith("application/json") else None
            if st in ("Succeeded", "Completed") or s.status_code == 200:
                r = requests.get(loc + "/result", headers=_hdr(tok))
                return r.json() if r.status_code == 200 else {}
            if st == "Failed":
                raise RuntimeError(f"LRO failed: {s.text[:400]}")
    if resp.status_code not in (200, 201, 202):
        raise RuntimeError(f"{resp.status_code}: {resp.text[:500]}")
    return resp.json() if resp.text else {}


def resolve_workspace(tok, name):
    for w in requests.get(f"{FAB}/workspaces", headers=_hdr(tok)).json().get("value", []):
        if w.get("displayName") == name:
            return w["id"]
    sys.exit(f"Workspace '{name}' not found.")


def lakehouse_info(tok, wid, name):
    for lh in requests.get(f"{FAB}/workspaces/{wid}/lakehouses", headers=_hdr(tok)).json().get("value", []):
        if lh.get("displayName") == name:
            d = requests.get(f"{FAB}/workspaces/{wid}/lakehouses/{lh['id']}", headers=_hdr(tok)).json()
            sep = d.get("properties", {}).get("sqlEndpointProperties", {})
            return lh["id"], sep.get("connectionString"), sep.get("id")
    sys.exit(f"Lakehouse '{name}' not found.")


def ensure_folder(tok, wid, name):
    for f in requests.get(f"{FAB}/workspaces/{wid}/folders", headers=_hdr(tok)).json().get("value", []):
        if f.get("displayName") == name and not f.get("parentFolderId"):
            return f["id"]
    return requests.post(f"{FAB}/workspaces/{wid}/folders", headers=_hdr(tok),
                         json={"displayName": name}).json()["id"]


def existing_gold_tables(store_tok, ws_name, lh_name, domains):
    """Return {schema: set(table)} of Gold tables present in OneLake."""
    out = {}
    for d in domains:
        url = (f"https://onelake.dfs.fabric.microsoft.com/{ws_name.replace(' ', '%20')}/"
               f"{lh_name}.Lakehouse/Tables/{d}?resource=filesystem&recursive=false")
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {store_tok}"})
            paths = json.load(urllib.request.urlopen(req)).get("paths", [])
            tabs = {p["name"].split("/")[-1] for p in paths if p["name"].split("/")[-1].startswith("gold_")}
            if tabs:
                out[d] = tabs
        except Exception:
            pass
    return out


def gold_contracts():
    """Yield (domain, table_name, [(col, tmdl_type)]) for every gold contract."""
    for cy in sorted((REPO / "contracts").rglob("contracts/gold/*.yaml")):
        c = yaml.safe_load(cy.read_text(encoding="utf-8"))
        info = c.get("info", {})
        tbl = (info.get("table_name", "")
               .replace("{gold_layer}", "gold").replace("{system}", info.get("system", ""))
               .replace("{domain}", info.get("domain", "")))
        cols = [(f["name"], TYPE_MAP.get(str(f.get("type", "string")).lower(), "string"))
                for f in c.get("model", {}).get("fields", []) if f.get("name")]
        if tbl and cols:
            yield info.get("domain"), tbl, cols


def _lt():
    return str(uuid.uuid4())


def _table_tmdl(schema, table, cols):
    body = f"table {table}\n\tlineageTag: {_lt()}\n\n"
    for n, t in cols:
        summ = "sum" if t in ("int64", "double") and not n.endswith(("_id", "_sk", "_code", "_date", "date")) else "none"
        body += (f"\tcolumn {n}\n\t\tdataType: {t}\n\t\tlineageTag: {_lt()}\n"
                 f"\t\tsummarizeBy: {summ}\n\t\tsourceColumn: {n}\n\n")
    body += (f"\tpartition {table} = entity\n\t\tmode: directLake\n\t\tsource\n"
             f"\t\t\tentityName: {table}\n\t\t\tschemaName: {schema}\n\t\t\texpressionSource: DatabaseQuery\n")
    return body


def build_model(tok, wid, folder_id, server, db, tables):
    """tables: list of (schema, table, cols). Create/update the semantic model."""
    expr = (f'expression DatabaseQuery =\n\t\tlet\n\t\t\tdatabase = Sql.Database("{server}", "{db}")\n'
            f'\t\tin\n\t\t\tdatabase\n\tlineageTag: {_lt()}\n\tannotation PBI_ResultType = Table\n')
    parts = {
        "definition.pbism": json.dumps({"version": "4.0", "settings": {}}),
        "definition/database.tmdl": "database\n\tcompatibilityLevel: 1604\n",
        "definition/model.tmdl": ("model Model\n\tculture: en-US\n"
                                  "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n\tsourceQueryCulture: en-US\n"),
        "definition/expressions.tmdl": expr,
    }
    for schema, table, cols in tables:
        parts[f"definition/tables/{table}.tmdl"] = _table_tmdl(schema, table, cols)
    defn = {"parts": [{"path": p, "payload": base64.b64encode(c.encode()).decode(),
                       "payloadType": "InlineBase64"} for p, c in parts.items()]}

    existing = next((m for m in requests.get(f"{FAB}/workspaces/{wid}/semanticModels",
                     headers=_hdr(tok)).json().get("value", []) if m["displayName"] == MODEL_NAME), None)
    if existing:
        _poll_lro(requests.post(f"{FAB}/workspaces/{wid}/semanticModels/{existing['id']}/updateDefinition",
                                headers=_hdr(tok), json={"definition": defn}), tok)
        print(f"  ✓ updated semantic model '{MODEL_NAME}' ({len(tables)} tables)")
        return existing["id"]
    body = {"displayName": MODEL_NAME, "type": "SemanticModel", "definition": defn, "folderId": folder_id}
    res = _poll_lro(requests.post(f"{FAB}/workspaces/{wid}/items", headers=_hdr(tok), json=body), tok)
    print(f"  ✓ created semantic model '{MODEL_NAME}' ({len(tables)} tables)")
    return res.get("id")


def _card(name, x, table, col, agg="Sum"):
    fn = {"Sum": 0, "Average": 1}[agg]
    qr = f"{agg}({table}.{col})"
    cfg = {"name": name, "layouts": [{"id": 0, "position": {"x": x, "y": 40, "z": 0, "width": 210, "height": 120}}],
           "singleVisual": {"visualType": "card", "projections": {"Values": [{"queryRef": qr}]},
           "prototypeQuery": {"Version": 2, "From": [{"Name": "g", "Entity": table, "Type": 0}],
           "Select": [{"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Source": "g"}},
           "Property": col}}, "Function": fn}, "Name": qr}]}}}
    return {"x": x, "y": 40, "z": 0, "width": 210, "height": 120, "config": json.dumps(cfg)}


def _chart(name, table, cat_col, val_col, y=190):
    catq, valq = f"{table}.{cat_col}", f"Sum({table}.{val_col})"
    cfg = {"name": name, "layouts": [{"id": 0, "position": {"x": 40, "y": y, "z": 0, "width": 620, "height": 320}}],
           "singleVisual": {"visualType": "clusteredColumnChart",
           "projections": {"Category": [{"queryRef": catq}], "Y": [{"queryRef": valq}]},
           "prototypeQuery": {"Version": 2, "From": [{"Name": "g", "Entity": table, "Type": 0}],
           "Select": [{"Column": {"Expression": {"SourceRef": {"Source": "g"}}, "Property": cat_col}, "Name": catq},
           {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Source": "g"}},
           "Property": val_col}}, "Function": 0}, "Name": valq}]}}}
    return {"x": 40, "y": y, "z": 0, "width": 620, "height": 320, "config": json.dumps(cfg)}


def build_report(tok, wid, folder_id, mid, tables):
    """One page per 'interesting' Gold table (fact/agg/kpi/metrics/scorecard):
    up to 3 numeric cards + a category chart. Bound to the semantic model."""
    interesting = [(s, t, c) for s, t, c in tables
                   if any(k in t for k in ("fact", "agg", "kpi", "metric", "scorecard"))] or tables
    sections = []
    for i, (schema, table, cols) in enumerate(interesting[:8]):
        nums = [n for n, ty in cols if ty in ("int64", "double")
                and not n.endswith(("_id", "_sk", "_code")) and "date" not in n]
        cats = [n for n, ty in cols if ty == "string" and n.endswith(("_code", "_id")) or ty == "string"]
        vis = [_card(f"c{i}_{j}", 40 + j * 230, table, c, "Average" if c.startswith("avg") else "Sum")
               for j, c in enumerate(nums[:3])]
        if cats and nums:
            vis.append(_chart(f"ch{i}", table, cats[0], nums[0]))
        title = table.replace("gold_", "").replace("_", " ").title()
        sections.append({"name": f"p{i}", "displayName": f"{schema} · {title}"[:40],
                         "width": 1280, "height": 720, "config": "{}", "visualContainers": vis})
    report = {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/1.0.0/schema.json",
              "config": json.dumps({"version": "5.43", "themeCollection": {"baseTheme": {"name": "CY24SU10"}}}),
              "layoutOptimization": 0, "sections": sections}
    pbir = {"version": "4.0", "datasetReference": {"byConnection": {
        "connectionString": None, "pbiServiceModelId": None,
        "pbiModelVirtualServerName": "sobe_wowvirtualserver", "pbiModelDatabaseName": mid,
        "name": "EntityDataSource", "connectionType": "pbiServiceXmlaStyleLive"}}}
    parts = {"report.json": json.dumps(report), "definition.pbir": json.dumps(pbir)}
    defn = {"parts": [{"path": p, "payload": base64.b64encode(c.encode()).decode(),
                       "payloadType": "InlineBase64"} for p, c in parts.items()]}
    existing = next((r for r in requests.get(f"{FAB}/workspaces/{wid}/reports",
                     headers=_hdr(tok)).json().get("value", []) if r["displayName"] == REPORT_NAME), None)
    if existing:
        _poll_lro(requests.post(f"{FAB}/workspaces/{wid}/reports/{existing['id']}/updateDefinition",
                                headers=_hdr(tok), json={"definition": defn}), tok)
        print(f"  ✓ updated report '{REPORT_NAME}' ({len(sections)} pages)")
        return existing["id"]
    res = _poll_lro(requests.post(f"{FAB}/workspaces/{wid}/items", headers=_hdr(tok),
                    json={"displayName": REPORT_NAME, "type": "Report", "definition": defn, "folderId": folder_id}), tok)
    print(f"  ✓ created report '{REPORT_NAME}' ({len(sections)} pages)")
    return res.get("id")


def refresh_sql_metadata(tok, wid, sep_id):
    r = requests.post(f"{FAB}/workspaces/{wid}/sqlEndpoints/{sep_id}/refreshMetadata?preview=true",
                      headers=_hdr(tok), json={})
    print(f"  ↻ SQL endpoint metadata refresh ({r.status_code})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-only", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load((REPO / "platform" / "fabric" / "fabric_variables.yaml").read_text(encoding="utf-8"))
    ws_name = cfg["workspace"]["name"]
    lh_name = cfg["lakehouse"]["name"]

    tok = _token()
    store_tok = _token("https://storage.azure.com")
    wid = resolve_workspace(tok, ws_name)
    _, server, sep_id = lakehouse_info(tok, wid, lh_name)
    folder_id = ensure_folder(tok, wid, "Dashboards")
    print(f"Workspace {ws_name} | SQL {server}")

    # Which gold tables exist right now?
    domains = sorted({d for d, _, _ in gold_contracts()})
    present = existing_gold_tables(store_tok, ws_name, lh_name, domains)
    tables = [(d, t, cols) for d, t, cols in gold_contracts()
              if t in present.get(d, set())]
    print(f"── Gold tables present: {len(tables)} across {len(present)} domains ──")
    for d, t, _ in tables:
        print(f"   {d}.{t}")
    if not tables:
        sys.exit("No Gold tables found yet — run the pipelines first.")

    refresh_sql_metadata(tok, wid, sep_id)
    print("── Semantic model ──")
    mid = build_model(tok, wid, folder_id, server, sep_id, tables)
    if not mid:  # LRO didn't return the id — look it up by name
        mid = next((m["id"] for m in requests.get(f"{FAB}/workspaces/{wid}/semanticModels",
                    headers=_hdr(tok)).json().get("value", []) if m["displayName"] == MODEL_NAME), None)
    print(f"✅ Direct Lake semantic model ready: {mid}")

    if not args.model_only:
        print("── Report ──")
        build_report(tok, wid, folder_id, mid, tables)
        print("✅ Report ready in the 'Dashboards' folder.")


if __name__ == "__main__":
    main()
