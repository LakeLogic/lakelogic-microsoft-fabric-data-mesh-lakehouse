# Fabric notebook source
# CELL ********************
import json, os, traceback
res = {}
def cap(n, fn):
    try:
        res[n] = "OK " + (str(fn()) or "")[:140]
    except Exception as e:
        res[n] = "FAIL " + str(e).strip().split("\n")[0][:220]

WS = "ddad2075-b65c-402f-8339-97ad244f548f"
LH = "6dc51ef0-3ea4-47c4-883d-a01071012f25"
p = "landing_marketplace/rideflow/trip_completed"

cap("fuse_dir_exists", lambda: os.path.isdir(f"/lakehouse/default/Files/{p}"))
cap("read_relative_Files",
    lambda: spark.read.option("recursiveFileLookup", "true").option("header", "true").csv(f"Files/{p}").count())
cap("read_abfss_guid",
    lambda: spark.read.option("recursiveFileLookup", "true").option("header", "true")
    .csv(f"abfss://{WS}@onelake.dfs.fabric.microsoft.com/{LH}/Files/{p}").count())
cap("read_abfss_name",
    lambda: spark.read.option("recursiveFileLookup", "true").option("header", "true")
    .csv(f"abfss://{WS}@onelake.dfs.fabric.microsoft.com/rideflow_dev_demo.Lakehouse/Files/{p}").count())
cap("read_fuse_err",
    lambda: spark.read.option("header", "true").csv(f"/lakehouse/default/Files/{p}").count())

import notebookutils
notebookutils.fs.put("Files/_logs/diag7.txt", json.dumps(res, indent=2), True)
print(json.dumps(res, indent=2))
