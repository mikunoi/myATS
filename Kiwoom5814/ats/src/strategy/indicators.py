"""Pure Ichimoku and Wilder ATR calculations in chronological order."""
from dataclasses import dataclass
from decimal import Decimal

from src.market_data.models import Candle


@dataclass(frozen=True)
class IndicatorPoint:
    date: str
    close: Decimal
    tenkan: Decimal | None
    kijun: Decimal | None
    senkou_a: Decimal | None
    senkou_b: Decimal | None
    atr: Decimal | None
    chikou_valid: bool | None

    @property
    def bullish(self):
        return self.tenkan is not None and self.kijun is not None and self.tenkan > self.kijun


def _decimal(value):
    return Decimal(str(value))


def _midpoint(candles, start, end):
    window = candles[start:end]
    return (_decimal(max(c.high for c in window)) + _decimal(min(c.low for c in window))) / 2


def indicator_series(candles: tuple[Candle, ...] | list[Candle], periods=(9, 26, 52), atr_period=14):
    """Return oldest-to-newest points; input may arrive in either date order."""
    if len(periods) != 3 or any(type(p) is not int or p <= 0 for p in periods):
        raise ValueError("Invalid Ichimoku periods")
    if type(atr_period) is not int or atr_period <= 0:
        raise ValueError("Invalid ATR period")
    ordered = sorted(candles, key=lambda c: c.date)
    if len({c.date for c in ordered}) != len(ordered):
        raise ValueError("Duplicate candle date")
    for candle in ordered:
        if candle.high < max(candle.open, candle.close, candle.low) or candle.low > min(
                candle.open, candle.close, candle.high):
            raise ValueError("Inconsistent OHLC candle")

    conversion, base, span = periods
    true_ranges = []
    atrs = [None] * len(ordered)
    for index, candle in enumerate(ordered):
        high, low = _decimal(candle.high), _decimal(candle.low)
        if index == 0:
            true_range = high - low
        else:
            previous_close = _decimal(ordered[index - 1].close)
            true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(true_range)
        if index == atr_period - 1:
            atrs[index] = sum(true_ranges[:atr_period], Decimal(0)) / atr_period
        elif index >= atr_period:
            atrs[index] = (atrs[index - 1] * (atr_period - 1) + true_range) / atr_period

    result = []
    for index, candle in enumerate(ordered):
        tenkan = _midpoint(ordered, index + 1 - conversion, index + 1) if index + 1 >= conversion else None
        kijun = _midpoint(ordered, index + 1 - base, index + 1) if index + 1 >= base else None
        senkou_a = (tenkan + kijun) / 2 if tenkan is not None and kijun is not None else None
        senkou_b = _midpoint(ordered, index + 1 - span, index + 1) if index + 1 >= span else None
        chikou_valid = (_decimal(candle.close) >= _decimal(ordered[index - base].close)
                        if index >= base else None)
        result.append(IndicatorPoint(candle.date, _decimal(candle.close), tenkan, kijun,
                                     senkou_a, senkou_b, atrs[index], chikou_valid))
    return tuple(result)
