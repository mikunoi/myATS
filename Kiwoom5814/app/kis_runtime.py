"""Run one KIS account independently of the installed Kiwoom services."""
import argparse
import asyncio
from contextlib import closing
from datetime import datetime, time
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from api_accounts import account_dir, private_dir, save_json
from ats_models import require_kis_model
from managed_snapshots import visible_accounts
from kis_execution import ExecutionClient
from kis_managed import watchlist_client
from kis_signals import _imports, candles
from refresh_schedule import KST, calendar


def profile_for(root, identity):
    matches = [p for p in visible_accounts(root) if p.identity == identity and p.enabled and p.ats]
    if len(matches) != 1:
        raise ValueError('KIS_ATS_NOT_CONFIGURED')
    require_kis_model(matches[0].model_id)
    return matches[0]


class MarketClock:
    def __init__(self):
        self.confirmed_day = None

    def match_date(self, now):
        return self.confirmed_day == now.date()

    def trading_date(self, now):
        known = calendar()
        return (now.year in known['years'] and now.date().isoformat() not in known['holidays']
                and now.weekday() < 5)

    def regular_market_open(self, now):
        return self.match_date(now) and time(9) <= now.time() < time(15,20)


def runtime_types(root):
    binary, Worker, NativeEngine, load_settings = _imports(root)
    from src.database.store import Store
    from src.platform_support import FileLock
    from src.recovery.service import RecoveryGate
    from src.runtime.close_phases import CloseEvaluationPhases
    from src.runtime.live import LiveAccountBroker, LiveTradingCoordinator
    from src.runtime.live_service import LiveRuntimeService, WatchlistReloadRequired

    class Coordinator(LiveTradingCoordinator):
        def _resolve_unsubmitted_uncertain_orders(self):
            # An empty response after submission failure cannot prove nonacceptance.
            return 0

    class Service(LiveRuntimeService):
        def __init__(self, settings, client, coordinator, store, symbols, entries, source, clock):
            self.settings, self.client, self.coordinator, self.store = settings, client, coordinator, store
            self.symbols, self.candidate_symbols, self.entry_symbols = symbols, symbols, frozenset(entries)
            self.source, self.clock = source, clock
            self.checked_day = None
            self.telegram = SimpleNamespace(notify=lambda *_args: None)
            self.close_phases = CloseEvaluationPhases(store, 'domestic', time(14), time(14,10), time(14,30))
            self.evaluating_close = False
            self.quotes, self.daily, self.weekly = {}, {}, {}

        def label(self, symbol):
            return symbol

        async def refresh_buy_priority(self, now, phase):
            profile = self.client.profile
            _, symbols = await asyncio.to_thread(self.source.watchlist, profile.group, profile.group_code)
            entries = tuple(s for s in symbols if s not in self.settings.excluded_symbols)
            if any(s not in self.symbols for s in entries):
                raise WatchlistReloadRequired('KIS_WATCHLIST_CHANGED')
            self.entry_symbols = frozenset(entries)
            self.candidate_symbols = entries + tuple(s for s in self.symbols if s not in self.entry_symbols)

        async def refresh_indicators(self, symbol):
            today = datetime.now(KST).date()
            daily = await asyncio.to_thread(candles, self.client, symbol, 'daily', today)
            weekly = await asyncio.to_thread(candles, self.client, symbol, 'weekly', today)
            if daily[-1].date != today.strftime('%Y%m%d'):
                raise RuntimeError('KIS_SAME_DAY_CANDLE_REQUIRED')
            engine = self.coordinator.strategy
            self.daily[symbol] = engine.indicators(daily, 'DAILY')
            self.weekly[symbol] = engine.indicators(weekly, 'WEEKLY')

        async def intraday(self, now):
            if self.close_phases.buys_paused(now):
                return
            day = now.date().isoformat()
            for symbol in self.candidate_symbols:
                state = self.coordinator.states.get(symbol)
                if not state or self.coordinator.addons_today.get((day,symbol),0):
                    continue
                daily = self.daily.get(symbol)
                if not daily or daily[-1].date != now.strftime('%Y%m%d'):
                    await self.refresh_indicators(symbol)
                points = self.weekly[symbol] if state.mode == 'WEEKLY' else self.daily[symbol]
                if points[-1].atr is not None:
                    quote = await asyncio.to_thread(self.client.quote, symbol)
                    self.coordinator.evaluate_addon(symbol, quote.current_price, points[-1].atr, day)

    return (binary, Worker, NativeEngine, load_settings, Store, FileLock, RecoveryGate,
            LiveAccountBroker, Coordinator, Service, WatchlistReloadRequired)


