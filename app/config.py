"""Central settings, loaded from .env (see .env.example)."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
load_dotenv(ROOT / ".env")

IST = ZoneInfo("Asia/Kolkata")


def _f(name: str, default: float) -> float:
    v = os.getenv(name, "")
    return float(v) if v.strip() else default


def _s(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


@dataclass
class Settings:
    # Groww is optional: used for faster/official quotes, candles and option chain when set
    groww_totp_token: str = field(default_factory=lambda: _s("GROWW_TOTP_TOKEN"))
    groww_totp_secret: str = field(default_factory=lambda: _s("GROWW_TOTP_SECRET"))
    groww_api_key: str = field(default_factory=lambda: _s("GROWW_API_KEY"))
    groww_api_secret: str = field(default_factory=lambda: _s("GROWW_API_SECRET"))

    anthropic_key: str = field(default_factory=lambda: _s("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _s("CLAUDE_MODEL", "claude-opus-5-5"))

    # Live analysis loop
    tick_seconds: float = field(default_factory=lambda: _f("TICK_SECONDS", 1))
    refresh_seconds: int = field(default_factory=lambda: int(_f("REFRESH_SECONDS", 10)))
    chain_refresh_seconds: int = field(default_factory=lambda: int(_f("CHAIN_REFRESH_SECONDS", 60)))
    # MCX without Groww: real prices from Moneycontrol's public API (unofficial: keep polling modest)
    mcx_tick_seconds: float = field(default_factory=lambda: _f("MCX_TICK_SECONDS", 3))
    mcx_chain_refresh_seconds: int = field(default_factory=lambda: int(_f("MCX_CHAIN_REFRESH_SECONDS", 180)))
    mcx_chain_width: int = field(default_factory=lambda: int(_f("MCX_CHAIN_WIDTH", 6)))

    # Hourly prediction log in docs/data, pushed to GitHub (Pages) when enabled
    publish_git: bool = field(default_factory=lambda: _s("PUBLISH_GIT", "0") == "1")
    publish_remote: str = field(default_factory=lambda: _s("PUBLISH_REMOTE", "origin"))
    pages_every_minutes: int = field(default_factory=lambda: int(_f("PAGES_EVERY_MINUTES", 15)))

    # Signal rules
    signal_threshold: float = field(default_factory=lambda: _f("SIGNAL_THRESHOLD", 45))
    min_adx: float = field(default_factory=lambda: _f("MIN_ADX", 18))
    no_signal_before: str = field(default_factory=lambda: _s("NO_SIGNAL_BEFORE", "09:30"))
    no_signal_after: str = field(default_factory=lambda: _s("NO_SIGNAL_AFTER", "14:45"))

    @property
    def has_groww_creds(self) -> bool:
        return bool((self.groww_totp_token and self.groww_totp_secret)
                    or (self.groww_api_key and self.groww_api_secret))


settings = Settings()
