# OLC — Open Lakehouse Contract layout

This repo's data contracts are Open Lakehouse Contract (OLC) documents, laid out
as a mesh registry under `_contracts/{domain}/{system}/`. Any OLC-integrated
assistant can `discover` / `contract` / `review` / `validate` / `impact` against
them.

`validate_contracts.py` runs the `validate` verb against the OSS `lakelogic`
engine and is wired into `.github/workflows/validate-olc.yml` — every PR that
touches a contract is gated on structural validity. See the Open Lakehouse
Contract project for the full verb set.
