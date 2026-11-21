"""
config/settings.py
==================
Application Settings Loader for mcx-comm.

Architecture:
  - Secrets & Credentials (API key, token, webhook URL) → .env
  - All strategy, indicator, timing and trade parameters   → config/settings.yml
    (SINGLE SOURCE OF TRUTH)

The Kite access token is shared from nifty-strike-engine's .env file, read
at startup via NSE_ENV_PATH. If KITE_ACCESS_TOKEN is set directly in this
project's .env, it takes precedence.
"""

from __future__ import annotations

import os
import re
import yaml
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SETTINGS_YML_PATH = os.path.join(os.path.dirname(__file__), "settings.yml")


def _load_yaml_settings(yaml_path: str = SETTINGS_YML_PATH) -> dict:
    """Parse settings.yml into a flat dictionary for Pydantic."""
    if not os.path.exists(yaml_path):
        raise FileNotFoundError(
            f"Configuration file not found: {yaml_path}. "
            "config/settings.yml is required as the single source of truth."
        )
    with open(yaml_path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}

    parsed: dict = {}

    strat = doc.get("strategy", {})
    parsed["strategy_name"]      = str(strat.get("name", "GoldTrendFilter")).strip()
    parsed["instrument_prefix"]  = str(strat.get("instrument_prefix", "MCX:GOLDPETAL")).strip()

    trade = doc.get("trade", {})
    parsed["capital"]            = float(trade.get("capital", 200000.0))
    parsed["base_risk_pct"]      = float(trade.get("base_risk_pct", 2.0))
    parsed["lot_multiplier"]     = int(trade.get("lot_multiplier", 1))
    parsed["trade_mode"]         = str(trade.get("mode", "Both")).strip()

    ind = doc.get("indicators", {})
    parsed["enter_channel"]      = int(ind.get("enter_channel", 20))
    parsed["exit_channel"]       = int(ind.get("exit_channel", 10))
    parsed["ema_length"]         = int(ind.get("ema_length", 200))
    parsed["atr_period"]         = int(ind.get("atr_period", 20))
    parsed["atr_tp_multiplier"]  = float(ind.get("atr_tp_multiplier", 4.0))
    parsed["atr_trail_multiplier"] = float(ind.get("atr_trail_multiplier", 2.0))
    parsed["enable_partial_tp"]  = bool(ind.get("enable_partial_tp", False))
    parsed["min_long_days"]      = int(ind.get("min_long_days", 0))

    skip = doc.get("skip_opening_candles", {})
    parsed["skip_opening_candles"] = bool(skip.get("enabled", True))
    parsed["bars_to_skip"]         = int(skip.get("bars_to_skip", 2))

    timing = doc.get("timing", {})
    parsed["candle_interval_minutes"] = int(timing.get("candle_interval_minutes", 60))
    parsed["market_open"]         = str(timing.get("market_open", "09:00")).strip()
    parsed["market_close"]        = str(timing.get("market_close", "23:30")).strip()
    parsed["entry_cutoff"]        = str(timing.get("entry_cutoff", "22:00")).strip()
    parsed["eod_exit"]            = str(timing.get("eod_exit", "23:25")).strip()
    parsed["enforce_market_hours"] = bool(timing.get("enforce_market_hours", True))
    parsed["warmup_candles"]      = int(timing.get("warmup_candles", 150))

    logging_elem = doc.get("logging", {})
    parsed["log_level"] = str(logging_elem.get("level", "INFO")).strip().upper()

    return parsed


