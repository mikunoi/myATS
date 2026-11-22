"""Windows-user encrypted, process-shared Kiwoom production token cache."""
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import time
from src.platform_support import FileLock, linux_cipher


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(data: bytes) -> bytes:
    if os.name != 'nt':
        return linux_cipher().encrypt(data)
    source, keepalive = _blob(data)
    target = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise OSError("DPAPI encryption failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def _unprotect(data: bytes) -> bytes:
    if os.name != 'nt':
        from cryptography.fernet import InvalidToken
        try:
            return linux_cipher().decrypt(data)
        except InvalidToken:
            raise ValueError('Invalid local token ciphertext') from None
    source, keepalive = _blob(data)
    target = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise OSError("DPAPI decryption failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def shared_token(cache_path: Path, appkey: str, now: datetime, issuer, rejected_token=None):
    if os.name != 'nt':
        cache_path = cache_path.with_suffix('.linux.enc')
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
    fingerprint = hashlib.sha256(appkey.encode()).hexdigest()
    with FileLock(lock_path, timeout=30):
        try:
            try:
                saved = json.loads(_unprotect(cache_path.read_bytes()))
                expires = datetime.fromisoformat(saved["expires"])
                if (saved.get("app") == fingerprint and saved.get("token")
                        and saved.get("token") != rejected_token
                        and now + timedelta(seconds=60) < expires):
                    return saved["token"], expires
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
            result = issuer()
            from src.broker.http import KiwoomError
            if not isinstance(result, dict):
                raise KiwoomError("Invalid authentication response")
            if str(result.get("return_code", "0")) != "0":
                if "8050" in str(result.get("return_msg", "")):
                    raise KiwoomError("Kiwoom authentication rejected (8050): register this PC's public IP in Kiwoom REST API settings")
                raise KiwoomError("Kiwoom rejected authentication; check local credentials and API access")
            token = result.get("token")
            if not isinstance(token, str) or not token or result.get("token_type", "").lower() != "bearer":
                raise KiwoomError("Invalid authentication token response")
            try:
                expires = datetime.strptime(result["expires_dt"], "%Y%m%d%H%M%S").replace(tzinfo=now.tzinfo)
            except (KeyError, TypeError, ValueError):
                raise KiwoomError("Invalid authentication expiry") from None
            if expires <= now + timedelta(seconds=60):
                raise KiwoomError("Authentication token already expired or too close to expiry")
            payload = json.dumps({"app": fingerprint, "token": token,
                                  "expires": expires.isoformat()}).encode()
            temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
            temporary.write_bytes(_protect(payload))
            os.replace(temporary, cache_path)
            return token, expires
        finally:
            pass  # FileLock releases on all success/error paths.
