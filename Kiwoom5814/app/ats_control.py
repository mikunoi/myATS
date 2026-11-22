"""Explicit user-triggered lifecycle control, using the existing 5814 scripts."""
import json
import os
from pathlib import Path
import subprocess
import sys


def invoke(root, action, runner=subprocess.run):
    if action not in ('Status', 'Start', 'Stop'):
        raise ValueError('Unsupported ATS action')
    if os.name != 'nt':
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'portable'))
        from linux_service import request
        return request(root, action, arm_stopped=(action == 'Start'))
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass',
               '-File', str(Path(__file__).with_suffix('.ps1')), '-Action', action, '-Root', str(Path(root).resolve())]
    result = runner(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=15 if action == 'Status' else 180)
    if result.returncode:
        raise RuntimeError('ATS 상태를 확인하지 못했습니다.' if action == 'Status' else 'ATS 제어 실패 또는 일부만 처리됨. 상태를 확인하세요.')
    parsed = json.loads(result.stdout)
    if action == 'Stop' and runner is subprocess.run:
        from data_reader import read_market
        if any(read_market(root, market).get('control') != 'STOP' for market in ('domestic','us')):
            raise RuntimeError('ATS 중지 상태가 아직 확인되지 않았습니다.')
    return parsed


def summarize(processes, markets):
    by_market = {p['market']: p for p in processes}
    if any(not by_market.get(m, {}).get('verified') for m in ('domestic', 'us')):
        return 'unknown'
    active = [by_market[m].get('running', False) for m in ('domestic', 'us')]
    controls = {m['market']: m.get('control') for m in markets}
    if all(active):
        return 'running' if all(controls.get(m) == 'RUNNING' and
                                next((row.get('operating', False) for row in markets if row['market'] == m), False)
                                for m in ('domestic', 'us')) else 'not_ready'
    if any(active):
        return 'partial'
    return 'stopped'
