# AlgTrd: live Nifty, Bank Nifty and MCX commodity analysis with call & put signals

A local web app that watches Nifty 50, Bank Nifty and every MCX commodity with listed options live and tells you **BUY CALL (CE)**,
**BUY PUT (PE)** or **WAIT**. Each signal comes with the strike, expiry, stop-loss, targets
and the reasons behind it. It also shows live charts, the option chain, market breadth,
news sentiment and 19 years of market history.
It does **not** place orders. See [REQUIREMENTS.md](REQUIREMENTS.md).

## Instruments

| Group | Instruments | Without Groww keys |
|---|---|---|
| Indices | Nifty 50, Bank Nifty | Yahoo + NSE option chain |
| Energy | Crude Oil, Crude Oil Mini, Natural Gas, Natural Gas Mini | Real MCX prices, candles and option chain from Moneycontrol |
| Bullion | Gold, Gold Mini, Silver, Silver Mini | Real MCX prices, candles and option chain from Moneycontrol |
| Bullion | MCX Bullion Index | none: needs Groww keys |
| Base metals | Copper, Zinc | Real MCX prices, candles and option chain from Moneycontrol |

MCX's own website blocks automated access, so MCX data comes from Moneycontrol's public price API
(no login or keys; see [app/market/mcx.py](app/market/mcx.py)). If it fails, the app falls back to a proxy:
the global USD future (NYMEX WTI / Henry Hub, COMEX gold / silver / copper) × USD/INR from Yahoo, delayed ~10 min.

