# Databricks notebook source
# MAGIC %md
# MAGIC # `_shared` / `schema_gate`
# MAGIC
# MAGIC Defensive check for the case where Auto Loader inferred a bronze `ARRAY` field
# MAGIC with an empty inner struct. Lesson from debugging `05_sa_tran_tax` on an
# MAGIC IND-only Pass-1: a notebook that explodes and projects nested struct fields
# MAGIC crashes at planning time if Auto Loader never observed a populated array.
# MAGIC Use this gate **before** starting the stream so the planner never has to
# MAGIC resolve `col("alias.inner_field")` against an `ARRAY<>` with no inner type.
# MAGIC
# MAGIC ## Usage
# MAGIC ```python
# MAGIC # MAGIC %run ../_shared/schema_gate
# MAGIC
# MAGIC if not bronze_array_has_inner_fields(SOURCE_TABLE, "tran_tax"):
# MAGIC     print("SKIPPING STREAM: bronze.tran_tax has no inner struct fields ...")
# MAGIC else:
# MAGIC     spark.readStream.table(SOURCE_TABLE).writeStream...start().awaitTermination()
# MAGIC ```
# MAGIC
# MAGIC ## Use this gate when
# MAGIC - The notebook explodes a nested array AND projects multiple inner fields
# MAGIC - The target table could legitimately be empty under some channel mixes (e.g. `sa_tran_tax` for IND-only, `sa_tran_igtax` for USA-only)
# MAGIC - You can't guarantee the bronze schema saw at least one populated array during ingestion
# MAGIC
# MAGIC ## Don't use this gate when
# MAGIC - The bronze field is always present and non-empty (e.g. `tran_head` struct, `tran_item` array for any real transaction)

# COMMAND ----------

from pyspark.sql import SparkSession
from pyspark.sql.types import ArrayType, StructType


def bronze_array_has_inner_fields(source_table: str, array_column: str) -> bool:
    """Return True iff `array_column` in `source_table` is `ARRAY<STRUCT<...>>` with at least one inner field.

    Auto Loader infers schema from observed JSON. If a nested array was always empty
    in the data it saw, the inferred type is `ARRAY<>` with no inner struct, and any
    reference to `col("alias.field")` after exploding will fail at planning time.

    Args:
        source_table: fully-qualified bronze table (e.g. "retaildp.bronze.pos_rtlog").
        array_column: name of the ARRAY field on that table (e.g. "tran_tax").

    Returns:
        True  — safe to reference inner struct fields; start the stream.
        False — caller should print a skip message and let the validation cell
                report an empty target.
    """
    spark = SparkSession.getActiveSession()
    schema = spark.table(source_table).schema
    field = next((f for f in schema.fields if f.name == array_column), None)

    return (
        field is not None
        and isinstance(field.dataType, ArrayType)
        and isinstance(field.dataType.elementType, StructType)
        and len(field.dataType.elementType.fields) > 0
    )

