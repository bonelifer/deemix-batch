#!/usr/bin/env python3
"""Shared pieces for import_artists.py and monitor_releases.py.

Holds the release-monitor SQLite database (state/releases.db), a small
rate-limited client for Deezer's public API (no login needed), artist-name
lookup, release filtering helpers, .env loading, and the private venv used
for the two optional third-party packages (Apprise for notifications,
mutagen for scanning music files).

Standard library only at import time, so the offline tests and the
MPD-only/no-notification paths never need the private venv.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import venv

PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = PROJECT_DIR / "state"
DB_PATH = STATE_DIR / "releases.db"
VENV_DIR = PROJECT_DIR / ".release-monitor-venv"

# Pinned deliberately; bump after checking the new release.
PRIVATE_PACKAGES = {
    "apprise": "2.1.0",
    "mutagen": "1.48.1",
}

DEEZER_API = "https://api.deezer.com"
USER_AGENT = "deemix-batch-release-monitor/1.0"

# Deezer's documented limit is 50 requests per 5 seconds; stay well under it.
MIN_REQUEST_INTERVAL = 0.15
REQUEST_TIMEOUT = 20
MAX_REQUEST_ATTEMPTS = 4

# Release types a user can select. "featured" is an album whose main artist
# is someone else (a guest spot or a various-artists compilation).
RELEASE_TYPES = ("album", "ep", "single", "compile", "featured")

# Release statuses stored in the releases table.
STATUS_BASELINE = "baseline"  # already out when the artist was followed
STATUS_IGNORED = "ignored"  # filtered out; reason column says why
STATUS_QUEUED = "queued"  # waiting to be downloaded
STATUS_DOWNLOADING = "downloading"  # deemix is running for it right now
STATUS_DOWNLOADED = "downloaded"  # deemix exited 0 (track gaps: errors.txt)
STATUS_FAILED = "failed"  # gave up after the maximum attempts
STATUSES = (
    STATUS_BASELINE,
    STATUS_IGNORED,
    STATUS_QUEUED,
    STATUS_DOWNLOADING,
    STATUS_DOWNLOADED,
    STATUS_FAILED,
)

DEFAULT_IGNORED_ARTISTS = (
    "various artists",
    "various",
    "va",
    "unknown artist",
    "[unknown]",
    "[no artist]",
)

# Words inside brackets that only mark another edition of the same album.
EDITION_WORDS = re.compile(
    r"\b(deluxe|expanded|explicit|clean|edited|bonus tracks?|"
    r"special edition|standard edition|standard version|super deluxe)\b",
    re.IGNORECASE,
)

JOINED_ARTISTS = re.compile(
    r"\s*/\s*|\s*;\s*|\s*,\s+|\s+&\s+|\s+(?:feat\.?|ft\.?|featuring)\s+",
    re.IGNORECASE,
)

SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    deezer_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    mbid TEXT,
    followed INTEGER NOT NULL DEFAULT 1,
    followed_at TEXT NOT NULL,
    last_checked_at TEXT,
    baseline_done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS unresolved_artists (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    source TEXT NOT NULL,
    mbid TEXT,
    reason TEXT NOT NULL,
    candidates TEXT,
    ignored INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    album_id INTEGER PRIMARY KEY,
    artist_id INTEGER NOT NULL REFERENCES artists(deezer_id),
    title TEXT NOT NULL,
    record_type TEXT NOT NULL,
    release_type TEXT,
    release_date TEXT,
    main_artist_id INTEGER,
    main_artist_name TEXT,
    nb_tracks INTEGER,
    explicit INTEGER,
    upc TEXT,
    edition_key TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    first_seen_at TEXT NOT NULL,
    status_changed_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS releases_status ON releases(status);
CREATE INDEX IF NOT EXISTS releases_edition ON releases(edition_key);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT
);
"""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string (seconds)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_dotenv(path: Path | str = PROJECT_DIR / ".env") -> None:
    """Minimal .env loader, same rules as the other scripts.

    Real environment variables take precedence over .env values.
    """
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key:
                    os.environ.setdefault(
                        key, value.strip().strip('"').strip("'")
                    )
    except FileNotFoundError:
        pass


def split_list(value: str | None) -> list[str]:
    """Split a comma- or whitespace-separated setting into its items."""
    if not value:
        return []
    return [item for item in re.split(r"[,\s]+", value.strip()) if item]


