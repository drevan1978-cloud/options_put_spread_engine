# Forensic End-to-End Audit - options_put_spread_engine

Date: 2026-06-21

Repository copy added: 2026-06-23

Scope: source inventory, full test baseline, compile smoke check, governance-rule search, and end-to-end trace from config/data ingestion through data quality, regime, risk, scanner, eligibility, ticketing, live pilot controls, release gate, reporting, paper trading, and backtest surfaces.

Remediation update:

- P1 was fixed after this audit. The legacy `live-fill` CLI now requires `--pilot-id` and routes through `record_live_fill_for_active_session(...)`; tests now prove it blocks without an active pilot session. Full-suite baseline after this fix: `246 passed in 2.86s`.
- P2 open-position verification was fixed after this audit. The release gate now requires the latest SQLite audit event to be `POSITION_RECONCILIATION_VERIFIED` with `open_risk_verified=True`, and the demo packet seeds that reconciliation evidence. Full-suite baseline after this fix: `247 passed in 3.12s`.
- P2 release-gate audit persistence was fixed after this audit. `ReleaseGateReport` now converts to a `PILOT_RELEASE_GATE_*` audit event, and the CLI records the event to SQLite for both `GO` and `NO_GO` decisions when the database is available. Full-suite baseline after this fix: `249 passed in 3.05s`.
- P3 live-fill classification/reporting was fixed after this audit. Manual live fills now emit a `LIVE_FILL_CLASSIFIED` audit event with clean-vs-violation classification and reason codes, and daily/live reports split clean pilot fills from violation-observation fills and unclassified fills. Full-suite baseline after this fix: `251 passed in 3.63s`.
- P4 release-gate account-equity evidence was fixed after this audit. The release gate now requires the latest risk snapshot to be positive, timestamp-parseable, timezone-aware, not after the release-gate timestamp, and config-version aligned with the evidence packet. Full-suite baseline after this fix: `253 passed in 3.26s`.
- P5 release-gate readiness coupling was fixed after this audit. The release gate now requires the readiness dry-run report to have a valid/current `run_at`, not be after the release gate or evidence packet, and have a config version matching the evidence packet. Full-suite baseline after this fix: `256 passed in 3.44s`.
- P6 release-gate position reconciliation timing was fixed after this audit. The release gate now requires position reconciliation evidence to have a valid/current `checked_at`, not be after the release gate or evidence packet, and stay within the configured evidence age window. Full-suite baseline after this fix: `259 passed in 4.67s`.
- P7 release-gate config/shutdown as-of checks were fixed after this audit. The release gate now evaluates config-lock and emergency-shutdown audit state at or before the gate timestamp, preventing future-dated lock or clear events from making the gate look safe. Full-suite baseline after this fix: `261 passed in 3.92s`.
- P8 release-gate pilot-session as-of checks were fixed after this audit. Pilot session derivation now supports `as_of`, and the release gate evaluates active/stopped/resumed state at the gate timestamp rather than using future session events. Full-suite baseline after this fix: `263 passed in 3.99s`.
- P9 release-gate rule-violation as-of checks were fixed after this audit. The release gate now counts live-pilot rule violations at or before the gate timestamp, preventing future-dated violations from rewriting a prior gate evaluation. Full-suite baseline after this fix: `264 passed in 3.97s`.
- P10 pilot evidence packet as-of generation was fixed after this audit. Evidence packet generation now derives session state, audit events, fill rows, slippage events, rule violations, signoffs, and dashboard event counts from records at or before `generated_at`. Full-suite baseline after this fix: `265 passed in 4.29s`.
- P11 live dashboard and daily-report intraday as-of filtering was fixed after this audit. Dashboard generation now passes `generated_at` through daily-report loaders and same-day counters so future risk snapshots, fills, audit events, config-lock state, and shutdown state are excluded. Full-suite baseline after this fix: `266 passed in 5.77s`.
- P12 release-gate account-equity snapshot selection was fixed after this audit. The gate now selects the latest risk snapshot available at or before the gate timestamp instead of allowing a later risk snapshot to rewrite a prior release-gate evaluation. Full-suite baseline after this fix: `267 passed in 4.20s`.

