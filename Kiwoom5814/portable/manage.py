"""Portable Rust/Python ATS launcher. Doctor/dashboard are strictly local read-only."""
import argparse
from contextlib import closing, nullcontext
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import time

if os.name != 'nt':
    os.environ['TZ'] = 'Asia/Seoul'
    time.tzset()

INSTALL = Path(__file__).resolve().parents[1]
ATS = INSTALL / 'ats'
sys.path.insert(0, str(ATS))
sys.path.insert(0, str(INSTALL / 'portable'))
ACTIVE = {'SENDING', 'UNCERTAIN', 'ACK', 'PARTIAL', 'CANCEL_SENT', 'PARTIAL_CANCELED'}


def binary():
    suffix = '.exe' if os.name == 'nt' else ''
    for path in (INSTALL / 'rust/target/release' / ('kiwoom5814-core' + suffix),
                 INSTALL / 'rust/bin' / ('kiwoom5814-core' + suffix)):
        if path.is_file():
            return path
    raise RuntimeError('Build the Rust core first: cargo build --release --locked --manifest-path rust/Cargo.toml')


def source_hash():
    digest = hashlib.sha256()
    paths = [*ATS.joinpath('src').rglob('*.py'), *ATS.joinpath('scripts').glob('*.py'),
             *INSTALL.joinpath('portable').glob('*.py'), ATS/'config/default.json']
    for path in sorted(paths):
        digest.update(path.relative_to(INSTALL).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def state(market):
    filename = 'kiwoom5814_live.sqlite3' if market == 'domestic' else 'kiwoom5814_us_live.sqlite3'
    path = ATS / 'data' / filename
    if not path.is_file():
        return {'market': market, 'database': 'MISSING', 'ready': False}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as connection:
        connection.execute('PRAGMA query_only=ON')
        values = dict(connection.execute('SELECT key,value FROM system_state'))
        integrity = connection.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        count, malformed = 0, 0
        for key, value in values.items():
            if key.startswith('live_intent:'):
                try:
                    count += json.loads(value).get('status') in ACTIVE
                except (ValueError, AttributeError):
                    malformed += 1
        heartbeat = values.get('service_last_heartbeat:' + market, '')
        try:
            when = datetime.fromisoformat(heartbeat)
            age = (datetime.now(timezone.utc) - when.astimezone(timezone.utc)).total_seconds() if when.tzinfo else None
        except ValueError:
            age = None
        armed = values.get('live_execution_armed' if market == 'domestic' else 'us_execution_armed') == '1'
        return {'market': market, 'database': 'OK' if integrity else 'INVALID',
                'control': values.get('control', 'UNKNOWN'), 'armed': armed,
                'heartbeat': heartbeat, 'heartbeat_age_seconds': round(age, 1) if age is not None else None,
                'heartbeat_recent': age is not None and 0 <= age < 180,
                'active_order_intents': count, 'malformed_intents': malformed,
                'managed_positions': connection.execute('SELECT count(*) FROM strategy_state').fetchone()[0],
                'account_sync_error_present': bool(values.get('account_sync_error:' + market)),
                'block_present': bool(values.get('live_block_reason')),
                'note': 'Local persisted evidence only; no broker/PID check, not proof of trading readiness'}


def doctor():
    from rust_strategy import Worker
    result = {'os': platform.system(), 'python': platform.python_version(), 'imports': {},
              'live_started': False, 'api_called': False}
    for module in ('scripts.live_service', 'scripts.us_live_service', 'src.platform_support'):
        try:
            importlib.import_module(module)
            result['imports'][module] = 'OK'
        except Exception as error:
            result['imports'][module] = type(error).__name__
    worker = Worker(binary())
    try:
        result['rust'] = worker.call('hello')
    finally:
        worker.close()
    result['markets'] = [state(m) for m in ('domestic', 'us')]
    result['environment_present'] = (ATS / '.env').is_file()
    result['core_sha256'] = hashlib.sha256(binary().read_bytes()).hexdigest()
    result['source_sha256'] = source_hash()
    result['local_imports_ok'] = all(v == 'OK' for v in result['imports'].values())
    return result


def serve(port):
    html = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>5814 ATS 운영 모니터</title><style>body{background:#101820;color:#edf2f7;font:17px sans-serif;max-width:960px;margin:40px auto;padding:20px}
    .card{background:#1a2937;padding:24px;border-radius:16px;margin:16px 0}pre{white-space:pre-wrap}small{color:#a9bccb}</style>
    <h1>5814 ATS 운영 모니터</h1><p>Rust 전략 코어 · Windows / Ubuntu</p>
    <small>조회 전용 · 5초마다 저장 상태 갱신. 계좌 API 조회나 주문을 실행하지 않습니다.<br>
    표시된 RUNNING은 저장된 제어 상태입니다. 정상 운영 여부는 프로세스·계좌 대사와 함께 확인해야 합니다.</small>
    <div id="cards"></div><script>async function refresh(){try{const r=await fetch('/status',{cache:'no-store'});if(!r.ok)throw Error();const d=await r.json();
    document.getElementById('cards').replaceChildren(...d.map(x=>{const e=document.createElement('div');e.className='card';const h=document.createElement('h2');
    h.textContent=x.market==='domestic'?'국내 ATS':'미국 ATS';const p=document.createElement('pre');p.textContent=JSON.stringify(x,null,2);e.append(h,p);return e;}));
    }catch(e){document.getElementById('cards').textContent='상태 읽기 실패 — 정상 여부 확인 필요';}}refresh();setInterval(refresh,5000);</script></html>'''.encode()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/', '/status'):
                self.send_error(404)
                return
            try:
                data = html if self.path == '/' else json.dumps([state(m) for m in ('domestic','us')]).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8' if self.path == '/' else 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_error(503, 'Local status unavailable')
        def log_message(self, *args):
            pass
    print(f'Read-only monitor: http://127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


def run(market, allow_live):
    if not allow_live:
        raise RuntimeError('Live execution requires --allow-live after exclusive-account cutover approval')
    approval = INSTALL / 'portable/LIVE_APPROVAL.json'
    if not approval.is_file():
        raise RuntimeError('Local LIVE_APPROVAL.json is missing; package installation never activates live trading')
    receipt = json.loads(approval.read_text(encoding='utf-8'))
    if receipt.get('host') != platform.node() or receipt.get('exclusive_account_confirmed') is not True:
        raise RuntimeError('Exclusive host approval required')
    core = binary()
    if (receipt.get('core_sha256') != hashlib.sha256(core.read_bytes()).hexdigest()
            or receipt.get('source_sha256') != source_hash()):
        raise RuntimeError('Approved Rust binary hash mismatch')
    current = state(market)
    if current.get('control') != 'RUNNING' or not current.get('armed') or current.get('database') != 'OK':
        raise RuntimeError('Existing STOP/arming state prevents startup; no state changed')
    from rust_strategy import Worker, RustStrategyEngine
    from src.platform_support import FileLock
    # Additional per-user lock for both markets, independent of installation folder.
    from src.config import load_secrets
    account = load_secrets(ATS).kiwoom_expected_account
    if not account:
        raise RuntimeError('Expected account is missing')
    fingerprint = hashlib.sha256(account.encode()).hexdigest()
    lock = Path.home() / '.kiwoom5814-locks' / (fingerprint + '-' + market + '.lock')
    with FileLock(lock):
        worker = Worker(core)
        import src.strategy.engine as engine
        original_engine = engine.StrategyEngine
        original_cwd, original_argv = Path.cwd(), sys.argv
        original_factory = None
        original_single_instance = None
        try:
            engine.StrategyEngine = lambda settings: RustStrategyEngine(settings, worker)
            # Import only after injection; existing journal, schedule, reconciliation,
            # account checks and the original same-installation single-instance lock stay in charge.
            name = 'live_service' if market == 'domestic' else 'us_live_service'
            service = importlib.import_module('scripts.' + name)
            original_factory = getattr(service, 'StrategyEngine', None)
            original_single_instance = service.SingleInstance

            def native_strategy(settings, store=None):
                if store is not None:
                    store.set_system_value('strategy_engine_backend', 'rust_checked')
                    store.set_system_value('strategy_engine_worker_pid', str(worker.process.pid))
                return RustStrategyEngine(settings, worker)

            service.StrategyEngine = native_strategy
            basename = 'kiwoom5814_live' if market == 'domestic' else 'kiwoom5814_us_live'
            with service.SingleInstance(ATS / 'data' / (basename + '.lock')):
                service.SingleInstance = lambda path: nullcontext()  # Already held for this entire invocation.
                pid = ATS / 'data' / (basename + '.pid')
                temporary = pid.with_suffix('.pid.tmp')
                temporary.write_text(str(os.getpid()), encoding='ascii')
                temporary.replace(pid)
                try:
                    sys.argv = [name + '.py', 'run']
                    os.chdir(ATS)
                    service.main()
                finally:
                    if pid.exists() and pid.read_text().strip() == str(os.getpid()):
                        pid.unlink()
        finally:
            if original_single_instance is not None:
                service.SingleInstance = original_single_instance
                if original_factory is None:
                    del service.StrategyEngine
                else:
                    service.StrategyEngine = original_factory
            engine.StrategyEngine = original_engine
            os.chdir(original_cwd)
            sys.argv = original_argv
            worker.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    s = commands.add_parser('dashboard'); s.add_argument('--port', type=int, default=5814)
    s = commands.add_parser('run'); s.add_argument('--market', choices=('domestic','us'), required=True)
    s.add_argument('--allow-live', action='store_true')
    a = p.parse_args()
    if a.command == 'doctor':
        result = doctor(); print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['local_imports_ok'] else 1
    if a.command == 'dashboard':
        serve(a.port)
    if a.command == 'run':
        run(a.market, a.allow_live)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('Portable ATS refused operation: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
