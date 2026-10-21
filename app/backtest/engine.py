"""Vectorised portfolio backtester for daily target-weight strategies.

Assumptions (keep them in mind when reading results):
- Orders fill at the close of the signal day. The live engine mirrors this by
  rebalancing at DECISION_TIME (15:10 IST) using last traded prices.
- COST_BPS per side covers brokerage, STT/stamp/exchange charges and slippage.
- Weights are rebalanced to target only when the target changes (drift between
  changes is ignored).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

COST_BPS = 15          # 0.15% per side
RISK_FREE = 0.06       # ~Indian T-bill yield, for Sharpe
TRADING_DAYS = 252


def run(weights: pd.DataFrame, close: pd.DataFrame, cost_bps: float = COST_BPS) -> pd.DataFrame:
    """Returns a frame with daily gross/net returns, exposure and turnover."""
    w = weights.reindex(index=close.index, columns=close.columns).fillna(0.0)
    rets = close.pct_change(fill_method=None).fillna(0.0)
    held = w.shift(1).fillna(0.0)
    gross = (held * rets).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
    net = gross - turnover * cost_bps / 1e4
    return pd.DataFrame({"gross": gross, "net": net, "exposure": held.sum(axis=1), "turnover": turnover})


def metrics(ret: pd.Series, exposure: pd.Series | None = None, turnover: pd.Series | None = None) -> dict:
    ret = ret.dropna()
    if len(ret) < 20:
        return {}
    eq = (1 + ret).cumprod()
    years = len(ret) / TRADING_DAYS
    cagr = eq.iloc[-1] ** (1 / years) - 1
    vol = ret.std() * np.sqrt(TRADING_DAYS)
    excess = ret - RISK_FREE / TRADING_DAYS
    sharpe = excess.mean() / ret.std() * np.sqrt(TRADING_DAYS) if ret.std() > 0 else 0.0
    downside = ret[ret < 0].std() * np.sqrt(TRADING_DAYS)
    sortino = (ret.mean() * TRADING_DAYS - RISK_FREE) / downside if downside > 0 else 0.0
    dd = eq / eq.cummax() - 1
    max_dd = dd.min()
    out = {
        "cagr": cagr, "volatility": vol, "sharpe": sharpe, "sortino": sortino,
        "max_drawdown": max_dd, "calmar": cagr / abs(max_dd) if max_dd < 0 else 0.0,
        "total_return": eq.iloc[-1] - 1, "best_day": ret.max(), "worst_day": ret.min(),
        "pct_positive_days": (ret[ret != 0] > 0).mean() if (ret != 0).any() else 0.0,
    }
    if exposure is not None:
        out["avg_exposure"] = exposure.reindex(ret.index).mean()
    if turnover is not None:
        out["turnover_per_year"] = turnover.reindex(ret.index).sum() / years
    return {k: float(round(v, 4)) for k, v in out.items()}


def yearly_returns(ret: pd.Series) -> dict[str, float]:
    yr = (1 + ret).groupby(ret.index.year).prod() - 1
    return {str(k): float(round(v, 4)) for k, v in yr.items()}


def equity_curve(ret: pd.Series, every: str = "W-FRI") -> list[list]:
    eq = (1 + ret).cumprod().resample(every).last().dropna()
    return [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in eq.items()]
