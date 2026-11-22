# Handoff v0.1 — GitHub staging branch

## Implemented
Read-only ZIP / CSV / SQLite / Parquet candle adapters, safe synthetic formulas, pure position and limit helpers, Streamlit replay UI, manual hedged positions, partial close, simulated pending limits, P/L and scenario export.

## Known modeling limitations
Market execution is **last visible candle close**, not bid/ask. Limit touch is assumed fill after order placement; no intrabar sequencing or maker queue. No funding, option payoff, exchange margin or conversion. Equity curve is *not* event-sourced through partial closes and must be revised before results are treated as reliable. No full autoplay or scenario restore yet.

## Next
1. Run with actual Lighter/Binance uploads in WSL and inspect UI feedback.
2. Event-source all fills and closes and fix historical equity.
3. Build UTC replay datetime picker, chart resampling and source gap audit.
4. Add explicit bid/ask and maker/taker fee and funding assumptions.
5. Later: options instrument contracts and conditional multi-leg/grid execution.

Do not claim results demonstrate actual trading profitability.


## 2026-11-21: Bid/Ask + volume panel

Added `workbench/quotes.py` and `tests/test_quotes.py` with historical quote import (CSV, ZIP, Parquet, SQLite). The UI now shows actual historical best Bid/Ask and their available volume **when supplied**, plus OHLC, last close and honest total candle base volume. Quote timestamps are constrained by replay cursor and configurable staleness; future quotes are never shown. Manual market buy/sell and hypothetical liquidation valuation use historical best Ask/Bid when fresh, fallback otherwise to explicit candle-close proxy. Known insufficient best-level size blocks hypothetical market fills rather than assuming full fill.

**Not yet supported:** multiple L2 depth levels/VWAP by trade size, event-accurate maker queuing, latency, time-dependent quote updates between candles, spread-aware multi-leg execution, historical order-book ingestion directly from Lighter API, full order-price audit or stress tests. Do not advertise top-of-book prices as guaranteed fills. Needs user-run UI verification on WSL.

## Lighter 30-day historical L2 dataset — 2026-11-22

The user collected XAU/PAXG from 0xArchive (free historical API, rolling 30-day cutoff) to local WSL only:
`data/lighter_fresh_20261122/lighter_marketdata.sqlite3`, raw L2 JSONL.GZ under `book/<symbol>/`, raw trade prints under `trades/<symbol>/`; do not commit/archive this large dataset to public GitHub.

Current ZIP review reports:
- XAU 42,917 quote snapshots, 42,933 minute candles, 233,641 funding-rate observations, 2,585,530 raw trades.
- PAXG 42,012 quote snapshots, 15,068 minute candles, 223,854 funding-rate observations, 107,610 raw trades.
- Raw L2 is 20 bid + 20 ask levels per snapshot; only PAXG early snapshots were checked so far; sample reviewer now samples both.
- Free rolling cutoff clipped first ~2.8–3.0 hours of PAXG data (by record type). See `history_gaps`; **do not claim full 30 days or silently impute missing data**.
- PAXG has ~65% fewer candles vs calendar minutes. Hypothesis: no-trade minutes; run `python scripts/audit_lighter_trade_candle_coverage.py --input data/lighter_fresh_20261122 --symbol PAXG` to compare trade-minute coverage before concluding.
- Funding rows ~10 seconds apart are observed funding rates, **NOT cash funding payments**. Do not apply a cash transfer to every row.
- App now loads 30-day SQLite candles and top-of-book via read-only local path `workbench.data.*_from_path`, `workbench.quotes.read_quotes_from_path`. No 300-MB read-all upload limitation for SQLite.
- MVP **does not yet use full L2 depth, executable VWAP, synchronized multi-leg orders, maker queue, or self-volume depletion**. Existing quote-based Market behavior is best-level only, amount limited by top size. All fills labeled simulated.
- When building VWAP: use the last book snapshot **not later than** event time, enforce max age, check both books' timestamps, reject insufficient cumulative executable depth, and deplete depth across simultaneous grid legs. Do not treat pending maker as filled merely because a 1m candle touched the price. Prevent look-ahead and false arbitrage claims.