def _read_token_from_nse_env(nse_env_path: str) -> str | None:
    """
    Read KITE_ACCESS_TOKEN from the nifty-strike-engine .env file.
    Returns the token string, or None if the file / key is not found.
    """
    if not nse_env_path or not os.path.exists(nse_env_path):
        return None
    try:
        with open(nse_env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("KITE_ACCESS_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if token:
                        return token
    except Exception:
        pass
    return None


class Settings(BaseSettings):
    """
    Application settings.
    Secrets from .env, strategy parameters from config/settings.yml.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Secrets (.env) -------------------------------------------
    kite_api_key: str
    kite_api_secret: str = ""
    kite_access_token: str = ""    # resolved below — from NSE .env if not set here
    discord_webhook_url: str
    nse_env_path: str = "D:/Workspace/nifty-strike-engine/.env"
    live_order_placement: bool = False

    # ---- Strategy (settings.yml) -----------------------------------
    strategy_name: str = "GoldTrendFilter"
    instrument_prefix: str = "MCX:GOLDPETAL"
    capital: float = 200000.0
    base_risk_pct: float = 2.0
    lot_multiplier: int = 1
    trade_mode: str = "Both"

    # Indicators
    enter_channel: int = 20
    exit_channel: int = 10
    ema_length: int = 200
    atr_period: int = 20
    atr_tp_multiplier: float = 4.0
    atr_trail_multiplier: float = 2.0
    enable_partial_tp: bool = False
    min_long_days: int = 0

    # Skip opening candles
    skip_opening_candles: bool = True
    bars_to_skip: int = 2

    # Timing
    candle_interval_minutes: int = 60
    market_open: str = "09:00"
    market_close: str = "23:30"
    entry_cutoff: str = "22:00"
    eod_exit: str = "23:25"
    enforce_market_hours: bool = True
    warmup_candles: int = 150

    # Logging
    log_level: str = "INFO"

    @classmethod
    def load(cls, yaml_path: str = SETTINGS_YML_PATH) -> "Settings":
        """
        Load settings: merge .env credentials with config/settings.yml parameters.
        Resolves access token from nifty-strike-engine .env if not set locally.
        """
        yaml_settings = _load_yaml_settings(yaml_path)
        instance = cls(**yaml_settings)

        # Resolve access token: local .env wins; fall back to nifty-strike-engine .env
        if not instance.kite_access_token and instance.nse_env_path:
            token = _read_token_from_nse_env(instance.nse_env_path)
            if token:
                # Rebuild with token resolved
                instance = cls(**{**yaml_settings, "kite_access_token": token})

        return instance

    @field_validator("kite_api_key")
    @classmethod
    def api_key_must_not_be_placeholder(cls, v: str) -> str:
        if v.strip() in {"your_api_key_here", ""}:
            raise ValueError(
                "KITE_API_KEY has a placeholder/empty value. Set it in .env"
            )
        return v.strip()

    @field_validator("discord_webhook_url")
    @classmethod
    def webhook_must_not_be_placeholder(cls, v: str) -> str:
        if "YOUR_WEBHOOK_ID" in v or not v.strip():
            raise ValueError(
                "DISCORD_WEBHOOK_URL has a placeholder/empty value. Set it in .env"
            )
        return v.strip()

    @model_validator(mode="after")
    def validate_time_formats(self) -> "Settings":
        pattern = re.compile(r"^\d{2}:\d{2}$")
        for field in ("market_open", "market_close", "entry_cutoff", "eod_exit"):
            val = getattr(self, field)
            if not pattern.match(val):
                raise ValueError(f"{field} must be HH:MM format, got '{val}'")
        return self

    @model_validator(mode="after")
    def resolve_access_token(self) -> "Settings":
        """Warn loudly if no access token was resolved."""
        if not self.kite_access_token:
            import warnings
            warnings.warn(
                "KITE_ACCESS_TOKEN is not set. Set it in .env or ensure "
                f"nifty-strike-engine .env exists at {self.nse_env_path}",
                stacklevel=2,
            )
        return self

    # ---- Convenience properties ------------------------------------
    @property
    def market_open_hhmm(self) -> tuple[int, int]:
        h, m = self.market_open.split(":")
        return int(h), int(m)

    @property
    def market_close_hhmm(self) -> tuple[int, int]:
        h, m = self.market_close.split(":")
        return int(h), int(m)

    @property
    def entry_cutoff_hhmm(self) -> tuple[int, int]:
        h, m = self.entry_cutoff.split(":")
        return int(h), int(m)

    @property
    def eod_exit_hhmm(self) -> tuple[int, int]:
        h, m = self.eod_exit.split(":")
        return int(h), int(m)

    @property
    def allow_long(self) -> bool:
        return self.trade_mode in ("Long", "Both")

    @property
    def allow_short(self) -> bool:
        return self.trade_mode in ("Short", "Both")

    @property
    def min_long_bars(self) -> int:
        """Convert minimum long duration in days to 1H bars."""
        bars_per_day = 24  # MCX trades ~15 hours but pine uses 24 bars/day for filtering
        return self.min_long_days * bars_per_day


def get_settings(yaml_path: str = SETTINGS_YML_PATH) -> Settings:
    return Settings.load(yaml_path=yaml_path)
