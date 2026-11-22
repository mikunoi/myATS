"""Explainable candidate priority; ranking never creates a trade signal."""
from dataclasses import dataclass
from decimal import Decimal
from math import sqrt


@dataclass(frozen=True)
class RankedCandidate:
    symbol: str
    score: Decimal
    reasons: tuple[str, ...]


class CandidateRanker:
    @staticmethod
    def _returns(candles, length=20):
        closes = [Decimal(c.close) for c in candles[-(length + 1):]]
        return [float(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))
                if closes[i - 1] > 0]

    @staticmethod
    def _correlation(left, right):
        size = min(len(left), len(right))
        if size < 10:
            return None
        x, y = left[-size:], right[-size:]
        mx, my = sum(x) / size, sum(y) / size
        numerator = sum((a - mx) * (b - my) for a, b in zip(x, y))
        dx = sum((a - mx) ** 2 for a in x); dy = sum((b - my) ** 2 for b in y)
        return numerator / sqrt(dx * dy) if dx > 0 and dy > 0 else None

    def rank(self, symbols, daily, weekly, quotes, volumes=None, user_symbols=(),
             candles=None, books=None, held_symbols=(), industry_map=None):
        volumes = volumes or {}
        user_symbols = set(user_symbols)
        candles, books = candles or {}, books or {}
        held_symbols, industry_map = set(held_symbols), industry_map or {}
        returns = {s: self._returns(candles[s]) for s in symbols if s in candles}
        group_returns = {}
        for s, group in industry_map.items():
            if s in returns and returns[s]:
                group_returns.setdefault(group, []).append(sum(returns[s]))
        ranked = []
        for symbol in symbols:
            d, w = daily[symbol][-1], weekly[symbol][-1]
            raw_quote = quotes[symbol]
            quote = Decimal(str(getattr(raw_quote, "current_price", raw_quote)))
            score, reasons = Decimal(0), []
            if d.atr and d.atr > 0 and d.kijun is not None:
                strength = max(Decimal(-3), min(Decimal(3), (quote - d.kijun) / d.atr))
                score += strength * Decimal(20)
                reasons.append(f"일봉 추세강도 {strength:.2f}ATR")
            if d.bullish:
                score += 20; reasons.append("일봉 전환선 우위")
            if w.bullish:
                score += 15; reasons.append("주봉 전환선 우위")
            if d.chikou_valid is True:
                score += 10; reasons.append("후행스팬 조건 유효")
            if symbol in user_symbols:
                score += 3; reasons.append("사용자 감시종목 가산점")
            volume = max(0, int(volumes.get(symbol, getattr(raw_quote, "volume", 0))))
            liquidity = Decimal(min(15, len(str(max(1, int(quote) * volume)))))
            score += liquidity; reasons.append(f"거래대금 단계 {int(liquidity)}")
            series = candles.get(symbol, ())
            if len(series) >= 20:
                values = [Decimal(c.close) * Decimal(c.volume) for c in series[-20:]]
                recent = sum(values[-5:], Decimal(0)) / 5
                prior = sum(values[:-5], Decimal(0)) / 15
                ratio = recent / prior if prior > 0 else Decimal(1)
                persistence = max(Decimal(-8), min(Decimal(8), (ratio - 1) * 10))
                score += persistence
                reasons.append(f"거래대금 지속성 {ratio:.2f}배")
            book = books.get(symbol)
            if book and book.asks and book.bids and quote > 0:
                spread_bps = (Decimal(book.asks[0].price) - Decimal(book.bids[0].price)) / quote * 10000
                book_score = max(Decimal(-8), min(Decimal(8), Decimal(6) - spread_bps / 2))
                score += book_score; reasons.append(f"호가 스프레드 {spread_bps:.1f}bp")
            peer_corr = [self._correlation(returns.get(symbol, []), returns.get(held, []))
                         for held in held_symbols if held != symbol]
            peer_corr = [value for value in peer_corr if value is not None]
            if peer_corr:
                maximum = max(peer_corr)
                penalty = Decimal(str(max(0, maximum - 0.6))) * 20
                score -= penalty; reasons.append(f"보유종목 최대상관 {maximum:.2f}")
            group = industry_map.get(symbol)
            if group and len(group_returns.get(group, ())) >= 2 and returns.get(symbol):
                own = Decimal(str(sum(returns[symbol])))
                peer = Decimal(str(sum(group_returns[group]) / len(group_returns[group])))
                relative = max(Decimal(-8), min(Decimal(8), (own - peer) * 100))
                score += relative; reasons.append(f"산업 상대강도 {relative:+.2f}")
            ranked.append(RankedCandidate(symbol, score, tuple(reasons)))
        return tuple(sorted(ranked, key=lambda item: (-item.score, item.symbol)))
