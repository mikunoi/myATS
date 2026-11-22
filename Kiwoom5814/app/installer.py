"""Owned-file installation with staging, preservation and rollback."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

OWNER = 'kiwoom5814-dashboard-v1'
MANIFEST = '.dashboard-install.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_target(target, source):
    raw_target = Path(target).absolute()
    for ancestor in (raw_target, *raw_target.parents):
        if ancestor.is_symlink() or (hasattr(ancestor, 'is_junction') and ancestor.is_junction()):
            raise ValueError('연결 폴더에는 설치할 수 없습니다.')
    target, source = raw_target.resolve(), Path(source).resolve()
    if target == Path(target.anchor) or target == source or target in source.parents or source in target.parents:
        raise ValueError('설치 원본과 분리된 전용 폴더를 선택하세요.')
    if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
        raise ValueError('연결 폴더에는 설치할 수 없습니다.')
    # Only empty or explicitly owned directories are eligible.
    if target.exists() and any(target.iterdir()):
        try:
            manifest = json.loads((target / MANIFEST).read_text(encoding='utf-8'))
            if manifest.get('owner') != OWNER:
                raise ValueError()
        except (OSError, ValueError):
            raise ValueError('이 설치기가 만든 폴더가 아닙니다. 빈 전용 폴더를 선택하세요.') from None
        for data_dir in (target / 'data', target / 'ats/data'):
            if data_dir.exists() and (any(data_dir.glob('*.autoresume')) or any(data_dir.glob('*.pid'))):
                # Conservative: even stale PID files need an explicit operator check.
                # Reinstallation never stops or attempts to recover a live ATS.
                raise ValueError('ATS 실행/자동복구 기록이 있습니다. 해당 설치본의 ATS 중지를 확인한 뒤 재설치하세요.')
        return target, manifest
    return target, None


def install(source, target, *, allow_reinstall=False):
    import msvcrt
    destination, _ = validate_target(target, source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / (destination.name + '.install.lock')
    with lock_path.open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError('이 폴더에 다른 설치가 진행 중입니다.') from None
        try:
            return _install(source, target, allow_reinstall=allow_reinstall)
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _install(source, target, *, allow_reinstall=False):
    source = Path(source).resolve()
    target, previous = validate_target(target, source)
    if previous and not allow_reinstall:
        raise ValueError('재설치 동의가 필요합니다.')
    # A distributor-created allowlist, never a recursive copy of an ATS workspace.
    package = json.loads((source / 'package.json').read_text(encoding='utf-8'))
    files = package['files']
    for rel, checksum in files.items():
        path = (source / rel).resolve()
        if Path(rel).is_absolute() or '..' in Path(rel).parts or not path.is_relative_to(source) or not path.is_file() or digest(path) != checksum:
            raise ValueError('설치 파일 검증에 실패했습니다.')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / (target.name + '.stage-' + uuid.uuid4().hex)
    backup = target.parent / (target.name + '.backup-' + uuid.uuid4().hex)
    stage.mkdir()
    moved = False
    try:
        if target.exists():
            # Reject links before copying/deleting any tree; no traversal outside ownership.
            for item in target.rglob('*'):
                if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
                    raise ValueError('기존 설치 폴더의 연결 파일을 제거한 뒤 다시 시도하세요.')
            shutil.copytree(target, stage, dirs_exist_ok=True)
        old_files = (previous or {}).get('files', {})
        for rel, checksum in old_files.items():
            path = (stage / rel).resolve()
            if not path.is_relative_to(stage):
                raise ValueError('잘못된 설치 기록입니다.')
            if path.is_file():
                if digest(path) != checksum:
                    preserved = stage / 'preserved' / uuid.uuid4().hex / rel
                    preserved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, preserved)
                path.unlink()
        for rel in files:
            dest = stage / rel
            # Never silently overwrite a user-created file at a newly owned path.
            if dest.exists():
                preserved = stage / 'preserved' / uuid.uuid4().hex / rel
                preserved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, preserved)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / rel, dest)
        shutil.copy2(source / 'package.json', stage / 'package.json')
        (stage / MANIFEST).write_text(json.dumps(dict(owner=OWNER, version=package['version'], files=files), indent=2), encoding='utf-8')
        if target.exists():
            os.replace(target, backup)
            moved = True
        os.replace(stage, target)
        # Backup is kept so a user can recover custom sources/private state.
        return dict(target=str(target), backup=str(backup) if moved else None)
    except Exception:
        if moved and not target.exists():
            os.replace(backup, target)
        raise
    finally:
        if stage.exists() and stage.parent == target.parent and stage.name.startswith(target.name + '.stage-'):
            shutil.rmtree(stage)
