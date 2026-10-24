"""Single-owner credential storage, atomic commits, Windows DPAPI and private ACLs."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from .errors import SIWCError


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import csv
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        sid = next(csv.reader(result.stdout.decode(errors="replace").splitlines()))[1]
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
            capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        path.chmod(0o700)


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "AstrBot SIWC", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise SIWCError("storage", "Windows 凭据保护失败")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class CredentialStore:
    """Hold the OS lock throughout the plugin lifetime; never steal stale locks."""

    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self._lock_file = None
        self.path = self.directory / "credentials.bin"

    def open(self) -> dict:
        if self._lock_file is not None:
            raise SIWCError("storage", "凭据存储已打开")
        _private_directory(self.directory)
        handle = open(self.directory / "owner.lock", "a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SIWCError("busy", "凭据被其他进程使用；请确认没有其他 AstrBot 实例使用同一插件数据目录") from None
        self._lock_file = handle
        try:
            if self.path.exists():
                raw = self.path.read_bytes()
                if os.name == "nt":
                    raw = _dpapi(raw, decrypt=True)
                result = json.loads(raw)
                if not isinstance(result, dict) or not result.get("host_id"):
                    raise ValueError
                return result
            result = {"version": 1, "host_id": "urn:uuid:" + str(uuid.uuid4()), "registration": None}
            self.save(result)
            return result
        except BaseException:
            self.close()
            raise

    def save(self, record: dict) -> None:
        if self._lock_file is None:
            raise SIWCError("storage", "写入前必须持有凭据锁")
        raw = json.dumps(record, ensure_ascii=False).encode()
        if os.name == "nt":
            raw = _dpapi(raw)
        fd, name = tempfile.mkstemp(prefix=".credentials-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def close(self) -> None:
        if self._lock_file is not None:
            # Closing the handle releases the OS lock even after an abrupt process exit.
            self._lock_file.close()
            self._lock_file = None
