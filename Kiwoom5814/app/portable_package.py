"""Repackage the current editable installation without private runtime state."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime


def safe_file(base, rel):
    rel=Path(rel)
    if rel.is_absolute() or '..' in rel.parts:raise ValueError('잘못된 패키지 경로입니다.')
    path=base/rel
    for part in (path,*path.parents):
        if part==base.parent:break
        if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):
            raise ValueError('연결 파일은 패키지에 포함할 수 없습니다.')
    if not path.is_file():raise ValueError('필수 설치 파일이 없습니다: '+rel.as_posix())
    return path


def create_package(source, ats_root, app_root, destination, version):
    source,ats_root,app_root=map(lambda p:Path(p).resolve(),(source,ats_root,app_root))
    destination=Path(destination).absolute()
    manifest=json.loads((source/'package.json').read_text(encoding='utf-8'))
    selected={}
    # Only shipped runtime binaries and explicit configuration/templates. Never copy a workspace tree.
    for rel in manifest['files']:
        path=Path(rel)
        if rel=='Kiwoom5814.exe' or rel.startswith('_internal/') or rel.startswith('ats/runtime/') or rel in ('README.md','build-info.json','ats/.env.example'):
            if '__pycache__' in path.parts or path.suffix in ('.pyc','.log'):continue
            candidate=safe_file(source,rel)
            # Runtime is immutable; edited Python application files are selected separately below.
            if hashlib.sha256(candidate.read_bytes()).hexdigest()!=manifest['files'][rel]:
                raise ValueError('실행 구성 파일이 변경되었습니다. 개발 빌드가 필요합니다.')
            selected[rel]=candidate
        elif rel=='app/ats_control.ps1':
            selected[rel]=safe_file(app_root,'ats_control.ps1')
        elif rel.startswith('ats/scripts/') or rel in ('ats/config/default.json','ats/config/codex_recovery_schema.json','ats/requirements.txt','ats/pyproject.toml','ats/src/database/schema.sql'):
            selected[rel]=safe_file(ats_root,rel[4:])
    for base,prefix,pattern in [(app_root,'app','*.py'),(app_root/'assets','app/assets','*'),(ats_root/'src','ats/src','*.py'),(ats_root/'handoff','ats/handoff','*.md')]:
        for path in (base.glob(pattern) if prefix=='app' else base.rglob(pattern)):
            rel=path.relative_to(base)
            if prefix=='app' and (path.name.startswith('test_') or path.name in ('build_package.py','verify_package.py')):continue
            if not path.is_file() or any(p.startswith('.') or p=='__pycache__' for p in rel.parts):continue
            if prefix=='app/assets' and path.suffix.lower() not in ('.png','.ico'):continue
            selected[prefix+'/'+rel.as_posix()]=safe_file(base,rel)
    selected['app/kr_holidays.json'] = safe_file(app_root, 'kr_holidays.json')
    for name in ('Kiwoom5814.exe','ats/handoff/READ_FIRST.md','ats/handoff/DECISIONS_AND_TASKS.md','app/app.py'):
        if name not in selected:raise ValueError('필수 설치 파일이 없습니다: '+name)
    # The current ATS instructions are canonical even when a developer workspace is selected.
    instructions=safe_file(ats_root,'AGENTS.md').read_text(encoding='utf-8')
    if '## 첫 작업' not in instructions:
        instructions=safe_file(ats_root.parent,'AGENTS.md').read_text(encoding='utf-8')
    instructions=instructions.replace('handoff/','ats/handoff/').replace('ats/ats/handoff/','ats/handoff/')
    generated={'AGENTS.md':instructions.encode('utf-8')}
    for child,doc in [('ats','handoff'),('app','../ats/handoff')]:
        generated[child+'/AGENTS.md']=(f'Read ../AGENTS.md and {doc}/READ_FIRST.md before working. Follow all parent rules, including updating {doc}/DECISIONS_AND_TASKS.md and related handoff documents before final replies for new requirements, operating decisions, unfinished work and code changes. These rules remain in effect on every PC. Never include secrets.\n').encode('utf-8')
    if destination.suffix.lower()!='.zip' or destination.resolve() in {p.resolve() for p in selected.values()}:
        raise ValueError('별도의 ZIP 파일로 저장하세요.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.ats-package-',suffix='.tmp',dir=destination.parent);os.close(fd)
    try:
        hashes={}
        with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as z:
            for rel,path in sorted(selected.items()):
                data=path.read_bytes();hashes[rel]=hashlib.sha256(data).hexdigest();z.writestr('Kiwoom5814/'+rel,data)
            for rel,data in generated.items():
                hashes[rel]=hashlib.sha256(data).hexdigest();z.writestr('Kiwoom5814/'+rel,data)
            z.writestr('Kiwoom5814/package.json',json.dumps(dict(version=version,created_at=datetime.now().isoformat(),files=hashes),ensure_ascii=False,indent=2))
        os.replace(temp,destination)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return str(destination)


def _private_file(ats_root, relative):
    path = ats_root / relative
    for part in (path, *path.parents):
        if part == ats_root.parent:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise ValueError('개인 자료에 연결 파일이 있어 백업할 수 없습니다.')
    if not path.is_file():
        raise ValueError('필수 개인 자료가 없습니다: ats/' + relative.as_posix())
    return path


def _snapshot_database(source, destination):
    # SQLite backup includes committed WAL pages without copying live WAL/SHM files.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)) as live:
        with closing(sqlite3.connect(destination)) as snapshot:
            live.backup(snapshot, pages=256, sleep=0.25)
            if snapshot.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('거래 DB 백업 무결성 확인에 실패했습니다.')


def create_private_package(ats_root, destination):
    ats_root, destination = Path(ats_root).resolve(), Path(destination).absolute()
    required = [Path('.env'), Path('data/kiwoom5814_live.sqlite3'),
                Path('data/kiwoom5814_us_live.sqlite3')]
    optional = [Path('config/notion_5814.mapping.json'),
                Path('reports/output/holding_adoption_draft.json'),
                Path('reports/input/portfolio_cashflows.json')]
    selected = {rel: _private_file(ats_root, rel) for rel in required}
    for rel in optional:
        if (ats_root / rel).exists():
            selected[rel] = _private_file(ats_root, rel)
    if destination.suffix.lower() != '.zip' or destination.resolve() in {p.resolve() for p in selected.values()}:
        raise ValueError('별도의 개인 ZIP 파일로 저장하세요.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep raw database snapshots in the local temp area, even if the user saves
    # the final archives in a synchronized folder.
    with tempfile.TemporaryDirectory(prefix='.ats-private-') as staging:
        staging = Path(staging)
        members = {}
        for rel, source in selected.items():
            if rel.suffix == '.sqlite3':
                snapshot = staging / rel.name
                _snapshot_database(source, snapshot)
                source = snapshot
            members['ats/' + rel.as_posix()] = source
        readme = (
            '키움 5814 개인 상태 이전 자료\n'
            '이 ZIP은 암호화되지 않았습니다. 인증정보와 거래 DB가 들어 있으므로 안전하게 보관하세요.\n'
            '설치 ZIP과 별도로 보관하고 공개 저장소나 공유 폴더에 올리지 마세요.\n'
            '복원 전 기존 PC와 새 PC의 동일 계좌 ATS 및 자동복구를 중지하고 미체결을 확인하세요.\n'
            '설치가 끝난 새 PC의 Kiwoom5814 폴더에 ats/ 아래 파일을 대응 경로로 복사하세요.\n'
            '실행 중인 SQLite DB를 덮어쓰지 마세요. -wal/-shm, PID/lock, autoresume, DPAPI 토큰은 복사하지 않습니다.\n'
            '국내/해외 DB는 각각 SQLite backup API로 저장했으며 두 DB의 동시 원자적 시점은 보장하지 않습니다.\n'
            '복원 후 인증, 등록 IP, 계좌/보유/미체결, STOP 상태를 확인한 뒤 운영을 재개하세요.\n'
        )
        archive = staging / 'private.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, source in sorted(members.items()):
                z.write(source, name)
            z.writestr('PRIVATE_RESTORE_README.txt', readme)
        fd, temp = tempfile.mkstemp(prefix='.ats-private-', suffix='.tmp', dir=destination.parent)
        os.close(fd)
        try:
            shutil.copyfile(archive, temp)
            os.replace(temp, destination)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return str(destination)


def create_transfer_packages(source, ats_root, app_root, destination, version):
    install_path = Path(destination).absolute()
    private_path = install_path.with_name(install_path.stem + '-PRIVATE-State.zip')
    if install_path == private_path or install_path.suffix.lower() != '.zip':
        raise ValueError('설치 ZIP 경로를 선택하세요.')
    if install_path.exists() or private_path.exists():
        raise ValueError('같은 이름의 ZIP이 이미 있습니다. 새 이름을 선택하세요.')
    install_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ats-transfer-', dir=install_path.parent) as staging:
        staging = Path(staging)
        staged_install = staging / 'install.zip'
        staged_private = staging / 'private.zip'
        create_private_package(ats_root, staged_private)
        create_package(source, ats_root, app_root, staged_install, version)
        # Both archives are complete before either result appears in the chosen folder.
        os.replace(staged_private, private_path)
        try:
            os.replace(staged_install, install_path)
        except Exception:
            private_path.unlink()
            raise
    return str(install_path), str(private_path)
