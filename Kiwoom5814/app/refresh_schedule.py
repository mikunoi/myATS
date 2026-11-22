"""Dashboard-only, KST account refresh scheduling. Never changes ATS schedules."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path

KST = timezone(timedelta(hours=9))


@lru_cache(maxsize=1)
def calendar():
    return json.loads(Path(__file__).with_name('kr_holidays.json').read_text(encoding='utf-8'))


def protected_reason(now=None):
    now = (now or datetime.now(KST)).astimezone(KST)
    minute = now.hour * 60 + now.minute
    if 830 <= minute <= 885 or 225 <= minute <= 375:
        return 'ATS 평가 우선 · 보호시간 종료 후 자동 재시도'
    if 475 <= minute <= 490 or 1195 <= minute <= 1210:
        return 'ATS 정기 점검 우선 · 점검 보호시간 종료 후 자동 재시도'
    return ''


def automatic_due(last_at, frequent=False, now=None):
    now = (now or datetime.now(KST)).astimezone(KST)
    if not frequent:
        if now.weekday() >= 5 or not 8 <= now.hour < 20:
            return False
        try:
            dates = calendar()
            if now.year not in dates['years'] or now.date().isoformat() in dates['holidays']:
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False  # Unknown holiday coverage must not trigger automatic reads.
    try:
        elapsed = (now - datetime.fromisoformat(last_at)).total_seconds()
        return elapsed < -5 or elapsed >= (60 if frequent else 3600)
    except (ValueError, TypeError):
        return True
