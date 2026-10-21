# AlgTrd Live: Requirements

What you need to run the live Nifty / Bank Nifty / MCX commodity analysis app, and what it does.

## 1. To run it

| Need | Required? | Notes |
|---|---|---|
| Python 3.10+ and `pip install -r requirements.txt` | Yes | Tested on 3.12 |
| Internet | Yes | Yahoo Finance, nseindia.com, news RSS feeds |
| A modern browser | Yes | Chrome / Edge / Firefox. Allow notifications if you want alerts |
| Groww Trading API subscription | Optional | Official live candles + option chain with Greeks. Without it the app uses free data. Paid. The free trial excludes live data |
| Anthropic API key | Optional | Claude reads headlines for real market impact. Without it a local word-based model is used |
| Computer on during market hours | For live signals | 09:15–15:30 IST. Signals are graded only while the app runs |

No static IP or SEBI algo registration is needed, because the app never places orders.
You place trades yourself in Groww.

## 2. Functional requirements

### Live data
- L1. 1-minute index candles for Nifty 50 and Bank Nifty, refreshed every 20 s in market hours.
- L2. NSE option chain (OI, change in OI, IV, LTP, bid/ask) every 60 s, any listed expiry.
- L3. India VIX, and day change for 40 large caps (gainers, losers, heatmap).
- L4. News from 7 Indian and global feeds every minute, scored for sentiment and impact.

### Analysis and signals
- A1. Indicators: EMA 9/21, 15-min EMA 20/50, Supertrend (10,3), RSI 14, MACD, ADX, ATR,
  previous-day high/low/close, opening range.
- A2. Option-chain analytics: PCR, change-in-OI PCR, max pain, call wall (resistance), put wall (support), ATM IV.
- A3. Weighted score −100..+100 and a verdict of BUY CALL, BUY PUT or WAIT, with every vote and every blocking reason shown.
- A4. For a signal: ATM strike, nearest expiry, entry, stop-loss, T1 and T2 on the index, and estimated premium
  levels from delta (from Groww, or Black-Scholes from IV).
- A5. Warnings: VIX spike, expiry day, spot near an OI wall, extreme RSI.
- A6. Every signal is stored and graded automatically (stop, breakeven, target, end of day, reversed).
- A7. 60-day backtest of the exact rules, with stats after estimated option costs.

### Multi-decade study
- Q1. Daily history for Nifty 50 (1990→), Nifty Bank (2000→), Sensex, S&P 500 (1950→) and Nasdaq, cached and refreshed daily.
- Q2. 50 rules evaluated per market and period, with costs, against random-timing baselines.
- Q3. A combined daily algorithm selected on training years only; its bias is a vote in the live score.
- Q4. Conditional "how the market moves after its moves" studies with sample sizes and z-scores, for all years and for the recent decade.
- Q5. Today's setup matched against its 40 nearest historical analogs.

### Website
- W1. Live tab: interactive candlestick chart (1/3/5/15 min) with overlays, levels and signal markers; RSI pane; verdict card; option chain.
- W2. Market, Signals, News and History tabs.
- W3. Pushes updates over WebSocket. Alerts by desktop notification and speech.
- W4. Light and dark mode; works on a phone.

## 3. Honest limitations
- **The rules have no proven edge after costs.** Over the last 60 days, Nifty signals averaged
  about +3.5 index points before costs and about −0.5 after estimated costs. Bank Nifty lost money
  after costs. Calls lost and puts won in a falling market, which is regime luck, not skill.
  The Signals tab keeps this scorecard up to date. Read it before trusting any signal.
- **The multi-decade study found no rule that beat simply holding Nifty in the last decade.** Short-term trend edges were
  strong in 1990–2005 and have faded. The combined algorithm's main value is avoiding deep drawdowns, not higher returns.
  Mean-reversion and candlestick rules failed on Indian indices.
- **MCX data without Groww comes from Moneycontrol's public price API, which is unofficial.** MCX's own website blocks
  automated access. Moneycontrol can change or rate-limit it without notice; the app then pauses 60 s and falls
  back to the Yahoo USD-future proxy (delayed ~10 min, live signals blocked). The proxy is not the MCX price: gold
  was ~10% off in October 2026 even with the assumed 6% import duty (`BULLION_DUTY`).
  It makes roughly 4–6 requests a second while MCX is open (futures every 3 s, chains every 3 min, ATM premiums
  every 10 s); raise `MCX_TICK_SECONDS` / `MCX_CHAIN_REFRESH_SECONDS` to be gentler.
- The MCX Bullion Index is on neither Moneycontrol nor Yahoo, so it needs Groww keys.
- MCX option IV and delta are implied from the last traded price, which can be stale on thin strikes
  (some gold and zinc strikes trade only a few lots a day).
- **Every commodity's intraday backtest lost money after estimated costs** (real MCX 5-min data, 23–41 usable days
  to October 2026, about 70–120 signals each). Mini contracts lose more because costs are larger relative to the lot.
  Option-cost estimates for the commodities are rough guesses; check them against real spreads.
- MCX trading hours: 09:00–23:30 IST while the US is on daylight time, 09:00–23:55 in US winter. Crude signals
  fire between 09:15 and 30 minutes before the close.
- Option P&L is worse than index P&L: time decay (theta) and IV changes are not modelled.
- Yahoo intraday data can lag by a minute or more. Groww keys give fresher candles.
- The NSE option chain is a public website endpoint, not an official API. NSE may rate-limit or change it.
  Groww is the fallback.
- Exchange holidays are not modelled.
- Sharing these signals with others as paid tips needs SEBI Research Analyst registration.
- SEBI studies found about 9 in 10 individual F&O traders lose money. Use small sizes, always use the stop-loss,
  and treat this as a decision aid, not an oracle.
