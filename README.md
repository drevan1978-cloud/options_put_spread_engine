# options_put_spread_engine

`options_put_spread_engine` is a Python project for a semi-automated SPX/SPY/QQQ defined-risk put spread decision-support engine.

Safety warning: this project is not a broker, not an auto-trader, and not an order placement system. The system must never place live orders or submit market orders. Its purpose is to support auditable decision review and reject unsafe conditions before any candidate can be considered.

The current MVP includes local config validation, SQLite storage, CSV market and option-chain ingestion, hard risk controls, deterministic regime classification, eligibility checks, spread scanning, manual ticket drafts, manual fill tracking, local position records, exit review, audit logging, and daily reporting.

## Read-only CLI

Generate a Markdown daily audit report from an existing local SQLite database:

```bash
options-engine daily-report --database data/processed/engine.sqlite --date 2026-06-20
```

This command only reads local storage and prints a report. It does not scan markets, create tickets, import fills, connect to brokers, or place orders.
