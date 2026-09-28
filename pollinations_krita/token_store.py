"""Authorization storage for the Krita plug-in.

Owner-only on POSIX. On Windows the file is DPAPI-encrypted and bound to the
Windows user, which is what keeps a copied configuration blob useless elsewhere.
"""
import os
import sys
import tempfile
from pathlib import Path

# The plug-in is named pollinations-krita everywhere user-visible.
_WIN_NAME = "PollinationsKrita"
_POSIX_NAME = "pollinations-krita"


def config_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise OSError("LOCALAPPDATA is not set, so the authorization cannot be stored.")
        return Path(base) / _WIN_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _WIN_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / _POSIX_NAME


def _protect(data, decrypt=False):
    from ctypes import wintypes
    import ctypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError("Windows could not protect the account authorization.")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


class TokenStore:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else config_dir()
        self.path = self.directory / "authorization"

    def load(self):
        try:
            data = self.path.read_bytes()
            if sys.platform == "win32":
                data = _protect(data, decrypt=True)
            token = data.decode("utf-8")
            return token if token.startswith("sk_") else None
        except (OSError, UnicodeError):
            return None

    def save(self, token):
        if not token.startswith("sk_"):
            raise ValueError("Invalid account authorization.")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = token.encode("utf-8")
        if sys.platform == "win32":
            data = _protect(data)
        fd, temporary = tempfile.mkstemp(dir=str(self.directory))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(self.path))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def clear(self):
        self.path.unlink(missing_ok=True)
