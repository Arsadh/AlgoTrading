"""Option-chain analytics and strike selection."""
from __future__ import annotations

import math
from datetime import datetime

from ..config import IST


def _num(x):
    try:
        return float(x) if x is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def analyze_chain(chain: dict, spot: float, step: int, width: int = 10) -> dict:
    rows = chain["rows"]
    atm = round(spot / step) * step
    near = [r for r in rows if abs(r["strike"] - atm) <= width * step]
    ce_oi = sum(_num(r["ce"] and r["ce"]["oi"]) for r in rows)
    pe_oi = sum(_num(r["pe"] and r["pe"]["oi"]) for r in rows)
    ce_chg = sum(_num(r["ce"] and r["ce"]["chg_oi"]) for r in near)
    pe_chg = sum(_num(r["pe"] and r["pe"]["chg_oi"]) for r in near)
    has_chg = any(r["ce"] and r["ce"]["chg_oi"] is not None for r in near)

    above = [r for r in near if r["strike"] >= spot and r["ce"]]
    below = [r for r in near if r["strike"] <= spot and r["pe"]]
    call_wall = max(above, key=lambda r: _num(r["ce"]["oi"]))["strike"] if above else None
    put_wall = max(below, key=lambda r: _num(r["pe"]["oi"]))["strike"] if below else None

    # Max pain: strike where option writers pay out the least at expiry
    strikes = [r["strike"] for r in rows]
    pain = None
    if strikes:
        def payout(k):
            return sum(_num(r["ce"] and r["ce"]["oi"]) * max(0.0, k - r["strike"])
                       + _num(r["pe"] and r["pe"]["oi"]) * max(0.0, r["strike"] - k) for r in rows)
        cand = [s for s in strikes if abs(s - atm) <= 30 * step]
        pain = min(cand, key=payout) if cand else None

    atm_row = next((r for r in rows if r["strike"] == atm), None)
    atm_iv = None
    if atm_row:
        ivs = [_num(atm_row[s]["iv"]) for s in ("ce", "pe") if atm_row[s] and _num(atm_row[s]["iv"]) > 0]
        atm_iv = sum(ivs) / len(ivs) if ivs else None

    pcr = pe_oi / ce_oi if ce_oi else None
    chg_pcr = (pe_chg / ce_chg) if has_chg and ce_chg > 0 and pe_chg > 0 else None
    # Vote: put writers adding positions = support below = bullish (and vice versa)
    vote = 0.0
    if pcr is not None:
        vote += 0.5 if pcr > 1.2 else -0.5 if pcr < 0.8 else (pcr - 1.0) * 2.5 * 0.5
    if has_chg:
        if pe_chg > 0 and ce_chg <= 0:
            vote += 0.5
        elif ce_chg > 0 and pe_chg <= 0:
            vote -= 0.5
        elif chg_pcr is not None:
            vote += 0.5 if chg_pcr > 1.3 else -0.5 if chg_pcr < 0.7 else 0.0
    return {
        "atm": atm, "pcr": round(pcr, 2) if pcr else None, "chg_pcr": round(chg_pcr, 2) if chg_pcr else None,
        "ce_chg_oi": ce_chg if has_chg else None, "pe_chg_oi": pe_chg if has_chg else None,
        "call_wall": call_wall, "put_wall": put_wall, "max_pain": pain,
        "atm_iv": round(atm_iv, 2) if atm_iv else None, "vote": max(-1.0, min(1.0, vote)),
    }


def chain_window(chain: dict, spot: float, step: int, width: int = 8) -> list[dict]:
    atm = round(spot / step) * step
    return [r for r in chain["rows"] if abs(r["strike"] - atm) <= width * step]


# ------------------------------------------------------------ Black-Scholes
def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_delta(spot: float, strike: float, years: float, iv_pct: float, call: bool, r: float = 0.065) -> float:
    if years <= 0 or not iv_pct:
        return (1.0 if spot > strike else 0.0) if call else (-1.0 if spot < strike else 0.0)
    sig = iv_pct / 100
    d1 = (math.log(spot / strike) + (r + sig * sig / 2) * years) / (sig * math.sqrt(years))
    return _ncdf(d1) if call else _ncdf(d1) - 1


