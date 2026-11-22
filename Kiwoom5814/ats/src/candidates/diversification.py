"""Sector exposure cap shared by stocks and ETFs."""
from dataclasses import dataclass


@dataclass(frozen=True)
class DiversificationResult:
    selected: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]


class SectorDiversifier:
    def __init__(self, metadata, maximum_per_sector=2):
        if type(maximum_per_sector) is not int or maximum_per_sector <= 0:
            raise ValueError("maximum_per_sector must be a positive integer")
        self.metadata = metadata or {}
        self.maximum = maximum_per_sector

    def sectors(self, symbol):
        row = self.metadata.get(symbol)
        if not isinstance(row, dict):
            return ()
        direct = row.get("sectors", ())
        sectors = {str(value).strip() for value in direct if str(value).strip()}
        weights = row.get("sector_weights", {})
        if isinstance(weights, dict) and weights:
            parsed = {str(name).strip(): float(weight) for name, weight in weights.items()
                      if str(name).strip() and isinstance(weight, (int, float)) and weight >= 0}
            if parsed:
                primary = max(parsed, key=parsed.get)
                sectors.add(primary)
                sectors.update(name for name, weight in parsed.items() if weight >= 0.20)
        emerging = row.get("emerging_sectors", ())
        for item in emerging if isinstance(emerging, list) else ():
            if (isinstance(item, dict) and float(item.get("confidence", 0)) >= 0.7
                    and str(item.get("sector", "")).strip()):
                sectors.add(str(item["sector"]).strip())
        holdings = row.get("top_holdings", ())[:5]
        holding_counts = {}
        for holding in holdings:
            if isinstance(holding, dict):
                sector = str(holding.get("sector", "")).strip()
                if sector:
                    holding_counts[sector] = holding_counts.get(sector, 0) + 1
        # A sector represented by at least two of the top five holdings is a
        # material ETF exposure. The ETF's explicit theme always remains tagged.
        sectors.update(sector for sector, count in holding_counts.items() if count >= 2)
        return tuple(sorted(sectors))

    def select(self, symbols, maximum):
        counts, selected, excluded = {}, [], []
        for symbol in dict.fromkeys(symbols):
            sectors = self.sectors(symbol)
            if not sectors:
                excluded.append((symbol, "산업·섹터 분류자료 없음"))
                continue
            full = [sector for sector in sectors if counts.get(sector, 0) >= self.maximum]
            if full:
                excluded.append((symbol, f"{', '.join(full)} 섹터 {self.maximum}개 한도"))
                continue
            selected.append(symbol)
            for sector in sectors:
                counts[sector] = counts.get(sector, 0) + 1
            if len(selected) >= maximum:
                break
        return DiversificationResult(tuple(selected), tuple(excluded))
