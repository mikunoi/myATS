"""Account-specific watchlist discovery and storage for the default API connections."""
from pathlib import Path
import sys

from api_accounts import directory, env_values, read_json, save_json
from kis_managed import ManagedKIS
from kis_portfolio import KISError, PortfolioClient
from kis_snapshot import guard


def kiwoom_connection(root, values=None):
    source = str(Path(__file__).resolve().parent.parent / 'ats')
    if source not in sys.path:
        sys.path.insert(0, source)
    from src.config import Secrets
    if values is None:
        values = env_values(root)
    return values.get('KIWOOM_EXPECTED_ACCOUNT', ''), Secrets(
        kiwoom_app_key=values.get('KIWOOM_PROD_APP_KEY', ''),
        kiwoom_secret_key=values.get('KIWOOM_PROD_SECRET_KEY', ''))


def kiwoom_groups(root, connection):
    from src.broker.http import AuthTransport, KiwoomError
    from src.broker.kiwoom import KiwoomClient

    class WatchlistTransport(AuthTransport):
        def issue_token(self, *_args, **_kwargs):
            raise KiwoomError('DASHBOARD_KIWOOM_TOKEN_UNAVAILABLE')

        def read(self, path, api_id, token, body, **kwargs):
            if api_id not in {'ka00001', 'ka01300'}:
                raise KiwoomError('DASHBOARD_ENDPOINT_DENIED')
            guard(root)
            return super().read(path, api_id, token, body, **kwargs)

    expected, credentials = connection
    guard(root)
    client = KiwoomClient(credentials, transport=WatchlistTransport(
        'production', timeout=5, minimum_interval=2.5))
    if client.account_number() != expected:
        raise ValueError('DASHBOARD_KIWOOM_ACCOUNT_MISMATCH')
    return tuple((group.code, group.name) for group in client.watchlist_groups())


def kis_groups(root, profile, *, legacy=False):
    if profile.mode != 'live' or not profile.hts_id:
        raise KISError('KIS_WATCHLIST_LIVE_HTS_REQUIRED')
    guard(root)
    client = PortfolioClient(root) if legacy else ManagedKIS(root, profile)
    if legacy and (client.account, client.product, client.key, client.secret) != (
        profile.number, profile.product, profile.app_key, profile.app_secret
    ):
        raise KISError('KIS_ACCOUNT_SETTINGS_CHANGED')
    client.guard = lambda: guard(root)
    return client.watchlist_groups(profile.hts_id)


def selection_path(root, source_id):
    return directory(root) / ('ats-selection-' + source_id + '.json')


def default_selection(root, source_id):
    data = read_json(selection_path(root, source_id))
    if data is None:
        return '', '', ''
    fields = ('group_code', 'group', 'model_id')
    if not isinstance(data, dict) or not all(isinstance(data.get(key), str) for key in fields):
        raise ValueError('ATS 선택 설정을 읽을 수 없습니다.')
    return tuple(data[key] for key in fields)


def save_default_selection(root, source_id, group_code, group, model_id):
    save_json(selection_path(root, source_id), dict(group_code=group_code, group=group, model_id=model_id))
