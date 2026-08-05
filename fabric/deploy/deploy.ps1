# deploy.ps1 — deploy the RideFlow LakeLogic data mesh into Microsoft Fabric (Windows)
#
# Auth comes from the Azure CLI (`az login`). Requires: az CLI, Python with
# `pyyaml requests`. Configure fabric/fabric_variables.yaml first (workspace name,
# capacity_id, lakehouse name).
#
# Usage:  .\deploy.ps1                 full bootstrap
#         .\deploy.ps1 -SetupOnly      provision + upload + import, no run
param([switch]$SetupOnly, [string]$Domain = "")
Set-Location (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent)

python -m pip install --disable-pip-version-check pyyaml requests | Out-Null
$args = @()
if ($SetupOnly) { $args += "--setup-only" }
if ($Domain)    { $args += @("--domain", $Domain) }
python fabric/deploy/deploy.py @args
exit $LASTEXITCODE
