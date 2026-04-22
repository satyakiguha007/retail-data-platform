# ReSA Canonical Model — Reference

**Source:** Oracle Retail Merchandising System (RMS) 16.0 Data Model
**Extracted:** April 2026
**Purpose:** Authoritative column definitions for the 11 core ReSA tables used in this project, plus one `_REV` table as a revision-history demonstrator.

This document is the source of truth. Where our DLT / PySpark code defines a column, it must match the name, type, and nullability here. Deviations (e.g. dropping unused columns in our simplified model) are called out explicitly.

---

## Table of Contents
1. [Core 11 Scope](#core-11-scope)
2. [Conventions](#conventions)
3. [SA_STORE_DAY](#sa_store_day)
4. [SA_TRAN_HEAD](#sa_tran_head)
5. [SA_TRAN_ITEM](#sa_tran_item)
6. [SA_TRAN_DISC](#sa_tran_disc)
7. [SA_TRAN_TENDER](#sa_tran_tender)
8. [SA_TRAN_TAX](#sa_tran_tax)
9. [SA_TRAN_IGTAX](#sa_tran_igtax)
10. [SA_MISSING_TRAN](#sa_missing_tran)
11. [SA_ERROR](#sa_error)
12. [SA_ERROR_CODES](#sa_error_codes)
13. [SA_TOTAL_HEAD](#sa_total_head)
14. [SA_TRAN_HEAD_REV — revision history demo](#sa_tran_head_rev)
15. [Multi-channel convention — RTLOG_ORIG_SYS](#rtlog_orig_sys)
16. [Code Types we reference](#code-types)

---

## Core 11 Scope

| # | Table | Role | In-scope cols |
|---|---|---|---|
| 1 | `SA_STORE_DAY` | Parent — one row per store-business-date | 13 |
| 2 | `SA_TRAN_HEAD` | Transaction header — anchor row | 39 |
| 3 | `SA_TRAN_ITEM` | Line items | 51 |
| 4 | `SA_TRAN_DISC` | Discounts per line | 22 |
| 5 | `SA_TRAN_TENDER` | Tender / payment | 29 |
| 6 | `SA_TRAN_TAX` | Tax breakdown | 11 |
| 7 | `SA_TRAN_IGTAX` | Inclusive (VAT-style) tax | 16 |
| 8 | `SA_MISSING_TRAN` | Gap detection | 6 |
| 9 | `SA_ERROR` | Audit-rule failures | 17 |
| 10 | `SA_ERROR_CODES` | Lookup for error types | 12 |
| 11 | `SA_TOTAL_HEAD` | Category totals (reconciliation) | 31 |
| (demo) | `SA_TRAN_HEAD_REV` | Revision history pattern | 39 + REV_NO |

**Out of scope (v1):**
- All other `_REV` tables (`SA_TRAN_ITEM_REV`, `SA_TRAN_TENDER_REV`, etc.)
- `_TL` translation tables
- Rule-engine tables (`SA_RULE_HEAD`, `SA_RULE_COMP`, `SA_RULE_ERRORS`, …)
- `SA_VOUCHER*`, `SA_ESCHEAT*`, `SA_BANK*`, `SA_ACH*`
- `SA_REALM*`, `SA_ROLE_FIELD` (security machinery)
- Worksheet / temp tables (`SA_*_WKSHT`, `SA_*_TEMP`)

---

## Conventions

- **Data types** preserve Oracle types (NUMERIC, VARCHAR, Date). In Delta we map as: NUMERIC(p) → `BIGINT` or `DECIMAL(p,s)`, VARCHAR → `STRING`, Date → `TIMESTAMP`.
- **Flag columns** `*_IND` use `'Y'` / `'N'` — we preserve this for fidelity, not `true`/`false`.
- **PK** = primary key component
- **FK** = foreign key
- **M** = mandatory (NOT NULL)
- **Default** column shows Oracle default values where present.
- Partition strategy in Delta: `STORE` + `DAY` (matches Oracle's partitioning by those same columns).

---

## SA_STORE_DAY

> One row per store + business-date combination. Parent of all transaction tables via `STORE_DAY_SEQ_NO`. Tracks import/audit workflow state.

| # | Column | PK | FK | M | Type | Notes |
|---|---|---|---|---|---|---|
| 1 | STORE_DAY_SEQ_NO | P | | Y | NUMERIC(20) | System-generated ID |
| 2 | BUSINESS_DATE | | | Y | Date | |
| 3 | STORE | | | Y | NUMERIC(10) | |
| 4 | DAY | | | Y | NUMERIC(3) | Derived via SA_DATE_HASH |
| 5 | INV_BUS_DATE_IND | | | Y | VARCHAR(1) | Invalid-business-date flag |
| 6 | INV_STORE_IND | | | Y | VARCHAR(1) | Invalid-store flag |
| 7 | STORE_STATUS | | | Y | VARCHAR(6) | code_type SASS |
| 8 | STORE_CLOSED_DATETIME | | | | Date | |
| 9 | DATA_STATUS | | | Y | VARCHAR(6) | P=partial, F=full, G=purging |
| 10 | AUDIT_STATUS | | | Y | VARCHAR(6) | code_type SAAS |
| 11 | AUDIT_CHANGED_DATETIME | | | | Date | |
| 12 | FILES_LOADED | | | | NUMERIC(10) | # of RTLOGs loaded |
| 13 | OMS_FILES_LOADED | | | | NUMERIC(10) | # of OMS files loaded |

**PK:** STORE_DAY_SEQ_NO

---

## SA_TRAN_HEAD

> Base-level information about each transaction processed by sales audit. One row per transaction. **The anchor.**

| # | Column | PK | FK | M | Type | Notes |
|---|---|---|---|---|---|---|
| 1 | STORE | P | | Y | NUMERIC(10) | Partitioning |
| 2 | DAY | P | | Y | NUMERIC(3) | Partitioning (SA_DATE_HASH) |
| 3 | TRAN_SEQ_NO | P | | Y | NUMERIC(20) | Sales-audit tran ID |
| 4 | REV_NO | | | Y | NUMERIC(3) | Revision counter |
| 5 | STORE_DAY_SEQ_NO | | F | Y | NUMERIC(20) | → SA_STORE_DAY |
| 6 | TRAN_DATETIME | | | Y | Date(7) | |
| 7 | REGISTER | | | | VARCHAR(5) | Till ID |
| 8 | TRAN_NO | | | | NUMERIC(10) | POS-assigned, unique per register |
| 9 | CASHIER | | | | VARCHAR(10) | |
| 10 | SALESPERSON | | | | VARCHAR(10) | |
| 11 | TRAN_TYPE | | | Y | VARCHAR(6) | code_type TRAT — SALE/RETURN/PVOID/PAIDIN/PAIDOUT/NOSALE/… |
| 12 | SUB_TRAN_TYPE | | | | VARCHAR(6) | code_type TRAS |
| 13 | ORIG_TRAN_NO | | | | NUMERIC(10) | For post-void |
| 14 | ORIG_TRAN_TYPE | | | | VARCHAR(6) | For post-void |
| 15 | ORIG_REG_NO | | | | VARCHAR(5) | For post-void |
| 16 | REF_NO1 | | | | VARCHAR(30) | Flex field |
| 17 | REF_NO2 | | | | VARCHAR(30) | Flex field |
| 18 | REF_NO3 | | | | VARCHAR(30) | Flex field |
| 19 | REF_NO4 | | | | VARCHAR(30) | Flex field |
| 20 | REASON_CODE | | | | VARCHAR(6) | |
| 21 | VENDOR_NO | | | | VARCHAR(10) | For PAIDIN/PAIDOUT |
| 22 | VENDOR_INVC_NO | | | | VARCHAR(30) | |
| 23 | PAYMENT_REF_NO | | | | VARCHAR(16) | |
| 24 | PROOF_OF_DELIVERY_NO | | | | VARCHAR(30) | |
| 25 | STATUS | | | Y | VARCHAR(6) | Workflow: imported/validated/errored/approved/posted |
| 26 | VALUE | | | | NUMERIC(20,4) | |
| 27 | POS_TRAN_IND | | | Y | VARCHAR(1) | Y/N |
| 28 | UPDATE_DATETIME | | | Y | Date(7) | |
| 29 | UPDATE_ID | | | Y | VARCHAR(30) | |
| 30 | ERROR_IND | | | Y | VARCHAR(1) | Y if SA_ERROR row exists |
| 31 | BANNER_NO | | | | NUMERIC(4) | Multi-banner retailers |
| 32 | ROUNDED_AMT | | | | NUMERIC(20,4) | Rounded total |
| 33 | ROUNDED_OFF_AMT | | | | NUMERIC(20,4) | Rounding delta |
| 34 | CREDIT_PROMOTION_ID | | | | NUMERIC(10) | |
| 35 | REF_NO25 | | | | VARCHAR(30) | Flex field |
| 36 | REF_NO26 | | | | VARCHAR(30) | Flex field |
| 37 | REF_NO27 | | | | VARCHAR(30) | Flex field |
| 38 | RTLOG_ORIG_SYS | | | Y | VARCHAR(3) | Default `'POS'`. **Channel discriminator — see § RTLOG_ORIG_SYS** |
| 39 | TRAN_PROCESS_SYS | | | | VARCHAR(3) | |

**PK:** (STORE, DAY, TRAN_SEQ_NO)

---

## SA_TRAN_ITEM

> Details about each item contained in a transaction. Child of SA_TRAN_HEAD.

| # | Column | PK | FK | M | Type | Notes |
|---|---|---|---|---|---|---|
| 1 | STORE | P | F | Y | NUMERIC(10) | |
| 2 | DAY | P | F | Y | NUMERIC(3) | |
| 3 | TRAN_SEQ_NO | P | F | Y | NUMERIC(20) | → SA_TRAN_HEAD |
| 4 | ITEM_SEQ_NO | P | | Y | NUMERIC(4) | Line number |
| 5 | ITEM_STATUS | | | Y | VARCHAR(6) | code_type SASI |
| 6 | ITEM_TYPE | | | Y | VARCHAR(6) | ITM/GCN/REF/TND etc. |
| 7 | ITEM | | | | VARCHAR(25) | SKU |
| 8 | REF_ITEM | | | | VARCHAR(25) | Reference item for packs |
| 9 | NON_MERCH_ITEM | | | | VARCHAR(25) | Non-merch codes |
| 10 | VOUCHER_NO | | | | VARCHAR(25) | |
| 11 | DEPT | | | | NUMERIC(4) | Merchandise hierarchy |
| 12 | CLASS | | | | NUMERIC(4) | |
| 13 | SUBCLASS | | | | NUMERIC(4) | |
| 14 | QTY | | | | NUMERIC(12,4) | |
| 15 | UNIT_RETAIL | | | | NUMERIC(20,4) | Sale price |
| 16 | SELLING_UOM | | | | VARCHAR(4) | |
| 17 | OVERRIDE_REASON | | | | VARCHAR(6) | |
| 18 | ORIG_UNIT_RETAIL | | | | NUMERIC(20,4) | Pre-override |
| 19 | STANDARD_ORIG_UNIT_RETAIL | | | | NUMERIC(20,4) | |
| 20 | TAX_IND | | | Y | VARCHAR(1) | |
| 21 | ITEM_SWIPED_IND | | | Y | VARCHAR(1) | |
| 22 | ERROR_IND | | | Y | VARCHAR(1) | |
| 23 | DROP_SHIP_IND | | | Y | VARCHAR(1) | Marketplace flag |
| 24 | WASTE_TYPE | | | | VARCHAR(6) | |
| 25 | WASTE_PCT | | | | NUMERIC(12,4) | |
| 26 | PUMP | | | | VARCHAR(8) | Fuel pumps |
| 27 | RETURN_REASON_CODE | | | | VARCHAR(6) | |
| 28 | SALESPERSON | | | | VARCHAR(10) | |
| 29 | EXPIRATION_DATE | | | | Date(7) | |
| 30 | STANDARD_QTY | | | | NUMERIC(12,4) | In standard UOM |
| 31 | STANDARD_UNIT_RETAIL | | | | NUMERIC(20,4) | |
| 32 | STANDARD_UOM | | | | VARCHAR(4) | |
| 33-36 | REF_NO5…REF_NO8 | | | | VARCHAR(30) | Flex fields |
| 37 | UOM_QUANTITY | | | Y | NUMERIC(12,4) | |
| 38 | CATCHWEIGHT_IND | | | | VARCHAR(1) | Variable-weight grocery |
| 39 | SELLING_ITEM | | | | VARCHAR(25) | |
| 40 | CUSTOMER_ORDER_LINE_NO | | | | NUMERIC(6) | |
| 41 | MEDIA_ID | | | | NUMERIC(10) | |
| 42 | UNIT_RETAIL_VAT_INCL | | | Y | VARCHAR(1) | Default `'N'` |
| 43 | TOTAL_IGTAX_AMT | | | | NUMERIC(20,4) | |
| 44 | UNIQUE_ID | | | | VARCHAR(128) | |
| 45 | CUST_ORDER_NO | | | | VARCHAR(48) | **E-commerce linkage** |
| 46 | CUST_ORDER_DATE | | | | Date(7) | |
| 47 | FULFILL_ORDER_NO | | | | VARCHAR(48) | |
| 48 | NO_INV_RET_IND | | | | VARCHAR(1) | |
| 49 | RETURN_WH | | | | NUMERIC(10) | |
| 50 | SALES_TYPE | | | | VARCHAR(1) | Default `'R'` |
| 51 | RETURN_DISPOSITION | | | | VARCHAR(10) | |

**PK:** (STORE, DAY, TRAN_SEQ_NO, ITEM_SEQ_NO)

---

## SA_TRAN_DISC

> Discounts applied per line item. One row per (item × discount).

| # | Column | PK | FK | M | Type | Notes |
|---|---|---|---|---|---|---|
| 1 | STORE | P | F | Y | NUMERIC(10) | |
| 2 | DAY | P | F | Y | NUMERIC(3) | |
| 3 | TRAN_SEQ_NO | P | F | Y | NUMERIC(20) | |
| 4 | ITEM_SEQ_NO | P | F | Y | NUMERIC(4) | → SA_TRAN_ITEM |
| 5 | DISCOUNT_SEQ_NO | P | | Y | NUMERIC(4) | |
| 6 | RMS_PROMO_TYPE | P | | Y | VARCHAR(6) | |
| 7 | PROMOTION | | | | NUMERIC(10) | Promotion ID |
| 8 | DISC_TYPE | | | | VARCHAR(6) | PROMO / MANUAL / COUPON / EMP |
| 9 | COUPON_NO | | | | VARCHAR(40) | |
| 10 | COUPON_REF_NO | | | | VARCHAR(16) | |
| 11 | QTY | | | | NUMERIC(12,4) | |
| 12 | UNIT_DISCOUNT_AMT | | | | NUMERIC(20,4) | |
| 13 | STANDARD_QTY | | | | NUMERIC(12,4) | |
| 14 | STANDARD_UNIT_DISC_AMT | | | | NUMERIC(20,4) | |
| 15-18 | REF_NO13…REF_NO16 | | | | VARCHAR(30) | Flex |
| 19 | ERROR_IND | | | Y | VARCHAR(1) | |
| 20 | UOM_QUANTITY | | | Y | NUMERIC(12,4) | |
| 21 | CATCHWEIGHT_IND | | | | VARCHAR(1) | |
| 22 | PROMO_COMP | | | | NUMERIC(10) | Promotion component ID |

**PK:** (STORE, DAY, TRAN_SEQ_NO, ITEM_SEQ_NO, DISCOUNT_SEQ_NO, RMS_PROMO_TYPE)

---

## SA_TRAN_TENDER

> Tender / payment records. One row per tender used in the transaction.

| # | Column | PK | FK | M | Type | Notes |
|---|---|---|---|---|---|---|
| 1 | STORE | P | F | Y | NUMERIC(10) | |
| 2 | DAY | P | F | Y | NUMERIC(3) | |
| 3 | TRAN_SEQ_NO | P | F | Y | NUMERIC(20) | |
| 4 | TENDER_SEQ_NO | P | | Y | NUMERIC(4) | |
| 5 | TENDER_TYPE_GROUP | | | Y | VARCHAR(6) | CASH/CARD/CHECK/VOUCHER/… |
| 6 | TENDER_TYPE_ID | | | Y | NUMERIC(6) | |
| 7 | TENDER_AMT | | | Y | NUMERIC(20,4) | |
| 8 | CC_NO | | | | VARCHAR(40) | **Store masked only** |
| 9 | CC_EXP_DATE | | | | Date(7) | |
| 10 | CC_AUTH_NO | | | | VARCHAR(16) | |
| 11 | CC_AUTH_SRC | | | | VARCHAR(6) | |
| 12 | CC_ENTRY_MODE | | | | VARCHAR(6) | Swipe/chip/contactless/manual |
| 13 | CC_CARDHOLDER_VERF | | | | VARCHAR(6) | |
| 14 | CC_TERM_ID | | | | VARCHAR(5) | |
| 15 | CC_SPEC_COND | | | | VARCHAR(6) | |
| 16 | VOUCHER_NO | | | | VARCHAR(25) | |
| 17 | COUPON_NO | | | | VARCHAR(40) | |
| 18 | COUPON_REF_NO | | | | VARCHAR(16) | |
| 19-22 | REF_NO9…REF_NO12 | | | | VARCHAR(30) | Flex |
| 23 | ERROR_IND | | | Y | VARCHAR(1) | |
| 24 | CHECK_ACCT_NO | | | | VARCHAR(30) | |
| 25 | CHECK_NO | | | | NUMERIC(10) | |
| 26 | IDENTI_METHOD | | | | VARCHAR(6) | |
| 27 | IDENTI_ID | | | | VARCHAR(40) | |
| 28 | ORIG_CURRENCY | | | | VARCHAR(3) | **Multi-currency** |
| 29 | ORIG_CURR_AMT | | | | NUMERIC(20,4) | Amount in original currency |

**PK:** (STORE, DAY, TRAN_SEQ_NO, TENDER_SEQ_NO)

---

## SA_TRAN_TAX

> Standard (add-on) tax records — sales tax / GST style.

| # | Column | PK | FK | M | Type | Notes |
|---|---|---|---|---|---|---|
| 1 | STORE | P | F | Y | NUMERIC(10) | |
| 2 | DAY | P | F | Y | NUMERIC(3) | |
| 3 | TRAN_SEQ_NO | P | F | Y | NUMERIC(20) | |
| 4 | TAX_CODE | | | Y | VARCHAR(6) | |
| 5 | TAX_SEQ_NO | P | | Y | NUMERIC(4) | |
| 6 | TAX_AMT | | | Y | NUMERIC(20,4) | |
| 7 | ERROR_IND | | | Y | VARCHAR(1) | |
| 8-11 | REF_NO17…REF_NO20 | | | | VARCHAR(30) | Flex |

**PK:** (STORE, DAY, TRAN_SEQ_NO, TAX_SEQ_NO)

---

## SA_TRAN_IGTAX

> "Ignorable" / inclusive tax (VAT-style) — tax already baked into `UNIT_RETAIL`. Keyed per item per tax authority.

| # | Column | PK | FK | M | Type | Notes |
|---|---|---|---|---|---|---|
| 1 | STORE | P | F | Y | NUMERIC(10) | |
| 2 | DAY | P | F | Y | NUMERIC(3) | |
| 3 | TRAN_SEQ_NO | P | F | Y | NUMERIC(20) | |
| 4 | ITEM_SEQ_NO | P | F | Y | NUMERIC(4) | |
| 5 | IGTAX_SEQ_NO | P | | Y | NUMERIC(4) | |
| 6 | TAX_AUTHORITY | | | Y | VARCHAR(10) | |
| 7 | IGTAX_CODE | | | Y | VARCHAR(6) | maps to VAT_CODES |
| 8 | IGTAX_RATE | | | | NUMERIC(20,4) | |
| 9 | TOTAL_IGTAX_AMT | | | Y | NUMERIC(20,4) | |
| 10 | STANDARD_QTY | | | | NUMERIC(12,4) | |
| 11 | STANDARD_UNIT_IGTAX_AMT | | | | NUMERIC(20,4) | |
| 12 | ERROR_IND | | | Y | VARCHAR(1) | |
| 13-16 | REF_NO21…REF_NO24 | | | | VARCHAR(30) | Flex |

**PK:** (STORE, DAY, TRAN_SEQ_NO, ITEM_SEQ_NO, IGTAX_SEQ_NO)

---

## SA_MISSING_TRAN

> Gap-detection. When ReSA sees `TRAN_NO` 1, 2, 4, 5 from register X, it writes `3` here. Populated during import reconciliation.

| # | Column | PK | FK | M | Type | Notes |
|---|---|---|---|---|---|---|
| 1 | MISS_TRAN_SEQ_NO | P | | Y | NUMERIC(20) | |
| 2 | STORE_DAY_SEQ_NO | | | Y | NUMERIC(20) | → SA_STORE_DAY |
| 3 | REGISTER | | | | VARCHAR(5) | |
| 4 | TRAN_NO | | | Y | NUMERIC(10) | The missing number |
| 5 | STATUS | | | Y | VARCHAR(6) | Resolution state |
| 6 | RTLOG_ORIG_SYS | | | Y | VARCHAR(3) | Default `'POS'` |

---

## SA_ERROR

> Every audit-rule failure lands here. One row per error occurrence.

| # | Column | PK | FK | M | Type | Notes |
|---|---|---|---|---|---|---|
| 1 | STORE | P | F | Y | NUMERIC(10) | |
| 2 | DAY | P | F | Y | NUMERIC(3) | |
| 3 | ERROR_SEQ_NO | P | | Y | NUMERIC(20) | |
| 4 | STORE_DAY_SEQ_NO | | F | Y | NUMERIC(20) | |
| 5 | BAL_GROUP_SEQ_NO | | F | Y | NUMERIC(20) | Balance group |
| 6 | TOTAL_SEQ_NO | | F | Y | NUMERIC(20) | Which total failed (if applicable) |
| 7 | TRAN_SEQ_NO | | F | Y | NUMERIC(20) | Which tran failed (if applicable) |
| 8 | ERROR_CODE | | F | Y | VARCHAR(25) | → SA_ERROR_CODES |
| 9 | KEY_VALUE_1 | | | | NUMERIC(4) | e.g. item_seq_no |
| 10 | KEY_VALUE_2 | | | | NUMERIC(4) | |
| 11 | REC_TYPE | | | Y | VARCHAR(6) | SART (tran) / SABG (bal group) / SATT (total) |
| 12 | STORE_OVERRIDE_IND | | | Y | VARCHAR(1) | |
| 13 | HQ_OVERRIDE_IND | | | Y | VARCHAR(1) | |
| 14 | UPDATE_ID | | | Y | VARCHAR(30) | |
| 15 | UPDATE_DATETIME | | | Y | Date | |
| 16 | ORIG_VALUE | | | | VARCHAR(70) | Captured original value |
| 17 | ORIG_CC_NO | | | | VARCHAR(40) | PII-protected |

**PK:** (STORE, DAY, ERROR_SEQ_NO)
**Check constraint:** exactly one of `BAL_GROUP_SEQ_NO` / `TOTAL_SEQ_NO` / `TRAN_SEQ_NO` is populated.

---

## SA_ERROR_CODES

> Lookup table for error types. We seed this with our 18 audit-rule codes.

| # | Column | PK | M | Type | Notes |
|---|---|---|---|---|---|
| 1 | ERROR_CODE | P | Y | VARCHAR(25) | e.g. `TENDER_VAR_GT_01` |
| 2 | ERROR_DESC | | Y | VARCHAR(255) | Human-readable |
| 3 | TARGET_FORM | | | VARCHAR(6) | UI hint |
| 4 | TARGET_TAB | | | VARCHAR(6) | UI hint |
| 5 | REC_SOLUTION | | | VARCHAR(255) | Suggested fix |
| 6 | STORE_OVERRIDE_IND | | Y | VARCHAR(1) | Default `'N'` |
| 7 | HQ_OVERRIDE_IND | | Y | VARCHAR(1) | |
| 8 | REQUIRED_IND | | Y | VARCHAR(1) | Blocks posting if Y |
| 9 | SHORT_DESC | | Y | VARCHAR(40) | |
| 10 | MASS_RES_POP_UP_TYPE | | | VARCHAR(20) | |
| 11 | ERROR_FIX_TABLE | | | VARCHAR(30) | Where to fix |
| 12 | ERROR_FIX_COLUMN | | | VARCHAR(30) | |

---

## SA_TOTAL_HEAD

> Defines named totals (buckets of value) that get calculated and reconciled. Example totals: `TNDCASH`, `TNDCARD`, `TAXTOTAL`, `DISCTOTAL`. Basis for channel-level reconciliation rules.

| # | Column | PK | M | Type | Notes |
|---|---|---|---|---|---|
| 1 | TOTAL_ID | P | Y | VARCHAR(10) | |
| 2 | TOTAL_REV_NO | P | Y | NUMERIC(3) | Rev of definition |
| 3 | VR_ID | | | VARCHAR(15) | Version rule |
| 4 | VR_REV_NO | | | NUMERIC(3) | |
| 5 | TOTAL_DESC | | Y | VARCHAR(255) | |
| 6 | TOTAL_TYPE | | Y | VARCHAR(1) | |
| 7 | OS_GROUP | | | VARCHAR(1) | |
| 8 | OS_OPERATOR | | | VARCHAR(1) | |
| 9 | COMB_TOTAL_IND | | Y | VARCHAR(1) | Combined total? |
| 10 | TOTAL_CAT | | Y | VARCHAR(6) | Category |
| 11 | BAL_LEVEL | | Y | VARCHAR(1) | |
| 12 | UPDATE_DATETIME | | Y | Date | |
| 13 | UPDATE_ID | | Y | VARCHAR(30) | |
| 14 | COUNT_SUM_IND | | Y | VARCHAR(1) | Count vs sum |
| 15 | POS_IND | | Y | VARCHAR(1) | POS-sourced |
| 16 | SYS_CALC_IND | | Y | VARCHAR(1) | System-calculated |
| 17 | STORE_UPDATE_IND | | Y | VARCHAR(1) | |
| 18 | HQ_UPDATE_IND | | Y | VARCHAR(1) | |
| 19 | REQ_IND | | Y | VARCHAR(1) | Required |
| 20 | WIZ_IND | | Y | VARCHAR(1) | |
| 21 | START_BUSINESS_DATE | | Y | Date | |
| 22 | END_BUSINESS_DATE | | | Date | |
| 23 | GROUP_SEQ_NO1 | | | NUMERIC(3) | |
| 24 | REF_LABEL_CODE_1 | | | VARCHAR(6) | |
| 25 | GROUP_SEQ_NO2 | | | NUMERIC(3) | |
| 26 | REF_LABEL_CODE_2 | | | VARCHAR(6) | |
| 27 | GROUP_SEQ_NO3 | | | NUMERIC(3) | |
| 28 | REF_LABEL_CODE_3 | | | VARCHAR(6) | |
| 29 | TOTAL_PARM_SEQ_NO | | Y | NUMERIC(3) | |
| 30 | DISPLAY_ORDER | | Y | VARCHAR(6) | |
| 31 | STATUS | | | VARCHAR(6) | |

---

## SA_TRAN_HEAD_REV

> **Revision history demonstrator.** Every time a row in `SA_TRAN_HEAD` is updated (e.g. an auditor corrects a value), a snapshot is written here with an incremented `REV_NO`. All 39 columns match `SA_TRAN_HEAD` exactly; `REV_NO` joins the PK.

**PK:** (STORE, DAY, TRAN_SEQ_NO, REV_NO)

We implement this on `SA_TRAN_HEAD` only (the other `_REV` tables in real ReSA are out of scope for v1). The implementation uses a Databricks Delta CDF (Change Data Feed) stream off `SA_TRAN_HEAD` that appends into `SA_TRAN_HEAD_REV`. This showcases:
- Delta CDF as a change-capture mechanism
- Type-2-style history without a separate MERGE pipeline
- How the audit trail supports "who changed what, when" queries

---

## RTLOG_ORIG_SYS — channel discriminator

`RTLOG_ORIG_SYS` is the column we use to separate our three source channels into a single canonical model:

| Value | Channel | Source in our project |
|---|---|---|
| `POS` | In-store point-of-sale | Module 1 POS RTLOG simulator |
| `OMS` | Order management system (e-commerce) | Olist dataset, transformed in Silver |
| `MKT` | Marketplace aggregator (simulated) | Simulated feed for third channel |

Every downstream query that wants channel-level analysis filters on this column. Every audit rule runs the same logic across channels, but results can be grouped by `RTLOG_ORIG_SYS` for channel-level variance dashboards.

---

## Code Types we reference

ReSA uses the `CODES` / `CODE_DETAIL` tables (out of our 11 but populated as seed data) for enumerations. We seed the minimum needed:

| code_type | Meaning | Values we use |
|---|---|---|
| `TRAT` | Transaction types | SALE, RETURN, PVOID, PAIDIN, PAIDOUT, NOSALE, OPEN, CLOSE |
| `TRAS` | Sub-transaction types | (reserved) |
| `SASS` | Store statuses | OPEN, CLOSED |
| `SAAS` | Audit statuses | UNAUDITED, INPROGRESS, AUDITED, POSTED |
| `SASI` | Item statuses | ACTIVE, VOIDED, REFUNDED |
| `SART` | Error rec types | SART=tran, SABG=bal group, SATT=total |

---

*End of reference. Source: Oracle RMS 16.0 Data Model (2017 release). Extracted for educational/portfolio use.*
