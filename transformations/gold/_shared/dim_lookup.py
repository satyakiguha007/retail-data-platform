# Databricks notebook source
# MAGIC %md
# MAGIC # Gold `_shared` — `dim_lookup.py`
# MAGIC
# MAGIC Surrogate-key resolution for fact builders. Each helper takes a fact DataFrame, joins
# MAGIC the relevant dimension on its natural key, and attaches the surrogate key. Loaded via
# MAGIC `%run ../_shared/dim_lookup`.
# MAGIC
# MAGIC ## Design rules
# MAGIC - **Never drop fact rows on a missed dim match.** Unmatched rows get surrogate `-1`
# MAGIC   (the "unknown member" convention). Facts keep every row; orphans are visible as
# MAGIC   `*_key = -1` and surface in `99_gold_core_validation`.
# MAGIC - **broadcast the dim.** All dims are small (largest is dim_item ~33k); broadcast joins
# MAGIC   avoid shuffles.
# MAGIC - **dim_item uses is_current = TRUE** (SCD2). The point-in-time variant is stubbed
# MAGIC   (`add_item_key_pit`) for when attribute history starts mattering — currently all
# MAGIC   rows are version 1 so it's identical to the current-flag join.
# MAGIC
# MAGIC ## Natural-key contracts (fact column -> dim natural key)
# MAGIC | helper | fact col(s) | dim | dim natural key |
# MAGIC |---|---|---|---|
# MAGIC | add_date_key    | business_date          | dim_date    | full_date |
# MAGIC | add_store_key   | store                  | dim_store   | store |
# MAGIC | add_channel_key | rtlog_orig_sys         | dim_channel | source_system_code |
# MAGIC | add_item_key    | item                   | dim_item    | item (is_current) |
# MAGIC | add_tender_key  | tender_type_group, tender_type_id | dim_tender | (group, id) |

# COMMAND ----------

from pyspark.sql import DataFrame as _DataFrame
from pyspark.sql import functions as _F

_CORE = "retaildp.gold_core"


def _attach(fact: _DataFrame, dim_df: _DataFrame, fact_keys, dim_keys,
            surrogate_col: str) -> _DataFrame:
    """Left-join dim onto fact, attach surrogate, coalesce misses to -1.
    fact_keys / dim_keys are equal-length lists of column names."""
    cond = None
    dim_alias = dim_df.alias("_d")
    for fk, dk in zip(fact_keys, dim_keys):
        c = fact[fk] == _F.col(f"_d.{dk}")
        cond = c if cond is None else (cond & c)
    joined = (
        fact.join(dim_alias, cond, "left")
        .withColumn(surrogate_col,
                    _F.coalesce(_F.col(f"_d.{surrogate_col}"), _F.lit(-1)))
    )
    # drop dim columns we pulled in (keep only the surrogate we just coalesced)
    drop_cols = [f"_d.{c}" for c in dim_df.columns if c != surrogate_col]
    return joined.drop(*[_F.col(c) for c in drop_cols]) if drop_cols else joined


def add_date_key(fact: _DataFrame, fact_date_col: str = "BUSINESS_DATE") -> _DataFrame:
    dim = spark.table(f"{_CORE}.dim_date").select("date_key", "full_date")
    return _attach(fact, dim, [fact_date_col], ["full_date"], "date_key")


def add_store_key(fact: _DataFrame, fact_store_col: str = "STORE") -> _DataFrame:
    dim = spark.table(f"{_CORE}.dim_store").select("store_key", "store")
    return _attach(fact, dim, [fact_store_col], ["store"], "store_key")


def add_channel_key(fact: _DataFrame, fact_channel_col: str = "RTLOG_ORIG_SYS") -> _DataFrame:
    dim = spark.table(f"{_CORE}.dim_channel").select("channel_key", "source_system_code")
    return _attach(fact, dim, [fact_channel_col], ["source_system_code"], "channel_key")


def add_item_key(fact: _DataFrame, fact_item_col: str = "ITEM") -> _DataFrame:
    """SCD2 dim_item join on the CURRENT version."""
    dim = (
        spark.table(f"{_CORE}.dim_item")
        .where(_F.col("is_current") == True)  # noqa: E712
        .select("item_key", "item")
    )
    return _attach(fact, dim, [fact_item_col], ["item"], "item_key")


def add_item_key_pit(fact: _DataFrame,
                     fact_item_col: str = "ITEM",
                     fact_date_col: str = "BUSINESS_DATE") -> _DataFrame:
    """Point-in-time SCD2 join — resolves item_key as of business_date.
    Currently identical in effect to add_item_key (all rows version 1) but correct once
    attribute history exists. Use this instead of add_item_key when that day comes."""
    dim = spark.table(f"{_CORE}.dim_item").select(
        "item_key", "item", "effective_from", "effective_to")
    dim_alias = dim.alias("_d")
    cond = (
        (fact[fact_item_col] == _F.col("_d.item"))
        & (fact[fact_date_col].cast("timestamp") >= _F.col("_d.effective_from"))
        & (
            _F.col("_d.effective_to").isNull()
            | (fact[fact_date_col].cast("timestamp") < _F.col("_d.effective_to"))
        )
    )
    joined = (
        fact.join(dim_alias, cond, "left")
        .withColumn("item_key", _F.coalesce(_F.col("_d.item_key"), _F.lit(-1)))
        .drop(_F.col("_d.item"), _F.col("_d.effective_from"), _F.col("_d.effective_to"))
    )
    return joined


def add_tender_key(fact: _DataFrame,
                   fact_group_col: str = "TENDER_TYPE_GROUP",
                   fact_id_col: str = "TENDER_TYPE_ID") -> _DataFrame:
    """Composite-key join. NULL tender_type_id on the fact coalesces to -1 to match the
    dim's sentinel (dim_tender stores -1 for unspecified)."""
    dim = spark.table(f"{_CORE}.dim_tender").select(
        "tender_key", "tender_type_group", "tender_type_id")
    fact2 = fact.withColumn(
        "_tid_norm", _F.coalesce(_F.col(fact_id_col), _F.lit(-1)))
    out = _attach(
        fact2, dim,
        [fact_group_col, "_tid_norm"],
        ["tender_type_group", "tender_type_id"],
        "tender_key",
    ).drop("_tid_norm")
    return out


print("[dim_lookup] helpers loaded: add_date_key, add_store_key, add_channel_key, "
      "add_item_key, add_item_key_pit, add_tender_key")

