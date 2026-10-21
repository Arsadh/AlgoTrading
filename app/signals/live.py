"""Turn the latest candles + option chain + news into one live CALL / PUT / WAIT view."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from ..config import IST, settings
from ..market.feed import resample
from ..market.instruments import Instrument
from . import engine as E
from . import options

IST_OFFSET = 19800  # charts show IST wall-clock time


def _ts(t: pd.Timestamp) -> int:
    return int(t.timestamp()) + IST_OFFSET


def completed_5m(df1m: pd.DataFrame, now: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    df5 = resample(df1m, 5)
    if len(df5) and df5.index[-1] + timedelta(minutes=5) > now:
        return df5, df5.iloc[:-1]
    return df5, df5


def _detail(name: str, row: pd.Series, chain_info: dict | None, mood: dict | None, daily: dict | None = None) -> str:
    f = lambda x: f"{x:,.1f}" if pd.notna(x) else "–"  # noqa: E731
    c = row["Close"]
    return {
        "trend_15m": f"price {f(c)} · EMA20 {f(row['ema20_15'])} · EMA50 {f(row['ema50_15'])}",
        "supertrend": f"{'up' if row['st_dir'] == 1 else 'down'}trend, line at {f(row['st_line'])}",
        "ema_9_21": f"EMA9 {f(row['ema9'])} vs EMA21 {f(row['ema21'])}",
        "macd": f"histogram {row['macd_h']:+.2f}" if pd.notna(row["macd_h"]) else "–",
        "rsi": f"RSI {row['rsi']:.0f}" if pd.notna(row["rsi"]) else "–",
        "prev_day": f"PDH {f(row['pdh'])} · PDL {f(row['pdl'])} · PDC {f(row['pdc'])}",
        "opening_range": f"OR high {f(row['orh'])} · low {f(row['orl'])}" if pd.notna(row["orh"]) else "forms 09:15-09:30",
        "option_chain": (f"PCR {chain_info['pcr']} · change-in-OI PCR {chain_info['chg_pcr'] or '–'}"
                         if chain_info else "unavailable"),
        "news": f"mood {mood['score']:+.2f} ({mood['label']})" if mood else "–",
        "daily_algo": (f"{daily['bullish']} of {daily['members']} daily rules bullish, {daily['bearish']} bearish "
                       f"(bias {daily['bias']:+.2f}, as of {daily['date']} close)") if daily else "–",
    }[name]


def analyze(inst: Instrument, df1m: pd.DataFrame, chain: dict | None, mood: dict | None,
            vix: pd.DataFrame | None, source: str, daily: dict | None = None,
            expiries: list[str] | None = None, iv_hint: float | None = None) -> dict:
    now = datetime.now(IST)
    hrs = inst.hours(now.date())
    df5, done = completed_5m(df1m, now)
    f = E.frame(done, hrs["or_end"])
    v = E.votes(f)
    row, vrow = f.iloc[-1], v.iloc[-1].to_dict()
    spot = float(df1m["Close"].iloc[-1])
    last_tick = df1m.index[-1]

    chain_info = options.analyze_chain(chain, spot, inst.strike_step) if chain else None
    weights = dict(E.TECH_WEIGHTS)
    if chain_info:
        vrow["option_chain"], weights["option_chain"] = chain_info["vote"], E.LIVE_WEIGHTS["option_chain"]
    if daily:
        vrow["daily_algo"], weights["daily_algo"] = daily["bias"], E.DAILY_WEIGHT["daily_algo"]
    if mood and mood.get("headlines"):
        vrow["news"], weights["news"] = max(-1.0, min(1.0, mood["score"] * 2)), E.LIVE_WEIGHTS["news"]
    score = sum(weights[k] * vrow[k] for k in weights) / sum(weights.values()) * 100

    # Same gates as the backtest
    blockers = []
    t = last_tick.time()
    if not inst.is_open(now):
        blockers.append(f"{inst.exchange} is closed (session {hrs['open']:%H:%M}–{hrs['close']:%H:%M} IST, Mon–Fri)")
    elif not (hrs["sig_from"] <= t <= hrs["sig_to"]):
        blockers.append(f"Outside the signal window {hrs['sig_from']:%H:%M}–{hrs['sig_to']:%H:%M} IST")
    age_min = (now - last_tick).total_seconds() / 60
    if inst.exchange == "MCX" and inst.is_open(now) and age_min > 3:
        why = (f"Yahoo delays {inst.proxy_name} ~10 min; the real MCX feed (Moneycontrol) failed" if source.startswith("proxy")
               else "no new MCX trades: a holiday, a half session or a feed problem")
        blockers.append(f"Price data is ~{age_min:.0f} min old ({why}): no live call/put from stale data.")
    if pd.isna(row["adx"]) or row["adx"] < settings.min_adx:
        blockers.append(f"ADX {row['adx']:.0f} below {settings.min_adx:.0f}: market is choppy, not trending")
    thr = settings.signal_threshold
    side = ""
    if score >= thr:
        if row["st_dir"] != 1:
            blockers.append("Score is bullish but Supertrend is still down")
        elif row["rsi"] >= 75:
            blockers.append(f"RSI {row['rsi']:.0f}: already overbought, late to buy calls")
        else:
            side = "CALL"
    elif score <= -thr:
        if row["st_dir"] != -1:
            blockers.append("Score is bearish but Supertrend is still up")
        elif row["rsi"] <= 25:
            blockers.append(f"RSI {row['rsi']:.0f}: already oversold, late to buy puts")
        else:
            side = "PUT"
    else:
        blockers.append(f"Score {score:+.0f} is inside ±{thr:.0f}: no clear edge")
    if blockers and side:
        side = ""

    warnings = []
    if vix is not None and len(vix):
        vt = vix[vix.index.date == vix.index[-1].date()]
        if len(vt) > 1:
            ch = vt["Close"].iloc[-1] / vt["Open"].iloc[0] - 1
            if ch > 0.08:
                warnings.append(f"India VIX up {ch:.0%} today: option premiums are expensive and moves are jumpy")
    nearest = (chain or {}).get("expiry") or ((expiries or [None])[0])
    if nearest and datetime.strptime(nearest, "%d-%b-%Y").date() == now.date():
        warnings.append("Expiry day: today's options decay very fast and moves get erratic"
                        + (" (next-month expiry is suggested instead)" if not chain else ""))
    if source.startswith("proxy"):
        conv = " × unit conversion" + (" + import duty" if inst.group == "Bullion" else "") if inst.proxy_mult != 1 else ""
        warnings.append(f"The real MCX feed (Moneycontrol) is unavailable, so the price is a proxy ({inst.proxy_name} × USD/INR{conv}, "
                        "delayed ~10 min by Yahoo), not the MCX quote. It can differ by local premiums, duty and the contract "
                        "roll (gold by ~10%), so strikes may be several steps off. It retries automatically.")
    if chain_info:
        if chain_info["call_wall"] and 0 <= chain_info["call_wall"] - spot <= 0.3 * inst.strike_step * 4:
            warnings.append(f"Spot is close to heavy call writing at {chain_info['call_wall']:,.0f} (resistance)")
        if chain_info["put_wall"] and 0 <= spot - chain_info["put_wall"] <= 0.3 * inst.strike_step * 4:
            warnings.append(f"Spot is close to heavy put writing at {chain_info['put_wall']:,.0f} (support)")
    if pd.notna(row["rsi"]) and (row["rsi"] > 75 or row["rsi"] < 25):
        warnings.append(f"RSI {row['rsi']:.0f} is extreme: pullbacks are likely")

    lv = E.levels(side or ("CALL" if score >= 0 else "PUT"), spot, row["st_line"], row["atr"])
    opt = options.pick_option(side or ("CALL" if score >= 0 else "PUT"), spot, lv, chain, inst.strike_step,
                              expiries, iv_hint, f"{hrs['close']:%H:%M}")
    bias = "bullish" if score >= 20 else "bearish" if score <= -20 else "neutral"

    reasons = [{"key": k, "label": E.LABELS[k], "vote": round(float(vrow[k]), 2), "weight": weights[k],
                "detail": _detail(k, row, chain_info, mood, daily)} for k in weights]
    day_df = df1m[df1m.index.date == last_tick.date()]
    prev_close = row["pdc"]
    return {
        "key": inst.key, "name": inst.name, "exchange": inst.exchange, "kind": inst.kind, "news_topic": inst.news_topic,
        "open": inst.is_open(now), "spot": round(spot, 2), "source": source,
        "last_tick": last_tick.isoformat(), "bar_time": done.index[-1].isoformat(),
        "change": round(spot - prev_close, 2) if pd.notna(prev_close) else None,
        "change_pct": round((spot / prev_close - 1) * 100, 2) if pd.notna(prev_close) else None,
        "day_high": round(float(day_df["High"].max()), 2) if len(day_df) else None,
        "day_low": round(float(day_df["Low"].min()), 2) if len(day_df) else None,
        "score": round(score, 1), "bias": bias, "signal": side, "blockers": blockers, "warnings": warnings,
        "levels": {k: round(float(x), 2) for k, x in lv.items()}, "option": opt, "reasons": reasons,
        "indicators": {k: (round(float(row[k]), 2) if pd.notna(row[k]) else None)
                       for k in ("rsi", "adx", "atr", "ema9", "ema21", "st_line", "pdh", "pdl", "pdc", "orh", "orl")},
        "chain": chain_info, "chain_meta": {"source": chain["source"], "timestamp": chain["timestamp"],
                                            "expiry": chain["expiry"], "expiries": chain.get("expiries", [])} if chain else None,
        "computed_at": now.isoformat(timespec="seconds"),
    }


def chart(df1m: pd.DataFrame, tf: int, sessions: int = 3, chain_info: dict | None = None,
          inst: Instrument | None = None) -> dict:
    """Candles + overlays for the browser chart (lightweight-charts format)."""
    hrs = inst.hours() if inst else None
    df = resample(df1m, tf) if tf > 1 else df1m
    days = sorted(set(df.index.date))[-sessions:]
    full = E.frame(df, hrs["or_end"]) if hrs else E.frame(df)
    f = full[pd.Series(full.index.date, index=full.index).isin(days).values]
    st = [{"time": _ts(t), "value": round(float(r.st_line), 2), "color": "up" if r.st_dir == 1 else "down"}
          for t, r in f.iterrows() if pd.notna(r.st_line)]
    line = lambda col: [{"time": _ts(t), "value": round(float(x), 2)} for t, x in f[col].items() if pd.notna(x)]  # noqa: E731
    sig = E.fresh(E.decide(full, E.score(E.votes(full), E.TECH_WEIGHTS),
                           (hrs["sig_from"], hrs["sig_to"]) if hrs else None))
    sig = sig[sig != ""]
    sig = sig[pd.Series(sig.index.date, index=sig.index).isin(days).values]
    last = f.iloc[-1] if len(f) else None
    levels = {}
    if last is not None:
        for k, name in (("pdh", "Prev high"), ("pdl", "Prev low"), ("pdc", "Prev close"), ("orh", "OR high"), ("orl", "OR low")):
            if pd.notna(last[k]):
                levels[name] = round(float(last[k]), 2)
    if chain_info:
        for k, name in (("call_wall", "Call wall (resistance)"), ("put_wall", "Put wall (support)"), ("max_pain", "Max pain")):
            if chain_info.get(k):
                levels[name] = chain_info[k]
    return {
        "tf": tf,
        "candles": [{"time": _ts(t), "open": round(r.Open, 2), "high": round(r.High, 2), "low": round(r.Low, 2),
                     "close": round(r.Close, 2)} for t, r in f.iterrows()],
        "ema9": line("ema9"), "ema21": line("ema21"), "supertrend": st, "rsi": line("rsi"),
        "levels": levels,
        "rule_markers": [{"time": _ts(t), "side": s} for t, s in sig.items()],
    }
