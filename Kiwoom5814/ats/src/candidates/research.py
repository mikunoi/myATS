"""Validated weekly fundamental and macro research for candidate selection."""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ResearchScore:
    symbol: str
    score: Decimal
    eligible: bool
    reasons: tuple[str, ...]


class WeeklyResearch:
    def __init__(self, document):
        self.document = document if isinstance(document, dict) else {}

    @property
    def as_of(self):
        return str(self.document.get("as_of", ""))

    def score(self, symbol):
        row = self.document.get("candidates", {}).get(symbol)
        if not isinstance(row, dict):
            return ResearchScore(symbol, Decimal(0), False, ("주간 재무·거시 분석 없음",))
        fcf = row.get("fcf_krw", ())
        if not isinstance(fcf, list) or len(fcf) < 3 or any(type(x) not in (int, float) for x in fcf):
            return ResearchScore(symbol, Decimal(0), False, ("FCF 3개년 자료 부족",))
        positive = sum(value > 0 for value in fcf)
        mean = sum(fcf) / len(fcf)
        variability = (sum((value - mean) ** 2 for value in fcf) / len(fcf)) ** 0.5
        stability = Decimal(str(max(0, min(25, 25 * positive / len(fcf)
                                            - (variability / abs(mean) * 8 if mean else 25)))))
        if mean <= 0 or positive < len(fcf) - 1:
            return ResearchScore(symbol, stability, False,
                                 (f"FCF 양수 {positive}/{len(fcf)}년", "FCF 안정성 기준 미달"))
        sector_weights = row.get("sector_weights", {})
        sector_outlooks = row.get("sector_outlooks", {})
        if isinstance(sector_weights, dict) and sector_weights and isinstance(sector_outlooks, dict):
            weights = {str(key): Decimal(str(value)) for key, value in sector_weights.items()}
            if any(value < 0 for value in weights.values()) or sum(weights.values()) <= 0:
                return ResearchScore(symbol, Decimal(0), False, ("섹터 비중 오류",))
            total = sum(weights.values())
            sector = sum((weight / total) * Decimal(str(sector_outlooks.get(name, 0)))
                         for name, weight in weights.items())
            sector_reason = "섹터 비중가중 전망 " + ", ".join(
                f"{name} {weight / total:.0%}" for name, weight in
                sorted(weights.items(), key=lambda item: (-item[1], item[0])))
        else:
            sector = Decimal(str(row.get("sector_outlook", 0)))
            sector_reason = f"섹터 거시전망 {sector:+.1f}/10"
        rate = Decimal(str(row.get("rate_cycle_fit", 0)))
        financial = Decimal(str(row.get("financial_quality", 0)))
        disclosure = Decimal(str(row.get("disclosure_score", 0)))
        if (not Decimal(-10) <= sector <= Decimal(10)
                or not Decimal(-5) <= rate <= Decimal(5)
                or not Decimal(-10) <= financial <= Decimal(10)
                or not Decimal(-5) <= disclosure <= Decimal(5)):
            return ResearchScore(symbol, Decimal(0), False, ("거시 점수 범위 오류",))
        reasons = (f"FCF 양수 {positive}/{len(fcf)}년·안정성 {stability:.1f}/25",
                   f"재무건전성 {financial:+.1f}/10", f"공시평가 {disclosure:+.1f}/5",
                   sector_reason, f"금리국면 적합도 {rate:+.1f}/5")
        return ResearchScore(symbol, stability + financial + disclosure + sector + rate,
                             True, reasons)

    def order(self, symbols):
        scored = [self.score(symbol) for symbol in dict.fromkeys(symbols)]
        eligible = [item for item in scored if item.eligible]
        return tuple(item.symbol for item in sorted(eligible,
                     key=lambda item: (-item.score, item.symbol))), tuple(scored)