def split_comma_list(value: str | None) -> list[str]:
    """Split a comma-separated setting whose items may contain spaces."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_release_types(value: str | None) -> list[str]:
    """Validate a RELEASE_TYPES-style list; empty means every type.

    Raises ValueError naming the bad entry and the accepted values.
    """
    items = [item.lower() for item in split_list(value)]
    if not items:
        return list(RELEASE_TYPES)
    for item in items:
        if item not in RELEASE_TYPES:
            raise ValueError(
                f"unknown release type {item!r}; expected a "
                f"comma-separated list of: {', '.join(RELEASE_TYPES)}"
            )
    return list(dict.fromkeys(items))


def env_positive_int(name: str, default: int) -> int:
    """Read a positive integer setting; errors name the setting."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        number = int(raw)
    except ValueError:
        raise ValueError(
            f"{name} must be a whole number, got {raw!r}"
        ) from None
    if number <= 0:
        raise ValueError(f"{name} must be greater than zero, got {number}")
    return number


def normalize_name(name: str) -> str:
    """Case-fold and squeeze whitespace for name comparisons."""
    return " ".join(name.casefold().split())


def edition_key(artist_id: int, title: str, record_type: str = "") -> str:
    """Key that treats deluxe/explicit/clean editions as the same release.

    Deezer's record type is part of the key, so a title-track single never
    counts as an edition of the album it comes from.
    """

    def strip_edition(match: re.Match) -> str:
        return "" if EDITION_WORDS.search(match.group(0)) else match.group(0)

    cleaned = re.sub(r"[\(\[][^\)\]]*[\)\]]", strip_edition, title)
    cleaned = re.sub(
        r"\s+-\s+.*\b(edition|version)\b.*$", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"[^\w]+", " ", cleaned.casefold())
    kind = record_type.strip().lower()
    return f"{artist_id}:{kind}:{' '.join(cleaned.split())}"


def split_joined_artists(name: str) -> list[str]:
    """Split "A/B", "A; B", "A & B", "A, B", "A feat. B" into names.

    Only used after the whole name failed to match, so real names that
    contain these characters (AC/DC, Simon & Garfunkel) match first.
    """
    parts = JOINED_ARTISTS.split(name)
    return list(dict.fromkeys(part.strip() for part in parts if part.strip()))


def release_age_days(
    release_date: str | None, today: date | None = None
) -> int | None:
    """Days since release; negative for future dates, None if unknown."""
    if not release_date or release_date.startswith("0000"):
        return None
    try:
        released = date.fromisoformat(release_date)
    except ValueError:
        return None
    return ((today or date.today()) - released).days


