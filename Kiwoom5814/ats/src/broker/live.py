"""Explicitly gated production order adapter. Ordinary KiwoomClient stays read-only."""
from src.broker.http import AuthTransport, KiwoomError
from src.broker.kiwoom import KiwoomClient
from src.orders.models import OrderRequest, Side


def best_limit_order(order):
    if not isinstance(order, OrderRequest) or order.market != "KRX" or order.trade_type != "6":
        raise ValueError("Production orders require KRX best-limit type 6")
    KiwoomClient._symbol(order.symbol)
    api_id = "kt10000" if order.side == Side.BUY else "kt10001"
    return api_id, {"dmst_stex_tp": "KRX", "stk_cd": order.symbol,
                    "ord_qty": str(order.quantity), "ord_uv": "",
                    "trde_tp": "6", "cond_uv": ""}


class ProductionOrderTransport(AuthTransport):
    def __init__(self, authorize, cancel_authorize=None, **kwargs):
        super().__init__("production", **kwargs)
        self.authorize = authorize
        self.cancel_authorize = cancel_authorize or authorize

    def order(self, api_id, token, body):
        if api_id == "kt10003":
            if self.cancel_authorize() is not True:
                raise KiwoomError("Production cancellation is not armed")
            if (not isinstance(body, dict) or body.get("dmst_stex_tp") != "KRX"
                    or not str(body.get("orig_ord_no", "")).isdigit()
                    or not str(body.get("cncl_qty", "")).isdigit()):
                raise KiwoomError("Invalid production cancel request")
            KiwoomClient._symbol(body.get("stk_cd"))
            return self._send_order(api_id, token, body)
        if self.authorize() is not True:
            raise KiwoomError("Production order execution is not armed")
        if api_id not in {"kt10000", "kt10001"}:
            raise KiwoomError("Only reviewed production buy/sell requests are supported")
        if (not isinstance(body, dict) or body.get("dmst_stex_tp") != "KRX"
                or body.get("trde_tp") != "6" or body.get("ord_uv") != ""
                or body.get("cond_uv") != ""):
            raise KiwoomError("Production market/order type mismatch")
        quantity = body.get("ord_qty", "")
        if not isinstance(quantity, str) or not quantity.isdigit() or int(quantity) <= 0:
            raise KiwoomError("Invalid production order quantity")
        KiwoomClient._symbol(body.get("stk_cd"))
        return self._send_order(api_id, token, body)


class KiwoomLiveClient(KiwoomClient):
    def __init__(self, secrets, *, expected_account, authorize, managed_symbols=(), cancel_authorize=None, transport=None, clock=None):
        if not isinstance(expected_account, str) or not expected_account.isdigit():
            raise ValueError("An exact API account number is required")
        self.expected_account = expected_account
        self.managed_symbols = frozenset(managed_symbols)
        self.authorize = authorize
        self.cancel_authorize = cancel_authorize or authorize
        super().__init__(secrets, environment="production", clock=clock,
            transport=transport or ProductionOrderTransport(authorize, self.cancel_authorize))

    @staticmethod
    def market_for(symbol):
        return "KRX"

    def submit(self, order=None):
        if self.authorize() is not True:
            raise RuntimeError("Production order execution is not armed")
        api_id, body = best_limit_order(order)
        if order.symbol not in self.managed_symbols:
            raise RuntimeError("Production order symbol is outside the current watchlist")
        if self.account_number() != self.expected_account:
            raise RuntimeError("Production account mismatch; order blocked")
        # Repeat the gate after the account lookup, which may block for network I/O.
        if self.authorize() is not True:
            raise RuntimeError("Production order execution was disarmed")
        result = self._transport.order(api_id, self._token, body)
        order_id = result.get("ord_no")
        if not isinstance(order_id, str) or not order_id.isdigit() or len(order_id) > 12:
            raise KiwoomError("Production order outcome is uncertain; reconcile before retry")
        return order_id

    def cancel(self, order_id="", symbol="", quantity=0):
        if self.cancel_authorize() is not True:
            raise RuntimeError("Production cancellation is not armed")
        if symbol not in self.managed_symbols or not str(order_id).isdigit() or type(quantity) is not int or quantity < 0:
            raise ValueError("Invalid managed cancellation")
        if self.account_number() != self.expected_account or self.cancel_authorize() is not True:
            raise RuntimeError("Account mismatch or disarmed cancellation")
        result = self._transport.order("kt10003", self._token, {"dmst_stex_tp": "KRX",
            "orig_ord_no": order_id, "stk_cd": symbol, "cncl_qty": str(quantity)})
        cancel_id = result.get("ord_no")
        if not isinstance(cancel_id, str) or not cancel_id.isdigit():
            raise RuntimeError("Cancellation outcome uncertain")
        return cancel_id
