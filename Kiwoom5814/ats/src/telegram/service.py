import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from src.config import Secrets
from src.database.store import Store
from src.telegram.error_messages import compact_error_notification


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelegramTransport:
    def __init__(self, timeout=10, opener=None):
        self.timeout = timeout
        self.opener = opener or build_opener(NoRedirect())

    def identity(self, token):
        if not token:
            raise RuntimeError("Telegram bot token is missing")
        request = Request(f"https://api.telegram.org/bot{token}/getMe", method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(1_000_001))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, UnicodeError):
            raise RuntimeError("Telegram bot verification failed") from None
        identity = result.get("result") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get("ok") is not True or not isinstance(identity, dict)
                or identity.get("is_bot") is not True):
            raise RuntimeError("Telegram bot identity is invalid")
        return {"id": identity.get("id"), "username": identity.get("username", "")}

    def send(self, token, chat_id, message):
        if not token or not chat_id:
            raise RuntimeError("Telegram credentials are missing")
        request = Request(f"https://api.telegram.org/bot{token}/sendMessage", method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data=urlencode({"chat_id": chat_id, "text": message}).encode())
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(1_000_001))
        except HTTPError as error:
            raise RuntimeError(f"Telegram HTTP status {error.code}") from None
        except (URLError, TimeoutError, OSError, ValueError, UnicodeError):
            raise RuntimeError("Telegram delivery failed") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram rejected notification")

class TelegramService:
    def __init__(self, secrets: Secrets, store: Store, transport=None, service_label=""):
        self.secrets, self.store = secrets, store
        self.transport = transport or TelegramTransport()
        self.service_label = service_label.strip()

    @property
    def configured(self):
        return bool(self.secrets.telegram_bot_token and self.secrets.telegram_chat_id)

    def notify(self, event_key: str, message: str):
        if not event_key or not message:
            raise ValueError("Notification identity and message are required")
        self.store.enqueue_notification(event_key, message)
        return self.retry_pending()

    def retry_pending(self):
        if not self.configured:
            return 0
        sent = 0
        for event_key, message in self.store.pending_notifications():
            try:
                self.transport.send(self.secrets.telegram_bot_token,
                                    self.secrets.telegram_chat_id,
                                    compact_error_notification(event_key, message, self.service_label))
            except RuntimeError:
                self.store.mark_notification_failed(event_key)
                continue
            self.store.mark_notification_sent(event_key)
            sent += 1
        return sent
