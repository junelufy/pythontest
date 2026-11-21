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
