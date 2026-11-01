"""OLC `validate` verb — structural validation of this repo's contracts.

Runs the OSS `lakelogic` engine's `validate_contract` over every contract under
`_contracts/`. Structural only — no data, no dry-run, no deploy. Exit 1 on any
invalid contract so CI gates the PR.
"""
from __future__ import annotations

import sys
from pathlib import Path

from lakelogic import validate_contract

HERE = Path(__file__).resolve().parent
# The contracts folder: `contracts/` at the repo root (shared layout), `_contracts/` in an
# older repo. Searched upward, so this file works from olc/ or platform/fabric/olc/.
ROOT = next((d for d in [HERE, *HERE.parents] if (d / 'contracts').is_dir() or (d / '_contracts').is_dir()), HERE)
CDIR = ROOT / 'contracts' if (ROOT / 'contracts').is_dir() else ROOT / '_contracts'
CONTRACTS = sorted(CDIR.rglob('*.yaml'))
CONTRACTS = [p for p in CONTRACTS if p.name not in ('_domain.yaml', '_system.yaml')]


def main() -> int:
    if not CONTRACTS:
        print('No contracts found under _contracts/ — nothing to validate.')
        return 0
    failed = 0
    for path in CONTRACTS:
        result = validate_contract(path)
        if result.valid:
            print(f'OK    {path.relative_to(ROOT)}')
        else:
            failed += 1
            print(f'FAIL  {path.relative_to(ROOT)}')
            for err in result.error_only:
                print(f'      -> [{err.field}] {err.message}')
    print(f"\n{'FAIL' if failed else 'OK'} - {len(CONTRACTS)} contract(s), {failed} invalid")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