# --------------------------------------------------------------------------- #
# Private venv for optional third-party packages
# --------------------------------------------------------------------------- #
def in_private_env() -> bool:
    """True when running inside .release-monitor-venv."""
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def ensure_private_env(script: Path | str, argv: list[str]) -> None:
    """Create/repair .release-monitor-venv and re-exec the script inside it.

    Installs the pinned PRIVATE_PACKAGES on first use; later runs reuse the
    venv. Returns only when already running inside it. No shell activation
    or PATH change is involved.
    """
    if in_private_env():
        return
    python = VENV_DIR / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    if VENV_DIR.exists() and not (VENV_DIR / "pyvenv.cfg").is_file():
        raise RuntimeError(
            f"Refusing to modify an unrelated directory: {VENV_DIR}"
        )
    if not python.exists():
        created = not VENV_DIR.exists()
        print(f"Creating private Python environment: {VENV_DIR}", flush=True)
        try:
            venv.EnvBuilder(with_pip=True).create(VENV_DIR)
        except (OSError, subprocess.CalledProcessError) as exc:
            # Remove only a directory this call created, so the next run
            # starts clean instead of reusing a venv without pip.
            if created and (VENV_DIR / "pyvenv.cfg").is_file():
                shutil.rmtree(VENV_DIR)
            version = f"{sys.version_info.major}.{sys.version_info.minor}"
            raise RuntimeError(
                f"This Python ({sys.executable}, {version}) cannot create a "
                f"venv with pip. On Ubuntu install python{version}-venv, or "
                "run the script with another Python 3.10+ that has it."
            ) from exc
    checks = "; ".join(
        f"assert version({name!r}) == {wanted!r}"
        for name, wanted in PRIVATE_PACKAGES.items()
    )
    probe = [
        str(python),
        "-I",
        "-c",
        f"from importlib.metadata import version; {checks}",
    ]
    if subprocess.run(
        probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode:
        print(
            "Installing pinned packages in the private environment...",
            flush=True,
        )
        try:
            subprocess.run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    *(
                        f"{name}=={wanted}"
                        for name, wanted in PRIVATE_PACKAGES.items()
                    ),
                ],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Could not install packages into {VENV_DIR}. On Ubuntu, make "
                "sure the matching python3-venv package is installed, "
                "then rerun."
            ) from exc
        subprocess.run(probe, check=True)
    os.execv(
        str(python), [str(python), "-I", str(Path(script).resolve()), *argv]
    )


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open (and create or upgrade) the release-monitor database."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        connection.close()
        raise RuntimeError(
            f"{path} was created by a newer version "
            f"(schema {version}); update deemix-batch."
        )
    with connection:
        connection.executescript(SCHEMA)
    # Migrations and the version bump commit together or not at all.
    with connection:
        if 0 < version < 2:
            migrate_edition_keys(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return connection


def migrate_edition_keys(connection: sqlite3.Connection) -> None:
    """Schema 1 -> 2: recompute edition keys to include the record type."""
    rows = connection.execute(
        "SELECT album_id, artist_id, main_artist_id, title, record_type "
        "FROM releases"
    ).fetchall()
    connection.executemany(
        "UPDATE releases SET edition_key = ? WHERE album_id = ?",
        [
            (
                edition_key(
                    row["main_artist_id"] or row["artist_id"],
                    row["title"],
                    row["record_type"] or "",
                ),
                row["album_id"],
            )
            for row in rows
        ],
    )


@contextmanager
def recorded_run(connection: sqlite3.Connection, command: str):
    """Record a run's start/finish; the caller fills the summary dict."""
    summary: dict = {}
    with connection:
        cursor = connection.execute(
            "INSERT INTO runs (command, started_at) VALUES (?, ?)",
            (command, utc_now()),
        )
    run_id = cursor.lastrowid
    try:
        yield summary
    finally:
        with connection:
            connection.execute(
                "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
                (utc_now(), json.dumps(summary, sort_keys=True), run_id),
            )


def follow_artist(
    connection: sqlite3.Connection,
    deezer_id: int,
    name: str,
    source: str,
    mbid: str | None = None,
) -> bool:
    """Follow an artist; returns True if it was newly added or re-followed.

    An artist that was unfollowed stays unfollowed when it shows up again in
    an import (source != "manual"); only a manual follow brings it back.
    """
    row = connection.execute(
        "SELECT followed FROM artists WHERE deezer_id = ?", (deezer_id,)
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO artists (deezer_id, name, source, mbid, followed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (deezer_id, name, source, mbid, utc_now()),
        )
        return True
    if not row["followed"] and source == "manual":
        connection.execute(
            "UPDATE artists SET followed = 1 WHERE deezer_id = ?", (deezer_id,)
        )
        return True
    if mbid:
        connection.execute(
            "UPDATE artists SET mbid = COALESCE(mbid, ?) WHERE deezer_id = ?",
            (mbid, deezer_id),
        )
    return False


def record_unresolved(
    connection: sqlite3.Connection,
    name: str,
    source: str,
    reason: str,
    candidates: list[dict] | None = None,
    mbid: str | None = None,
) -> None:
    """Remember a name that could not be matched to one Deezer artist."""
    now = utc_now()
    connection.execute(
        "INSERT INTO unresolved_artists "
        "(name, source, mbid, reason, candidates, first_seen_at, "
        "last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET reason = excluded.reason, "
        "candidates = excluded.candidates, "
        "last_seen_at = excluded.last_seen_at, "
        "mbid = COALESCE(unresolved_artists.mbid, excluded.mbid)",
        (
            name,
            source,
            mbid,
            reason,
            json.dumps(candidates or [], ensure_ascii=False),
            now,
            now,
        ),
    )


# --------------------------------------------------------------------------- #
# Deezer public API
# --------------------------------------------------------------------------- #
class DeezerError(RuntimeError):
    """Deezer could not be reached or returned an unusable response."""


class DeezerClient:
    """Rate-limited, retrying client for Deezer's public JSON API.

    Retries only transient failures (network errors, HTTP 429/5xx, and
    Deezer's "quota exceeded" error, code 4) with a bounded backoff.
    """

    def __init__(
        self,
        base_url: str = DEEZER_API,
        min_interval: float = MIN_REQUEST_INTERVAL,
        sleep=time.sleep,
        opener=urllib.request.urlopen,
    ):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self._sleep = sleep
        self._open = opener
        self._last_request = 0.0

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET one API path (or full URL); return its decoded JSON."""
        url = (
            path
            if path.startswith("http")
            else f"{self.base_url}/{path.lstrip('/')}"
        )
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                params
            )
        if not url.startswith(self.base_url + "/"):
            raise DeezerError(
                f"Refusing to follow a URL outside the Deezer API: {url}"
            )
        delay = 2.0
        for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()
            transient = None
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT}
                )
                with self._open(request, timeout=REQUEST_TIMEOUT) as response:
                    data = json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    transient = f"HTTP {exc.code}"
                else:
                    raise DeezerError(f"HTTP {exc.code} from {url}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                transient = str(getattr(exc, "reason", exc))
            except ValueError as exc:
                raise DeezerError(f"Invalid JSON from {url}") from exc
            else:
                if not isinstance(data, dict):
                    raise DeezerError(f"Unexpected response from {url}")
                error = data.get("error")
                if not error:
                    return data
                if isinstance(error, dict) and error.get("code") == 4:
                    transient = "quota exceeded"
                else:
                    message = (
                        error.get("message")
                        if isinstance(error, dict)
                        else error
                    )
                    raise DeezerError(f"Deezer error for {url}: {message}")
            if attempt == MAX_REQUEST_ATTEMPTS:
                raise DeezerError(
                    f"Giving up on {url} after {attempt} attempts "
                    f"({transient})"
                )
            self._sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")

    def paged(self, path: str, params: dict | None = None, limit: int = 100):
        """Yield every item of a paged list, following "next" links."""
        data = self.get(path, {**(params or {}), "limit": limit})
        while True:
            items = data.get("data")
            if not isinstance(items, list):
                raise DeezerError(f"Missing data list in response for {path}")
            yield from items
            next_url = data.get("next")
            if not next_url:
                return
            data = self.get(next_url)

    def artist(self, artist_id: int) -> dict:
        """Return one artist's details."""
        return self.get(f"artist/{int(artist_id)}")

    def album(self, album_id: int) -> dict:
        """Return one album's details (main artist, track count, UPC...)."""
        return self.get(f"album/{int(album_id)}")

    def artist_albums(self, artist_id: int) -> list[dict]:
        """Return every album Deezer lists for an artist (all pages)."""
        return list(self.paged(f"artist/{int(artist_id)}/albums"))

    def search_artists(self, name: str, limit: int = 25) -> list[dict]:
        """Strict artist search, first page only (exact names rank first)."""
        data = self.get(
            "search/artist", {"q": name, "strict": "on", "limit": limit}
        )
        items = data.get("data")
        if not isinstance(items, list):
            raise DeezerError("Missing data list in artist search response")
        return items


def resolve_artist_name(
    client: DeezerClient, name: str
) -> tuple[dict | None, str, list[dict]]:
    """Match a library artist name to exactly one Deezer artist.

    Same rule as deemix_artist_download_all.py: an exact (case-insensitive)
    name match is required; if several artists share the name, the one with
    the most fans wins, unless fan counts are missing or tied.

    Returns (artist or None, reason, candidates). Reasons: "matched",
    "no match", "no exact match", "ambiguous". Raises DeezerError when the
    lookup itself failed, so that "not found" and "could not check" stay
    distinct.
    """
    results = client.search_artists(name)
    wanted = normalize_name(name)
    exact = [
        item
        for item in results
        if normalize_name(str(item.get("name", ""))) == wanted
    ]
    candidates = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "fans": item.get("nb_fan"),
        }
        for item in (exact or results)[:5]
    ]
    if not results:
        return None, "no match", candidates
    if not exact:
        return None, "no exact match", candidates
    if len(exact) == 1:
        return exact[0], "matched", candidates
    fans = [item.get("nb_fan") for item in exact]
    if not all(isinstance(count, int) for count in fans):
        return None, "ambiguous", candidates
    ranked = sorted(exact, key=lambda item: item["nb_fan"], reverse=True)
    if ranked[0]["nb_fan"] == ranked[1]["nb_fan"]:
        return None, "ambiguous", candidates
    return ranked[0], "matched", candidates


def classify_release(
    album: dict, detail: dict, followed_artist_id: int
) -> str:
    """Return the selectable release type for an album.

    "featured" when the album's main artist is not the followed artist,
    otherwise Deezer's own record_type (album, ep, single, compile).
    """
    main = detail.get("artist") or {}
    if main.get("id") is not None and int(main["id"]) != int(
        followed_artist_id
    ):
        return "featured"
    record_type = str(
        detail.get("record_type") or album.get("record_type") or "album"
    )
    return record_type.lower()


def is_ignored_artist_name(name: str, extra: list[str] | None = None) -> bool:
    """True for placeholder names (Various Artists) and configured ones."""
    names = set(DEFAULT_IGNORED_ARTISTS) | {
        normalize_name(item) for item in extra or []
    }
    return normalize_name(name) in names