def bs_price(spot: float, strike: float, years: float, iv_pct: float, call: bool, r: float = 0.065) -> float:
    if years <= 0 or not iv_pct:
        return max(0.0, spot - strike) if call else max(0.0, strike - spot)
    sig = iv_pct / 100
    d1 = (math.log(spot / strike) + (r + sig * sig / 2) * years) / (sig * math.sqrt(years))
    d2 = d1 - sig * math.sqrt(years)
    disc = math.exp(-r * years)
    if call:
        return spot * _ncdf(d1) - strike * disc * _ncdf(d2)
    return strike * disc * _ncdf(-d2) - spot * _ncdf(-d1)


def implied_vol(price: float, spot: float, strike: float, years: float, call: bool, r: float = 0.065) -> float | None:
    """IV in % that reproduces an option's price (bisection). None if the price is below intrinsic or expired."""
    if not price or years <= 0 or price <= max(0.0, spot - strike if call else strike - spot) * 0.999:
        return None
    lo, hi = 1.0, 400.0
    if bs_price(spot, strike, years, hi, call, r) < price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, years, mid, call, r) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def years_to_expiry(expiry: str, close: str = "15:30") -> float:
    hh, mm = map(int, close.split(":"))
    exp = datetime.strptime(expiry, "%d-%b-%Y").replace(hour=hh, minute=mm, tzinfo=IST)
    secs = (exp - datetime.now(IST)).total_seconds()
    return max(secs, 0) / (365 * 24 * 3600)


def pick_option(side: str, spot: float, lv: dict, chain: dict | None, step: int,
                expiries: list[str] | None = None, iv_hint: float | None = None, close: str = "15:30") -> dict:
    """ATM strike for the signal + rough premium levels from delta.

    With an option chain the premium is the live LTP. Without one (MCX without Groww keys)
    it is a Black-Scholes estimate using iv_hint (recent realised volatility), labelled as such."""
    atm = round(spot / step) * step
    out = {"strike": atm, "type": "CE" if side == "CALL" else "PE"}
    call = side == "CALL"
    if not chain:
        today = datetime.now(IST).date()
        future = [e for e in (expiries or []) if datetime.strptime(e, "%d-%b-%Y").date() > today]
        if not future or not iv_hint:
            return out
        out["expiry"] = future[0]
        yrs = years_to_expiry(future[0], close)
        prem = bs_price(spot, atm, yrs, iv_hint, call)
        delta = bs_delta(spot, atm, yrs, iv_hint, call)
        out["premium_source"] = f"theoretical (Black-Scholes, IV ≈ {iv_hint:.0f}% realised volatility)"
        leg = {"iv": round(iv_hint, 1)}
    else:
        out["expiry"] = chain["expiry"]
        row = next((r for r in chain["rows"] if r["strike"] == atm), None)
        leg = row and row["ce" if call else "pe"]
        if not leg or not leg.get("ltp"):
            return out
        prem = float(leg["ltp"])
        yrs = years_to_expiry(chain["expiry"], close)
        delta = leg.get("delta") or bs_delta(spot, atm, yrs, leg.get("iv") or 15, call)
        out["premium_source"] = f"{chain['source'].upper()} option chain"
    d = abs(delta)
    move_sl, move_t1, move_t2 = abs(lv["entry"] - lv["sl"]), abs(lv["t1"] - lv["entry"]), abs(lv["t2"] - lv["entry"])
    out.update({
        "premium": round(prem, 2), "delta": round(delta, 2), "iv": leg.get("iv"),
        "premium_sl": round(max(0.05, prem - d * move_sl), 2),
        "premium_t1": round(prem + d * move_t1, 2), "premium_t2": round(prem + d * move_t2, 2),
        "days_to_expiry": round(yrs * 365, 1),
    })
    return out
