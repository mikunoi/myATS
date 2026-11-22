"""Short operational error notices; technical details stay in runtime logs."""
import re


REASONS = (
    ("접속 IP 등록 필요", ("8050", "ip가 등록", "접속 ip 등록")),
    ("계좌인증 실패", ("expires_dt",)),
    ("거래기록 동기화 실패", ("notion", "거래기록 동기화")),
    ("알림 전송 실패", ("telegram", "알림 전송")),
    ("계좌정보 불일치", ("account mismatch", "계좌정보 불일치")),
    ("계좌인증 실패", ("8005", "authentication", "token", "credentials", "인증")),
    ("조회 요청 과다", ("429", "cooldown", "rate limit", "조회 요청 과다", "요청 제한")),
    ("증권사 정산 중", ("572070", "수도결제", "settlement", "정산")),
    ("주문·체결 확인 필요", ("uncertain", "unresolved", "reconciliation", "fill price",
                              "quantity differs", "order outcome", "주문·체결 확인")),
    ("주문가능금액 부족", ("insufficient cash", "insufficient funds", "증거금 부족",
                            "잔고 부족", "주문가능금액 부족")),
    ("관심종목 확인 실패", ("watchlist", "monitoring symbols", "관심종목")),
    ("증권사 연결 실패", ("connection failed", "timed out", "timeout", "http status 5",
                            "connectionerror", "urlerror", "증권사 연결")),
    ("시세자료 확인 실패", ("ohlc", "candle", "indicator", "quote", "chart", "시세자료",
                              "evaluation data", "stale market")),
    ("계좌조회 실패", ("account", "/acnt", "request kt", "request ust", "계좌조회")),
    ("증권사 조회 실패", ("rejected read", "read failure", "증권사 조회")),
)


def error_reason(error):
    text = str(error).lower()
    for reason, markers in REASONS:
        if any(marker in text for marker in markers):
            return reason
    return "시스템 오류(로그 확인)"


def order_blocked_message(label, error):
    return f"{label} 주문 차단: {error_reason(error)}"


def compact_error_notification(event_key, message, service_label=""):
    """Also shorten legacy error notices waiting in the persistent outbox."""
    match = re.match(r"^(\[(?:국내|해외)\])\s*(?:확인 실패·)?주문 차단(?:, 사유)?:\s*(.*)$",
                     message, flags=re.DOTALL)
    if match:
        return order_blocked_message(match[1], match[2])
    if event_key.startswith(("live-error:", "us-error:")):
        label = "[해외]" if event_key.startswith("us-error:") else "[국내]"
        return order_blocked_message(label, message)
    if not message.startswith(("[국내]", "[해외]")):
        return message
    # Display only; retain original text and identifiers in the durable outbox.
    message = re.sub(r"\bSELL:[A-Z0-9_:]+", "매도", message)
    message = re.sub(r"\bBUY\b", "매수", message)
    message = re.sub(r"\bSELL\b", "매도", message)
    message = re.sub(r"\bINITIAL\b", "신규매수", message)
    message = re.sub(r"\bADDON\b", "추가매수", message)
    message = re.sub(r"\s+#\d+\b", "", message)
    if message.startswith("[해외]"):
        message = re.sub(r"^\[해외\]\s+[^()\n]+\(([A-Z0-9.\-]+)\)", r"[해외] \1", message)
    else:
        message = re.sub(r"\([0-9A-Z]{6}\)", "", message)
    message = message.replace("자동 복구 완료", "연결 복구").replace("시스템 재개", "운영 재개")
    message = message.replace("시스템 중지", "운영 중지").replace("전략 주문", "주문 접수")
    return message.strip()
