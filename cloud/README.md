# Dhan Cloud package (Phase 6): read-only observer

- `src/cloud_main.py`: the Cloud entry point. Edit the CONFIG block in the
  generated `main.py`, not here, unless you rebuild.
- `build_bundle.py`: builds `dist/` from `engine/sensex/*`, `engine/refresh_table.py`
  and `src/cloud_main.py`. `--check` reports drift; a test enforces it.
- `dist/multi/`: upload all files if Cloud supports multiple files (probe P2).
- `dist/single/sensex_observer.py`: single-file build.
- `probe/import_probe/`: credential-free two-file import probe (P2).

No orders, no login, no token renewal, stdlib only.
Steps: `docs/PHASE6_CLOUD_DEPLOYMENT.md`. Daily routine: `docs/PHASE6_OBSERVATION_PLAN.md`.
