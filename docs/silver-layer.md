# Silver Layer — Architectural Narrative

The silver layer conforms heterogeneous bronze sources into a unified,
ReSA-canonical transaction model. Six tables, three sources, one unified
view of retail transactions.

## Design philosophy

The silver layer answers one question per table: *what is the canonical
shape of this retail concept, regardless of where it came from?* The
answer is the ReSA-canonical schema. Marketplace orders and Olist data
land in the same target tables as POS RTLOG — they just take a different
path to get there (different bronze shape → channel-specific conformance
notebook → same silver target).

That's why the folder layout groups by silver table, not by source:

```
01_sa_tran_head/
├── sa_tran_head_pos.py          # POS RTLOG path
├── sa_tran_head_marketplace.py  # Marketplace path (Pass-2)
└── sa_tran_head_olist.py        # Olist path (Pass-3)
```

Each file targets the same `silver.sa_tran_head` table. Channel is
distinguished by `RTLOG_ORIG_SYS` ∈ `{'POS', 'MKT', 'OLIST'}` on every
row. The surrogate key `TRAN_SEQ_NO = xxhash64(RTLOG_ORIG_SYS, ...)`
starts with the channel name, so cross-channel collisions are
structurally impossible.

## The six tables and their relationships

```
                     ┌─────────────────────────┐
                     │ silver.sa_tran_head     │  (transaction header)
                     │ PK: TRAN_SEQ_NO         │
                     │ Holds CURRENCY_CODE,    │
                     │ FX_RATE, TAX_MODE,      │
                     │ COUNTRY                 │
                     └────────┬────────────────┘
                              │
              ┌───────────────┼───────────────┬──────────────┐
              │               │               │              │
              ▼               ▼               ▼              ▼
    ┌─────────────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐
    │ sa_tran_item    │  │ sa_tran_ │  │ sa_tran_ │  │ sa_tran_tax  │
    │ PK: (TRAN_SEQ_  │  │ tender   │  │ tax      │  │ (may-be-     │
    │  NO, ITEM_SEQ_  │  │ Tran-    │  │ Tran-    │  │  empty)      │
    │  NO)            │  │ level    │  │ total    │  │              │
    │ One row per     │  │ FK       │  │ FK       │  │ Partner of   │
    │ line item       │  │          │  │          │  │ sa_tran_igtax│
    └─────────┬───────┘  └──────────┘  └──────────┘  └──────────────┘
              │
              ├──────────────────────────────────┐
              ▼                                  ▼
    ┌─────────────────┐                ┌──────────────────────┐
    │ sa_tran_disc    │                │ sa_tran_igtax        │
    │ PK: (TRAN_SEQ_  │                │ PK: (TRAN_SEQ_NO,    │
    │  NO, ITEM_SEQ_  │                │  ITEM_SEQ_NO,        │
    │  NO, DISCOUNT_  │                │  IGTAX_SEQ_NO)       │
    │  SEQ_NO, RMS_   │                │ Per-line per-        │
    │  PROMO_TYPE)    │                │ authority tax        │
    │ Two-level FK    │                │ Two-level FK         │
    └─────────────────┘                └──────────────────────┘
```

Inheritance flow for `CURRENCY_CODE` and `FX_RATE`:

```
bronze.fx_rates ──────┐
                      │
                      ▼
            sa_tran_head  ← derives FX from fx_rates by (BUSINESS_DATE, CURRENCY)
                  │
                  ▼ inherited via FK join
            sa_tran_item  ← inherits CURRENCY_CODE + FX_RATE from head
                  │
                  ▼ inherited via FK join
   sa_tran_disc, sa_tran_igtax  ← inherit from item

   sa_tran_tender, sa_tran_tax  ← inherit directly from head
```

Every monetary `*_USD` column is computed as `amount * FX_RATE` where
`FX_RATE` came from the chain above. By construction, every USD figure
in silver reconciles to its header's `VALUE_USD`.

## Table-by-table

### 01 — `sa_tran_head`

The central transactional table. One row per transaction. Every other
silver `sa_tran_*` table FKs back to here.

