# Fresh Lighter Historical Depth Data — acquisition plan

User requests a completely new Lighter XAU + PAXG market-data download. Do not reuse existing October-November dataset or touch prediction arbitrage.

Source: 0xArchive historical Lighter REST API (personal API key required). Native Lighter API only provides live order book.

Required: L2 bid/ask snapshots with volume at every returned price level; 1m OHLCV; funding; trade prints; UTC; resumable bounded windows; raw provenance. Export historical_bid_ask.csv (ts_ms,symbol,bid,ask,bid_size,ask_size) and lighter_marketdata.sqlite3 (candles, quotes, funding). Preserve full L2 JSONL.gz for later size-aware VWAP calculations.

A tested downloadable package lighter_30d_downloader.zip was provided in the conversation. User should unzip it into ~/code/trading-backtest/, run included unit tests, and eventually git add/commit/push scripts only, not data.

Routes: /v1/lighter/orderbook/{symbol}/history (granularity=checkpoint, depth=20), /v1/lighter/candles/{symbol} (interval=1m), /v1/lighter/funding/{symbol}, /v1/lighter/trades/{symbol}. Free tier reports access to last 30 days, with API credit limits. Never hardcode or paste API key into chat.

Store in local data/lighter_fresh/ (ignored by Git). Run in WSL window 2 and copy review ZIP to /mnt/c/Users/June-ASUS/Downloads/. Use RUN_INFO.json and QUALITY_REPORT.json to verify availability before claiming complete 30 days. Order book snapshots are not guaranteed maker fills.

Docs: https://docs.0xarchive.io/openapi and https://0xarchive.io/resources/market-data-guides/lighter-api