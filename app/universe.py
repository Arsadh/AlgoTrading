"""Tradable universe: long-listed, liquid NSE large caps.

Using today's index members for a backtest that starts in 2007 adds survivorship
bias (the stocks that dropped out aren't here), so treat backtest returns as
optimistic. Edit the list freely; Yahoo tickers are SYMBOL + ".NS".
"""

STOCKS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR", "ITC",
    "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE",
    "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO", "NESTLEIND",
    "WIPRO", "HCLTECH", "POWERGRID", "NTPC", "ONGC", "TATASTEEL", "JSWSTEEL",
    "M&M", "BAJAJ-AUTO", "HINDALCO", "DRREDDY", "CIPLA", "GRASIM",
    "ADANIPORTS", "TECHM", "COALINDIA", "BPCL", "EICHERMOT", "HEROMOTOCO",
    "BRITANNIA", "INDUSINDBK",
]

INDEX = "^NSEI"        # Nifty 50
VIX = "^INDIAVIX"      # India VIX (fear gauge)
BANK_INDEX = "^NSEBANK"

# Symbol used for live index quotes on Groww
GROWW_INDEX_SYMBOL = "NIFTY"


def yahoo(symbol: str) -> str:
    return symbol if symbol.startswith("^") else f"{symbol}.NS"