- **Source**: `bronze.pos_rtlog` (POS), planned marketplace and Olist
- **PK**: `TRAN_SEQ_NO` (unique surrogate)
- **Partition**: `BUSINESS_DATE`
- **Carries**: `CURRENCY_CODE`, `FX_RATE`, `TAX_MODE`, `COUNTRY`, `VALUE`, `VALUE_USD`
- **Channel discriminator**: `RTLOG_ORIG_SYS`
- **No FK enrich helper** — this IS the parent. Uses dimension joins
  to `sa_store_data` (currency, country) and `sa_store_day` (store-day
  spine), then an FX broadcast join against `bronze.fx_rates`.

**TAX_MODE routing logic:**
```
COUNTRY = IND        → TAX_MODE = IGTAX  (tax per line, lives in sa_tran_igtax)
COUNTRY = USA / GBR  → TAX_MODE = TAX    (tax on tran total, lives in sa_tran_tax)
COUNTRY = ARE / SGP  → TAX_MODE = BOTH   (basket-dependent)
```

The downstream silver layer reads `TAX_MODE` to decide which tax table
to populate. The validation cells in 05 and 06 use it as the headline
diagnostic.

### 02 — `sa_tran_item`

Line-item detail. One row per (transaction × line item).

- **Source**: `bronze.pos_rtlog.tran_item` (ARRAY of STRUCT)
- **PK**: `(TRAN_SEQ_NO, ITEM_SEQ_NO)`
- **Parent**: `sa_tran_head`
- **FK enrich**: `enrich_with_parent_fx(keyed, "retaildp.silver.sa_tran_head", ["TRAN_SEQ_NO"])`
- **Patterns introduced**:
  - First use of `explode(tran_item)` — one bronze row fans to N item rows
  - First FK to a Silver parent (not just a dimension table)
  - First USD inheritance — `UNIT_RETAIL_USD = UNIT_RETAIL * FX_RATE`

### 03 — `sa_tran_disc`

Discount detail. One row per (item × discount).

- **Source**: `bronze.pos_rtlog.tran_disc`
- **PK**: `(TRAN_SEQ_NO, ITEM_SEQ_NO, DISCOUNT_SEQ_NO, RMS_PROMO_TYPE)` — 4-col composite
- **Parent**: `sa_tran_item`
- **FK enrich**: `enrich_with_parent_fx(keyed, "retaildp.silver.sa_tran_item", ["TRAN_SEQ_NO", "ITEM_SEQ_NO"])`
- **Patterns introduced**:
  - Two-level FK collapsed to one join (line + transitively header)
  - Multi-column PK with a code column (`RMS_PROMO_TYPE` disambiguates
    re-used `DISCOUNT_SEQ_NO` across promo types)

### 04 — `sa_tran_tender`

Tender / payment detail.

- **Source**: `bronze.pos_rtlog.tran_tender`
- **PK**: `(TRAN_SEQ_NO, TENDER_SEQ_NO)`
- **Parent**: `sa_tran_head` (tran-level, not line-level)
- **FK enrich**: `enrich_with_parent_fx(keyed, "retaildp.silver.sa_tran_head", ["TRAN_SEQ_NO"])`
- **Patterns introduced**:
  - Multi-currency capture — `ORIG_CURRENCY` and `ORIG_CURR_AMT`
    coexist with the inherited `CURRENCY_CODE`. A foreign card paying
    in a local store has `ORIG_CURRENCY ≠ CURRENCY_CODE`. Pass-1 never
    exercises this; Pass-2 marketplace cross-border orders will.
  - PII handling — `CC_NO` arrives pre-masked from the simulator
    (`************4521`). Silver carries it through as-is.

### 05 — `sa_tran_tax`

Tax records at the **transaction-total level**. One row per tax line
per transaction. No `ITEM_SEQ_NO` in PK.

- **Source**: `bronze.pos_rtlog.tran_tax` (often empty for IND data)
- **PK**: `(TRAN_SEQ_NO, TAX_SEQ_NO)`
- **Parent**: `sa_tran_head`
- **FK enrich**: tran-level (single join key)
- **Schema gate**: `bronze_array_has_inner_fields(SOURCE_TABLE, "tran_tax")`
- **Patterns introduced**:
  - The "may-be-empty" child — empty for IND-only runs (correct, not a bug)
  - The schema gate — defensive against Auto Loader inferring `ARRAY<>` with no inner struct
  - The tax-presence-by-`TAX_MODE` diagnostic that validates the 01-side
    geography routing

### 06 — `sa_tran_igtax`