## Findings

### P1 - Legacy `live-fill` CLI bypasses the active pilot-session gate

The CLI still exposes `options-engine live-fill`, which records an observed manual fill by calling `record_live_fill(...)` directly. That path does not require exactly one active pilot session, does not verify the requested pilot id, and does not use the session gate introduced for `pilot-live-fill`.

Evidence:

- `src/options_engine/main.py:99` defines the legacy `live-fill` command.
- `src/options_engine/main.py:387` opens the database and `src/options_engine/main.py:388` calls `record_live_fill(...)` directly.
- The safer path exists: `src/options_engine/main.py:492` defines `_run_pilot_live_fill`, and `src/options_engine/main.py:497` calls `record_live_fill_for_active_session(...)`.
- The session-gated implementation records the active-session gate before fill persistence in `src/options_engine/live/operations.py:560` through `src/options_engine/live/operations.py:568`.

Impact: an operator can record a live fill without the pilot-session invariant that later milestones rely on. This does not submit broker orders, but it weakens audit chronology and pilot controls.

Recommended fix: retire `live-fill`, hide it behind an explicit legacy/debug label, or route it through `record_live_fill_for_active_session(...)` with required `--pilot-id`. Update CLI tests to prove direct ungated live fills are blocked.

### P2 - Release gate open-position verification is operator-asserted only

The release gate requires `open_positions_verified=True`, but it does not independently verify a recent successful position reconciliation event in the database.

Evidence:

- `src/options_engine/live/release_gate.py:204` through `src/options_engine/live/release_gate.py:209` treat open-position verification as a boolean input.
- Database checks validate config lock, emergency shutdown, rule violations, pilot session, and account equity in `src/options_engine/live/release_gate.py:442` through `src/options_engine/live/release_gate.py:548`.
- No release-gate database check currently queries `POSITION_RECONCILIATION_VERIFIED`.
- The position monitor already provides a durable audit signal: `src/options_engine/execution/position_monitor.py:103` through `src/options_engine/execution/position_monitor.py:121` emits `POSITION_RECONCILIATION_*` with `open_risk_verified`.

Impact: a `GO` release gate can be produced from operator assertion plus positive account equity even if the database lacks a replayable position reconciliation event.

Recommended fix: add a release-gate database check requiring the latest `POSITION_RECONCILIATION_VERIFIED` audit event for the active config/session date, or return `NO_GO`/`REVIEW_REQUIRED` with `OPEN_POSITIONS_NOT_VERIFIED`.

### P2 - Release gate decision is file-backed, not stored in SQLite audit log

The release gate writes JSON/Markdown reports but does not persist the gate decision to `audit_log`.

Evidence:

- `src/options_engine/live/release_gate.py:167` through `src/options_engine/live/release_gate.py:176` write report files.
- `src/options_engine/live/release_gate.py:180` through `src/options_engine/live/release_gate.py:229` evaluate the gate and return a report object.
- No `record_audit_event(...)` call exists in `release_gate.py`.

Impact: if the output report is lost or not copied with the pilot database, the database alone cannot replay the final `GO` / `NO_GO` / `REVIEW_REQUIRED` operator gate.

Recommended fix: add a `ReleaseGateReport.to_audit_event(...)` method and persist it from the CLI after report generation. Include status, blocking/review reason codes, readiness/evidence paths, and `broker_orders_submitted_by_system=False`.

### P3 - Violating manual fills are persisted before violation classification

`record_live_fill(...)` inserts a fill row and records slippage before computing live-pilot violations such as market order, non-one-lot size, RED/BLACK override, critical error, or risk-rule violation.

