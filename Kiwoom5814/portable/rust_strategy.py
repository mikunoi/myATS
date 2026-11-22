"""Persistent Rust worker with Python decision parity gate. No silent fallback."""
import atexit
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
from queue import Queue, Empty
import subprocess
import threading

from src.strategy.engine import StrategyEngine as PythonEngine
from src.strategy.indicators import IndicatorPoint


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, '__dataclass_fields__'):
        return {k: encode(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


class Worker:
    def __init__(self, executable):
        self.lock = threading.RLock()
        self.process = subprocess.Popen([str(Path(executable).resolve()), 'worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.responses = Queue()
        def receive():
            try:
                for line in self.process.stdout:
                    self.responses.put(line)
            finally:
                self.responses.put(None)
        threading.Thread(target=receive, daemon=True).start()
        atexit.register(self.close)
        if self.call('hello')['protocol'] != 1:
            self.close()
            raise RuntimeError('RUST_PROTOCOL_MISMATCH')

    def close(self):
        with self.lock:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)

    def call(self, op, **payload):
        with self.lock:
            if self.process.poll() is not None:
                raise RuntimeError('RUST_WORKER_UNAVAILABLE')
            try:
                self.process.stdin.write(json.dumps(encode(dict(op=op, **payload))) + '\n')
                self.process.stdin.flush()
                line = self.responses.get(timeout=15)
                if line is None:
                    raise RuntimeError('RUST_WORKER_EXITED')
                result = json.loads(line)
                if result.get('ok') is not True:
                    raise RuntimeError('RUST_REJECTED_INPUT')
                return result['result']
            except (OSError, Empty, ValueError, KeyError, RuntimeError):
                self.close()
                raise RuntimeError('RUST_EVALUATION_FAILED') from None


class RustStrategyEngine:
    """Engine API used by existing domestic/US coordinators, checked every call."""
    def __init__(self, settings, worker):
        self.settings, self.worker = settings, worker
        self.reference = PythonEngine(settings)

    def checked(self, native, expected):
        if native != expected:
            raise RuntimeError('RUST_PYTHON_PARITY_MISMATCH')
        return native

    def initial_quantity(self, capital, atr):
        return self.initial_quantity_for_mode(capital, atr, 'DAILY')

    def initial_quantity_for_mode(self, capital, atr, mode):
        expected = self.reference.initial_quantity_for_mode(capital, atr, mode)
        multiple = self.settings.entry_stop_atr_multiple if mode == 'DAILY' else self.settings.weekly_entry_stop_atr_multiple
        return self.checked(self.worker.call('quantity', capital=str(capital), atr=str(atr),
                            risk=str(self.settings.risk_rate), multiple=str(multiple)), expected)

    def indicators(self, candles, mode='DAILY'):
        expected = self.reference.indicators(candles, mode)
        periods = self.settings.daily_ichimoku if mode == 'DAILY' else self.settings.weekly_ichimoku
        encoded = [dict(date=c.date, **{k: str(getattr(c, k)) for k in ('open','high','low','close')}) for c in candles]
        result = self.worker.call('indicators', candles=encoded, periods=periods, atr_period=self.settings.atr_period)
        native = tuple(IndicatorPoint(**{k: (Decimal(v) if k in ('close','tenkan','kijun','senkou_a','senkou_b','atr') and v is not None else v)
                                      for k,v in row.items()}) for row in result)
        return self.checked(native, expected)

    def entry_common(self, current):
        return self.checked(self.worker.call('entry_common', current=current), self.reference.entry_common(current))

    def initial_entry(self, previous, current):
        return self.checked(self.worker.call('initial_entry', previous=previous, current=current), self.reference.initial_entry(previous,current))

    def cycle_reentry(self, previous, current):
        return self.checked(self.worker.call('cycle_reentry', previous=previous, current=current), self.reference.cycle_reentry(previous,current))

    def addon_entry(self, current_price, state, addons_today):
        expected = self.reference.addon_entry(current_price, state, addons_today)
        if state.latest_entry_price is None or state.latest_entry_atr is None:
            return False
        return self.checked(self.worker.call('addon_entry', current_price=current_price,
            latest_entry_price=state.latest_entry_price, latest_entry_atr=state.latest_entry_atr,
            addon_count=state.addon_count, addons_today=addons_today,
            cycle_limit=self.settings.max_addons_cycle, daily_limit=self.settings.max_addons_day,
            multiple=str(self.settings.addon_atr_multiple)), expected)

    def sell_signals(self, previous, current, current_price, state):
        expected = self.reference.sell_signals(previous, current, current_price, state)
        return self.checked(tuple(self.worker.call('sell_signals', previous=previous,current=current,
            current_price=current_price, mode=state.mode, latest_entry_price=state.latest_entry_price,
            latest_entry_atr=state.latest_entry_atr)), expected)

    def rule3_reentry(self, previous, current, previous_price, current_price):
        return self.checked(self.worker.call('rule3_reentry',previous=previous,current=current,
            previous_price=previous_price,current_price=current_price),
            self.reference.rule3_reentry(previous,current,previous_price,current_price))

    def weekly_rule3_reentry(self, previous, current, previous_price, current_price):
        return self.checked(self.worker.call('weekly_rule3_reentry',previous=previous,current=current,
            previous_price=previous_price,current_price=current_price),
            self.reference.weekly_rule3_reentry(previous,current,previous_price,current_price))
