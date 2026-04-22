# Retail Data Platform on Azure + Databricks

End-to-end retail data platform implementing Oracle ReSA-style sales audit over multi-channel transaction data, with LLM-powered analytics on top.

## What's inside
- Streaming POS ingestion simulator
- Medallion lakehouse with Silver conformed to the Oracle ReSA canonical model
- ReSA-style Sales Audit layer with 18 reconciliation rules
- LLM layer: review intelligence, text-to-SQL, weekly narrative
- Terraform infra, GitHub Actions CI/CD, Databricks Asset Bundles

## Design docs
See `docs/` - start with `retail_data_platform_design_v1.1.pdf`.

## Status
Work in progress. See `docs/context_for_claude.md` for current state.
