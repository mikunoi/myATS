from src.reporting.notion_manual_holdings import NotionManualHoldingsSync
"""Deterministic, event-driven synchronization for the shared Holdings database."""
import json
import time
from decimal import Decimal

from src.reporting.notion_api import NotionError, NotionTransport
from src.config import load_settings, Settings


class Notion5814Sync:
    def __init__(self, root, token, store, logger, transport=None, retry_seconds=60,
                 symbol_names=None):
        self.root, self.store, self.logger = root, store, logger
        self.transport = transport or NotionTransport(token)
        self.config = json.loads((root / "config/notion_5814.mapping.json").read_text(encoding="utf-8"))
        self.configured = bool(token) and self.config.get("enabled") is True
        self.manual_holdings = NotionManualHoldingsSync(self.transport, self.config, logger)
        self.retry_seconds, self.last_attempt = retry_seconds, 0.0
        self.symbol_names = dict(symbol_names or {})
        self.addon_multiple = (load_settings(root) if (root / "config/default.json").exists() else Settings()).addon_atr_multiple

    @staticmethod
    def _number(value):
        return {"number": float(Decimal(str(value)))}

    @staticmethod
    def _whole_number(value):
        """Notion values for Korean securities use whole KRW/share units."""
        return {"number": int(Decimal(str(value)))}

    @staticmethod
    def _select(value):
        return {"select": {"name": value}}

    @staticmethod
    def _status(value):
        return {"status": {"name": value}}

    @staticmethod
    def _title(value):
        return {"title": [{"type": "text", "text": {"content": value}}]}

    @staticmethod
    def _text(value):
        return {"rich_text": [{"type": "text", "text": {"content": value}}]}

    @staticmethod
    def _date(value):
        return {"date": {"start": value}}

    def _page(self, symbol):
        return self.store.system_value("notion_5814_page:" + symbol,
                                       self.config["page_map"].get(symbol, ""))

    def _buy_properties(self, payload, *, include_title=False):
        properties = {
            "계좌": self._select("키움"), "통화": self._select("KRW"),
            "상태": self._status("매수"), "리스크": self._select("0.5%"),
            "포지션": self._select("일봉" if payload["mode"] == "DAILY" else "주봉"),
            "평단": self._whole_number(payload["average_price"]),
            "보유수량": self._whole_number(payload["quantity"]),
            "최근 진입가": self._whole_number(payload["latest_entry_price"]),
            "최근 n값": self._whole_number(payload["atr"]),
            "유닛수": self._whole_number(payload["units"]),
            "계약수": self._whole_number(payload["contracts"]),
            "최근 체결일자": self._date(payload["date"]),
            "추매 ATR 배수": self._number(self.addon_multiple),
        }
        if include_title:
            name = (self.symbol_names.get(payload["symbol"])
                    or self.config.get("symbol_names", {}).get(payload["symbol"])
                    or payload["symbol"])
            properties["종목"] = self._title(name)
            properties["비고"] = self._text("자동매매 거래ID:" + payload["order_id"])
        return properties

    def _find_created_page(self, marker):
        result = self.transport.request("POST",
            f"/v1/data_sources/{self.config['data_source_id']}/query",
            {"page_size": 2, "filter": {"property": "비고", "rich_text": {"equals": marker}}})
        rows = result.get("results", [])
        if len(rows) > 1:
            raise NotionError("Duplicate automatic trade pages found")
        return rows[0].get("id") if rows else None

    def _apply(self, payload):
        symbol = payload["symbol"]
        page = payload.get("_target_page") or self._page(symbol)
        # Manual reconciliation emits MANUAL_BUY with new_cycle=False even for
        # an unmapped holding. Create its event-keyed reporting row so the later
        # SELL can close that row; never guess another historical row by symbol.
        create_page = payload.get("side") == "BUY" and (
            payload.get("new_cycle") or
            (payload.get("kind") == "MANUAL_BUY" and not page))
        if not page and not create_page:
            raise NotionError("Unmapped account symbol")
        if payload["side"] == "SYNC":
            page = self._page(symbol)
            if not page:
                return False
            self.transport.request("PATCH", "/v1/pages/" + page, {"properties": {
                "평단": self._whole_number(payload["average_price"]),
                "보유수량": self._whole_number(payload["quantity"]),
                "최근 체결일자": self._date(payload["date"])}})
            return True
        if payload["side"] == "SELL":
            page = payload.get("_target_page") or self._page(symbol)
            if not page:
                raise NotionError("Holding page mapping is missing")
            properties = {"상태": self._status("종료"),
                          "최근 체결일자": self._date(payload["date"])}
            if payload.get("profit_pending"):
                if not payload.get("_status_synced"):
                    self.transport.request("PATCH", "/v1/pages/" + page, {"properties": properties})
                return False
            properties.update({"실현손익": self._whole_number(payload["profit_amount"]),
                               "손익률": self._number(payload["profit_rate"])})
            self.transport.request("PATCH", "/v1/pages/" + page, {"properties": properties})
            return True
        if create_page:
            marker = "자동매매 거래ID:" + payload["order_id"]
            page = self._find_created_page(marker)
            if not page:
                result = self.transport.request("POST", "/v1/pages", {
                    "parent": {"type": "data_source_id", "data_source_id": self.config["data_source_id"]},
                    "properties": self._buy_properties(payload, include_title=True)})
                page = result.get("id")
            if not page:
                raise NotionError("Created holding page identifier is missing")
            self.store.set_system_value("notion_5814_page:" + symbol, page)
            return True
        page = self._page(symbol)
        if not page:
            raise NotionError("Holding page mapping is missing")
        self.transport.request("PATCH", "/v1/pages/" + page,
                               {"properties": self._buy_properties(payload)})
        return True

    def sync_pending(self):
        if not self.configured:
            return False
        now = time.monotonic()
        if now - self.last_attempt < self.retry_seconds:
            return False
        self.last_attempt = now
        changed = False
        errors = []
        blocked_symbols = set()
        for event_key, raw in self.store.events_by_category("notion_holding_update"):
            if self.store.system_value("notion_5814_applied:" + event_key, "") == "1":
                continue
            try:
                payload = json.loads(raw)
                if payload["symbol"] in blocked_symbols:
                    continue
                if payload.get("side") == "SELL":
                    target_key = "notion_5814_sell_page:" + event_key
                    page = self.store.system_value(target_key, "") or self._page(payload["symbol"])
                    if not page:
                        raise NotionError("Holding page mapping is missing")
                    self.store.set_system_value(target_key, page)
                    payload["_target_page"] = page
                    closed_key = "notion_5814_closed:" + event_key
                    payload["_status_synced"] = self.store.system_value(closed_key, "") == "1"
                if not self._apply(payload):
                    if payload.get("side") == "SELL":
                        changed |= not payload["_status_synced"]
                        self.store.set_system_value(closed_key, "1")
                    continue
                self.store.set_system_value("notion_5814_applied:" + event_key, "1")
                changed = True
            except (NotionError, KeyError, TypeError, ValueError, OSError) as error:
                try:
                    blocked_symbols.add(json.loads(raw)["symbol"])
                except (KeyError, TypeError, ValueError):
                    pass
                errors.append(type(error).__name__)
                self.logger.error("Notion 5814 synchronization failed for %s: %s", event_key, str(error))
        self.store.set_system_value("notion_5814_last_error", ",".join(errors))
        return changed

    def sync_cash(self, integrated_estimated_cash, *, verify=False):
        """Keep the one-unit Kiwoom cash row equal to consolidated estimated cash."""
        if not self.configured:
            return False
        value = int(integrated_estimated_cash)
        key = "notion_5814_cash_integrated_estimated"
        if not verify and self.store.system_value(key, "") == str(value):
            return False
        try:
            page = self.config["cash_page_id"]
            if verify:
                actual = self.transport.request("GET", "/v1/pages/" + page)["properties"]
                if (actual.get("평단", {}).get("number") == value
                        and actual.get("보유수량", {}).get("number") == 1):
                    self.store.set_system_value(key, value)
                    self.store.set_system_value("notion_5814_cash_last_error", "")
                    return True
            self.transport.request("PATCH", "/v1/pages/" + page,
                {"properties": {"평단": self._whole_number(value), "보유수량": self._whole_number(1)}})
            if verify:
                actual = self.transport.request("GET", "/v1/pages/" + page)["properties"]
                if (actual.get("평단", {}).get("number") != value
                        or actual.get("보유수량", {}).get("number") != 1):
                    raise NotionError("Cash page verification failed")
            self.store.set_system_value(key, value)
            self.store.set_system_value("notion_5814_cash_last_error", "")
            return True
        except (NotionError, KeyError, TypeError, ValueError, OSError) as error:
            self.store.set_system_value("notion_5814_cash_last_error", type(error).__name__)
            self.logger.error("Notion 5814 cash synchronization failed: %s", type(error).__name__)
            return False
