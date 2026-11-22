"""Append a relocatable runtime and explicitly selected app sources to the Rust installer."""
from copy import copy
import posixpath
import shutil
import stat
import struct
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZIP_DEFLATED

app, binary, runtime, output = sys.argv[1:]
with TemporaryDirectory() as directory:
    payload = Path(directory) / 'payload.zip'
    with ZipFile(runtime) as environment, ZipFile(payload, 'w', ZIP_DEFLATED, compresslevel=2) as archive:
        for member in environment.infolist():
            if not member.is_dir() and member.filename != 'lib/terminfo':
                actual = member
                for _ in range(16):
                    if not stat.S_ISLNK(actual.external_attr >> 16):
                        break
                    link = environment.read(actual).decode('utf-8')
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(actual.filename), link))
                    if target.startswith(('/', '../')) or target == '..':
                        raise ValueError('Runtime symlink outside the bundle')
                    actual = environment.getinfo(target)
                else:
                    raise ValueError('Cyclic runtime symlink')
                info = copy(actual)
                info.filename = 'runtime/' + member.filename
                with environment.open(actual) as source:
                    with archive.open(info, 'w', force_zip64=True) as sink:
                        shutil.copyfileobj(source, sink)
        roots = ('Kiwoom5814', 'fonts') if app == 'myATS' else ('ui', 'assets', 'licenses', 'LICENSE', 'fonts')
        for name in roots:
            root = Path(name)
            files = root.rglob('*') if root.is_dir() else (root,)
            for path in sorted(files):
                if not path.is_file() or path.is_symlink() or '__pycache__' in path.parts:
                    continue
                if path.name.startswith('.') or 'target' in path.parts or 'tests' in path.parts:
                    continue
                if path.suffix not in ('.py','.json','.png','.ico','.md','.sql','.txt','.toml','.lock','.rs','.exe','.otf') and path.name not in ('LICENSE','kiwoom5814-core'):
                    continue
                if 'data' in path.parts or 'reports' in path.parts or path.name == 'notion_5814.mapping.json':
                    continue
                archive.write(path)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('wb') as sink, open(binary, 'rb') as executable, payload.open('rb') as data:
        shutil.copyfileobj(executable, sink)
        shutil.copyfileobj(data, sink)
        sink.write(struct.pack('<Q', payload.stat().st_size))
        sink.write(b'MKN-APP-PACK-v1!')
    destination.chmod(0o755)
