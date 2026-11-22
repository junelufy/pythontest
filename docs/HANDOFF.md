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


## Verified Lighter trade finalization diagnosis (2026-11-22)
After user ran `scripts/diagnose_lighter_trade_finalization.py --symbols PAXG --probe`, actual 0xArchive response:
- Finalized through: **2026-11-18T23:59:59.999Z**
- Last saved canonical PAXG trade: **2026-11-18T23:59:40.610Z**
- 11 6-hour PAXG trade windows returned zero rows; the canonical API clamps requests beyond the watermark and can respond with zero despite positive-volume candles.
- Of 1,349 candle minutes with volume >0 but no canonical trades, **1,344** are Nov 19–21 (647 Nov 19, 342 Nov 20, 355 Nov 21) after the finalization watermark; 5 minutes on Oct 23 still merit separate investigation of the rolling free-plan start boundary.
- These 1,344 are **not proven no-trade**; canonical archive is not yet published through those times. Historical L2/candles can remain valid independently. Preserve original raw files and provenance.
- API `/v1/lighter/trades/{symbol}/recent` gives newest preliminary feed rows, **not** a guaranteed paginated 3-day full historical tape. Do not merge into canonical trade archives or claim reconciliation. Native Lighter free S3 daily Parquet is an independent alternative for verifying complete historical trade days.
- For any maker/taker fill inference, use canonical trades only through `finalized_through`; L2 minute snapshots cannot verify order queue position. When watermark advances, targeted finalized backfill should replace only previously empty/incomplete trade windows, preserving a provenance audit, never regenerate 30 days of unrelated data.


## 27-day first-pass XAU/PAXG spread feasibility study (offline)
The user wants to decide whether the PAXG maker-first / XAU short spread grid 25/27/29/31 may make money, without buying archived data beyond the existing 27 days.
- Run `bash scripts/run_27d_feasibility_wsl.sh` in WSL #2 after `git pull`.
- Reads actual local XAU/PAXG raw L2 20-level JSONL.gz, **no API credits**.
- Test window 2026-10-23 01:00 through 2026-11-18 23:59 UTC; uses only as-of snapshots <=120 seconds old.
- Separate take-both-legs baseline and **optimistic instant PAXG best-bid maker-fill assumption**, never label as verified fills.
- Parameters: qty 0.1, 0.5, 1.0 oz; grid levels 25/27/29/31 USD/oz; exit compression targets 2/5 USD/oz or forced closes after 60/240 minutes; sensitivity 0/2 bps for each taker leg. Maker entry fee assumed 0 bps. Each configuration is independent and cannot be added into a single portfolio P/L.
- Treat the time-based exit policy as **research assumption**, not settled user strategy. No real cash funding; no Maker queue position, intraminute sequencing, margin, adverse selection, simultaneous depth sharing. End-of-study open positions excluded; inspect unclosed_positions. This is a feasibility diagnostic only. Do not call reported net P/L realized/guaranteed or annualize.
- Outputs `SPREAD_FEASIBILITY/spread_scenarios.csv` and `summary.json`, small review ZIP in user's Windows Downloads. Ask user for resulting ZIP before giving quantitative profitability conclusion.


## Training/test time split requested by user (2026-11-22)
User clarified grid $25/$27/$29/$31 were illustrative, not endorsed entry thresholds. Correct workflow:
1. Read ONLY first five days from Lighter historical L2: **2026-10-23 01:00 to 2026-10-28 01:00 UTC exclusive**. Use `bash scripts/run_lighter_first5_calibration_wsl.sh` to produce signed maker/taker spread min/max, percentiles (P1-P99.9), day-by-day stats, distribution, sample CSV and offline Plotly HTML. The training reader only opens files intersecting training time. **Do not read test interval for selection.**
2. Ask user to select entry zones using 5-day distribution (better: P75/P90/P95 and time-above; min/max alone can be one-off). Freeze threshold, qty, exit rules, fee assumptions and portfolio risk BEFORE 22-calendar-date holdout test. No out-of-sample test in this script.
3. Reserved test **2026-10-28 01:00 UTC to 2026-11-19 00:00 UTC exclusive** (21d 23h, 22 calendar dates), respecting last finalized Lighter trades through Nov 18.
4. Maker signal = XAU sell VWAP from L2 bid side minus PAXG best displayed bid where a maker buy might rest. This is *not a verified fill*, and no paired positions are created by calibration script.
5. Prior study already exposed combined 27-day maximum to the analyst/user, so the holdout is not completely information-blind. Still prohibit any further test-interval optimization; ultimately validate on truly unseen forward data.


