"""Kiwoom read-only real-time client. This module has no order operations."""
import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from websockets.asyncio.client import connect

from src.broker.http import KiwoomError
from src.market_data.models import OrderBook, OrderBookLevel, Quote


@dataclass(frozen=True)
class RealtimeSnapshot:
    value: object
    received_at: datetime


@dataclass(frozen=True)
class MarketStatus:
    code: str
    event_time: str


class StaleMarketData(KiwoomError):
    pass


class RealtimeCache:
    def __init__(self, stale_after=5.0, *, monotonic=None):
        if not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError("Invalid stale interval")
        self.stale_after = stale_after
        self._clock = monotonic or time.monotonic
        self._values = {}
        self._connected = False

    def connected(self):
        self._connected = True

    def disconnected(self):
        self._connected = False
        self._values.clear()

    def put(self, kind, symbol, value):
        if self._connected:
            self._values[(kind, symbol)] = (value, self._clock(), datetime.now(timezone.utc))

    def get(self, kind, symbol):
        record = self._values.get((kind, symbol))
        if not self._connected or record is None or self._clock() - record[1] > self.stale_after:
            raise StaleMarketData(f"No fresh {kind} data for requested symbol")
        return RealtimeSnapshot(record[0], record[2])


def _integer(values, key, *, nonnegative=False):
    value = values.get(key)
    if not isinstance(value, str):
        raise KiwoomError("Invalid real-time numeric field")
    try:
        result = abs(int(value.strip()))
    except ValueError:
        raise KiwoomError("Invalid real-time numeric field") from None
    if nonnegative and result < 0:
        raise KiwoomError("Invalid real-time quantity")
    return result


def parse_realtime(row):
    if not isinstance(row, dict) or not isinstance(row.get("values"), dict):
        raise KiwoomError("Invalid real-time row")
    kind, symbol, values = row.get("type"), row.get("item"), row["values"]
    if not isinstance(symbol, str) or (not symbol and kind != "0s"):
        raise KiwoomError("Invalid real-time symbol")
    if kind == "0s":
        code = values.get("215")
        if not isinstance(code, str) or not code:
            raise KiwoomError("Invalid market status field")
        return "market_status", "", MarketStatus(code, str(values.get("20", "")))
    if kind == "0B":
        return "quote", symbol, Quote(symbol, "", _integer(values, "10"),
                                       _integer(values, "13", nonnegative=True))
    if kind == "0D":
        asks = tuple(OrderBookLevel(level, _integer(values, str(40 + level)),
                                    _integer(values, str(60 + level), nonnegative=True))
                     for level in range(1, 11))
        bids = tuple(OrderBookLevel(level, _integer(values, str(50 + level)),
                                    _integer(values, str(70 + level), nonnegative=True))
                     for level in range(1, 11))
        return "order_book", symbol, OrderBook(symbol, str(values.get("21", "")), asks, bids)
    return None


class KiwoomRealtimeClient:
    URLS = {
        "production": "wss://api.kiwoom.com:10000/api/dostk/websocket",
        "mock": "wss://mockapi.kiwoom.com:10000/api/dostk/websocket",
    }

    def __init__(self, token_provider, symbols, *, environment="production", cache=None,
                 login_timeout=10.0, reconnect_base=1.0, reconnect_cap=30.0,
                 connector=connect, sleep=asyncio.sleep, event_handler=None, state_handler=None):
        if environment not in self.URLS:
            raise ValueError("Invalid Kiwoom environment")
        symbols = tuple(dict.fromkeys(symbols))
        if not symbols or len(symbols) > 200 or any(not isinstance(s, str) or not s for s in symbols):
            raise ValueError("Realtime symbols must contain 1 to 200 unique stock codes")
        if login_timeout <= 0 or reconnect_base <= 0 or reconnect_cap < reconnect_base:
            raise ValueError("Invalid realtime timing")
        self.token_provider, self.symbols = token_provider, symbols
        self.url, self.cache = self.URLS[environment], cache or RealtimeCache()
        self.login_timeout = login_timeout
        self.reconnect_base, self.reconnect_cap = reconnect_base, reconnect_cap
        self._connector, self._sleep = connector, sleep
        self._event_handler = event_handler
        self._state_handler = state_handler
        self._stop = asyncio.Event()

    async def _state(self, state):
        if self._state_handler is not None:
            handled = self._state_handler(state)
            if asyncio.iscoroutine(handled):
                await handled

    async def _token(self):
        token = self.token_provider()
        if asyncio.iscoroutine(token):
            token = await token
        if not isinstance(token, str) or not token:
            raise KiwoomError("Missing WebSocket access token")
        return token

    async def session(self, *, stop_after_registration=False):
        async with self._connector(self.url, open_timeout=self.login_timeout,
                                   ping_interval=None) as websocket:
            await websocket.send(json.dumps({"trnm": "LOGIN", "token": await self._token()}))
            while True:
                raw = await asyncio.wait_for(websocket.recv(), self.login_timeout)
                message = json.loads(raw)
                if message.get("trnm") == "PING":
                    await websocket.send(raw)
                    continue
                if message.get("trnm") != "LOGIN" or str(message.get("return_code")) != "0":
                    raise KiwoomError("Kiwoom WebSocket login failed")
                break
            await websocket.send(json.dumps({"trnm": "REG", "grp_no": "1", "refresh": "1",
                "data": [{"item": list(self.symbols), "type": ["0B", "0D"]},
                         {"item": [""], "type": ["0s"]}]}))
            self.cache.connected()
            try:
                async for raw in websocket:
                    message = json.loads(raw)
                    if message.get("trnm") == "PING":
                        await websocket.send(raw)
                    elif message.get("trnm") == "REG":
                        if str(message.get("return_code")) != "0":
                            raise KiwoomError("Kiwoom real-time registration failed")
                        await self._state("CONNECTED")
                        if stop_after_registration:
                            return
                    elif message.get("trnm") == "REAL":
                        for row in message.get("data", ()):
                            parsed = parse_realtime(row)
                            if parsed:
                                self.cache.put(*parsed)
                                if self._event_handler is not None:
                                    handled = self._event_handler(*parsed)
                                    if asyncio.iscoroutine(handled):
                                        await handled
                    if self._stop.is_set():
                        return
            finally:
                self.cache.disconnected()
                await self._state("DISCONNECTED")

    async def run(self):
        delay = self.reconnect_base
        while not self._stop.is_set():
            try:
                await self.session()
                delay = self.reconnect_base
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.cache.disconnected()
                await self._state("ERROR:" + type(error).__name__)
            if not self._stop.is_set():
                await self._sleep(delay)
                delay = min(delay * 2, self.reconnect_cap)

    def stop(self):
        self._stop.set()
