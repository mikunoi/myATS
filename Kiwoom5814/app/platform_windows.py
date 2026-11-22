"""Dashboard preferences, tray, and opt-in Windows startup registration."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import winreg

APP_ID = 'Kiwoom5814Dashboard'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
STARTUP_FILE = 'Kiwoom5814Startup.cmd'
STARTUP_LOCK = threading.RLock()


def preferences_dir():
    path = Path(os.environ['LOCALAPPDATA']) / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_preferences():
    try:
        return json.loads((preferences_dir() / 'preferences.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_preferences(value):
    target = preferences_dir() / 'preferences.json'
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, target)


def startup_command(root):
    if getattr(sys, 'frozen', False):
        args = [sys.executable, '--root', str(root), '--autostart']
    else:
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        args = [str(pythonw), str(Path(__file__).with_name('launcher.py')), '--root', str(root), '--autostart']
    return subprocess.list2cmdline(args)


def _run_value(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return ''


def startup_enabled(preferences=None):
    preferences = read_preferences() if preferences is None else preferences
    if 'start_with_windows' in preferences:
        return bool(preferences['start_with_windows'])
    return _startup_path().is_file() or bool(_run_value(APP_ID))


def _startup_path():
    return Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup' / STARTUP_FILE


def _watchdog_alive(root):
    try:
        pid = int((Path(root) / 'data/kiwoom5814_watch.pid').read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    kernel.GetExitCodeProcess.restype = ctypes.c_bool
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_uint()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def _ats_startup_registered(root):
    script = str(Path(root).resolve() / 'scripts/watch_5814_live.ps1').casefold()
    command = startup_command(root).casefold()
    startup = _startup_path()
    try:
        content = startup.read_text(encoding='utf-8').casefold()
        return (script in content and command in content
                and _watchdog_alive(root))
    except (OSError, UnicodeError):
        return False


def _ats_script(root, name):
    script = Path(root).resolve() / 'scripts' / name
    if not script.is_file():
        raise OSError('5814 ATS 자동시작 스크립트를 찾지 못했습니다.')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                             '-WindowStyle', 'Hidden', '-File', str(script)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    if result.returncode:
        raise OSError('5814 ATS 자동시작 등록을 처리하지 못했습니다.')


def ensure_startup(root):
    with STARTUP_LOCK:
        preferences = read_preferences()
        if not startup_enabled(preferences):
            return
        root = Path(preferences.get('ats_root') or root).resolve()
        command = startup_command(root)
        if preferences.get('startup_dashboard_command') != command:
            preferences['startup_dashboard_command'] = command
            save_preferences(preferences)
        _ats_script(root, 'ensure_5814_autostart.ps1')
        if not _ats_startup_registered(root):
            _ats_script(root, 'install_5814_autostart.ps1')
            if not _ats_startup_registered(root):
                raise OSError('5814 ATS 자동시작 등록을 확인하지 못했습니다.')


def set_startup(enabled, root, preferences=None):
    with STARTUP_LOCK:
        root = Path(root).resolve()
        previous = read_preferences()
        updated = dict(previous if preferences is None else preferences)
        if enabled:
            updated['start_with_windows'] = True
            updated['ats_root'] = str(root)
            updated['startup_dashboard_command'] = startup_command(root)
            save_preferences(updated)
            try:
                _ats_script(root, 'install_5814_autostart.ps1')
                if not _ats_startup_registered(root):
                    raise OSError('5814 ATS 자동시작 등록을 확인하지 못했습니다.')
            except Exception:
                save_preferences(previous)
                raise
        else:
            _ats_script(root, 'uninstall_5814_autostart.ps1')
            updated['start_with_windows'] = False
            updated.pop('startup_dashboard_command', None)
            save_preferences(updated)


def run_elevated(args):
    # UAC prompt is native; 1223 / <=32 signals cancellation or launch failure.
    params = subprocess.list2cmdline(args if getattr(sys, 'frozen', False) else [str(Path(__file__).with_name('launcher.py')), *args])
    result = ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, params, None, 0)
    if result <= 32:
        raise OSError('관리자 설치가 취소되었거나 실행되지 않았습니다.')


def create_tray(show, quit_app):
    import pystray
    from PIL import Image, ImageDraw
    icon = Image.open(Path(__file__).parent/'assets/ats-icon.png').convert('RGBA')
    tray = pystray.Icon(APP_ID, icon, '키움(5814) ATS', menu=pystray.Menu(
        pystray.MenuItem('대시보드 열기', lambda: show(), default=True),
        pystray.MenuItem('대시보드 종료', lambda: quit_app())))
    tray.run_detached()
    return tray


def open_api_settings(ats_root, launcher=None):
    """Open the selected ATS .env locally; never load or log its secret values."""
    root=Path(ats_root).resolve()
    if not (root/'config/default.json').is_file() or (not os.environ.get('DISTRIBUTION_LAUNCHER') and not (root/'src/config.py').is_file()):
        raise ValueError('ATS 폴더를 먼저 선택하세요.')
    path=root/'.env'
    if path.is_symlink():
        raise ValueError('연결 파일은 API 설정에서 열 수 없습니다.')
    if not path.exists():
        with path.open('x',encoding='utf-8') as file:
            file.write('# Fill locally. Keep this file private.\n'
                       'KIWOOM_EXPECTED_ACCOUNT=\nKIWOOM_PROD_APP_KEY=\nKIWOOM_PROD_SECRET_KEY=\n'
                       'TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\nTELEGRAM_USER_ID=\nNOTION_TOKEN=\n')
    run=launcher or subprocess.Popen
    run([str(Path(os.environ.get('WINDIR','C:/Windows'))/'System32/notepad.exe'),str(path)],shell=False)
    return path