## OVERRIDES previous 5-day setup: user chose 12-day AVERAGED zones (2026-11-22)
Latest decision supersedes 5-day train / ~22-day test proposal. Do not run an exploratory 22-day test or optimize against prior full-27-day scenarios.
- Training/calibration: **2026-10-23 01:00 UTC inclusive to 2026-11-04 01:00 UTC exclusive**, precisely twelve contiguous 24h windows.
- Holdout: **2026-11-04 01:00 UTC inclusive to 2026-11-19 00:00 UTC exclusive** (14d 23h, 15 calendar dates). Do not read holdout to calibrate zones.
- Use `bash scripts/run_lighter_12d_calibration_wsl.sh` in WSL #2 after `git pull`. Output to Windows Downloads: `LIGHTER_SPREAD_CALIBRATION_12D.zip`; no 0xArchive calls.
- Direction: **short spread on high upper-tail readings:** SELL XAU (VWAP bids) and BUY PAXG (maker at best BID assumed or taker asks). **Long spread on low lower-tail readings:** BUY XAU (VWAP asks) and SELL PAXG (maker at best ASK assumed or taker bids). Note PAXG maker LONG means maker SELL, not BUY.
- User requested "12-day average zone": output the arithmetic mean of each day's 24-hour P75/P90/P95 upper (SHORT) and P5/P10/P25 lower (LONG), **daily equal weighting**, for each size and maker/taker cases. Also retain pooled minute quantiles and per-day trends; don't use overall spread mean as a zone. Review thresholds with user before freezing any backtest.
- The 12-day driver reads raw L2 only through calibration cutoff; `summarize_lighter_12d_zones.py` uses only 12-day CSV and validates bounds. Run tests before analysis. No profits or actual maker fills can be inferred from calibration output alone.


## Forward TEST: TP $2 vs TP $3 gross USD/oz, frozen 12-day grid (2026-11-22)
User requested immediate comparison on the reserved 15-calendar-day period. Raw held-out 20-level order books are stored only in local WSL, not the ChatGPT attachment. Script authored in GitHub, but **numerical forward results require user to run it locally and return the generated ZIP**.
- Run in WSL #2: `cd ~/code/trading-backtest && source .venv/bin/activate && git pull origin master && bash scripts/run_lighter_tp2_tp3_forward_wsl.sh`.
- Window: 2026-11-04 01:00 to 2026-11-19 00:00 UTC exclusive, from local `data/lighter_fresh_20261122/book/{XAU,PAXG}/*.jsonl.gz`, no calls to archive API.
- Frozen entry levels from `CALIBRATION_12D_TWO_SIDED/zone_12d_summary.json`: SHORT at mean daily P75/P90/P95 when short reference rises above; LONG at mean daily P25/P10/P5 when long reference falls below. These depend on tested quantity 0.5/1.0 oz. One concurrent position per grid, close on TP only, then require observation outside zone before a new crossing/re-entry. No timeout/stop loss.
- Compare `TP=2` and `TP=3` **gross executable spread improvement in USD per ounce**, not USD total for 0.5 oz and not a guaranteed net profit. Commission for both instruments at open and close (4 orders); Standard 0 maker/0 taker, Plus .5/.5 bps, Premium .4/2.8 bps as sensitivities. Net profit and hypothetical terminal liquidation MTM are separate. Never deduct bid/ask twice.
- PAXG maker-assumed first plus XAU taker; opposite PAXG maker SELL for LONG and maker BUY for SHORT. Mark maker instant fills as optimistic, NOT historically verified. Also output both-taker sensitivity using same maker-derived trigger zones. Per-tick 20-level liquidity reserved between grid orders.
- Files in Windows Downloads: `LIGHTER_FORWARD_TP2_VS_TP3_15D.zip` with primary skew <=120s and stricter skew <=10s sensitivity; tests and output. Each `tp_2_vs_3_scenarios.csv` has independent combinations; do not sum results across TP, fee tiers, execution modes, or sizes.
- For claims of profitability, **first inspect open positions / terminal liquidation value and maximum marked drawdown**; do not rely on TP-closed realized gains alone. No funding cash transfers, slippage from latency, liquidation/margin, cross-side physical netting, or order history maker fills modeled. A true executable forward-live proof still needed.


