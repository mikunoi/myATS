from dataclasses import dataclass


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    current_price: int
    volume: int


@dataclass(frozen=True)
class OrderBookLevel:
    level: int
    price: int
    quantity: int


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    timestamp: str
    asks: tuple[OrderBookLevel, ...]
    bids: tuple[OrderBookLevel, ...]


@dataclass(frozen=True)
class Candle:
    date: str
    open: int
    high: int
    low: int
    close: int
    volume: int
