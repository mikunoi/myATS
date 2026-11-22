"""Evaluate the installed live strategy against a registered KIS watchlist."""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys


from kis_managed import ManagedKIS, watchlist_client
from ats_models import require_kis_model
from kis_portfolio import KISError
from kis_snapshot import guard
from managed_snapshots import domestic_balance
from refresh_schedule import KST


def candles(client, symbol, mode, today):
    from src.market_data.models import Candle

    window = 135 if mode == 'daily' else 650
    start = today - timedelta(days=window)
    body = client.market_read('chart', FID_COND_MRKT_DIV_CODE='J', FID_INPUT_ISCD=symbol,
                              FID_INPUT_DATE_1=start.strftime('%Y%m%d'),
                              FID_INPUT_DATE_2=today.strftime('%Y%m%d'),
                              FID_PERIOD_DIV_CODE='D' if mode == 'daily' else 'W',
                              FID_ORG_ADJ_PRC='0')
    points = []
    for row in body['output2']:
        day = str(row['stck_bsop_date']).strip()
        if not day:
            continue
        stamp = datetime.strptime(day, '%Y%m%d').date()
        values = tuple(int(row[k]) for k in ('stck_oprc','stck_hgpr','stck_lwpr','stck_clpr','acml_vol'))
        if stamp > today or min(values[:4]) <= 0 or values[-1] < 0:
            raise ValueError('Invalid candle')
        points.append(Candle(day, *values))
    points.sort(key=lambda point: point.date)
    if len(points) < 53 or len({p.date for p in points}) != len(points):
        raise ValueError('Missing or repeated candles')
    if mode == 'weekly' and (today - datetime.strptime(points[-1].date, '%Y%m%d').date()).days > 10:
        raise ValueError('Weekly candles unavailable')
    return tuple(points)


def _imports(root):
    install = Path(__file__).resolve().parent.parent
    for path in (str(root), str(install/'portable')):
        if path not in sys.path:
            sys.path.insert(0, path)
    from manage import binary
    from rust_strategy import Worker, RustStrategyEngine
    from src.config import load_settings
    return binary, Worker, RustStrategyEngine, load_settings


def evaluate(root, profile):
    """Use only read APIs. Holdings without existing ATS state stay unadopted."""
    if not profile.ats:
        raise ValueError('저장된 ATS 적용 설정이 필요합니다.')
    require_kis_model(profile.model_id)
    client = ManagedKIS(root, profile)
    client.guard = lambda: guard(root)
    source = watchlist_client(root, profile)
    source.guard = client.guard
    _, stocks = source.watchlist(profile.group, profile.group_code)
    balance = domestic_balance(client)
    return predict(root, client, stocks, balance)


def predict(root, client, stocks, balance):
    binary, Worker, RustStrategyEngine, load_settings = _imports(root)
    from src.orders.models import BrokerSnapshot
    from src.runtime.live import LiveTradingCoordinator
    from src.runtime.us_preview import PreviewBroker, ReadOverlay
    from src.orders.models import Side
    from types import SimpleNamespace

    class Signals(LiveTradingCoordinator):
        def _submit(self, symbol, side, quantity, key, action, **kwargs):
            required = self.price * quantity
            if side == Side.BUY and quantity > 0:
                allowed = required <= self.cash and required <= self.capacity
                if allowed:
                    self.cash -= required
                    self.capacity -= required
                return dict(action=action.kind, quantity=quantity,
                            status='조건 충족' if allowed else '자금 부족')
            return None

    today = datetime.now(KST).date()
    held = {row['symbol'] for row in balance['holdings']}
    cash = Decimal(str(balance['cash']))
    cost = sum((Decimal(str(row['purchase'])) for row in balance['holdings']), Decimal(0))
    capital = max(Decimal(0), cash + cost)
    store = ReadOverlay(SimpleNamespace(system_value=lambda _k, default='': default))
    settings = load_settings(Path(root))
    worker = Worker(binary())
    try:
        engine = RustStrategyEngine(settings, worker)
        broker = PreviewBroker(BrokerSnapshot(max(Decimal(0), cash), (), ()),
                               capital, cost, stocks)
        coordinator = Signals(settings, engine, broker, SimpleNamespace(), store)
        coordinator.cash = max(Decimal(0), cash)
        coordinator.capacity = max(Decimal(0), coordinator.maximum_commitment - cost)
        rows = []
        for symbol, name in stocks.items():
            row = dict(symbol=symbol, name=name, status='조건 미충족', action='', quantity=0)
            if symbol in held:
                row['status'] = '기존 보유 · 전략 상태 확인 필요'
            else:
                try:
                    daily = engine.indicators(candles(client, symbol, 'daily', today), 'DAILY')
                    weekly = engine.indicators(candles(client, symbol, 'weekly', today), 'WEEKLY')
                    if (today - datetime.strptime(daily[-1].date, '%Y%m%d').date()).days > 5:
                        raise ValueError('Recent candle unavailable')
                    coordinator.price = daily[-1].close
                    result = coordinator.evaluate_close(symbol, daily, weekly, coordinator.price,
                                                         today.isoformat(), allow_sells=False)
                    if result:
                        row.update(result)
                except (ValueError, TypeError, KeyError, ArithmeticError, KISError):
                    row['status'] = '시세 확인 필요'
            rows.append(row)
        return rows
    finally:
        worker.close()
