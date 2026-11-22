"""Direct Notion API synchronization; failures never block trading."""
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NotionError(RuntimeError):
    pass


class NotionTransport:
    def __init__(self, token, timeout=10, opener=None):
        self.token, self.timeout = token, timeout
        self.opener = opener or build_opener(NoRedirect())

    def request(self, method, path, body=None):
        if not self.token:
            raise NotionError("Notion token is missing")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request("https://api.notion.com" + path, method=method, data=data,
                          headers={"Authorization": f"Bearer {self.token}",
                                   "Notion-Version": "2026-03-11",
                                   "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise NotionError("Notion response too large")
            result = json.loads(raw)
        except HTTPError as error:
            raise NotionError(f"Notion HTTP status {error.code}") from None
        except (URLError, TimeoutError, OSError):
            raise NotionError("Notion connection failed") from None
        except (ValueError, UnicodeError):
            raise NotionError("Malformed Notion response") from None
        if not isinstance(result, dict):
            raise NotionError("Malformed Notion response")
        return result


class NotionHoldingsSync:
    def __init__(self, root, token, store, logger, transport=None, retry_seconds=60):
        self.root, self.store, self.logger = root, store, logger
        self.transport = transport or NotionTransport(token)
        self.configured = bool(token)
        self.retry_seconds, self.last_attempt = retry_seconds, 0.0

    @staticmethod
    def _text(value):
        return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}

    @staticmethod
    def _title(value):
        return {"title": [{"type": "text", "text": {"content": str(value)}}]}

    @staticmethod
    def _number(value):
        return {"number": value}

    @classmethod
    def properties(cls, row, *, closed=False):
        updated = row.get("updated_at")
        return {
            "종목명": cls._title(row["name"]),
            "종목코드": cls._text(row["symbol"]),
            "매입가": cls._number(0 if closed else row["average_price"]),
            "수량": cls._number(0 if closed else row["quantity"]),
            "매입금액": cls._number(0 if closed else row["purchase_amount"]),
            "유닛수": cls._number(row["units"]),
            "계약수": cls._number(row["contracts"]),
            "최근 진입가": cls._number(row["latest_entry_price"]),
            "ATR": cls._number(row["atr"]),
            "추매가": cls._number(row["addon_price"]),
            "손절가": cls._number(row["stop_price"]),
            "시장구분": {"select": {"name": row["market"]}},
            "상태": {"select": {"name": "청산" if closed else "보유"}},
            "최종체결일": {"date": {"start": updated}},
        }

    @staticmethod
    def _symbol(page):
        values = page.get("properties", {}).get("종목코드", {}).get("rich_text", [])
        return str(values[0].get("plain_text", "")) if values else ""

    def _pages(self, data_source_id):
        pages, cursor = [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            result = self.transport.request("POST", f"/v1/data_sources/{data_source_id}/query", body)
            if not isinstance(result.get("results"), list):
                raise NotionError("Invalid Notion query response")
            pages.extend(result["results"])
            if not result.get("has_more"):
                return pages
            cursor = result.get("next_cursor")
            if not cursor:
                raise NotionError("Invalid Notion pagination")

    @classmethod
    def trade_properties(cls, row):
        return {
            "종목명": cls._title(row["name"]),
            "종목코드": cls._text(row["symbol"]),
            "첫 매수일": {"date": {"start": row["first_buy_date"]}},
            "매도일": {"date": {"start": row["sell_date"]}},
            "매입단가": cls._number(row["buy_price"]),
            "매도체결가": cls._number(row["sell_price"]),
            "실현수익률": cls._number(row["profit_rate"]),
            "실현수익액": cls._number(row["profit_amount"]),
            "청산사유": {"select": {"name": row["exit_reason"]}},
            "거래ID": cls._text(row["trade_id"]),
        }

    @staticmethod
    def _trade_id(page):
        values = page.get("properties", {}).get("거래ID", {}).get("rich_text", [])
        return str(values[0].get("plain_text", "")) if values else ""

    def sync_if_pending(self):
        holdings_pending = self.store.system_value("notion_holdings_pending", "0") == "1"
        trades_pending = self.store.system_value("notion_trades_pending", "0") == "1"
        if not self.configured or not (holdings_pending or trades_pending):
            return False
        now = time.monotonic()
        if now - self.last_attempt < self.retry_seconds:
            return False
        self.last_attempt = now
        try:
            config = json.loads((self.root / "config" / "notion.json").read_text(encoding="utf-8"))
            if holdings_pending:
                payload = json.loads((self.root / "reports" / "input" / "notion_holdings.json").read_text(encoding="utf-8"))
                data_source_id = config["holdings_data_source_id"]
                existing = {self._symbol(page): page for page in self._pages(data_source_id)}
                current = {row["symbol"]: row for row in payload["rows"]}
                for symbol, row in current.items():
                    properties = self.properties(row)
                    if symbol in existing:
                        self.transport.request("PATCH", f"/v1/pages/{existing[symbol]['id']}", {"properties": properties})
                    else:
                        self.transport.request("POST", "/v1/pages", {"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties})
                for symbol, page in existing.items():
                    if symbol and symbol not in current:
                        prior = {"symbol": symbol, "name": page.get("properties", {}).get("종목명", {}).get("title", [{}])[0].get("plain_text", symbol), "units": 0, "contracts": 0, "latest_entry_price": 0, "atr": 0, "addon_price": 0, "stop_price": 0, "market": "KRX", "updated_at": payload["generated_at"]}
                        self.transport.request("PATCH", f"/v1/pages/{page['id']}", {"properties": self.properties(prior, closed=True)})
                self.store.set_system_value("notion_holdings_pending", "0")
            if trades_pending:
                data_source_id = config["trades_data_source_id"]
                existing = {self._trade_id(page): page for page in self._pages(data_source_id)}
                for _event_key, raw in self.store.events_by_category("closed_trade"):
                    row = json.loads(raw)
                    properties = self.trade_properties(row)
                    trade_id = row["trade_id"]
                    if trade_id in existing:
                        self.transport.request("PATCH", f"/v1/pages/{existing[trade_id]['id']}", {"properties": properties})
                    else:
                        self.transport.request("POST", "/v1/pages", {"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties})
                self.store.set_system_value("notion_trades_pending", "0")
            self.store.set_system_value("notion_holdings_last_error", "")
            return True
        except (NotionError, KeyError, TypeError, ValueError, OSError) as error:
            self.store.set_system_value("notion_holdings_last_error", type(error).__name__)
            self.logger.error("Notion holdings synchronization failed: %s", type(error).__name__)
            return False
