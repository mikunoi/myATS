"""Linux desktop integration kept separate from Windows startup and tray code."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess


def preferences_dir():
    path = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'Kiwoom5814Dashboard'
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return path


def read_preferences():
    try:
        value = json.loads((preferences_dir() / 'preferences.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preferences(value):
    target = preferences_dir() / 'preferences.json'
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(target)


def startup_enabled(_prefs):
    return False


def ensure_startup(_root, _prefs):
    return None


def set_startup(_enabled, _root, prefs):
    save_preferences(dict(prefs, minimize_to_tray=False, start_with_windows=False))


def create_tray(*_args):
    return None


def run_elevated(*_args):
    raise OSError('Linux 설치는 setup-steamos.sh를 사용하세요.')


def open_api_settings(root):
    path = Path(root) / '.env'
    if not path.is_file():
        raise OSError('설치 안내에 따라 개인 .env를 배치하세요.')
    editor = shlex.split(os.environ.get('VISUAL', ''))
    if not editor:
        if kate := shutil.which('kate'):
            editor = [kate, '--new', '--encoding', 'UTF-8']
        elif kwrite := shutil.which('kwrite'):
            editor = [kwrite, '--encoding', 'UTF-8']
        else:
            raise OSError('그래픽 텍스트 편집기를 찾을 수 없습니다.')
    subprocess.Popen([*editor, str(path)], start_new_session=True)


def lock_dashboard():
    handle = (preferences_dir() / 'dashboard.lock').open('a+b')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle
