from dataclasses import dataclass


@dataclass(frozen=True)
class WatchlistGroup:
    code: str
    name: str


@dataclass(frozen=True)
class WatchlistItem:
    symbol: str
    bookmark: str = ""
    bookmark_color: str = ""
