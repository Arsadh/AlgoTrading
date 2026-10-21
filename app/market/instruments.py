"""Instruments that get live call/put analysis. Add more by extending INSTRUMENTS.

NSE index options and MCX commodity options share the same analysis; what differs
is captured here: exchange, trading hours, signal window, price source, contract
units and the news filter used for the sentiment vote.

MCX covers every commodity with listed options: crude oil, natural gas, gold,
silver (big and mini contracts), copper, zinc and the MCX Bullion Index.
Real MCX prices come from Groww (with keys) or Moneycontrol (free, see mcx.py);
the Yahoo USD-future proxy is only a fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from ..config import IST, settings

CRUDE_NEWS = r"\b(crude|oil|opec|brent|wti|petroleum|refiner|shale|barrel)"
GAS_NEWS = r"\b(natural gas|natgas|lng|henry hub|gas (price|storage|inventor|demand|supply|export))"
BULLION_NEWS = r"\b(gold|silver|bullion|precious metal|fed|fomc|powell|dollar index|treasury yield)"
METAL_NEWS = r"\b(copper|zinc|aluminium|aluminum|nickel|base metal|lme|china (pmi|factory|demand|stimulus))"

TROY_OZ_G = 31.1035
LB_KG = 0.45359237
# India's customs duty on gold and silver (6% since July 2024). MCX bullion trades at the
# landed price, so the USD proxy is scaled up by this. Update it if the duty changes.
BULLION_DUTY = 0.06


@dataclass(frozen=True)
class Instrument:
    key: str            # option underlying symbol (NSE / MCX), e.g. NIFTY, CRUDEOIL
    name: str
    yahoo: str          # Yahoo ticker for candles (MCX: the global USD future used as a proxy; "" = none)
    groww: str          # Groww candle symbol for indices; MCX uses the nearest future (see contracts)
    strike_step: int | float  # distance between listed strikes near the money
    # Rough round-trip cost of trading the ATM option (bid-ask spread + brokerage,
    # taxes, exchange charges) in underlying points at delta 0.5. An estimate.
    cost_points: float
    exchange: str = "NSE"
    kind: str = "index"             # index | commodity
    group: str = "Indices"          # button group in the UI
    fx: str | None = None           # Yahoo FX ticker converting the USD proxy into INR
    proxy_mult: float = 1.0         # USD-proxy unit conversion (x import duty) into the MCX quote unit
    proxy_name: str = ""            # e.g. "NYMEX WTI crude"
    quant_market: str = ""          # multi-decade market used for the daily algorithm / setup
    news_filter: str | None = None  # regex: only these headlines feed this instrument's news vote
    news_topic: str = ""            # short name for that news, e.g. "crude"
    unit: tuple[float, str] = (1, "qty")  # one quoted price unit: (quantity, unit), e.g. gold (10, "g")
    moneycontrol: bool = False      # MCX: real prices from Moneycontrol's public API

    @property
    def needs_groww(self) -> bool:
        """No free price source: only works with Groww API keys."""
        return not self.yahoo and not self.moneycontrol

    @property
    def available(self) -> bool:
        return not self.needs_groww or settings.has_groww_creds

    def lot_desc(self, lot: int) -> str:
        """Human contract size, e.g. gold lot 100 x 10 g -> '1 kg'."""
        qty, u = lot * self.unit[0], self.unit[1]
        if u == "g" and qty >= 1000:
            qty, u = qty / 1000, "kg"
        return f"{qty:,g} {u}"

    def hours(self, d: date | None = None) -> dict[str, dtime]:
        """Session open/close, signal window and end-of-day exit for a trading date."""
        if self.exchange == "MCX":
            close = mcx_close(d)
            dt = lambda t, m: (datetime.combine(date.today(), t) - timedelta(minutes=m)).time()  # noqa: E731
            return {"open": dtime(9, 0), "close": close, "or_end": dtime(9, 15),
                    "sig_from": dtime(9, 15), "sig_to": dt(close, 30), "eod": dt(close, 15)}
        h = lambda s: dtime(*map(int, s.split(":")))  # noqa: E731
        return {"open": dtime(9, 15), "close": dtime(15, 30), "or_end": dtime(9, 30),
                "sig_from": h(settings.no_signal_before), "sig_to": h(settings.no_signal_after), "eod": dtime(15, 20)}

    def is_open(self, now: datetime | None = None) -> bool:
        """Mon-Fri inside the session. Exchange holidays are not modelled."""
        now = now or datetime.now(IST)
        h = self.hours(now.date())
        return now.weekday() < 5 and h["open"] <= now.time() <= h["close"]


def mcx_close(d: date | None = None) -> dtime:
    """MCX energy, bullion and base metals close 23:30 IST while the US is on daylight time, 23:55 in US winter."""
    d = d or date.today()
    us = datetime(d.year, d.month, d.day, 12, tzinfo=ZoneInfo("America/New_York"))
    return dtime(23, 30) if us.dst() else dtime(23, 55)


def _mcx(key, name, yahoo, step, cost, group, unit, proxy_mult=1.0, proxy_name="", quant="", news=None, topic="", mc=True):
    return Instrument(key, name, yahoo, "", step, cost, exchange="MCX", kind="commodity", group=group,
                      fx="INR=X" if yahoo else None, proxy_mult=proxy_mult, proxy_name=proxy_name,
                      quant_market=quant, news_filter=news, news_topic=topic, unit=unit, moneycontrol=mc)


_GOLD = 10 / TROY_OZ_G * (1 + BULLION_DUTY)      # USD/oz -> INR per 10 g, duty paid
_SILVER = 1000 / TROY_OZ_G * (1 + BULLION_DUTY)  # USD/oz -> INR per kg, duty paid
_COPPER = 1 / LB_KG                              # USD/lb -> INR per kg

INSTRUMENTS = {
    "NIFTY": Instrument("NIFTY", "Nifty 50", "^NSEI", "NSE-NIFTY", 50, 4.0, quant_market="NIFTY"),
    "BANKNIFTY": Instrument("BANKNIFTY", "Bank Nifty", "^NSEBANK", "NSE-BANKNIFTY", 100, 10.0, quant_market="BANKNIFTY"),
    # Energy
    "CRUDEOIL": _mcx("CRUDEOIL", "MCX Crude Oil", "CL=F", 50, 6.0, "Energy", (1, "bbl"),
                     proxy_name="NYMEX WTI crude", quant="CRUDE", news=CRUDE_NEWS, topic="crude"),
    "CRUDEOILM": _mcx("CRUDEOILM", "MCX Crude Oil Mini", "CL=F", 50, 16.0, "Energy", (1, "bbl"),
                      proxy_name="NYMEX WTI crude", quant="CRUDE", news=CRUDE_NEWS, topic="crude"),
    "NATURALGAS": _mcx("NATURALGAS", "MCX Natural Gas", "NG=F", 5, 0.5, "Energy", (1, "mmBtu"),
                       proxy_name="NYMEX natural gas", quant="NATGAS", news=GAS_NEWS, topic="natural gas"),
    "NATGASMINI": _mcx("NATGASMINI", "MCX Natural Gas Mini", "NG=F", 5, 1.2, "Energy", (1, "mmBtu"),
                       proxy_name="NYMEX natural gas", quant="NATGAS", news=GAS_NEWS, topic="natural gas"),
    # Bullion
    "GOLD": _mcx("GOLD", "MCX Gold", "GC=F", 500, 60.0, "Bullion", (10, "g"), _GOLD,
                 "COMEX gold", "GOLD", BULLION_NEWS, "bullion"),
    "GOLDM": _mcx("GOLDM", "MCX Gold Mini", "GC=F", 500, 120.0, "Bullion", (10, "g"), _GOLD,
                  "COMEX gold", "GOLD", BULLION_NEWS, "bullion"),
    "SILVER": _mcx("SILVER", "MCX Silver", "SI=F", 1000, 150.0, "Bullion", (1, "kg"), _SILVER,
                   "COMEX silver", "SILVER", BULLION_NEWS, "bullion"),
    "SILVERM": _mcx("SILVERM", "MCX Silver Mini", "SI=F", 1000, 350.0, "Bullion", (1, "kg"), _SILVER,
                    "COMEX silver", "SILVER", BULLION_NEWS, "bullion"),
    # Neither Moneycontrol nor Yahoo carries the Bullion Index: Groww only
    "MCXBULLDEX": _mcx("MCXBULLDEX", "MCX Bullion Index", "", 100, 40.0, "Bullion", (1, "x index"),
                       news=BULLION_NEWS, topic="bullion", mc=False),
    # Base metals
    "COPPER": _mcx("COPPER", "MCX Copper", "HG=F", 10, 1.2, "Base metals", (1, "kg"), _COPPER,
                   "COMEX copper", "COPPER", METAL_NEWS, "metals"),
    # LME zinc has no free Yahoo feed: Moneycontrol (or Groww) only
    "ZINC": _mcx("ZINC", "MCX Zinc", "", 5, 0.8, "Base metals", (1, "kg"), news=METAL_NEWS, topic="metals"),
}
VIX_YAHOO = "^INDIAVIX"
MCX_KEYS = {k for k, i in INSTRUMENTS.items() if i.exchange == "MCX"}


def active() -> dict[str, Instrument]:
    """Instruments with a usable price source (Groww-only ones need API keys)."""
    return {k: i for k, i in INSTRUMENTS.items() if i.available}


def any_open() -> bool:
    return any(i.is_open() for i in INSTRUMENTS.values())
