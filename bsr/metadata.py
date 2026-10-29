"""Season listings from TMDB and TVDB (optional, online).

Only a show name (and year, if known) is sent; nothing about your files. Results
are cached for a week under the app's cache folder. Credentials come from the
macOS keychain or Windows Credential Manager (saved from the app), or the
BSR_TMDB_TOKEN / BSR_TVDB_APIKEY and BSR_TVDB_PIN environment variables.

TMDB: create a free API key or Read Access Token at themoviedb.org → Settings → API.
TVDB: use a v4 API key from thetvdb.com (plus your subscriber PIN for user-supported keys).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .episodes import DbEpisode

USER_AGENT = "BananaSplitRedux"
CACHE_TTL_S = 7 * 24 * 3600
KEYCHAIN_SERVICE = "Banana Split Redux"
PROVIDERS = {"tmdb": "TMDB (The Movie Database)", "tvdb": "TVDB (TheTVDB)"}


class LookupError_(Exception):
    """A plain-language lookup problem (no network, bad key, show not found)."""


@dataclass
class ShowMatch:
    provider: str
    id: str
    name: str
    year: Optional[int] = None

    @property
    def label(self) -> str:
        return f"{self.name}" + (f" ({self.year})" if self.year else "")


# ------------------------------------------------------------------ credentials

def _keychain(account: str) -> Optional[str]:
    if sys.platform == "win32":
        return _wincred_read(account)
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=10)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


# Windows Credential Manager (generic credentials), through advapi32 with ctypes.
def _wincred():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, CREDENTIAL, api


CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE = 1, 2


def _wincred_target(account: str) -> str:
    return f"{KEYCHAIN_SERVICE}/{account}"


def _wincred_read(account: str) -> Optional[str]:
    try:
        ctypes, CREDENTIAL, api = _wincred()
        p = ctypes.POINTER(CREDENTIAL)()
        if not api.CredReadW(_wincred_target(account), CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
            return None
        try:
            c = p.contents
            return bytes(c.CredentialBlob[:c.CredentialBlobSize]).decode("utf-16-le") or None
        finally:
            api.CredFree(p)
    except Exception:
        return None


def _wincred_write(account: str, value: str) -> bool:
    try:
        ctypes, CREDENTIAL, api = _wincred()
        target = _wincred_target(account)
        if not value:
            api.CredDeleteW(target, CRED_TYPE_GENERIC, 0)
            return True
        blob = value.encode("utf-16-le")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=target, CredentialBlobSize=len(blob),
                          CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)),
                          Persist=CRED_PERSIST_LOCAL_MACHINE, UserName=account)
        return bool(api.CredWriteW(ctypes.byref(cred), 0))
    except Exception:
        return False


_session_secrets: dict[str, str] = {}


def get_secret(account: str) -> Optional[str]:
    env = {"tmdb": "BSR_TMDB_TOKEN", "tvdb": "BSR_TVDB_APIKEY", "tvdb-pin": "BSR_TVDB_PIN"}[account]
    if os.environ.get(env):
        return os.environ[env]
    if account in _session_secrets:           # saved or cleared in this session
        return _session_secrets[account] or None
    return _keychain(account)


def set_secret(account: str, value: str) -> bool:
    """Store in the macOS keychain or Windows Credential Manager (True), or for this session only (False)."""
    value = (value or "").strip()
    _session_secrets[account] = value
    if sys.platform == "win32":
        return _wincred_write(account, value)
    if sys.platform != "darwin":
        return False
    try:
        if not value:
            subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
                           capture_output=True, timeout=10)
            return True
        r = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", account,
                            "-w", value], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def configured(provider: str) -> bool:
    return bool(get_secret("tmdb" if provider == "tmdb" else "tvdb"))


# ------------------------------------------------------------------ transport + cache

Fetch = Callable[[str, dict, Optional[bytes]], dict]


def http_json(url: str, headers: dict, body: Optional[bytes] = None) -> dict:
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                          **headers}, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LookupError_("The service refused the key. Check it under Episode names → Keys.") from e
        if e.code == 404:
            raise LookupError_("Not found on the service.") from e
        if e.code == 429:
            raise LookupError_("The service asked us to slow down. Try again in a minute.") from e
        raise LookupError_(f"The service returned an error ({e.code}).") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LookupError_("Could not reach the service. Check your internet connection.") from e
    except ValueError as e:
        raise LookupError_("The service sent an unreadable reply.") from e


def _cache_path(url: str) -> str:
    from .cache import cache_dir
    d = os.path.join(cache_dir(), "metadata")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.sha1(url.encode()).hexdigest()[:24] + ".json")


def _cached(url: str, fetch: Callable[[], dict], use_cache: bool = True) -> dict:
    path = _cache_path(url)
    if use_cache and os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL_S:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    data = fetch()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


# ------------------------------------------------------------------ providers

class TMDB:
    base = "https://api.themoviedb.org/3"

    def __init__(self, token: Optional[str] = None, fetch: Fetch = http_json, use_cache: bool = True):
        self.token = token or get_secret("tmdb")
        if not self.token:
            raise LookupError_("Add a TMDB API key or Read Access Token first (Keys…).")
        self.fetch, self.use_cache = fetch, use_cache

    def _get(self, path: str, **params) -> dict:
        headers = {}
        if self.token.count(".") >= 2:            # v4 Read Access Token (JWT)
            headers["Authorization"] = f"Bearer {self.token}"
        else:                                       # v3 API key
            params["api_key"] = self.token
        public = f"{self.base}{path}?" + urllib.parse.urlencode(sorted((k, v) for k, v in params.items() if k != "api_key"))
        url = f"{self.base}{path}?" + urllib.parse.urlencode(params)
        return _cached("tmdb:" + public, lambda: self.fetch(url, headers, None), self.use_cache)

    def search(self, name: str, year: Optional[int] = None) -> list[ShowMatch]:
        params = {"query": name, "include_adult": "false"}
        if year:
            params["first_air_date_year"] = str(year)
        data = self._get("/search/tv", **params)
        out = []
        for r in data.get("results") or []:
            y = (r.get("first_air_date") or "")[:4]
            out.append(ShowMatch("tmdb", str(r["id"]), r.get("name") or r.get("original_name") or "?",
                                 int(y) if y.isdigit() else None))
        if not out and year:
            return self.search(name)
        return out

    def season(self, show_id: str, season: int) -> list[DbEpisode]:
        data = self._get(f"/tv/{show_id}/season/{season}")
        return [DbEpisode(season=int(e.get("season_number", season)), number=int(e["episode_number"]),
                          title=e.get("name") or "", runtime=float(e["runtime"]) if e.get("runtime") else None)
                for e in data.get("episodes") or [] if e.get("episode_number") is not None]


class TVDB:
    base = "https://api4.thetvdb.com/v4"

    def __init__(self, apikey: Optional[str] = None, pin: Optional[str] = None, fetch: Fetch = http_json,
                 use_cache: bool = True):
        self.apikey = apikey or get_secret("tvdb")
        self.pin = pin if pin is not None else get_secret("tvdb-pin")
        if not self.apikey:
            raise LookupError_("Add a TVDB API key first (Keys…).")
        self.fetch, self.use_cache = fetch, use_cache
        self._token = None

    def _auth(self) -> dict:
        if self._token is None:
            body = {"apikey": self.apikey}
            if self.pin:
                body["pin"] = self.pin
            data = self.fetch(f"{self.base}/login", {"Content-Type": "application/json"}, json.dumps(body).encode())
            self._token = ((data or {}).get("data") or {}).get("token")
            if not self._token:
                raise LookupError_("TVDB did not accept the key or PIN.")
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, **params) -> dict:
        url = f"{self.base}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        return _cached("tvdb:" + url, lambda: self.fetch(url, self._auth(), None), self.use_cache)

    def search(self, name: str, year: Optional[int] = None) -> list[ShowMatch]:
        params = {"query": name, "type": "series"}
        if year:
            params["year"] = str(year)
        data = self._get("/search", **params)
        out = []
        for r in data.get("data") or []:
            sid = r.get("tvdb_id") or str(r.get("id", "")).replace("series-", "")
            y = str(r.get("year") or (r.get("first_air_time") or "")[:4])
            out.append(ShowMatch("tvdb", str(sid), r.get("name") or "?", int(y) if y.isdigit() else None))
        if not out and year:
            return self.search(name)
        return out

    def season(self, show_id: str, season: int) -> list[DbEpisode]:
        out, page = [], 0
        while page < 50:
            data = self._get(f"/series/{show_id}/episodes/default", page=str(page), season=str(season))
            body = data.get("data") or {}
            for e in body.get("episodes") or []:
                if e.get("seasonNumber") == season and e.get("number") is not None:
                    out.append(DbEpisode(season=season, number=int(e["number"]), title=e.get("name") or "",
                                         runtime=float(e["runtime"]) if e.get("runtime") else None))
            if not (data.get("links") or {}).get("next"):
                break
            page += 1
        return sorted(out, key=lambda e: e.number)


def provider(name: str, **kw):
    if name == "tmdb":
        return TMDB(**kw)
    if name == "tvdb":
        return TVDB(**kw)
    raise LookupError_(f"Unknown service {name}")
