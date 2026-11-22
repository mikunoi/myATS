"""Broker-independent strategy rules from PROJECT_SPEC.md."""
from dataclasses import dataclass
from decimal import Decimal

from src.strategy.indicators import IndicatorPoint


SELL_RULE_1 = "SELL_RULE_1_BEARISH_CROSS"
SELL_RULE_2 = "SELL_RULE_2_LATEST_ENTRY_3ATR"
SELL_RULE_3 = "SELL_RULE_3_KIJUN_CURRENT_ATR"
SELL_MISSED_DAILY_CROSS = "SELL_MISSED_DAILY_CROSS_BELOW_KIJUN"
SELL_WEEKLY_RULE_2 = "SELL_WEEKLY_RULE_2_LATEST_ENTRY_2ATR"


def _ready(previous: IndicatorPoint, current: IndicatorPoint):
    return all(value is not None for value in
               (previous.tenkan, previous.kijun, current.tenkan, current.kijun))


def crossed_above(previous: IndicatorPoint, current: IndicatorPoint):
    return _ready(previous, current) and previous.tenkan <= previous.kijun and current.tenkan > current.kijun


def crossed_below(previous: IndicatorPoint, current: IndicatorPoint):
    return _ready(previous, current) and previous.tenkan >= previous.kijun and current.tenkan < current.kijun


def missed_daily_cross_exit(previous, current, current_price):
    """A surviving daily holding remains bearish after the crossover day."""
    return (_ready(previous, current) and previous.tenkan < previous.kijun
            and current.tenkan < current.kijun and current_price < current.kijun)


def entry_common(current: IndicatorPoint):
    """Approved entry state; equality between Tenkan and Kijun is eligible."""
    return (current.tenkan is not None and current.kijun is not None
            and current.tenkan >= current.kijun and current.chikou_valid is True)


def daily_chikou_tenkan_entry(daily: tuple[IndicatorPoint, ...] | list[IndicatorPoint]):
    """Require the latest daily close (Chikou value) to reach Tenkan 26 sessions ago."""
    if len(daily) < 27:
        return False
    prior_tenkan = daily[-27].tenkan
    return prior_tenkan is not None and daily[-1].close >= prior_tenkan


def initial_entry(previous: IndicatorPoint, current: IndicatorPoint):
    # A symbol may be added to the broker watchlist after its cross.  Eligibility
    # is therefore based on the current state at the scheduled evaluation.
    return entry_common(current)


def cycle_reentry(previous: IndicatorPoint, current: IndicatorPoint):
    """At evaluation, a completed cycle uses the current daily entry state."""
    return entry_common(current)


def addon_trigger(latest_entry_price: Decimal, latest_entry_atr: Decimal, multiple=Decimal("1.0")):
    if latest_entry_atr <= 0 or multiple <= 0:
        raise ValueError("ATR and add-on multiple must be positive")
    return latest_entry_price + multiple * latest_entry_atr


def addon_entry(current_price: Decimal, latest_entry_price: Decimal, latest_entry_atr: Decimal,
                addon_count: int, addons_today: int, *, cycle_limit=4, daily_limit=2,
                multiple=Decimal("1.0")):
    if addon_count >= cycle_limit or addons_today >= daily_limit:
        return False
    return current_price >= addon_trigger(latest_entry_price, latest_entry_atr, multiple)


def sell_signals(previous: IndicatorPoint, current: IndicatorPoint, current_price: Decimal,
                 latest_entry_price: Decimal, latest_entry_atr: Decimal):
    """Return every satisfied exit; the caller records all reasons and exits once."""
    if current.atr is None or current.kijun is None or latest_entry_atr <= 0:
        return ()
    reasons = []
    if crossed_below(previous, current):
        reasons.append(SELL_RULE_1)
    if current_price < latest_entry_price - Decimal(3) * latest_entry_atr:
        reasons.append(SELL_RULE_2)
    if current_price < current.kijun - current.atr:
        reasons.append(SELL_RULE_3)
    return tuple(reasons)


def weekly_sell_signals(previous: IndicatorPoint, current: IndicatorPoint,
                        current_price: Decimal, latest_entry_price: Decimal,
                        latest_entry_atr: Decimal):
    """Weekly positions exit only strictly below the current weekly Kijun."""
    if current.kijun is None:
        return ()
    return ("SELL_WEEKLY_KIJUN",) if current_price < current.kijun else ()



def rule3_reentry(previous: IndicatorPoint, current: IndicatorPoint,
                  previous_price: Decimal, current_price: Decimal):
    # After a daily Kijun-1ATR exit, the current approved state and a price at
    # or above Kijun are sufficient; a new crossover is not required.
    return (entry_common(current) and current.kijun is not None
            and current_price >= current.kijun)


def weekly_rule3_reentry(previous: IndicatorPoint, current: IndicatorPoint,
                         previous_price: Decimal, current_price: Decimal):
    """After a weekly Kijun-1ATR exit, require an actual daily Kijun recovery."""
    if previous.kijun is None or current.kijun is None:
        return False
    return (entry_common(current)
            and previous_price < previous.kijun and current_price >= current.kijun)


def cycle_mode(daily_entry_signal: bool, weekly_current: IndicatorPoint, *, rule3_reentry=False):
    if rule3_reentry or not daily_entry_signal:
        return "DAILY"
    return "WEEKLY" if weekly_current.bullish else "DAILY"


def reentry_mode(next_cycle: str, weekly_entry_signal: bool):
    """Use weekly when it is next and its current bullish/chikou state is valid."""
    if next_cycle not in {"DAILY", "WEEKLY"}:
        raise ValueError("Invalid next cycle")
    return "WEEKLY" if next_cycle == "WEEKLY" and weekly_entry_signal else "DAILY"
