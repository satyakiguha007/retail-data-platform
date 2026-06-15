# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — `01_dim_date.py`
# MAGIC
# MAGIC Conformed date dimension. Generated, not sourced from silver.
# MAGIC
# MAGIC | Item | Value |
# MAGIC |---|---|
# MAGIC | **Target** | `retaildp.gold_core.dim_date` |
# MAGIC | **Grain** | One row per calendar date |
# MAGIC | **Natural key** | `full_date` (DATE, unique) |
# MAGIC | **Surrogate key** | `date_key` (BIGINT, IDENTITY) |
# MAGIC | **Write mode** | MERGE on `full_date` — re-runs are idempotent |
# MAGIC | **Extension** | Change `end_date` widget and re-run. MERGE inserts only gap rows. |
# MAGIC | **Default range** | 2023-01-01 → 2027-12-31 (5 years) |
# MAGIC
# MAGIC ## How extension works
# MAGIC On re-run with a wider `end_date`:
# MAGIC - All rows for `[start_date, end_date]` are generated in memory
# MAGIC - MERGE on `full_date` → existing rows untouched, missing rows inserted
# MAGIC - IDENTITY auto-assigns `date_key` to new rows
# MAGIC
# MAGIC ## ⚠ IDENTITY caveat
# MAGIC `GENERATED ALWAYS AS IDENTITY` assigns keys **monotonically but not contiguously in
# MAGIC chronological order**. If you ever insert 2022 dates retroactively, they get higher
# MAGIC `date_key` values than 2023 dates. This is fine — surrogate keys are meaningless integers.
# MAGIC **Always order by `full_date`, never by `date_key`.**
# MAGIC
# MAGIC ## Fiscal year convention
# MAGIC Retail-standard April start. Fiscal year is named by the year it **starts** in
# MAGIC (FY2026 = Apr 2026 – Mar 2027). Matches ReSA / Oracle Retail and Indian retail convention.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Widgets & configuration

# COMMAND ----------

dbutils.widgets.text("start_date", "2023-01-01", "Date range start (YYYY-MM-DD)")
dbutils.widgets.text("end_date",   "2027-12-31", "Date range end (YYYY-MM-DD)")

CATALOG     = "retaildp"
SCHEMA      = "gold_core"
TABLE       = "dim_date"
TABLE_FQN   = f"{CATALOG}.{SCHEMA}.{TABLE}"

START_DATE = dbutils.widgets.get("start_date")
END_DATE   = dbutils.widgets.get("end_date")