Strike steps, lot sizes and expiries come from Groww's public instrument list (a downloadable file, no keys). To add another contract,
add one line to `INSTRUMENTS` in [app/market/instruments.py](app/market/instruments.py).

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on Linux/macOS)
pip install -r requirements.txt
copy .env.example .env            # optional: add Groww / Claude keys
python -m app.quant.research      # optional: multi-decade study (also auto-runs on first start)
python -m app.research.analyze    # optional: 19-year stock history for the History tab
python run.py                     # open http://127.0.0.1:3001
```

## Tabs

| Tab | What you get |
|---|---|
| **Live** | 1/3/5/15-min candles with EMA 9/21, Supertrend, RSI, previous-day and opening-range levels, option walls, max pain. The verdict card shows score −100..+100, BUY CE/PE or WAIT, strike, entry/SL/T1/T2 on spot and estimated premium, every indicator's vote, blockers and warnings. Also the live option chain (OI, change in OI, IV, LTP). |
| **Market** | India VIX, advancing vs declining stocks, large-cap heatmap, top gainers and losers |
| **Signals** | 60-day backtest of the exact rules (after estimated costs), live scorecard, log of every signal and how it ended |
| **Quant Lab** | 50 rules tested on 56,000+ days across 5 markets (Nifty since 1990, S&P 500 since 1950); the combined daily algorithm; how the market moves after streaks, big days and weeks, drawdowns, RSI levels, gaps and calendar dates; today's 40 closest historical matches |
| **News** | Live headlines with sentiment and impact; optional read-aloud |
| **History** | 19-year Nifty behaviour: VIX regimes, gaps, shock days, event study, seasonality |

Tick **Alerts** in the header for a desktop notification plus spoken alert on every new signal.

## How a signal is made

Every completed 5-min candle gets a weighted score from 9 votes:

| Vote | Weight | Bullish when |
|---|---|---|
| 15-min trend (EMA 20/50) | 20 | price > EMA20 > EMA50 |
| Supertrend (10, 3) | 20 | in up-trend |
| EMA 9 vs 21 | 10 | EMA9 above EMA21 |
| MACD histogram | 10 | positive and rising |
| RSI 14 | 10 | above 55–60 |
| Previous day H/L/C | 10 | above previous high |
| Opening range 09:15–09:30 | 10 | above OR high |
| Option chain | 10 | PCR > 1.2, put writers adding OI |
| Daily algorithm | 10 | most of the 21 multi-decade rules are bullish at yesterday's close |
| News mood | 5 | positive headlines |

**BUY CALL** needs: score ≥ +45, Supertrend up, ADX ≥ 18, RSI < 75, time between 09:30 and 14:45.
**BUY PUT** is the mirror image. The stop-loss is the Supertrend line limited to 0.5–1.5 ATR away. T1 = 1R and T2 = 2R; after T1 the stop moves to entry.
There are at most 3 signals per index per day, and only on fresh setups.

All thresholds are in `.env`. The rules are in [app/signals/engine.py](app/signals/engine.py).

## Data sources

| Data | Primary | Fallback |
|---|---|---|
| Live price (pushed every second) | Groww live feed: real exchange ticks (needs Groww Trade API keys) | Yahoo: polled every second, but Yahoo itself only refreshes every ~10–30 s |
| Index candles | Groww (if keys set) | Yahoo Finance 1-min |
| Lot sizes, option contracts | Groww public instrument master (no keys needed) | – |
| MCX commodity prices | Groww: nearest MCX future, streamed (if keys set) | Moneycontrol: nearest MCX future every 3 s (`MCX_TICK_SECONDS`), 1-min candles. If that fails: the Yahoo USD-future proxy, **delayed ~10 min**, and live signals are blocked. Bullion Index: Groww only. |
| MCX option chain and premiums | Groww | Moneycontrol: one quote per strike, ±6 strikes (`MCX_CHAIN_WIDTH`) every 3 min (`MCX_CHAIN_REFRESH_SECONDS`), with OI, change in OI, bid/ask; IV and delta implied from the price |
| MCX backtest history | – | Moneycontrol: ~60 days of 5-min candles of today's nearest future (days that are mostly flat bars, when it was a thin far month, are dropped) |
| Option chain | NSE public API | Groww |
| News | 7 RSS feeds | – |
| Long history | NSE (Nifty 1990→, Bank Nifty 2000→) | Yahoo Finance (Sensex, S&P 500, Nasdaq) |

## Prediction log and public page

Every hour while a market is open, the app records each instrument's call (score, bias, signal, strike,
stop-loss, targets) in `docs/data/predictions/YYYY-MM-DD.json`, with that day's live signals and results.
Each bullish/bearish call is graded twice: 1 hour later and at the close. `docs/data/summary.json` keeps the
running accuracy (50% = coin flip) and `docs/data/latest.json` the current calls.

With `PUBLISH_GIT=1` in `.env`, the app commits and pushes `docs/data` after every hourly write and at each
close; GitHub Pages serves `docs/index.html` as a one-page dashboard. Git handles the login (Git Credential
Manager), so no token is stored by the app.

**Measured so far:** on ~60 days of 5-minute data (12 instruments, held-out period), the score called the
direction of the next 30–60 minutes correctly 47–50% of the time, and the strongest scores (≥ 95) only 41–45%.
No filter reached anything close to 80%. The log exists to keep measuring this honestly.

## The multi-decade study (Quant Lab)

- **Data:** Nifty 50 from 1990 and Nifty Bank from 2000 (NSE), Sensex from 1997, S&P 500 from 1950 and Nasdaq from 1971 (Yahoo).
  For the MCX commodities: WTI crude, Henry Hub natural gas and COMEX gold, silver and copper from 2000 (Yahoo). These drive each commodity's daily-algorithm vote.
- **Rules:** 50 trend, momentum, mean-reversion, candlestick, calendar and volatility rules, listed in [app/quant/rules.py](app/quant/rules.py).
- **Each rule is tested in three separate periods** (train, test, holdout), on all 5 markets, and against 200 random strategies with the same exposure.
- **The algorithm:** rules are chosen using only the train years of Nifty and S&P 500, then each gets an equal vote. Its daily bias feeds the live score.
- **Move studies:** see [app/quant/moves.py](app/quant/moves.py).
