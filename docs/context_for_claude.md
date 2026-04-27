# Current project state

Last updated: 2026-04-27

## Done
- Repo scaffolded (folders, design docs, initial commits)
- Claude Code installed and configured in VS Code
- CLAUDE.md written: project overview, 6 critical conventions, reference doc map,
  module status table, out-of-scope list, and an 8-step task-start checklist
- Module 1 COMPLETE: POS RTLOG Simulator (pos_simulator/)
  - SimConfig, reference_data, RTLOG dataclasses (models.py)
  - TransactionGenerator: all 8 TRAN_TYPEs, temporal patterns, IGTAX/TAX/BOTH modes
  - inject_faults: 6 fault types (MISSING_TENDER, TENDER_VAR_GT_01, TRAN_NO_DUP,
    VOID_OOH, NEG_QTY_NO_REF, CC_NOT_MASKED)
  - Writer: NDJSON partitioned store=/date=/hour= (Auto Loader ready)
  - CLI: `generate` and `sample` subcommands
  - 1K sample fixture checked in at pos_simulator/sample_data/
  - Dockerfile + run.sh
  - 41 passing pytest tests
  - Full documentation at docs/module1_pos_simulator.md

## In progress
- Nothing. Clean stopping point.

## Next task when returning
Start Module 2 — Batch Sources & Additional Channels (ingestion/).
Goal: ADF pipelines + Bronze notebooks for:
  1. Olist Brazilian E-commerce dataset (9 CSV tables -> bronze.olist_*)
  2. Marketplace aggregator simulated JSON feed (bronze.mkt_feed)
  3. FX rates daily pull (bronze.fx_rates)
  4. Weather data daily pull (bronze.weather)
See design doc §4.2 for full spec.
Also update CLAUDE.md module status table: Module 1 -> Complete.
