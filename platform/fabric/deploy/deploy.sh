#!/usr/bin/env bash
# deploy.sh — deploy the RideFlow LakeLogic data mesh into Microsoft Fabric (Unix)
#
# Auth comes from the Azure CLI (`az login`). Requires: az CLI, Python with
# `pyyaml requests`. Configure platform/fabric/fabric_variables.yaml first.
#
# Usage:  ./deploy.sh                 full bootstrap
#         ./deploy.sh --setup-only    provision + upload + import, no run
#         ./deploy.sh --domain marketplace/rideflow
set -euo pipefail
cd "$(dirname "$0")/../../.."
python -m pip install --disable-pip-version-check pyyaml requests >/dev/null
python platform/fabric/deploy/deploy.py "$@"