def run(root, expected, *, live_confirmed=False):
    require_kis_model(expected.model_id)
    if expected.mode == 'live' and not live_confirmed:
        raise RuntimeError('KIS_LIVE_CONFIRMATION_REQUIRED')
    (binary, Worker, NativeEngine, load_settings, Store, FileLock, RecoveryGate,
     Broker, Coordinator, Service, WatchlistReloadRequired) = runtime_types(root)
    path = private_dir(account_dir(root, expected))
    status = path / 'ats-status.json'
    stop = path / 'ats-stop'
    settings = load_settings(root)
    clock = MarketClock()

    def current():
        return not stop.exists() and profile_for(root, expected.identity) == expected

    with FileLock(path/'ats.lock', timeout=5):
        if stop.exists():
            return False
        save_json(status, {'state':'사전 확인 중','at':datetime.now(KST).isoformat(),'pid':os.getpid()})
        try:
            with closing(Store(path/'ats.sqlite3')) as store:
                store.migrate()
                store.set_control('STOP')
                worker = Worker(binary())
                try:
                    gate = RecoveryGate(store)
                    def authorize(action):
                        now = datetime.now(KST)
                        time_ok = clock.regular_market_open(now) if action == 'submit' else clock.match_date(now) and time(9) <= now.time() < time(15,30)
                        return time_ok and current() and store.control() == 'RUNNING' and gate.reconciled

                    client = ExecutionClient(root, expected, authorize)
                    source = watchlist_client(root, expected)
                    engine = NativeEngine(settings, worker)
                    store.set_control('RUNNING')
                    while current():
                        _, stocks = source.watchlist(expected.group, expected.group_code)
                        entries = tuple(s for s in stocks if s not in settings.excluded_symbols)
                        tracked = [s for s,_,_ in store.load_strategy_states()]
                        pending = [json.loads(raw)['symbol'] for _,raw in store.system_values('live_intent:')
                                   if json.loads(raw)['status'] in Coordinator.ACTIVE]
                        symbols = tuple(dict.fromkeys((*entries,*tracked,*pending)))
                        if not entries:
                            raise RuntimeError('KIS_WATCHLIST_EMPTY')
                        broker = Broker(client, symbols)
                        coordinator = Coordinator(settings, engine, broker, gate, store)
                        coordinator.restore()
                        if any(e['status'] in {'SENDING','UNCERTAIN','PARTIAL_CANCELED'} for e in coordinator.journal.values()):
                            raise RuntimeError('KIS_ORDER_RECONCILIATION_REQUIRED')
                        snapshot = broker.snapshot()
                        if any(p.symbol in symbols and p.symbol not in coordinator.states for p in snapshot.positions):
                            raise RuntimeError('KIS_HOLDING_ADOPTION_REQUIRED')
                        session = Service(settings, client, coordinator, store, symbols, entries, source, clock)
                        try:
                            asyncio.run(supervise(session, current, status))
                            break
                        except WatchlistReloadRequired:
                            continue
                finally:
                    store.set_control('STOP')
                    worker.close()
        except Exception as exc:
            label = str(exc)
            if not label.startswith(('KIS_', 'RUST_')) or not label.replace('_','').isalnum():
                label = type(exc).__name__
            save_json(status, {'state':'중지 · 점검 필요','error':label,'at':datetime.now(KST).isoformat(),'pid':os.getpid()})
            return False
        save_json(status, {'state':'중지','at':datetime.now(KST).isoformat(),'pid':os.getpid()})
        return True


async def supervise(session, current, status):
    while current():
        now = datetime.now(KST)
        coordinator = session.coordinator
        clock = session.clock
        if (session.checked_day != now.date() and clock.trading_date(now)
                and time(9) <= now.time() < time(15,30)):
            open_day = await asyncio.to_thread(session.source.market_day, now.strftime('%Y%m%d'))
            clock.confirmed_day = now.date() if open_day else None
            session.checked_day = now.date()
        if not clock.regular_market_open(now):
            if clock.match_date(now) and time(15,20) <= now.time() < time(16) and session.has_active_orders():
                coordinator.sync_orders()
                if now.time() < time(15,30):
                    coordinator.cancel_remaining()
            save_json(status, {'state':'장외 대기','at':now.isoformat(),'pid':os.getpid()})
            await asyncio.sleep(5)
            continue
        coordinator.sync_orders()
        if any(e['status'] in {'SENDING','UNCERTAIN','PARTIAL_CANCELED'} for e in coordinator.journal.values()):
            raise RuntimeError('KIS_ORDER_RECONCILIATION_REQUIRED')
        phase = session.close_phases.due(now, True)
        if phase:
            await session.evaluate_close(now, phase)
        else:
            await session.intraday(now)
        save_json(status, {'state':'운용 중','at':now.isoformat(),'pid':os.getpid(),
                           'pending':sum(e['status'] in coordinator.ACTIVE for e in coordinator.journal.values())})
        await asyncio.sleep(10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('identity')
    parser.add_argument('--confirm-live', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    try:
        profile = profile_for(root, args.identity)
        return 0 if run(root, profile, live_confirmed=args.confirm_live) else 1
    except (OSError, ValueError, RuntimeError):
        return 1


if __name__ == '__main__':
    sys.exit(main())
