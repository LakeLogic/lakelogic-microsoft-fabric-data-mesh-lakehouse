# Synthetic data generator bundle (LakeLogic Synthetic v2)

Source: medallion design bp:snap-1f09b935d5194015 r1
Generated at: revision:chv1:b923be7ecb5204b7e732ee88e204f3b7e0ad4399baebddecbf7096861f7ef99e seal:sha256:9e5ae31d73707a34beb59b7d2b5e37531f6a5cd8df34014615c54e3f7ffeb7db
Generator: lakelogic DataGenerator v1.51.0

## Fidelity profile: contract-derived

This bundle ships a **deterministic seeded generator**, not synthetic rows. It was built
from the contract **schemas only** — **no production rows were read** — so it can be shared
freely: no synthetic data crosses a boundary until *you* run `generate.py` locally.

Values are contract-derived: the generator uses semantic hints from column names (email,
name, phone, address, ...) and type-correct fakes otherwise. This is NOT profile- or
sample-informed fidelity (those are later, separately-governed modes); no distribution or
value from your production data informs these fakes.

## Permanent synthetic classification

Every artifact produced by this bundle is **synthetic, permanently** (ADR-0011 decision 6:
synthetic is not the same as anonymised). Do not re-classify generated data as production.

## PII / masked columns

Columns flagged `pii` / `sensitive` in the contracts carry a `masking` strategy. Run the
generated frames through `lakelogic.DataProcessor(<contract>).run(df)` to apply masking by
construction (`redact` -> `***REDACTED***`, `hash` -> pseudonym). The generator emits
semantically-shaped fakes for these columns; it never emits a real identifier.

## Staged rollout (use synthetic BEFORE real data)

Run any exported pipeline against THIS synthetic data first, staged:
`dry_run` -> `ddl_only` -> `live` — validate the design on synthetic before pointing it at
production. Injected-invalid rows (`invalid_ratio=0.0`) let you prove your
quality gates quarantine bad rows.

## Contents

  - `generate.py` — the standalone generator (needs `pip install lakelogic`)
  - `config.json` — the pinned config (seed, rows, window, relationships)
  - `contracts/*.yaml` — the contracts (deterministically serialized, once; the canonical source of truth)

## Config

  - seed: 7
  - rows_per_table: 100
  - invalid_ratio: 0.0
  - window: 2020-01-01T00:00:00 .. 2025-01-01T00:00:00 (module default)

## Tables

  - dim_internal_cities
  - dim_rideflow_driver_locations
  - dim_rideflow_drivers
  - dim_rideflow_riders
  - fact_charges
  - fact_trips
  - internal_cities
  - rideflow_driver_locations
  - rideflow_drivers
  - rideflow_riders
  - rideflow_trips
  - silver_internal_cities
  - silver_rideflow_driver_locations
  - silver_rideflow_drivers
  - silver_rideflow_riders
  - silver_rideflow_trips
  - silver_stripe_charges
  - stripe_charges
