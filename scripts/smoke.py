"""Exercise a fresh install and its stable local data path with no broker credentials."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

installer, application, version = sys.argv[1:]
with TemporaryDirectory(prefix='github-installer-test-') as scratch:
    home = Path(scratch)
    env = {**os.environ, 'XDG_DATA_HOME': scratch, 'LOCALAPPDATA': scratch}
    command = [str(Path(installer).resolve()), '--install-update']
    subprocess.run(command, env=env, check=True, timeout=540)
    root = home / application
    assert (root / 'current').read_text().strip() == version
    if application == 'myATS':
        settings = root / 'workspace/.env'
        assert settings.read_text() == ''
        settings.write_text('# synthetic settings\n')
    subprocess.run(command, env=env, check=True, timeout=540)
    if application == 'myATS':
        assert settings.read_text() == '# synthetic settings\n'
    release = root / 'versions' / version
    python = release / ('runtime/python.exe' if os.name == 'nt' else 'runtime/bin/python')
    if os.name == 'nt':
        env['PATH'] = os.pathsep.join([str(release / 'runtime/Library/bin'), env.get('PATH','')])
    subprocess.run([str(python), '-c', 'import tkinter, PIL, openpyxl, cryptography, websockets'], check=True, env=env)
