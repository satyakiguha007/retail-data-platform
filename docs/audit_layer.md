 known-issues / v1.1 refinements.
 TODO list (deferred refinements — not blocking)
Item                                                Where
R17 channel-aware tolerance (OMS ±14 days, others ±1)transformations/silver/audit/rules/17_*.py
R10 whitelist — verify against actual data valuestransformations/silver/audit/rules/10_*.py

# R06 — restrict to POS (one line change in run()):
head = head.where(col("RTLOG_ORIG_SYS") == "POS")

# R10 — add 'MARKETPLACE' to VALID_TENDER_TYPE_GROUPS list