# Lighter XAU/PAXG — Modal Frequency Center Grid (7d) — 1s Replay — Hedge 2s

## Scope & immutable parts

Research-only historical backtest. No real exchange orders, no new API requests.
Prior Percentile TP2/3 tests and original scripts **remain unchanged**.

* Data: downloaded 1-second L2 20 levels, Nov 4 01:00 UTC through Nov 19 00:00 UTC (exclusive), plus previously downloaded PAXG finalized taker trade tape.
* Calibration: strictly first 7 x 24 hours, Nov 4 01:00 to Nov 11 01:00 UTC. Test: Nov 11 01:00 to Nov 19 00:00 UTC. Data period already inspected historically: **not fresh OOS**.
* Center construction: sample the first valid paired 1-second L2 quotes each minute with age <=3s; spread = midpoint XAU − midpoint PAXG. Make fixed-width 1 USD/oz histogram; take the **midpoint of the most-populated bin** as Grid center. Ties: closest to sample median, then smallest bin. No SD or Percentile grids. This is one explicit interpretation of “middle of the densest frequency range”.
* Levels: `center ± n * grid_step_usd` for n=1,2,...; dynamically create levels only when **observed test prices** reach them; maximum 64 levels per direction by default, fail rather than silently clip excess. Short higher, Long lower.
* Short reference (executable quote): XAU SELL VWAP for qty − PAXG best BID; Short if reference **>=** threshold. PAXG BUY at best BID maker; cancel unfilled maker when reference **<** threshold; re-post immediately on next qualifying fresh quote. Pending maker price stays fixed, not repriced while eligible.
* Long reference: XAU BUY VWAP for qty − PAXG best ASK; Long if reference **<=** threshold. PAXG SELL at best ASK maker; cancel unfilled maker when reference **>** threshold; re-post on next qualifying quote.
* Maker fill: trade timestamp strictly AFTER post and correct PAXG taker aggressor. Exact price consumes displayed visible queue ahead; trade strictly THROUGH resting limit assumes counterfactual full paper fill. It is **not** confirmed maker execution. Partial exact print opens proportionate unhedged PAXG lot, cancels unfilled remainder. No maker time-to-live.
* Hedge: XAU market SELL for Short, BUY for Long at **first valid XAU book with timestamp >= maker_fill_ms + 2000**; 20-level depth VWAP shared by concurrent hedges; not a purposeful 2s wait in a real trading system. Actual simulated delay is recorded separately.
* Closing: after hedge, two-leg taker liquidation at executable bid/ask L2 prices when gross gain >=2 or >=3 USD/oz. No timed close/stop. After TP, require spread leave zone before re-opening that grid; after mere Maker cancel, no such arm requirement.
* Fees: Standard 0/0 bps, Plus 0.5/0.5 bps, Premium 0.4/2.8 bps maker/taker. Same gross fills re-priced for three fee profiles; scenario profits must NOT be added across TP/fees/qty.

## RESEARCH DEFAULTS NOT VERIFIED AS USER'S LEGACY VALUES

- Histogram bin width: **1 USD/oz**, adjustable `HIST_BIN_USD`
- Grid step: **1 USD/oz**, adjustable `GRID_STEP_USD`
- Latest usable as-of quote age: **3,000 ms**, adjustable `MAX_QUOTE_AGE_MS`
- Max grid levels / side: **64**, adjustable `MAX_LEVELS`; failure on overflow
- Replayed gross TP: **2 and 3 USD/oz**, from most recent experiment, not a newly agreed target
- Center is frozen during test window; recalibrating each day needs explicit rules for already open grids and is a separate experiment.

The most-populated 1-USD bin midpoint is an explicit testable choice, but not enough evidence exists to say it matches every detail of the user's earlier first-thread definition of a *broader* dense interval. Do not claim it is the exact recovered original implementation without reviewing that primary source.

## Execution fidelity caveats

- L2 source contains crossed books and gaps; book invalidation is respected. Data after invalidation is not forward-filled.
- PAXG maker order acknowledgements, cancellation races, true L3 queue changes, hidden liquidity, microsecond event order, network jitter, local VPS performance, slippage beyond quoted book depth, funding, margin, liquidation, and cross-symbol API/order limits are not observed.
- Quote prices assume an immediate taker execution at selected **future source L2 snapshot** after 2s, not an actual matched market order.
- Maker cancels assumed immediate. Real orders may fill while cancellation is in flight.
- 60s **fee-free marked drawdown sample** is not a maximal intrasecond, intraday or fee-adjusted drawdown; unmarked terminal lots make total P&L undefined, not zero.
- Program may reject a scenario when >64 dynamically reached levels would be needed; better to report the limit rather than hide the exposure.

## WSL #2

```bash
cd ~/code/trading-backtest
source .venv/bin/activate
git pull --ff-only origin master
bash scripts/run_lighter_mode_grid_1s_wsl.sh
```

Runner creates `C:\Users\June-ASUS\Downloads\LIGHTER_MODE_GRID_7D_1S_HEDGE2S_RESULTS.zip`. Upload ZIP with console log. No Codex needed.