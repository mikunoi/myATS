"""US session dates and fixed KST bedtime reporting; no trading side effects."""
from datetime import time, timedelta, timezone

KST = timezone(timedelta(hours=9))
# NYSE published 2026-2028 calendar:
# https://ir.theice.com/press/news-details/2025/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar/default.aspx
HOLIDAYS = {
    2026: "01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25".split(),
    2027: "01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06 11-25 12-24".split(),
    2028: "01-17 02-21 04-14 05-29 06-19 07-04 09-04 11-23 12-25".split(),
}
EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26", "2028-07-03", "2028-11-24"}


def session_close(now):
    if now.year not in HOLIDAYS or now.weekday() >= 5:
        return None
    if now.strftime("%m-%d") in HOLIDAYS[now.year]:
        return None
    return time(13) if now.date().isoformat() in EARLY_CLOSE else time(16)


def bedtime_due(now):
    """23:00-23:59 KST catch-up window, keyed by US trading day."""
    return now.astimezone(KST).hour == 23
