"""OS file locks; Windows uses the existing byte-range lock protocol."""
import os
from pathlib import Path
import time

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


class FileLock:
    def __init__(self, path, timeout=0):
        self.path, self.timeout, self.handle = Path(path), timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError('Lock path must not be a symlink')
        self.handle = self.path.open('a+b')
        if self.handle.tell() == 0:
            self.handle.write(b'0')
            self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            self.handle.seek(0)
            try:
                if os.name == 'nt':
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise RuntimeError('Lock unavailable; another process may be running') from None
                time.sleep(0.05)

    def __exit__(self, *args):
        if self.handle is not None:
            try:
                self.handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def linux_cipher():
    """Dedicated per-user key, never copied with the installation/token cache."""
    from cryptography.fernet import Fernet
    directory = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'kiwoom5814'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise PermissionError('Token key directory must be owned by this user with mode 700')
    key = directory / 'token.key'
    try:
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as output:
            output.write(Fernet.generate_key())
            output.flush()
            os.fsync(output.fileno())
    if key.is_symlink() or key.stat().st_uid != os.getuid() or key.stat().st_mode & 0o077:
        raise PermissionError('Token key must be owned by this user with mode 600')
    return Fernet(key.read_bytes())
