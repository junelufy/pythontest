# AGENTS — ChatGPT and Codex handoff

This is the user's **standalone** historical manual trading simulator. Do not touch their prediction-arbitrage repository or existing Options Explorer.

- No live order placement. Research only.
- Separate pure accounting/data adapters from Streamlit UI.
- All timestamps are UTC milliseconds; NEVER use future candles for signals.
- Candle touch does not verify Maker fill, and candle close is a proxy market price.
- Do not commit user market databases, credentials, or private material.
- Work in short feature branches; run `python -m pytest -q`.
- Document tests, limitations, and next steps in `docs/HANDOFF.md`.
