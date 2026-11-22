"""Select the approved native engine without changing service entrypoints."""
import hashlib
import json
from pathlib import Path
import platform
import sys

from src.strategy.engine import StrategyEngine

_worker = None


def create_strategy(settings, store=None):
    global _worker
    install = Path(__file__).resolve().parents[3]
    mode_file = install / 'portable/engine_mode.json'
    mode = json.loads(mode_file.read_text(encoding='utf-8')).get('backend') if mode_file.exists() else 'python'
    if mode == 'python':
        engine = StrategyEngine(settings)
        backend, pid = 'python', ''
    elif mode == 'rust_checked':
        sys.path.insert(0, str(install / 'portable'))
        import manage
        from rust_strategy import Worker, RustStrategyEngine
        receipt = json.loads((install / 'portable/LIVE_APPROVAL.json').read_text(encoding='utf-8'))
        core = manage.binary()
        if (receipt.get('host') != platform.node()
                or receipt.get('exclusive_account_confirmed') is not True
                or receipt.get('core_sha256') != hashlib.sha256(core.read_bytes()).hexdigest()
                or receipt.get('source_sha256') != manage.source_hash()):
            raise RuntimeError('RUST_APPROVAL_MISMATCH')
        if _worker is None:
            _worker = Worker(core)
        _worker.call('hello')
        engine = RustStrategyEngine(settings, _worker)
        backend, pid = 'rust_checked', str(_worker.process.pid)
    else:
        raise RuntimeError('UNKNOWN_STRATEGY_BACKEND')
    if store is not None:
        store.set_system_value('strategy_engine_backend', backend)
        store.set_system_value('strategy_engine_worker_pid', pid)
    return engine