Evidence:

- `src/options_engine/live/pilot.py:615` through `src/options_engine/live/pilot.py:631` insert the fill and slippage audit records.
- `src/options_engine/live/pilot.py:633` begins violation classification after the fill has already been stored.
- Violation classification itself is explicit and comprehensive in `src/options_engine/live/pilot.py:766` through `src/options_engine/live/pilot.py:830`.

Impact: this is defensible as an observation log for bad manual behavior, but downstream readers that only inspect `fills` can mistake a violating fill for a clean pilot fill unless they also join the audit log.

Status: fixed. The fill recorder now writes `LIVE_FILL_CLASSIFIED` metadata with `classification`, `valid_for_pilot`, and `violation_reason_codes`; daily and live reports surface clean pilot fills, violation-observation fills, and unclassified fills.

### P4 - Release gate account-equity check accepted unbound risk snapshots

The release gate checked only that the latest `risk_snapshots.account_equity` value was parseable and positive. It did not verify that the snapshot belonged to the evidence-packet config version or that its timestamp was not after the release-gate evaluation time.

Impact: a final release gate could be satisfied by account-equity evidence from the wrong config version or by a future-dated snapshot, weakening the audit chain for one-lot live pilot readiness.

Status: fixed. The database account-equity check now blocks mismatched config versions, malformed/naive timestamps, and future-dated snapshots with `ACCOUNT_EQUITY_NOT_VERIFIED`.

### P5 - Release gate accepted readiness reports without freshness or evidence coupling

The release gate treated a readiness JSON artifact as valid once it existed, decoded as an object, and contained `ready: true`. It did not verify that the readiness `run_at` was current, not future-dated, not after the evidence packet, or config-version aligned with the evidence packet.

Impact: a stale or wrong-config readiness dry run could satisfy part of the final live-pilot gate even though the rest of the release packet came from a different evidence chain.

Status: fixed. Readiness validation now checks `run_at` freshness and ordering, and the release gate blocks readiness/evidence config mismatches with `READINESS_CONFIG_MISMATCH`.

### P6 - Release gate accepted unbounded position reconciliation timing

The release gate required the latest position reconciliation audit event to be `POSITION_RECONCILIATION_VERIFIED`, but it did not validate the reconciliation `checked_at` timestamp against the release gate or evidence packet.

Impact: future-dated, stale, or post-evidence position reconciliation could satisfy the final live-pilot gate, weakening the open-risk verification chain.

Status: fixed. Position reconciliation validation now rejects missing/malformed `checked_at`, future-dated reconciliation, reconciliation after the evidence packet, and reconciliation older than the configured evidence age window.

### P7 - Release gate used future audit state for config lock and shutdown checks

The release gate selected the latest config-lock and emergency-shutdown audit events, regardless of whether those events occurred after the release-gate timestamp.

Impact: a future-dated config lock could satisfy the config check, and a future-dated emergency-shutdown clear could hide an active shutdown at the actual release-gate evaluation time.

Status: fixed. Config-lock and emergency-shutdown checks now query the latest applicable audit state at or before the release-gate timestamp.

### P8 - Release gate used future pilot-session events

The release gate derived pilot sessions from all session audit events. Future stop or resume events could alter whether a session appeared active at the release-gate timestamp.

Impact: a session stopped before the release gate but resumed later could appear active, allowing the pilot-session check to pass when no active session existed at the actual gate time.

Status: fixed. `load_pilot_sessions(...)` now supports an optional `as_of` timestamp, and the release gate uses it for pilot-session validation.

### P9 - Release gate counted future rule violations

The release gate counted every `LIVE_PILOT_RULE_VIOLATION` row in the database, regardless of whether the violation occurred after the release-gate timestamp.

Impact: a future-dated violation could make a prior release-gate evaluation appear to have been `NO_GO`, which weakens audit replay and historical reportability.