Tax records at the **item-line level**. One row per (item × tax authority).
`ITEM_SEQ_NO` IS in the PK. The structural opposite of 05.

- **Source**: `bronze.pos_rtlog.tran_igtax`
- **PK**: `(TRAN_SEQ_NO, ITEM_SEQ_NO, IGTAX_SEQ_NO)`
- **Parent**: `sa_tran_item` (line-level FK)
- **FK enrich**: line-level (two join keys)
- **Schema gate**: defensive (against USA-only future runs)
- **Patterns introduced**:
  - Multiple rows per item (CGST + SGST for IND intra-state — not a duplicate)
  - Cross-table reconciliation — `SUM(TOTAL_IGTAX_AMT)` per item must
    equal `sa_tran_item.TOTAL_IGTAX_AMT` within 0.01 tolerance.
    The validation cell asserts this.

## TAX vs IGTAX — the structural distinction

The two tax tables exist because ReSA splits tax by where it sits, not
by accounting treatment:

| Aspect | `sa_tran_tax` | `sa_tran_igtax` |
|---|---|---|
| Sits at | Transaction total | Per item line |
| `ITEM_SEQ_NO` in PK | No | Yes |
| Typical scenario | US sales tax computed at the register | India GST baked into MRP, per-SKU rate |
| Geography (Pass-1) | USA, GBR | IND |
| `TAX_MODE` indicator | `TAX` | `IGTAX` |

The accounting framing ("additive" vs "inclusive") is the underlying
*reason* this split exists, but the structural framing ("on the total"
vs "per line") is what the PK actually encodes. The country drives
which table populates because of how tax law works in each jurisdiction —
US has additive sales tax on subtotals, India has inclusive GST that
varies per SKU.

## DQ philosophy

- **Quarantine, don't drop** — every silver notebook routes DQ failures
  to `retaildp.quarantine.silver_<table>_rejects` with a structured
  `rejection_reason` array. Rows are NEVER silently lost.
- **Quarantine reasons are first-class** — they read like ReSA error
  codes: `"orphan_no_parent_header (TRAN_SEQ_NO not in sa_tran_head)"`,
  `"ITEM_SEQ_NO null — cannot form PK"`, etc.
- **FK orphans are quarantined, not dropped** — if a child row's
  parent doesn't exist (header rejected upstream), the child is
  routed to quarantine, not silently dropped.
- **What's NOT a DQ failure** is documented inline in each notebook's
  markdown header. Things like `ERROR_IND='Y'` (fault-injected data
  passing through), `FX_RATE` null (fx_rates gap), empty arrays
  (legitimate for non-applicable transactions) all pass through silver
  as data, not rejects.

## Idempotency

Every silver notebook is fully re-runnable. Three guarantees:

1. **Deterministic surrogate keys** — `TRAN_SEQ_NO = xxhash64(...)` is
   pure function of inputs. Same bronze row always hashes to the same
   surrogate.
2. **MERGE on PK** — `whenMatchedUpdateAll` + `whenNotMatchedInsertAll`.
   Re-running over already-processed bronze data does nothing destructive.
3. **Streaming checkpoint** — Spark's checkpoint tracks consumed bronze
   commits. A failed run resumes from the last checkpoint without
   re-processing.

This is why re-running `02_sa_tran_item/sa_tran_item_pos.py` after Pass-1
produces *exactly* 22,649 rows. Same surrogate, same MERGE, same data.

## Cross-table invariants (used as smoke tests)

| Invariant | Validation cell |
|---|---|
| Every item's `TRAN_SEQ_NO` exists in `sa_tran_head` | 02 FK check |
| Every disc's `(TRAN_SEQ_NO, ITEM_SEQ_NO)` exists in `sa_tran_item` | 03 FK check |
| `SUM(sa_tran_igtax.TOTAL_IGTAX_AMT)` per item = `sa_tran_item.TOTAL_IGTAX_AMT` | 06 reconciliation |
| `IGTAX` headers have 0 rows in `sa_tran_tax` | 05 tax-presence-by-TAX_MODE |
| `TAX` headers have 0 rows in `sa_tran_igtax` | 06 tax-presence-by-TAX_MODE |
| `tender.ORIG_CURRENCY = tender.CURRENCY_CODE` (Pass-1 only) | 04 multi-currency drift |

These invariants are stronger than schema constraints — they encode
ReSA's audit-grade semantics.
