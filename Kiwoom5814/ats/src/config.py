import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    broker_mode: str = "paper"
    kiwoom_environment: str = "production"
    signal_chart_market: str = "KRX"
    database_path: str = "data/stock_trader_next.sqlite3"
    log_path: str = "logs/stock_trader_next.log"
    risk_rate: float = 0.005
    capital_usage_limit: float = 0.98
    max_symbol_capital_fraction: float | None = 0.20
    mock_capital_limit_krw: int | None = None
    atr_period: int = 14
    daily_ichimoku: tuple = (9, 26, 52)
    weekly_ichimoku: tuple = (5, 26, 52)
    max_watchlist: int = 10
    max_candidates_per_sector: int = 2
    weekly_analysis_weekday: int = 6
    weekly_analysis_hour: int = 6
    watchlist_sync_hour: int = 14
    watchlist_sync_minute: int = 0
    watchlist_group_name: str = "모니터링종목"
    excluded_symbols: tuple = ()
    max_positions: int = 10
    max_addons_cycle: int = 4
    max_addons_day: int = 2
    addon_atr_multiple: float = 1.0
    entry_stop_atr_multiple: float = 3.0
    weekly_entry_stop_atr_multiple: float = 2.0
    kijun_stop_atr_multiple: float = 1.0
    evaluation_minutes_before_close: int = 60
    order_trade_type: str = "6"
    realtime_stale_after_seconds: float = 5.0
    realtime_login_timeout_seconds: float = 10.0
    realtime_reconnect_base_seconds: float = 1.0
    realtime_reconnect_cap_seconds: float = 30.0

    def __post_init__(self):
        if self.broker_mode != "paper":
            raise ValueError("Live trading is disabled in phase 1")
        if self.kiwoom_environment not in {"mock", "production"}:
            raise ValueError("Invalid Kiwoom environment")
        if self.signal_chart_market != "KRX":
            raise ValueError("Trading signals must use KRX regular-session charts")
        for name in ("risk_rate", "capital_usage_limit"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"Invalid {name}")
        if (self.max_symbol_capital_fraction is not None
                and not 0 < self.max_symbol_capital_fraction <= 1):
            raise ValueError("Invalid max_symbol_capital_fraction")
        if (self.mock_capital_limit_krw is not None
                and (type(self.mock_capital_limit_krw) is not int
                     or self.mock_capital_limit_krw <= 0)):
            raise ValueError("Invalid mock_capital_limit_krw")
        for name in ("atr_period", "max_watchlist", "max_candidates_per_sector", "max_positions",
                     "max_addons_cycle", "max_addons_day", "evaluation_minutes_before_close"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"Invalid {name}")
        for name in ("addon_atr_multiple", "entry_stop_atr_multiple",
                     "weekly_entry_stop_atr_multiple", "kijun_stop_atr_multiple"):
            import math
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid {name}")
        for periods in (self.daily_ichimoku, self.weekly_ichimoku):
            if len(periods) != 3 or any(type(p) is not int or p <= 0 for p in periods):
                raise ValueError("Invalid Ichimoku periods")
        if self.order_trade_type != "6":
            raise ValueError("Only specified order type 6 is supported")
        import math
        for name in ("realtime_stale_after_seconds", "realtime_login_timeout_seconds",
                     "realtime_reconnect_base_seconds", "realtime_reconnect_cap_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid {name}")
        if self.realtime_reconnect_base_seconds > self.realtime_reconnect_cap_seconds:
            raise ValueError("Realtime reconnect base exceeds cap")
        if not self.watchlist_group_name.strip():
            raise ValueError("Watchlist group name is required")
        if (not isinstance(self.excluded_symbols, (list, tuple))
                or any(not isinstance(symbol, str) or not symbol.strip()
                       for symbol in self.excluded_symbols)
                or len(set(self.excluded_symbols)) != len(self.excluded_symbols)):
            raise ValueError("Invalid excluded_symbols")
        if self.weekly_analysis_weekday not in range(7):
            raise ValueError("Invalid weekly_analysis_weekday")
        if self.weekly_analysis_hour not in range(24):
            raise ValueError("Invalid weekly_analysis_hour")
        if self.watchlist_sync_hour not in range(24) or self.watchlist_sync_minute not in range(60):
            raise ValueError("Invalid watchlist sync time")


@dataclass(frozen=True)
class Secrets:
    kiwoom_expected_account: str = field(default="", repr=False)
    kiwoom_app_key: str = field(default="", repr=False)
    kiwoom_secret_key: str = field(default="", repr=False)
    kiwoom_prod_app_key: str = field(default="", repr=False)
    kiwoom_prod_secret_key: str = field(default="", repr=False)
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    telegram_user_id: str = field(default="", repr=False)
    opendart_api_key: str = field(default="", repr=False)
    notion_token: str = field(default="", repr=False)


def load_settings(root: Path) -> Settings:
    return Settings(**json.loads((root / "config/default.json").read_text(encoding="utf-8")))


def load_secrets(root: Path) -> Secrets:
    values = {}
    path = root / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep:
                raise ValueError("Invalid .env line (expected KEY=VALUE)")
            values[key.strip()] = value.strip().strip("\"'")
    return Secrets(**{name: values.get(name.upper(), "")
                      for name in Secrets.__dataclass_fields__})


def kiwoom_secrets(secrets: Secrets, environment: str) -> Secrets:
    if environment == "mock":
        return secrets
    if environment == "production":
        return Secrets(kiwoom_app_key=secrets.kiwoom_prod_app_key,
                       kiwoom_secret_key=secrets.kiwoom_prod_secret_key)
    raise ValueError("Invalid Kiwoom environment")
