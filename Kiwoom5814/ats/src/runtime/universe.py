"""Build the entry and managed universes for a live account."""


def trading_universe(watchlist, holdings, strategy_symbols=(), excluded_symbols=(), maximum=10):
    excluded = frozenset(excluded_symbols)
    entry = tuple(dict.fromkeys(symbol for symbol in watchlist if symbol not in excluded))
    if maximum is not None and len(entry) > maximum:
        raise RuntimeError(f"Filtered watchlist has {len(entry)} symbols; maximum is {maximum}")
    held = {holding.symbol for holding in holdings if holding.quantity > 0}
    retained = tuple(symbol for symbol in strategy_symbols
                     if symbol in held and symbol not in excluded and symbol not in entry)
    return entry, entry + retained
