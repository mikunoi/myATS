"""Authentication-only HTTPS transport. No arbitrary endpoints or redirects."""
import json
import math
import time
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from src.broker.request_budget import RequestBudget, RequestCooldown


class KiwoomError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AuthTransport:
    def __init__(self, environment="production", timeout=10, minimum_interval=1.1):
        hosts = {"production": "https://api.kiwoom.com", "mock": "https://mockapi.kiwoom.com"}
        if environment not in hosts:
            raise ValueError("Unknown Kiwoom environment")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid timeout")
        if not math.isfinite(minimum_interval) or minimum_interval < 0:
            raise ValueError("Invalid minimum interval")
        self.environment = environment
        self.host, self.timeout = hosts[environment], timeout
        self.opener = build_opener(NoRedirect())
        self.minimum_interval = minimum_interval
        self._last_request = None
        self._pace_lock = Lock()
        self._budget = RequestBudget(environment)

    def _pace(self):
        if self.minimum_interval > 0:
            self._budget.acquire(self.minimum_interval)
            return
        with self._pace_lock:
            now = time.monotonic()
            if self._last_request is not None:
                remaining = self.minimum_interval - (now - self._last_request)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_request = time.monotonic()

    def _http_failure(self, error, operation):
        if error.code == 429:
            try:
                seconds = float(error.headers.get("Retry-After", "30"))
            except (ValueError, TypeError, AttributeError):
                seconds = 30.0
            seconds = max(5.0, min(300.0, seconds))
            self._budget.defer(seconds)
            return RequestCooldown(seconds)
        return KiwoomError(f"Kiwoom {operation} HTTP status {error.code}")

    def issue_token(self, appkey, secretkey):
        self._pace()
        request = Request(self.host + "/oauth2/token", method="POST",
                          headers={"Content-Type": "application/json;charset=UTF-8"},
                          data=json.dumps({"grant_type": "client_credentials", "appkey": appkey,
                                           "secretkey": secretkey}).encode("utf-8"))
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise KiwoomError("Authentication response too large")
                result = json.loads(raw)
        except HTTPError as error:
            # Never include response bodies, request headers or credentials in errors.
            raise self._http_failure(error, "authentication") from None
        except (URLError, TimeoutError, OSError):
            raise KiwoomError("Kiwoom authentication connection failed") from None
        except (ValueError, UnicodeError):
            raise KiwoomError("Malformed Kiwoom authentication response") from None
        if not isinstance(result, dict):
            raise KiwoomError("Malformed Kiwoom authentication response")
        return result

    def read(self, path, api_id, token, body, *, cont_yn=None, next_key=None):
        allowed = {("/api/dostk/watchlist", "ka01300"),
                   ("/api/dostk/watchlist", "ka01301"),
                   ("/api/dostk/acnt", "ka00001"),
                   ("/api/dostk/acnt", "kt00007"),
                   ("/api/dostk/acnt", "kt00015"),
                   ("/api/dostk/acnt", "ka10073"),
                   ("/api/dostk/acnt", "kt00001"),
                   ("/api/dostk/acnt", "kt00018"),
                   ("/api/dostk/acnt", "ka10075"),
                   ("/api/dostk/acnt", "ka10077"),
                   ("/api/dostk/acnt", "ka10072"),
                   ("/api/dostk/acnt", "ka10074"),
                   ("/api/dostk/stkinfo", "ka10001"),
                   ("/api/dostk/mrkcond", "ka10004"),
                   ("/api/dostk/chart", "ka10081"),
                   ("/api/dostk/chart", "ka10082"),
                   ("/api/dostk/chart", "ka10080"),
                   ("/api/dostk/rkinfo", "ka10032")}
        allowed.update({
            ("/api/us/watchlist", "usa20200"), ("/api/us/watchlist", "usa20201"),
            ("/api/us/acnt", "ust21050"), ("/api/us/acnt", "ust21070"),
            ("/api/us/acnt", "ust21110"), ("/api/us/acnt", "ust21150"),
            ("/api/us/acnt", "ust21160"),
            ("/api/us/acnt", "ust21170"),
            ("/api/us/acnt", "ust21630"),
            ("/api/us/acnt", "ust21640"),
            ("/api/us/acnt", "ust21530"),
            ("/api/us/mrkcond", "usa20100"), ("/api/us/mrkcond", "usa20101"),
            ("/api/us/chart", "usa06012"), ("/api/us/chart", "usa06013"),
            ("/api/us/chart", "usa06011"),
            ("/api/us/exchange", "ust31301"),
        })
        if (path, api_id) not in allowed:
            raise KiwoomError("Read endpoint is not allowlisted")
        if not isinstance(body, dict):
            raise ValueError("Request body must be an object")
        self._pace()
        request = Request(self.host + path, method="POST",
                          headers={"Content-Type": "application/json;charset=UTF-8",
                                   "Authorization": f"Bearer {token}", "api-id": api_id},
                          data=json.dumps(body).encode("utf-8"))
        if cont_yn:
            request.add_header("cont-yn", cont_yn)
            request.add_header("next-key", next_key or "")
        continuation = None
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                headers = getattr(response, "headers", {})
                if headers.get("cont-yn") == "Y":
                    continuation = headers.get("next-key")
                    if not continuation:
                        raise KiwoomError("Continuation key is missing")
                raw = response.read(5_000_001)
                if len(raw) > 5_000_000:
                    raise KiwoomError("Kiwoom response too large")
                result = json.loads(raw)
        except HTTPError as error:
            raise self._http_failure(error, f"read api_id={api_id}") from None
        except (URLError, TimeoutError, OSError):
            raise KiwoomError(f"Kiwoom read connection failed api_id={api_id}") from None
        except (ValueError, UnicodeError):
            raise KiwoomError(f"Malformed Kiwoom read response api_id={api_id}") from None
        if not isinstance(result, dict):
            raise KiwoomError("Malformed Kiwoom read response")
        if str(result.get("return_code", "0")) != "0":
            message = str(result.get("return_msg", "")).replace("\r", " ").replace("\n", " ")[:200]
            suffix = f": {message}" if message else ""
            raise KiwoomError(f"Kiwoom rejected read request {api_id}{suffix}")
        if continuation:
            result["_next_key"] = continuation
        return result

    def order(self, api_id, token, body):
        """Submit only an allowlisted domestic order to Kiwoom mock investment."""
        if self.environment != "mock":
            raise KiwoomError("Order transport is restricted to the mock environment")
        if api_id not in {"kt10000", "kt10001", "kt10003"}:
            raise KiwoomError("Order endpoint is not allowlisted")
        if not isinstance(body, dict) or body.get("dmst_stex_tp") != "KRX":
            raise ValueError("Mock orders require the KRX market")
        return self._send_order(api_id, token, body)

    def _send_order(self, api_id, token, body):
        """Shared wire format; public callers must validate environment and policy."""
        return self._send_order_path("/api/dostk/ordr", api_id, token, body)

    def _send_order_path(self, path, api_id, token, body):
        if path not in {"/api/dostk/ordr", "/api/us/ordr"}:
            raise KiwoomError("Order path is not allowlisted")
        self._pace()
        request = Request(self.host + path, method="POST",
                          headers={"Content-Type": "application/json;charset=UTF-8",
                                   "Authorization": f"Bearer {token}", "api-id": api_id},
                          data=json.dumps(body).encode("utf-8"))
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise KiwoomError("Kiwoom order response too large")
                result = json.loads(raw)
        except HTTPError as error:
            raise self._http_failure(error, f"order api_id={api_id}") from None
        except (URLError, TimeoutError, OSError):
            raise KiwoomError("Kiwoom order connection failed") from None
        except (ValueError, UnicodeError):
            raise KiwoomError("Malformed Kiwoom order response") from None
        if not isinstance(result, dict):
            raise KiwoomError("Malformed Kiwoom order response")
        if str(result.get("return_code", "0")) != "0":
            message = str(result.get("return_msg", ""))[:300]
            message = message.replace(token, "[REDACTED]")
            raise KiwoomError(f"Kiwoom rejected order request {api_id}: code={result.get('return_code')} message={message}")
        return result
