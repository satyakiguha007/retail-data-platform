# Progress

A running ledger of what's done, what's in flight, and what's next.

## Pass-1 — POS (COMPLETE)

POS RTLOG ingested via Module 1 simulator. Three IND stores (33487, 39876, 41203),
three business days, full RTLOG payload exercised.

### Module 1 — POS simulator and bronze ingestion (done)
- Module 1 simulator emits RTLOG-style JSON with fault injection (duplicate
  `tran_seq_no`, duplicate `tran_no`, NULL injection, error_ind toggling)
- Bronze tables: `bronze.pos_rtlog`, `bronze.stores`, `bronze.fx_rates`,
  `bronze.weather`, `bronze.olist_orders` (loaded but not yet transformed)
- Auto Loader handles schema inference + checkpoint state

### Module 2 — Bronze layer (done)
- All bronze tables in Unity Catalog `retaildp.bronze.*`
- `_source_file` lineage column on every bronze row
- Partitioning by ingestion date where applicable

### Module 3 — Silver layer (DONE for POS)

| # | Notebook | Target | Row count | Validation |
|---|---|---|---:|---|
| 08 | `08_sa_store_data.py` | `silver.sa_store_data` | 26 | clean |
| 07 | `07_sa_store_day.py` | `silver.sa_store_day` | 9 | clean |
| 01 | `01_sa_tran_head/sa_tran_head_pos.py` | `silver.sa_tran_head` | 8,268 | PK ✓ FK ✓ |
| 02 | `02_sa_tran_item/sa_tran_item_pos.py` | `silver.sa_tran_item` | 22,649 | PK ✓ FK ✓ |
| 03 | `03_sa_tran_disc/sa_tran_disc_pos.py` | `silver.sa_tran_disc` | 5,094 | PK ✓ FK ✓ |
| 04 | `04_sa_tran_tender/sa_tran_tender_pos.py` | `silver.sa_tran_tender` | confirmed | PK ✓ FK ✓ |
| 05 | `05_sa_tran_tax/sa_tran_tax_pos.py` | `silver.sa_tran_tax` | 0 (IND-only) | gate skips correctly |
| 06 | `06_sa_tran_igtax/sa_tran_igtax_pos.py` | `silver.sa_tran_igtax` | confirmed | recon ✓ |

**Patterns established**:
- `xxhash64`-based deterministic surrogate keys with `TRAN_DATETIME` tie-breaker
- `readStream` + `availableNow` + `foreachBatch` → MERGE (idempotent)
- Quarantine-first DQ — `rejection_reason` arrays, never silent drops
- FK inheritance — children inherit `CURRENCY_CODE` + `FX_RATE` from parent
- Schema gate — defensive check on Auto Loader empty-array inference
- Cross-table reconciliation (06 ↔ 02 on `TOTAL_IGTAX_AMT`)

### `_shared/` helper extraction (done)

After all six POS notebooks were working, extracted four shared modules:

```
_shared/
├── surrogate_keys.py    tran_seq_no_expr()
├── fx_helpers.py        enrich_with_parent_fx(df, parent_table, join_keys)
├── quarantine.py        merge_and_quarantine(clean_df, rejects_df, target, quarantine, merge_keys)
└── schema_gate.py       bronze_array_has_inner_fields(source_table, array_column)
```

All six POS notebooks refactored to use these helpers. Pass-1 row counts
re-confirmed after refactor — identical. No semantic drift.

---

## Pass-2 — Marketplace (NEXT)

E-commerce marketplace data (Amazon Seller-style or custom generator).
Different bronze shape, same silver targets, distinguished by `RTLOG_ORIG_SYS='MKT'`.

### Build order (planned)
1. `01_sa_tran_head/marketplace.py` — order header
2. `02_sa_tran_item/marketplace.py` — order line items
3. `04_sa_tran_tender/marketplace.py` — payment methods
4. `06_sa_tran_igtax/marketplace.py` — VAT items (if marketplace serves VAT regions)
5. `03_sa_tran_disc/marketplace.py` — promotions (if marketplace emits them)
6. `05_sa_tran_tax/marketplace.py` — US-style sales tax (likely populates here for US orders)

### What's expected to be new
- Cross-border tenders — `ORIG_CURRENCY ≠ CURRENCY_CODE` finally exercised
- More USA / GBR data — `sa_tran_tax` finally populated
- Order-level discounts vs line-level discounts — may need PK refinement
- Customer dimension — new dim table likely needed (`sa_customer`?)

### Open design questions
- Will marketplace orders share `bronze.fx_rates` with POS, or have their own rate table?
- How do marketplace order statuses (PENDING / SHIPPED / DELIVERED / CANCELLED)
  map to ReSA `TRAN_TYPE`?
- Does each order produce a single ReSA transaction at order time, or one per shipment?

---

## Pass-3 — Olist (AFTER PASS-2)

Brazilian e-commerce Kaggle dataset (`olist_orders_dataset`, etc.). Narrower
coverage than marketplace.

### Coverage (planned)
- `01_sa_tran_head/olist.py` — `olist_orders_dataset` → tran_head
- `02_sa_tran_item/olist.py` — `olist_order_items_dataset` → tran_item
- `04_sa_tran_tender/olist.py` — `olist_order_payments_dataset` → tran_tender

### Skipped (Olist doesn't carry this data)
- `03_sa_tran_disc` — no per-line discount entity in Olist
- `05_sa_tran_tax`, `06_sa_tran_igtax` — Brazilian taxes typically baked into
  price; not broken out per line in the dataset

### What's expected to be new
- BRL (Brazilian Real) currency — new entry in `fx_rates`
- Multi-table source — Olist is a relational dataset (~7 CSVs that join),
  not a single JSON stream. Bronze ingestion will look different.
- Customer + seller dimensions — Olist has both, opens the door to a more
  complete `sa_customer` / `sa_supplier` story

---

## Module 4 — Audit & reconciliation (AFTER PASS-3)

- `sa_error` ingestion from rows flagged `ERROR_IND='Y'` across silver
- Reconciliation reports (header VALUE_USD vs sum of item UNIT_RETAIL_USD * QTY, etc.)
- Tender balancing (sum of tenders vs header VALUE)
- Tax reconciliation (covered partially by 06's existing recon)

---

## Gold layer (NOT STARTED)

Aggregates and analytic models on top of silver. Planned topics:
- Sales by store-day (with weather / holiday joins)
- Channel comparison (POS vs MKT vs OLIST)
- Customer lifetime value (LTV)
- Promotion effectiveness
- Stock-out / inventory analytics (if RMS dimension data is loaded)

---

## Second portfolio project (PARALLEL, SEPARATE REPO)

Microsoft Fabric supply chain intelligence — leverages the DP-700 cert.
Built on Data Factory, Dataflow Gen2, PySpark notebooks, pipeline observability.
Not part of this repo.

---

## SQL analytics showcase (PARALLEL)

15–20 queries of increasing complexity on top of the silver / gold layers.
Demonstrates SQL chops independent of the PySpark / Databricks engineering.
Probably lives in a `sql/` folder at the repo root once silver is feature-complete.
