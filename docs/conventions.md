# Conventions

Patterns and contracts that every silver notebook follows. Drift here causes
silent bugs across the layer — read this before writing a new notebook
(Pass-2 marketplace, Pass-3 Olist).

## File layout

```
transformations/silver/
├── _shared/                              # %run'd from each notebook
│   ├── surrogate_keys.py
│   ├── fx_helpers.py
│   ├── quarantine.py
│   └── schema_gate.py
├── 0N_sa_<table_name>/                   # folder per silver table
│   ├── sa_<table_name>_pos.py            # source: bronze.pos_rtlog
│   ├── sa_<table_name>_marketplace.py    # source: marketplace (Pass-2)
│   └── sa_<table_name>_olist.py          # source: Olist (Pass-3)
```

Rules:
- **Numeric prefix** on folders preserves the build-order narrative (`01_` → `06_`)
- **Underscore-prefix** `_shared/` is a valid Python package name (digit-prefix folders aren't)
- **One notebook per (source × silver table)** — never UNION sources in one notebook
- **File suffix** identifies the source: `_pos.py`, `_marketplace.py`, `_olist.py`

## Cell structure (every silver notebook)

Eight cells, in this order:

1. **Markdown header** — contract, "patterns introduced here", DQ rules,
   "NOT a DQ failure"
2. **Imports & widgets** — `dbutils.widgets.text("source_table", ...)`
3. **Shared helpers** — `%run ../_shared/<name>` for each helper used
4. **Configuration** — `TARGET_TABLE`, `QUARANTINE_TABLE`, `PARENT_TABLE`, `CHECKPOINT_PATH`
5. **Target schema** — `StructType([...])`, plus `quarantine_schema` derived from it
6. **Bootstrap** — create empty Delta target if not exists (partitioned by `BUSINESS_DATE`)
7. **`merge_microbatch` handler** — the heart (8 steps internally)
8. **Run the stream** — `readStream + foreachBatch + availableNow.start().awaitTermination()`
9. **Validation + diagnostics** — row count, quarantine summary, PK, FK,
   distributions, FX sanity

(That's actually 9, but cells 8 and 9 are tight enough that they read as
the run + the audit. The pattern is the same in all six notebooks.)

## The `merge_microbatch` handler — 7 steps

```python
def merge_microbatch(microBatchDF, batch_id):
    # 1 + 2. Explode (if array) + flatten with explicit aliases
    flat = (
        microBatchDF
        .withColumn("<elem>", explode(col("<array>")))    # only for children that explode
        .select(... explicit aliases for every column ...)
        .filter(... not-null checks on surrogate components ...)
    )

    # 3. Surrogate key — shared helper
    keyed = flat.withColumn("TRAN_SEQ_NO", tran_seq_no_expr())

    # 4. FK enrich — shared helper (skipped only for 01)
    enriched = enrich_with_parent_fx(keyed, PARENT_TABLE, [<join_keys>])

    # 5. Derive — USD companions + lineage
    derived = (
        enriched
        .withColumn("<AMOUNT>_USD", (col("<AMOUNT>") * col("FX_RATE")).cast(DecimalType(20, 4)))
        ...
        .withColumn("_silver_ts", current_timestamp())
        .withColumn("_source",    lit(SOURCE_TABLE))
    )

    # 6. DQ split — rejection_reason array
    dq = derived.withColumn(
        "rejection_reason",
        array_compact(array(
            when(<condition>, lit("<reason string>")),
            ...
        ))
    )

    clean   = dq.filter("size(rejection_reason) = 0").drop("rejection_reason", "_has_parent")
    rejects = dq.filter("size(rejection_reason) > 0").drop("_has_parent")
    # _quarantine_ts added INSIDE merge_and_quarantine — do NOT add here

    clean = clean.select(*[f.name for f in <schema>.fields])

    # 7. MERGE + quarantine — shared helper
    clean_n, reject_n = merge_and_quarantine(
        clean_df=clean,
        rejects_df=rejects,
        target_table=TARGET_TABLE,
        quarantine_table=QUARANTINE_TABLE,
        merge_keys=[<PK>],
    )

    print(f"Batch {batch_id}: clean={clean_n} rejects={reject_n}")
```

## Shared helper API contracts

### `surrogate_keys.tran_seq_no_expr()`

Returns a Column expression. Caller must have already flattened these three columns:

| Column | Type | Notes |
|---|---|---|
| `RTLOG_ORIG_SYS` | string | `'POS'` / `'MKT'` / `'OLIST'` |
| `TRAN_SEQ_NO_NATURAL` | string | POS-assigned natural composite |
| `TRAN_DATETIME` | timestamp | **must already be cast** to `TimestampType()` |

The cast on `TRAN_DATETIME` is part of the formula. Never hash the raw string.

### `fx_helpers.enrich_with_parent_fx(df, parent_table, join_keys)`

Broadcast-joins parent silver table, inherits `CURRENCY_CODE` + `FX_RATE`,
adds boolean `_has_parent` column.

| Arg | Example |
|---|---|
| `df` | DataFrame with surrogate + join keys populated |
| `parent_table` | `"retaildp.silver.sa_tran_head"` or `"retaildp.silver.sa_tran_item"` |
| `join_keys` | `["TRAN_SEQ_NO"]` (tran-level) or `["TRAN_SEQ_NO", "ITEM_SEQ_NO"]` (line-level) |

Returns: enriched DataFrame. Caller is responsible for dropping `_has_parent`
after using it in the DQ rules.

### `quarantine.merge_and_quarantine(clean_df, rejects_df, target_table, quarantine_table, merge_keys)`

Final write phase. MERGE clean rows on composite PK; append rejects to quarantine
(creates on first write). Adds `_quarantine_ts` to rejects internally.

Returns `(clean_n, reject_n)` for the per-batch print.

### `schema_gate.bronze_array_has_inner_fields(source_table, array_column)`

Returns `True` iff the column is `ARRAY<STRUCT<...>>` with at least one inner field.
Use BEFORE starting the stream, not inside `foreachBatch` — Spark must be able to
plan the explode + projection, and if the inner struct is empty, planning fails.

## Naming conventions

### Tables
- Silver targets: `retaildp.silver.sa_<entity>` (matches ReSA names)
- Quarantine: `retaildp.quarantine.silver_<entity>_rejects`
- Bronze sources: `retaildp.bronze.<source>` (e.g. `pos_rtlog`, `fx_rates`)

### Columns
- **PK / FK columns**: UPPER_SNAKE_CASE matching ReSA (`TRAN_SEQ_NO`, `ITEM_SEQ_NO`)
- **Lakehouse additions**: also UPPER_SNAKE_CASE (`VALUE_USD`, `TAX_MODE`)
- **Lineage**: `_`-prefixed lowercase (`_silver_ts`, `_source`, `_quarantine_ts`)
- **Temporary join helpers**: `_p_<NAME>` prefix (dropped before final output)

### Files
- Silver notebooks: `sa_<table_name>_<source>.py`
- Shared helpers: `<concept>.py` in `_shared/`

## DQ rules — quarantine, don't drop

Every silver notebook routes failures to `retaildp.quarantine.silver_<table>_rejects`.
The `rejection_reason` column is `ArrayType<StringType>` — a row can have multiple
reasons stacked.

### Standard rejection reasons by table

| Table | Reasons |
|---|---|
| `sa_tran_head` | `TRAN_DATETIME invalid or null`, `TRAN_TYPE not in valid set`, `VALUE must be NOT NULL`, `STORE_DAY_SEQ_NO FK lookup failed`, `CURRENCY_CODE FK lookup failed`, `TAX_MODE could not be derived` |
| `sa_tran_item` | `orphan_no_parent_header`, `ITEM_SEQ_NO null — cannot form PK`, `ITEM_TYPE null — mandatory in ReSA` |
| `sa_tran_disc` | `orphan_no_parent_item`, `ITEM_SEQ_NO null — cannot form FK`, `DISCOUNT_SEQ_NO null — cannot form PK`, `RMS_PROMO_TYPE null — cannot form PK` |
| `sa_tran_tender` | `orphan_no_parent_header`, `TENDER_SEQ_NO null`, `TENDER_TYPE_GROUP null`, `TENDER_AMT null — mandatory in ReSA` |
| `sa_tran_tax` | `orphan_no_parent_header`, `TAX_SEQ_NO null`, `TAX_CODE null`, `TAX_AMT null` |
| `sa_tran_igtax` | `orphan_no_parent_item`, `ITEM_SEQ_NO null`, `IGTAX_SEQ_NO null`, `TOTAL_IGTAX_AMT null` |

### What's NEVER a DQ failure
- `ERROR_IND = 'Y'` — fault-injected rows pass through; Module 4 audit catches them
- `FX_RATE` null — `*_USD` columns become null but row still lands
- Empty arrays in bronze (e.g. `tran_disc = []`) — produce 0 rows via `explode`, that's correct
- `ORIG_CURRENCY != CURRENCY_CODE` on tenders — legitimate foreign tender

## Idempotency requirements

Every silver notebook MUST be re-runnable without data drift. Three guarantees:

1. **Deterministic surrogate keys** — `TRAN_SEQ_NO = xxhash64(...)` is a pure
   function of inputs. Same bronze row always hashes to the same surrogate.
2. **MERGE on PK** — `whenMatchedUpdateAll` + `whenNotMatchedInsertAll`.
   Re-running over already-processed bronze does nothing destructive
   (just refreshes `_silver_ts`).
3. **Streaming checkpoint per notebook** — Spark tracks consumed bronze commits.
   A failed run resumes from the last checkpoint; checkpoints are per
   `(silver table × source channel)` pair to allow independent backfills.

**Test for idempotency**: re-run a silver notebook against the same bronze.
Row count must not change.

## Partitioning convention

Every silver table partitioned by `BUSINESS_DATE` (DateType). No `DAY` column
(redundant with BUSINESS_DATE; ReSA had it for Oracle's physical layout). The
surrogate `TRAN_SEQ_NO` is globally unique so we don't need the (STORE, DAY,
TRAN_SEQ_NO) composite that ReSA uses.

Bootstrap pattern:

```python
(
    spark.createDataFrame([], <schema>).write
    .format("delta")
    .partitionBy("BUSINESS_DATE")
    .option("delta.autoOptimize.optimizeWrite", "true")
    .option("delta.autoOptimize.autoCompact",   "true")
    .saveAsTable(TARGET_TABLE)
)
```

## Checkpoints

```
abfss://checkpoints@stretaildpsatyaki01.dfs.core.windows.net/silver/<table>/
```

For Pass-2 onwards, sub-path by source channel:

```
silver/sa_tran_head/             # Pass-1 POS (legacy, kept at this path)
silver/sa_tran_head/marketplace/ # Pass-2
silver/sa_tran_head/olist/       # Pass-3
```

Each source has its own checkpoint state. Independent backfills don't
collide.

## Validation cells — what every notebook checks

| Check | Purpose |
|---|---|
| Row count | Smoke test |
| Quarantine summary + top reasons | What's failing and why |
| PK uniqueness (`groupBy(PK).count().where("count > 1")` = 0) | Surrogate / composite is truly unique |
| FK integrity (`left_anti` against parent = 0) | Every child has a live parent |
| Domain distributions | TRAN_TYPE, RMS_PROMO_TYPE, TAX_MODE, etc. |
| Fan-out distribution | Items/tran, discounts/tran, etc. |
| Channel distribution | All POS in Pass-1; MKT or OLIST appears post-Pass-2 |
| FX sanity sample | First 10 rows: amount, currency, rate, USD |

Notebook 06 also runs a **cross-table reconciliation**:
`SUM(sa_tran_igtax.TOTAL_IGTAX_AMT) per item == sa_tran_item.TOTAL_IGTAX_AMT` ± 0.01.

## When to bend the rules

The `_shared/` helpers cover ~95% of patterns. The 5% that stays inline:

- **Source-specific flatten** — `01.pos.py` and `01.marketplace.py` will
  have totally different `.select(...)` projections. That's the conformance
  point; it must stay per-source.
- **Source-specific DQ rules** — POS validates `TRAN_TYPE`; marketplace
  might validate `ORDER_STATUS`. Different rules per source.
- **Dimension lookups** — `01.pos.py` joins to `sa_store_data` and
  `sa_store_day` for store-level enrichment. That's not the parent-FX
  pattern; it's a different shape and lives inline in 01.
- **First-time FX derivation** — `01` derives `FX_RATE` from
  `bronze.fx_rates` directly. Children inherit. Only 01 does the lookup.

When in doubt: **the helpers exist to prevent silent drift.** If you find
yourself copying ~10 lines of "broadcast join + inherit + drop" between
two notebooks, that's a sign to use `enrich_with_parent_fx`. If you find
yourself writing per-source flatten logic, that stays inline — that IS
the conformance work.
