from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from src.config import Settings
from src.strategy.indicators import indicator_series
from src.strategy.signals import (addon_entry, cycle_reentry, entry_common,
                                  initial_entry, rule3_reentry, sell_signals,
                                  weekly_rule3_reentry, weekly_sell_signals)


@dataclass(frozen=True)
class StrategyState:
    symbol: str
    mode: str = "DAILY"
    initial_quantity: int = 0
    latest_entry_price: Decimal | None = None
    latest_entry_atr: Decimal | None = None
    addon_count: int = 0
    sell_reason: str | None = None


class StrategyEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def initial_quantity(self, investment_capital: Decimal, atr: Decimal) -> int:
        if not investment_capital.is_finite() or investment_capital < 0:
            raise ValueError("Invalid investment capital")
        if not atr.is_finite() or atr <= 0:
            raise ValueError("ATR must be positive")
        risk = Decimal(str(self.settings.risk_rate))
        divisor = Decimal(str(self.settings.entry_stop_atr_multiple)) * atr
        return int((investment_capital * risk / divisor).to_integral_value(rounding=ROUND_FLOOR))

    def initial_quantity_for_mode(self, investment_capital: Decimal, atr: Decimal, mode: str) -> int:
        if mode not in {"DAILY", "WEEKLY"}:
            raise ValueError("Invalid strategy mode")
        if mode == "DAILY":
            return self.initial_quantity(investment_capital, atr)
        if not investment_capital.is_finite() or investment_capital < 0:
            raise ValueError("Invalid investment capital")
        if not atr.is_finite() or atr <= 0:
            raise ValueError("ATR must be positive")
        risk = Decimal(str(self.settings.risk_rate))
        divisor = Decimal(str(self.settings.weekly_entry_stop_atr_multiple)) * atr
        return int((investment_capital * risk / divisor).to_integral_value(rounding=ROUND_FLOOR))

    def indicators(self, candles, mode="DAILY"):
        if mode not in {"DAILY", "WEEKLY"}:
            raise ValueError("Invalid strategy mode")
        periods = self.settings.daily_ichimoku if mode == "DAILY" else self.settings.weekly_ichimoku
        return indicator_series(candles, periods, self.settings.atr_period)

    def initial_entry(self, previous, current):
        return initial_entry(previous, current)

    def entry_common(self, current):
        return entry_common(current)

    def cycle_reentry(self, previous, current):
        return cycle_reentry(previous, current)

    def addon_entry(self, current_price, state, addons_today):
        if state.latest_entry_price is None or state.latest_entry_atr is None:
            return False
        return addon_entry(current_price, state.latest_entry_price, state.latest_entry_atr,
                           state.addon_count, addons_today,
                           cycle_limit=self.settings.max_addons_cycle,
                           daily_limit=self.settings.max_addons_day,
                           multiple=Decimal(str(self.settings.addon_atr_multiple)))

    def sell_signals(self, previous, current, current_price, state):
        if state.mode == "WEEKLY":
            return weekly_sell_signals(previous, current, current_price,
                                       state.latest_entry_price, state.latest_entry_atr)
        if state.latest_entry_price is None or state.latest_entry_atr is None:
            return ()
        return sell_signals(previous, current, current_price,
                            state.latest_entry_price, state.latest_entry_atr)

    def rule3_reentry(self, previous, current, previous_price, current_price):
        return rule3_reentry(previous, current, previous_price, current_price)

    def weekly_rule3_reentry(self, previous, current, previous_price, current_price):
        return weekly_rule3_reentry(previous, current, previous_price, current_price)
