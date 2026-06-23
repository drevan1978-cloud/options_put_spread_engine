# Manual Live Pilot Runbook

This project is a decision-support and observation system only. It does not
place broker orders, auto-route orders, or authorize market orders.

## Pilot Scope

- Underlyings: SPX, SPY, QQQ only.
- Size: one lot only.
- Execution: manual operator entry at the broker.
- Order type: limit orders only.
- Prohibited: market orders, size increases, martingale sizing, RED or BLACK overrides.
- Stop conditions: critical system error, risk-rule violation, unverified open risk, missing account equity, or emergency shutdown.
- Review point: after 20 to 30 trades or 3 months, whichever comes first.

## Pre-Trade Checklist

1. Confirm config version is locked.
2. Review the daily risk report.
3. Confirm account equity is present and current.
4. Confirm open positions are reconciled.
5. Confirm data quality has no ERROR or CRITICAL failure.
6. Confirm kill switch is GREEN or allowed YELLOW.
7. Confirm regime is GREEN or YELLOW.
8. Confirm ticket says MANUAL_EXECUTION_REQUIRED.
9. Confirm ticket says NO_MARKET_ORDERS.
10. Confirm intended order is one lot.
11. Confirm no size increase is being made during drawdown.

## First Real Pilot Day Checklist

1. Run the readiness dry run against a separate rehearsal database.
2. Save the readiness JSON report.
3. Start exactly one pilot session in the live observation database.
4. Confirm the active pilot session id and locked config version.
5. Generate the session-gated pilot dashboard.
6. Verify account equity, open positions, regime, and kill-switch state.
7. Review the manual ticket and confirm it is one lot only.
8. Enter the order manually at the broker as a limit order only.
9. Record the observed fill locally after the broker confirms it.
10. Generate the dashboard again and review slippage.
11. Record daily operator signoff before ending the day.

## Do Not Continue If

- Account equity is missing, stale, or cannot be reconciled.
- Open positions or open risk cannot be verified.
- Data quality has any ERROR or CRITICAL failure.
- Regime is RED or BLACK.
- Kill switch is RED or BLACK.
- Emergency shutdown is active.
- Any risk-rule violation occurred.
- Any critical system error occurred.
- A market order was used or requested.
- More than one active pilot session exists.
- The pilot has reached 20 trades without review.
- The pilot has reached 30 trades.
- The pilot has reached 3 months without review.
- A stop event exists and reset review has not been recorded.

## Manual Fill Entry

Start one active pilot session before recording real live observations:

```powershell
options-engine pilot-start `
  --database path\to\engine.sqlite `
  --pilot-id pilot-001 `
  --config-version locked-config-version `
  --operator operator-initials `
  --started-at 2026-06-20T13:00:00+00:00
```

After the broker fill is observed manually, record it through the pilot session
gate:

```powershell
options-engine pilot-live-fill `
  --database path\to\engine.sqlite `
  --pilot-id pilot-001 `
  --config-version locked-config-version `
  --ticket-id 1 `
  --filled-at 2026-06-20T15:05:00+00:00 `
  --quantity 1 `
  --price 1.45 `
  --expected-credit 1.50 `
  --manual-execution-confirmed
```

The system verifies there is exactly one active pilot session, then records the
fill, slippage, audit event, and any live-pilot rule violation. It does not
contact a broker.

## Readiness Dry Run

Before the first real one-lot pilot, run a no-money rehearsal against a local
SQLite database:

```powershell
options-engine readiness-dry-run `
  --database path\to\readiness.sqlite `
  --config-version locked-config-version `
  --date 2026-06-20 `
  --operator operator-initials `
  --output-json reports\readiness.json
```

The rehearsal verifies config lock, checklist gating, dry-run fill entry,
slippage tracking, dashboard/report updates, and emergency shutdown behavior.

## First-Pilot Simulation Packet

Build a complete local rehearsal packet before the first real pilot day:

```powershell
options-engine build-pilot-demo `
  --database demo.sqlite `
  --output-dir reports\demo
```

The command creates a seeded demo database and writes:

- readiness report JSON
- daily risk report JSON
- pilot dashboard Markdown
- evidence packet JSON
- operator checklist Markdown
- manifest JSON

The simulation packet is local only. It does not connect to a broker, submit an
order, or allow market orders.

## Release Gate

Run the final release gate before the first one-lot manual pilot. The gate
requires a passed test suite, readiness report, evidence packet, active config
lock, clear emergency shutdown state, no rule violations, verified account
equity, verified open positions, and operator acknowledgement of this runbook.

```powershell
options-engine pilot-release-gate `
  --database demo.sqlite `
  --readiness reports\demo\readiness_report.json `
  --evidence reports\demo\evidence_packet.json `
  --output-json reports\demo\release_gate.json `
  --output-markdown reports\demo\release_gate.md `
  --full-test-suite-passed `
  --runbook-acknowledged `
  --account-equity-present `
  --open-positions-verified
```

Only a `GO` result allows the operator to continue. `NO_GO` means stop and fix
the blocking reason. `REVIEW_REQUIRED` means the operator must complete the
missing human acknowledgement before continuing.

## Emergency Shutdown

If emergency shutdown is active:

1. Stop new trade consideration.
2. Do not generate new live pilot tickets.
3. Review open positions only.
4. Record the reason and timestamp.
5. Reset only after an explicit review reason exists.

## Stop, Reset Review, And Resume

Stop the pilot with an explicit reason:

```powershell
options-engine pilot-stop `
  --database path\to\engine.sqlite `
  --pilot-id pilot-001 `
  --config-version locked-config-version `
  --reason-code OPERATOR_STOP `
  --reason "operator stopped for review"
```

After a hard stop caused by RED/BLACK state, emergency shutdown, critical system
error, or risk-rule violation, record reset review before resuming:

```powershell
options-engine pilot-reset-review `
  --database path\to\engine.sqlite `
  --pilot-id pilot-001 `
  --config-version locked-config-version `
  --review-note "risk reviewed; reset approved for one-lot pilot"
```

Resume only after review:

```powershell
options-engine pilot-resume `
  --database path\to\engine.sqlite `
  --pilot-id pilot-001 `
  --config-version locked-config-version `
  --review-note "resume approved after review"
```

## Daily Closeout

1. Generate the risk dashboard.
2. Generate the daily risk report.
3. Review rejected trades and reason codes.
4. Review slippage versus expected credit.
5. Review exit recommendations.
6. Confirm no rule violations occurred.
7. Stop the pilot if any critical system error or risk-rule violation occurred.

Record daily operator signoff:

```powershell
options-engine pilot-signoff `
  --database path\to\engine.sqlite `
  --pilot-id pilot-001 `
  --config-version locked-config-version `
  --date 2026-06-20 `
  --operator operator-initials `
  --notes "daily closeout reviewed" `
  --report-reviewed `
  --positions-reconciled `
  --slippage-reviewed `
  --violations-reviewed
```

Export an evidence packet:

```powershell
options-engine pilot-evidence `
  --database path\to\engine.sqlite `
  --pilot-id pilot-001 `
  --output-json reports\pilot-001-evidence.json
```