Status: fixed. Rule-violation checks now count only events created at or before the release-gate timestamp.

### P10 - Evidence packets included future live-pilot events

`build_pilot_evidence_packet(...)` accepted a `generated_at` timestamp but derived session state and audit rows from the full database, including records after that timestamp.

Impact: an evidence packet could include future fills, slippage, session state, rule violations, or signoffs that were not actually available when the packet claimed to be generated.

Status: fixed. Evidence packet generation now uses `generated_at` as an as-of boundary for session derivation, audit rows, fill rows, and dashboard event counts.

### P11 - Live dashboards and daily reports included same-day future records

`build_live_risk_dashboard_from_database(...)` accepted a `generated_at` timestamp but called daily-report loaders and dashboard counters that filtered by report date only.

Impact: an intraday dashboard or daily report could include future same-day fills, risk snapshots, audit events, config-lock changes, or emergency-shutdown clears that were not available at generation time.

Status: fixed. Daily-report database loaders now support an optional timezone-aware `as_of` boundary, and live dashboard generation applies `generated_at` to daily report input, emergency shutdown state, config lock state, live-fill counts, classification counts, and rule-violation counts.

### P12 - Release gate account-equity check selected future risk snapshots

`_account_equity_check(...)` ordered all `risk_snapshots` by `as_of` and selected the newest row in the database before comparing it with the release-gate timestamp.

Impact: a future risk snapshot could make a prior release-gate evaluation fail even when a valid account-equity snapshot existed at the gate timestamp. This weakened historical replay because later records could rewrite the gate outcome.

Status: fixed. The release gate now selects only risk snapshots with `as_of` and `created_at` at or before `generated_at`; if no such snapshot exists, it fails with an explicit account-equity missing reason.

## Positive Controls Verified

- Full pytest suite passed after remediation: `267 passed in 4.20s`.
- Compile smoke check passed after remediation: `python -m compileall -q src tests`.
- No broker SDK/network execution dependency was found in source dependencies.
- Pre-pilot static tests already reject broker submission patterns, market-order flags set true, auto-execution flags set true, martingale requests, and fantasy mid-only backtests.
- Config validation fails if `allow_martingale` or `allow_live_orders` is true.
- CSV market-data and option-chain ingestion reject missing/malformed fields and require timezone-aware timestamps.
- Data quality requires account equity and open-position snapshots; ERROR/CRITICAL failures map to `NO_TRADE`.
- Regime and kill-switch layers include BLACK/RED blocking conditions for data quality, account equity, open risk, broker reconciliation, loss caps, duplicate order risk, and wrong-way order risk.
- Scanner uses conservative credit (`short bid - long ask`) rather than assuming mid fills.
- Eligibility combines data quality, regime, risk, kill switch, scanner, and contracts, and emits explicit reason codes.
- Manual tickets require `APPROVED` eligibility and watchlist scanner status, include manual/no-market warnings, and reject kill-switch blocks.
- Paper trading uses conservative fill logic and marks unrealistic mid-required fills as not filled.
- Backtests reject `MID_ONLY` fill model and require slippage or commission costs.
- Release gate blocks missing/invalid readiness, stale/invalid evidence, emergency shutdown, rule violations, missing config lock, invalid pilot session, missing account equity, and broker execution flags set true.

## Verification Commands

```powershell
C:\Users\dreva\AppData\Local\Programs\Python\Python311\python.exe -m pytest
C:\Users\dreva\AppData\Local\Programs\Python\Python311\python.exe -m compileall -q src tests
```

## Repository Note

The original audit report was generated outside the project folder. This repository copy was added after the initial GitHub push so the forensic remediation history is versioned with the source code.

## Recommended Next Action

The P1, P2, P3, P4, P5, P6, P7, P8, P9, P10, P11, and P12 findings from this report are now remediated. Continue the forensic pass with the next highest-risk surface before adding new live-pilot functionality.
