"""SteamOS user-service controls. Installation and status never arm trading."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys

MARKETS = {
    'domestic': ('kiwoom5814_live.sqlite3', 'live_execution_armed'),
    'us': ('kiwoom5814_us_live.sqlite3', 'us_execution_armed'),
}


def unit_stem(root):
    identifier = hashlib.sha256(os.fsencode(Path(root).resolve())).hexdigest()[:16]
    return f'kiwoom5814-{identifier}'


def control(*args):
    result = subprocess.run(['systemctl', '--user', *args], capture_output=True,
                            text=True, timeout=45)
    if result.returncode:
        raise RuntimeError(f'systemctl {args[0]} failed. Check the user-service journal.')
    return result.stdout


def persist_stop(root, market):
    filename, armed = MARKETS[market]
    db = root / 'data' / filename
    if db.is_file():
        with closing(sqlite3.connect(db.as_uri() + '?mode=rw', uri=True, timeout=5)) as conn, conn:
            conn.execute("UPDATE system_state SET value='STOP' WHERE key='control'")
            conn.execute("INSERT OR REPLACE INTO system_state(key,value) VALUES (?, '0')", (armed,))


def request(root, action, *, arm_stopped=False):
    root = Path(root).resolve()
    stem = unit_stem(root)
    units = [f'{stem}@{market}.service' for market in MARKETS]
    if action == 'Status':
        rows = []
        for market, unit in zip(MARKETS, units):
            fields = dict(line.split('=', 1) for line in
                          control('show', '--property=LoadState,ActiveState,MainPID', unit).splitlines()
                          if '=' in line)
            state = fields.get('ActiveState')
            rows.append(dict(market=market, verified=fields.get('LoadState') == 'loaded',
                             running=state in ('active', 'activating') and
                             int(fields.get('MainPID', '0')) > 0))
        return rows
    import manage
    if root != manage.ATS:
        raise RuntimeError('ATS controls require this installation’s ATS folder.')
    from src.platform_support import FileLock
    with FileLock(root / 'data/linux-control.lock'):
        return change_state(root, action, stem, units, manage, arm_stopped)


def change_state(root, action, stem, units, manage, arm_stopped):
    if action == 'Start':
        check_approval()
        if not (root / '.env').is_file():
            raise RuntimeError('Place the local ATS .env before starting.')
        states = {market: manage.state(market) for market in MARKETS}
        arm = []
        for market in MARKETS:
            state = states[market]
            if state.get('database') != 'OK':
                raise RuntimeError('Check the transferred database before starting.')
            if state.get('control') == 'RUNNING' and state.get('armed'):
                continue
            if (not arm_stopped or state.get('control') != 'STOP' or state.get('armed')
                    or state.get('active_order_intents') or state.get('malformed_intents')):
                raise RuntimeError('ATS needs a review of STOP and the order journal. See START_HERE.md.')
            arm.append(market)
        armed_here = []
        try:
            for market in arm:
                script = 'live_service.py' if market == 'domestic' else 'us_live_service.py'
                armed_here.append(market)
                completed = subprocess.run([sys.executable, str(root / 'scripts' / script), 'arm'],
                                           cwd=root, capture_output=True, timeout=420)
                if completed.returncode:
                    raise RuntimeError(f'{market} preflight failed. Check the ATS log.')
            control('start', *units)
        except Exception:
            for market in armed_here:
                persist_stop(root, market)
            if armed_here:
                control('stop', *(f'{stem}@{market}.service' for market in armed_here))
            raise
    elif action == 'Stop':
        errors = []
        for market in MARKETS:
            try:
                persist_stop(root, market)
            except sqlite3.Error:
                errors.append(market)
        control('stop', *units)
        if errors:
            raise RuntimeError('Units stopped, but STOP could not be saved for all markets.')
    else:
        raise ValueError('Unsupported action')
    return request(root, 'Status')


def approval_values():
    import manage
    return dict(host=platform.node(), exclusive_account_confirmed=True,
                core_sha256=hashlib.sha256(manage.binary().read_bytes()).hexdigest(),
                source_sha256=manage.source_hash())


def check_approval():
    import manage
    path = manage.INSTALL / 'portable/LIVE_APPROVAL.json'
    if not path.is_file() or json.loads(path.read_text(encoding='utf-8')) != approval_values():
        raise RuntimeError('Current host, code and binary require cutover approval.')


def main():
    import manage
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('unit-name', 'status', 'start', 'stop', 'approve'))
    parser.add_argument('--confirm-exclusive-and-reconciled', action='store_true')
    args = parser.parse_args()
    if args.action == 'unit-name':
        print(unit_stem(manage.ATS))
    elif args.action == 'approve':
        if not args.confirm_exclusive_and_reconciled:
            parser.error('Confirm the cutover prerequisites in START_HERE.md first.')
        for market in MARKETS:
            state = manage.state(market)
            if (state.get('database') != 'OK' or state.get('control') != 'STOP'
                    or state.get('armed') or state.get('active_order_intents') or state.get('malformed_intents')):
                raise RuntimeError('Verified STOP and a clean local order journal are required.')
        if not (manage.ATS / '.env').is_file():
            raise RuntimeError('Local .env is required.')
        output = manage.INSTALL / 'portable/LIVE_APPROVAL.json'
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps(approval_values(), indent=2), encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(output)
        print('Local approval recorded. Trading remains STOP.')
    else:
        print(json.dumps(request(manage.ATS, args.action.title())))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
