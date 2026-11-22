"""Start and stop one registered KIS worker from Linux."""
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys

from api_accounts import account_dir, private_dir, read_json
from ats_models import require_kis_model
from refresh_schedule import KST


def status_record(root, profile):
    try:
        result = read_json(account_dir(root, profile) / 'ats-status.json')
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def alive(root, profile):
    if os.name != 'posix' or not Path('/proc').is_dir():
        return False
    pid = status_record(root, profile).get('pid')
    if type(pid) is not int or pid <= 0:
        return False
    try:
        args = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\x00')
        path = os.fsencode(str(Path(__file__).with_name('kis_runtime.py').resolve()))
        root_arg = os.fsencode(str(Path(root).resolve()))
        return len(args) >= 4 and args[1:4] == [path, root_arg, profile.identity.encode()]
    except OSError:
        return False


def worker_status(root, profile):
    record = status_record(root, profile)
    if alive(root, profile):
        if (account_dir(root, profile)/'ats-stop').exists():
            return '정지 처리 중'
        return record.get('state','운용 확인 중')
    if record.get('error'):
        return '중지 · 점검 필요'
    return '설정됨 · 정지'


def start(root, profile, *, confirm_live=False):
    if os.name != 'posix':
        raise RuntimeError('이 설치본의 추가 ATS 실행은 SteamOS에서 지원합니다.')
    if not profile.ats or (profile.mode == 'live' and not confirm_live):
        raise ValueError('이 계좌의 ATS 실행 설정을 확인하세요.')
    require_kis_model(profile.model_id)
    from kis_signals import _imports
    _imports(root)
    from src.platform_support import FileLock
    root = Path(root).resolve()
    path = private_dir(account_dir(root, profile))
    with FileLock(path/'ats.lock'):
        if alive(root, profile):
            raise RuntimeError('이 계좌의 ATS가 실행 중입니다.')
        (path/'ats-stop').unlink(missing_ok=True)
        script = Path(__file__).with_name('kis_runtime.py').resolve()
        args = [sys.executable, str(script), str(root), profile.identity]
        if confirm_live:
            args.append('--confirm-live')
        from api_accounts import save_json
        process = subprocess.Popen(args, start_new_session=True, cwd=root,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        save_json(path/'ats-status.json',dict(pid=process.pid,state='시작 대기',
                                             at=datetime.now(KST).isoformat()))
    return process.pid


def stop(root, profile):
    path = private_dir(account_dir(root, profile))
    (path/'ats-stop').touch(mode=0o600, exist_ok=True)
