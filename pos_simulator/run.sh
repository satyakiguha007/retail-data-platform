#!/usr/bin/env bash
# Quick-run script for local testing (no Docker required).
# Generates 3 stores × 3 days as a smoke test.
set -euo pipefail

OUTPUT=${1:-output/pos_rtlog}

echo "Running POS RTLOG Simulator (local, smoke test)..."
py -3 -m pos_simulator.main generate \
    --stores 1,2,3 \
    --start 2024-01-01 \
    --days 3 \
    --tpd 50 \
    --output "${OUTPUT}"

echo ""
echo "Output written to: ${OUTPUT}"
echo "Partitioning: store=*/date=*/hour=*/rtlog.ndjson"
