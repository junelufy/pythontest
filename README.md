# Trading Backtest Workstation (MVP)

Historical manual trading simulator for local CSV / ZIP / SQLite OHLC data. This repository is a temporary staging location for the new workbench; it does not modify the existing prediction-arbitrage or options-backtest projects.

**Status:** initial vertical slice, research-only simulator; no real orders. Market fills are modeled at the last *visible* candle close, and pending limit orders use subsequent candles' OHLC touches as assumed fills. **Not an executable bid/ask order-book backtest.**

Data and strategy configurations are **not** committed. See `README_TH.md`, `AGENTS.md`, and `docs/HANDOFF.md`.