print(f"Target:     {TABLE_FQN}")
print(f"Date range: {START_DATE}  →  {END_DATE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create `dim_date` (idempotent)
# MAGIC
# MAGIC Skip Liquid Clustering — dim_date is small (~1,826 rows for 5 years) and accessed by FK
# MAGIC join from facts. The IDENTITY column plus a Delta default file layout is sufficient.

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {TABLE_FQN} (
    date_key             BIGINT     GENERATED ALWAYS AS IDENTITY   COMMENT 'Surrogate key — assigned on INSERT; NOT chronologically ordered',
    full_date            DATE       NOT NULL                       COMMENT 'Natural key — order by this, never by date_key',
    year                 INT        NOT NULL,
    quarter              INT        NOT NULL                       COMMENT 'Calendar quarter 1-4',
    month                INT        NOT NULL                       COMMENT 'Calendar month 1-12',
    month_name           STRING     NOT NULL                       COMMENT 'January..December',
    month_short          STRING     NOT NULL                       COMMENT 'Jan..Dec',
    year_month           STRING     NOT NULL                       COMMENT 'YYYY-MM, sortable',
    year_quarter         STRING     NOT NULL                       COMMENT 'YYYY-Qn',
    week_of_year         INT        NOT NULL,
    day_of_year          INT        NOT NULL,
    day_of_month         INT        NOT NULL,
    day_of_week          INT        NOT NULL                       COMMENT 'Spark dayofweek: 1=Sunday..7=Saturday',
    day_name             STRING     NOT NULL                       COMMENT 'Sunday..Saturday',
    day_short            STRING     NOT NULL                       COMMENT 'Sun..Sat',
    is_weekend           BOOLEAN    NOT NULL,
    is_month_start       BOOLEAN    NOT NULL,
    is_month_end         BOOLEAN    NOT NULL,
    is_quarter_end       BOOLEAN    NOT NULL,
    is_year_end          BOOLEAN    NOT NULL,
    fiscal_year          INT        NOT NULL                       COMMENT 'April-start; named by starting year (FY2026 = Apr 2026 – Mar 2027)',
    fiscal_quarter       INT        NOT NULL                       COMMENT 'Apr-Jun=1, Jul-Sep=2, Oct-Dec=3, Jan-Mar=4',
    fiscal_month         INT        NOT NULL                       COMMENT 'April=1..March=12',
    fiscal_year_name     STRING     NOT NULL                       COMMENT 'FY2026',
    _ingest_ts           TIMESTAMP  NOT NULL  DEFAULT current_timestamp(),
    CONSTRAINT dim_date_full_date_uk UNIQUE (full_date)
)
USING DELTA
TBLPROPERTIES (
    'delta.feature.allowColumnDefaults' = 'supported',
    'delta.autoOptimize.optimizeWrite'  = 'true',
    'delta.autoOptimize.autoCompact'    = 'true'
)
COMMENT 'Conformed date dimension. Generated. April-start fiscal year. Extend via end_date widget.'
""")

print(f"{TABLE_FQN} ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Generate the date sequence
# MAGIC
# MAGIC Build all attributes from `full_date` in one pass. Spark's `sequence(DATE, DATE, INTERVAL)`
# MAGIC produces an array; `explode` flattens it into one row per date.

# COMMAND ----------

from pyspark.sql import functions as F
from pyspark.sql.types import IntegerType

staging = (
    spark.sql(f"""
        SELECT explode(sequence(DATE'{START_DATE}', DATE'{END_DATE}', INTERVAL 1 DAY)) AS full_date
    """)
    .select(
        F.col("full_date"),
        F.year("full_date").alias("year"),
        F.quarter("full_date").alias("quarter"),
        F.month("full_date").alias("month"),
        F.date_format("full_date", "MMMM").alias("month_name"),
        F.date_format("full_date", "MMM").alias("month_short"),
        F.date_format("full_date", "yyyy-MM").alias("year_month"),
        F.concat(F.year("full_date"), F.lit("-Q"), F.quarter("full_date")).alias("year_quarter"),
        F.weekofyear("full_date").alias("week_of_year"),
        F.dayofyear("full_date").alias("day_of_year"),
        F.dayofmonth("full_date").alias("day_of_month"),
        F.dayofweek("full_date").alias("day_of_week"),
        F.date_format("full_date", "EEEE").alias("day_name"),
        F.date_format("full_date", "EEE").alias("day_short"),
        F.dayofweek("full_date").isin(1, 7).alias("is_weekend"),
        (F.dayofmonth("full_date") == 1).alias("is_month_start"),
        (F.last_day("full_date") == F.col("full_date")).alias("is_month_end"),
        # Quarter end = last day of Mar, Jun, Sep, Dec
        ((F.last_day("full_date") == F.col("full_date")) &
         (F.month("full_date").isin(3, 6, 9, 12))).alias("is_quarter_end"),
        # Year end = Dec 31
        ((F.month("full_date") == 12) & (F.dayofmonth("full_date") == 31)).alias("is_year_end"),
        # Fiscal year: April-start, named by starting year
        F.when(F.month("full_date") >= 4, F.year("full_date"))
         .otherwise(F.year("full_date") - 1).alias("fiscal_year"),
        # Fiscal quarter: Apr-Jun=1, Jul-Sep=2, Oct-Dec=3, Jan-Mar=4
        F.when(F.month("full_date").isin(4, 5, 6),  1)
         .when(F.month("full_date").isin(7, 8, 9),  2)
         .when(F.month("full_date").isin(10, 11, 12), 3)
         .otherwise(4).alias("fiscal_quarter"),
        # Fiscal month: April=1..March=12
        (((F.month("full_date") - 4 + 12) % 12) + 1).cast(IntegerType()).alias("fiscal_month"),
    )
    .withColumn("fiscal_year_name", F.concat(F.lit("FY"), F.col("fiscal_year")))
)

print(f"Generated {staging.count():,} rows for [{START_DATE}, {END_DATE}]")
display(staging.orderBy("full_date").limit(10))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. MERGE into `dim_date`
# MAGIC
# MAGIC `WHEN NOT MATCHED THEN INSERT` is the only branch — existing rows are intentionally
# MAGIC untouched even if attribute logic changes. (If we ever *do* need to rewrite an attribute,
# MAGIC add a `WHEN MATCHED THEN UPDATE` branch; for now extensions are insert-only.)

# COMMAND ----------

staging.createOrReplaceTempView("dim_date_staging")

merge_result = spark.sql(f"""
    MERGE INTO {TABLE_FQN} AS tgt
    USING dim_date_staging   AS src
    ON tgt.full_date = src.full_date
    WHEN NOT MATCHED THEN INSERT (
        full_date, year, quarter, month, month_name, month_short,
        year_month, year_quarter, week_of_year, day_of_year, day_of_month,
        day_of_week, day_name, day_short, is_weekend, is_month_start,
        is_month_end, is_quarter_end, is_year_end, fiscal_year,
        fiscal_quarter, fiscal_month, fiscal_year_name
    ) VALUES (
        src.full_date, src.year, src.quarter, src.month, src.month_name, src.month_short,
        src.year_month, src.year_quarter, src.week_of_year, src.day_of_year, src.day_of_month,
        src.day_of_week, src.day_name, src.day_short, src.is_weekend, src.is_month_start,
        src.is_month_end, src.is_quarter_end, src.is_year_end, src.fiscal_year,
        src.fiscal_quarter, src.fiscal_month, src.fiscal_year_name
    )
""")

display(merge_result)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Validation

# COMMAND ----------

print("=== Row count & date coverage ===")
display(spark.sql(f"""
    SELECT
        COUNT(*)              AS total_rows,
        MIN(full_date)        AS min_date,
        MAX(full_date)        AS max_date,
        DATEDIFF(MAX(full_date), MIN(full_date)) + 1 AS expected_rows,
        COUNT(*) - (DATEDIFF(MAX(full_date), MIN(full_date)) + 1) AS gap_count
    FROM {TABLE_FQN}
"""))

# COMMAND ----------

print("=== Sample (10 rows around today) ===")
display(spark.sql(f"""
    SELECT date_key, full_date, day_name, year_quarter, fiscal_year_name, fiscal_quarter, is_weekend, is_month_end
    FROM {TABLE_FQN}
    WHERE full_date BETWEEN current_date() - INTERVAL 5 DAYS
                        AND current_date() + INTERVAL 5 DAYS
    ORDER BY full_date
"""))

# COMMAND ----------

print("=== Uniqueness check on full_date ===")
display(spark.sql(f"""
    SELECT
        COUNT(*)                  AS total_rows,
        COUNT(DISTINCT full_date) AS distinct_dates,
        COUNT(*) - COUNT(DISTINCT full_date) AS duplicates
    FROM {TABLE_FQN}
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Guardrail — forward coverage
# MAGIC
# MAGIC Warns if `max(full_date) - today` is below the buffer threshold. If you see this warning,
# MAGIC widen the `end_date` widget and re-run this notebook. Suggested buffer: 180 days.

# COMMAND ----------

BUFFER_DAYS = 180

coverage = spark.sql(f"""
    SELECT
        MAX(full_date)                                  AS max_date,
        DATEDIFF(MAX(full_date), current_date())        AS days_remaining
    FROM {TABLE_FQN}
""").collect()[0]

max_date       = coverage["max_date"]
days_remaining = coverage["days_remaining"]

print(f"Max date in dim_date:    {max_date}")
print(f"Days of forward coverage: {days_remaining}")
print(f"Buffer threshold:        {BUFFER_DAYS} days")

if days_remaining < BUFFER_DAYS:
    print(f"\n⚠ WARNING: Only {days_remaining} days of forward dim_date coverage.")
    print(f"   Extend by re-running this notebook with end_date set further out.")
else:
    print(f"\n✅ Forward coverage healthy ({days_remaining} days remaining).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Done
# MAGIC
# MAGIC Next: `02_dim_channel.py` (seed: POS, MKT, OLIST).

# COMMAND ----------

print(f"✅ dim_date complete.")
print(f"   Rows:         {spark.table(TABLE_FQN).count():,}")
print(f"   Range:        {START_DATE} → {END_DATE}")
print(f"   Forward days: {days_remaining}")
