"""Local account profiles and account-scoped files for myATS."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from desktop_platform import preferences_dir

KIS_FIELDS = ('KIS_APP_KEY', 'KIS_APP_SECRET', 'KIS_CANO', 'KIS_ACNT_PRDT_CD')
KIS_ENV_FIELDS = (*KIS_FIELDS, 'KIS_HTS_ID', 'KIS_ALIAS', 'KIS_ENV')
ENV_SLOTS = re.compile('(?:' + '|'.join(KIS_ENV_FIELDS) + r')(_[1-9][0-9]{0,8})?')


def parse_env(lines):
    values = {}
    for line in lines:
        key, equals, value = line.partition('=')
        if equals and not key.lstrip().startswith('#'):
            value = value.strip()
            if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                value = value[1:-1]
            values[key.strip()] = value
    return values


def env_values(root):
    try:
        lines = (Path(root) / '.env').read_text(encoding='utf-8-sig').splitlines()
    except FileNotFoundError:
        return {}
    return parse_env(lines)


def kis_slots(values):
    return sorted({match[1] or '' for key in values if (match := ENV_SLOTS.fullmatch(key))},
                  key=lambda suffix: int(suffix[1:] or 0))


def kis_env_account(values, suffix=''):
    key, secret, number, product = [values.get(name + suffix, '') for name in KIS_FIELDS]
    if not all((key, secret, number, product)):
        return None
    alias = values.get('KIS_ALIAS' + suffix) or ('한투 ' + suffix[1:] if suffix else '기본 한투')
    return Account(alias, values.get('KIS_ENV' + suffix) or 'live', number, product, key, secret,
                   values.get('KIS_HTS_ID' + suffix, ''))


def kis_env_accounts(values):
    profiles, invalid, seen = {}, [], set()
    for suffix in kis_slots(values):
        try:
            profile = kis_env_account(values, suffix)
        except ValueError:
            profile = None
        if profile is None or profile.identity in seen:
            if any(values.get(key + suffix) for key in KIS_FIELDS):
                invalid.append(suffix)
            continue
        profiles[suffix] = profile
        seen.add(profile.identity)
    return profiles, invalid


def validate_alias(alias):
    if not 1 <= len(alias) <= 24 or alias.splitlines() != [alias] or any(ord(char) < 32 for char in alias):
        raise ValueError('계좌 별칭을 1~24자로 입력하세요.')


def save_env_metadata(root, values, changes):
    path = (Path(root) / '.env').resolve(strict=True)
    before = path.read_bytes()
    lines = before.decode('utf-8-sig').splitlines(keepends=True)
    if parse_env(lines) != values:
        raise ValueError('.env 설정이 변경됐습니다. API 연결을 다시 눌러 주세요.')
    newline = '\r\n' if b'\r\n' in before else '\n'
    written = set()
    for index, line in enumerate(lines):
        key = line.partition('=')[0].strip()
        if key in changes:
            lines[index] = f'{key}="{changes[key]}"{newline}'
            written.add(key)
    if lines and not lines[-1].endswith(('\n', '\r')):
        lines[-1] += newline
    lines.extend(f'{key}="{value}"{newline}' for key, value in changes.items() if key not in written)
    encoding = 'utf-8-sig' if before.startswith(b'\xef\xbb\xbf') else 'utf-8'
    tmp = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as output:
            tmp = Path(output.name)
            output.write(''.join(lines).encode(encoding))
            output.flush()
            os.fsync(output.fileno())
        if path.read_bytes() != before:
            raise ValueError('.env 설정이 변경됐습니다. API 연결을 다시 눌러 주세요.')
        tmp.replace(path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def fingerprint(*parts):
    value = json.dumps(parts, separators=(',', ':')).encode()
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class Account:
    alias: str
    mode: str
    number: str = field(repr=False)
    product: str = field(repr=False)
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    hts_id: str = field(default='', repr=False)
    group: str = ''
    group_code: str = ''
    ats: bool = False
    enabled: bool = True
    model_id: str = ''

    def __post_init__(self):
        text = (self.alias, self.mode, self.number, self.product, self.app_key,
                self.app_secret, self.hts_id, self.group, self.group_code, self.model_id)
        if any(not isinstance(s, str) or any(ord(c) < 32 for c in s) for s in text):
            raise ValueError('입력 형식을 확인하세요.')
        validate_alias(self.alias)
        if self.mode not in ('live', 'demo'):
            raise ValueError('거래환경을 확인하세요.')
        if not re.fullmatch(r'[0-9]{8}', self.number) or not re.fullmatch(r'[0-9]{2}', self.product):
            raise ValueError('계좌 앞 8자리와 상품코드 2자리를 입력하세요.')
        if not all(0 < len(value) <= 256 for value in (self.app_key, self.app_secret)):
            raise ValueError('앱 키와 앱 시크릿을 입력하세요.')
        if self.hts_id and not re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', self.hts_id):
            raise ValueError('HTS ID를 확인하세요.')
        if (len(self.group) > 80 or
                (self.group_code and not re.fullmatch(r'[A-Za-z0-9]{1,16}', self.group_code))):
            raise ValueError('관심종목 그룹을 확인하세요.')
        if type(self.enabled) is not bool or type(self.ats) is not bool:
            raise ValueError('연결 설정을 확인하세요.')
        if self.model_id and not (
            re.fullmatch(r'[a-z0-9-]{1,64}', self.model_id) and self.group and self.group_code and self.hts_id
        ):
            raise ValueError('관심종목과 ATS 모델 설정을 확인하세요.')
        if self.ats and not (self.enabled and self.group and self.group_code and self.hts_id):
            raise ValueError('ATS 적용 전 관심종목 그룹을 확인하세요.')

    @property
    def identity(self):
        return fingerprint('kis', self.mode, self.number, self.product)

    @property
    def credentials_id(self):
        return fingerprint(self.identity, self.app_key, self.app_secret)

    @property
    def masked(self):
        return '••••' + self.number[-4:] + '-' + self.product


def directory(root):
    return preferences_dir() / 'private' / ('myats-' + fingerprint(str(Path(root).resolve()))[:20])


def private_dir(path):
    if path.is_symlink():
        raise OSError('연결 자료 경로를 확인하세요.')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'nt' and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise OSError('연결 자료 폴더 권한을 확인하세요.')
    return path


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


def save_json(path, data):
    private_dir(path.parent)
    temp = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
            temp = Path(file.name)
            json.dump(data, file, ensure_ascii=False, allow_nan=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def accounts(root):
    raw = read_json(directory(root) / 'accounts.json')
    if raw is None:
        return ()
    try:
        if raw['schema'] != 1 or not isinstance(raw['accounts'], list):
            raise ValueError()
        profiles = tuple(Account(**profile) for profile in raw['accounts'])
        if len({p.identity for p in profiles}) != len(profiles):
            raise ValueError()
        return profiles
    except (KeyError, TypeError, ValueError):
        raise ValueError('API 연결 설정 파일을 읽을 수 없습니다.') from None


def save_account(root, account, *, previous=None, allow_mode_change=False):
    profiles = list(accounts(root))
    env = env_values(root)
    for suffix in kis_slots(env):
        if (account.mode == (env.get('KIS_ENV' + suffix) or 'live')
                and account.number == env.get('KIS_CANO' + suffix)
                and account.product == env.get('KIS_ACNT_PRDT_CD' + suffix)):
            raise ValueError('.env에 이미 등록된 계좌입니다.')
    if any(p.identity == account.identity and p.identity != previous for p in profiles):
        raise ValueError('이미 등록된 계좌입니다.')
    if previous is None:
        profiles.append(account)
    else:
        for index, profile in enumerate(profiles):
            if profile.identity == previous:
                if previous != account.identity and not (
                    allow_mode_change and (profile.number, profile.product) == (account.number, account.product)
                    and not any((account.ats, account.group, account.group_code, account.model_id))
                ):
                    raise ValueError('다른 계좌는 새로 연결하세요.')
                profiles[index] = account
                break
        else:
            raise ValueError('등록된 계좌를 찾을 수 없습니다.')
    save_profiles(root, profiles)


def delete_account(root, identity):
    profiles = accounts(root)
    remaining = [profile for profile in profiles if profile.identity != identity]
    if len(remaining) == len(profiles):
        raise ValueError('등록된 계좌를 찾을 수 없습니다.')
    save_profiles(root, remaining)


def save_profiles(root, profiles):
    save_json(directory(root) / 'accounts.json', {'schema': 1, 'accounts': [asdict(p) for p in profiles]})


def account_dir(root, profile):
    return directory(root) / profile.identity