## 15-day forensic audit BEFORE interpreting TP $2/$3 (2026-11-22)
User explicitly requested independent verification of paired XAU/PAXG book prices and optimistic PAXG Maker fills. Do not make more strategy changes until source audit is inspected.
- Upstream forward 15-day ZIP (provided by user) has standard 0.5 oz maker-assumed, skew120: TP2=30 closes +$72.573201, TP3=22 closes +$70.323653, no terminal positions. Strict skew<=10s: TP2 total marked net=-$14.499527 and TP3=-$17.714841, with three unclosed grids each. This is NOT a like-for-like profitability estimate because strict filtering drops many minutes; only 6495 of 21536 paired minutes remain.
- Critical event Nov 14, 2026 20:55-20:56 UTC: THREE long maker PAXG sells at same hypothetical best ask 3817.36, XAU buys at ~3804.37; next minute exits XAU at ~3789.66 and PAXG at ~3783; combined estimated +$28.196825 (~38.9% of TP2 main scenario profits). ALL maker orders were assumed instantly filled without order acknowledgement, live queue or later public prints verification.
- New read-only audit code: \`scripts/audit_lighter_forward_maker_15d.py\` and launch \`scripts/run_lighter_maker_quote_audit_wsl.sh\`. It reads local L2 15d and PAXG finalized trade JSONL.gz, checks as-of quote ages and XAU/PAXG snapshot time skew for every hypothetical order, verifies quote-side bounds and replay arithmetic, and queries PAXG public trade prints at/beyond maker limit within 60s and 5min. It builds a minute-by-minute incident 20:52-20:59 quote timeline and PAXG prints.
- Run WSL #2: \`cd ~/code/trading-backtest && source .venv/bin/activate && git pull origin master && bash scripts/run_lighter_maker_quote_audit_wsl.sh\`.
- Return \`C:\Users\June-ASUS\Downloads\LIGHTER_15D_MAKER_QUOTE_FORENSIC.zip\` with summary, full maker entry evidence, 14 Nov incident as-of book timeline and PAXG trade prints. NO API calls, no data rewrite, no new trading.
- Important limitations: Public trade prints at required price indicate possible activity, NOT proof of user's maker fill; market order queue priority/cancellations remain unknown. Conversely no matching public prints in a fully finalized interval challenges instant-fill assumption. Public trade records can duplicate fill legs; only dedup on trade_id when supplied. Check raw trade keys/examples and parsed_in_window before interpreting missing prints.
- If audit is suspicious, replace optimistic maker-assumed execution with a causal maker-first pending-order model that waits for eligible trade activity before XAU taker hedge, accounts for queue uncertainty and adverse selection. Then retest both TP values without future data leakage. Never call old $72.57 "verified profit".


## Maker-first causal queue replay built after forensic report (2026-11-22)
- The user provided the 15d audit console output. Audit: 75 hypothetical maker-entry candidates deduplicated across TP+fee profiles, 45/75 entry snapshot skews >10 sec, only 16/75 any eligible PAXG public prints within 60sec, 0/75 public print size >= 0.5/1oz in first 60sec. Nov 14 20:55 UT PAXG snapshot stale by 58.233s vs XAU by 12.205s; delta skew 46.028s; maker SELL at PAXG $3817.36 not supported by any trade >= that price within 5min. Quoted $72.57 TP2 unrealistically assumed maker fills and not verified profits.
- New **development** replay: \`scripts/replay_lighter_causal_maker_first_15d.py\`, launcher \`scripts/run_lighter_causal_maker_15d_wsl.sh\`, unit tests \`tests/test_lighter_causal_maker_first_15d.py\`.
- Run only on WSL #2:
  \`cd ~/code/trading-backtest && source .venv/bin/activate && git pull --ff-only origin master && bash scripts/run_lighter_causal_maker_15d_wsl.sh\`
- The launcher runs Pytest then reads local XAU and PAXG L2 snapshots + finalized public PAXG trade legs and frozen 12-day \`zone_12d_summary.json\`. It writes \`C:\Users\June-ASUS\Downloads\LIGHTER_CAUSAL_MAKER_TP2_TP3_15D.zip\`; no API calls or overwrite of prior reports.
- IMPORTANT: this 15d window is **no longer untouched OOS** because results inspected. TP2/3 and grids are frozen, but use new unseen dates for fresh forward validation.
- Simulator causal chronology: at PAXG raw timestamp use latest XAU L2 if <=20s old; arm and post limit at PAXG best quote, pending TTL fixed 5min. Queue ahead initialized to displayed PAXG best level, shared among grids at same side and limit. Public trade tape: dedup \`trade_id\`; only \`crossed=true\` (Taker) leg determines aggressor B=BUY/A=SELL; trade must occur AFTER placement at exact limit and opposite aggression, consume queue ahead before any paper fill. On first partial/full evidence allocate only traded quantity and cancel remainder, waiting until a later raw XAU L2 snapshot to open proportional hedge. Exits use fresh/as-of order books and shared order-book depth. Distinct independent qty (0.5/1 oz), TP gross 2/3 USD/oz, fee profiles Standard/Plus/Premium. Positions have no stop/timeout; unfilled maker pending TTL is distinct. Terminal hedged and unhedged PAXG lot marked separately, including simulated fees; do not interpret only closed TP gains. All public trade queue fills remain hypothetical since orders never actually submitted.
- Caveats: 20s freshness and 5min order TTL are pre-set research assumptions, not optimized. Maker queue cancellations/hidden orders/min order sizes and latency aren't reconstructible from L2 1min. Close both takers at effectively separate snapshots (<=20s) still theoretical. Partial fills may be below venue order minimum. Funding margin/liquidations not modeled.
- If WSL tests fail, use console trace to repair; never claim tests ran before user executes WSL.
