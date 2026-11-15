#!/usr/bin/env python3
"""Import the artists to follow for new releases, from MPD or your music files.

Collects album-artist names from one source, looks each one up on Deezer,
and adds the matches to the release monitor's followed artists in
state/releases.db (see monitor_releases.py). Choose exactly one source:

  -m/--mpd [HOST[:PORT]]  ask MPD for its album artists ("list albumartist").
                          Standard library only. Defaults to MPD_HOST and
                          MPD_PORT (localhost:6600); MPD_HOST may be
                          "password@host" or a Unix socket path.
  -s/--scan DIR           read the tags of the music files under DIR
                          (album artist, else artist, plus the MusicBrainz
                          artist ID when present). Uses mutagen from the
                          private .release-monitor-venv, created on first use.

Matching uses the same rule as deemix_artist_download_all.py: the Deezer
name must match exactly (ignoring case); if several artists share it, the
one with the most fans wins. Names with no match or a tie are stored as
"unresolved"; list them with `monitor_releases.py artists --unresolved`
and follow the right one by ID with `monitor_releases.py follow ID --for NAME`.

Running it again only adds artists. It never unfollows anyone, and an
artist you unfollowed is not followed again by an import. Newly followed
artists start with a baseline: their existing releases are recorded on the
first check and are not downloaded.

Settings from .env: IMPORT_IGNORE_ARTISTS (comma-separated names to skip,
in addition to Various Artists and similar placeholders), MPD_HOST, MPD_PORT.

Exit status: 0 when every name was looked up, 1 when a Deezer lookup or the
source itself failed (unmatched names alone are not failures).

Examples:
    python3 import_artists.py --mpd --dry-run
    python3 import_artists.py --mpd localhost:6600
    python3 import_artists.py --scan /media/music/MP3
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_state as rs  # noqa: E402

MUSIC_EXTENSIONS = (".mp3", ".flac", ".ogg", ".opus", ".m4a")


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
class SourceError(RuntimeError):
    """The artist source (MPD or the music folder) could not be read."""


def parse_mpd_target(target: str | None) -> tuple[str, int | None, str | None]:
    """Split [password@]host[:port] (or a socket path) into its parts.

    Falls back to MPD_HOST/MPD_PORT, then localhost:6600.
    """
    host = target or os.environ.get("MPD_HOST") or "localhost"
    password = None
    if "@" in host and not host.startswith("/"):
        password, host = host.rsplit("@", 1)
    if host.startswith("/"):
        return host, None, password
    port_text = os.environ.get("MPD_PORT") or "6600"
    if host.count(":") == 1:
        host, port_text = host.split(":", 1)
    try:
        port = int(port_text)
    except ValueError:
        raise SourceError(
            f"MPD port must be a number, got {port_text!r}"
        ) from None
    if not 0 < port < 65536:
        raise SourceError(f"MPD port out of range: {port}")
    return host, port, password


def mpd_album_artists(
    target: str | None, timeout: float = 15
) -> list[tuple[str, str | None]]:
    """Return distinct album artists from MPD as (name, mbid) pairs.

    Speaks the MPD text protocol directly ("password", then
    "list albumartist").
    """
    host, port, password = parse_mpd_target(target)
    try:
        if port is None:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(timeout)
            connection.connect(host)
        else:
            connection = socket.create_connection((host, port), timeout)
    except OSError as exc:
        where = host if port is None else f"{host}:{port}"
        raise SourceError(f"Cannot connect to MPD at {where}: {exc}") from exc
    with connection, connection.makefile("rwb") as stream:
        greeting = stream.readline().decode("utf-8", "replace")
        if not greeting.startswith("OK MPD"):
            raise SourceError(f"Unexpected MPD greeting: {greeting.strip()!r}")

        def command(text: str) -> list[str]:
            stream.write(text.encode("utf-8") + b"\n")
            stream.flush()
            lines = []
            while True:
                raw = stream.readline()
                if not raw:
                    raise SourceError("MPD closed the connection")
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line == "OK":
                    return lines
                if line.startswith("ACK"):
                    raise SourceError(
                        f"MPD refused {text.split()[0]!r}: {line}"
                    )
                lines.append(line)

        if password:
            quoted = password.replace("\\", "\\\\").replace('"', '\\"')
            command(f'password "{quoted}"')
        names = []
        for line in command("list albumartist"):
            key, _, value = line.partition(": ")
            if key.lower() == "albumartist" and value.strip():
                names.append(value.strip())
    return [(name, None) for name in dict.fromkeys(names)]


def scan_album_artists(
    folder: Path,
) -> tuple[list[tuple[str, str | None]], int]:
    """Read album artists from music file tags under folder.

    Returns ((name, mbid) pairs, number of unreadable files). Symlinked
    folders are not followed. Requires mutagen.
    """
    import mutagen

    if not folder.is_dir():
        raise SourceError(f"Music folder not found: {folder}")
    found: dict[str, str | None] = {}
    unreadable = 0
    for root, directories, files in os.walk(folder):
        directories.sort()
        for name in sorted(files):
            if not name.lower().endswith(MUSIC_EXTENSIONS):
                continue
            try:
                tags = mutagen.File(os.path.join(root, name), easy=True)
            except (OSError, mutagen.MutagenError):
                unreadable += 1
                continue
            if tags is None:
                unreadable += 1
                continue
            artists = tags.get("albumartist") or tags.get("artist") or []
            ids = (
                tags.get("musicbrainz_albumartistid")
                or tags.get("musicbrainz_artistid")
                or []
            )
            for index, artist in enumerate(artists):
                artist = str(artist).strip()
                if not artist:
                    continue
                mbid = str(ids[index]).strip() if index < len(ids) else None
                if artist not in found or (mbid and not found[artist]):
                    found[artist] = mbid or None
    return list(found.items()), unreadable


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def import_names(
    connection,
    client: rs.DeezerClient,
    names: list[tuple[str, str | None]],
    source: str,
    ignore: list[str],
    dry_run: bool = False,
) -> dict:
    """Look up each name on Deezer and follow the matches.

    Names already followed (or already marked ignored as unresolved) are
    skipped without a lookup. Returns counts plus the new/unresolved names.
    """
    known_names = {
        rs.normalize_name(row["name"])
        for row in connection.execute("SELECT name FROM artists")
    }
    ignored_unresolved = {
        rs.normalize_name(row["name"])
        for row in connection.execute(
            "SELECT name FROM unresolved_artists WHERE ignored = 1"
        )
    }
    result = {
        "seen": len(names),
        "followed": [],
        "known": 0,
        "skipped": 0,
        "unresolved": [],
        "errors": [],
    }
    pending = list(names)
    while pending:
        name, mbid = pending.pop(0)
        key = rs.normalize_name(name)
        if (
            rs.is_ignored_artist_name(name, ignore)
            or key in ignored_unresolved
        ):
            result["skipped"] += 1
            continue
        if key in known_names:
            result["known"] += 1
            continue
        try:
            artist, reason, candidates = rs.resolve_artist_name(client, name)
        except rs.DeezerError as exc:
            result["errors"].append(f"{name}: {exc}")
            print(f"  lookup failed: {name}: {exc}", file=sys.stderr)
            continue
        parts = rs.split_joined_artists(name) if artist is None else []
        if len(parts) > 1:
            # "A/B" or "A & B" with no artist of that whole name: follow
            # each part instead ("AC/DC" matched whole and never gets here).
            print(f"  split: {name} -> {' | '.join(parts)}")
            pending[0:0] = [(part, None) for part in parts]
            continue
        if artist is None:
            result["unresolved"].append(f"{name} ({reason})")
            print(f"  unresolved: {name} ({reason})")
            if not dry_run:
                with connection:
                    rs.record_unresolved(
                        connection, name, source, reason, candidates, mbid
                    )
            continue
        deezer_id, deezer_name = int(artist["id"]), str(artist["name"])
        known_names.add(key)
        known_names.add(rs.normalize_name(deezer_name))
        if dry_run:
            result["followed"].append(f"{deezer_name} ({deezer_id})")
            print(f"  would follow: {deezer_name} (ID: {deezer_id})")
            continue
        with connection:
            added = rs.follow_artist(
                connection, deezer_id, deezer_name, source, mbid
            )
            connection.execute(
                "DELETE FROM unresolved_artists WHERE name = ?", (name,)
            )
        if added:
            result["followed"].append(f"{deezer_name} ({deezer_id})")
            print(f"  followed: {deezer_name} (ID: {deezer_id})")
        else:
            result["known"] += 1
    return result


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "-m",
        "--mpd",
        nargs="?",
        const="",
        metavar="HOST[:PORT]",
        help="read album artists from MPD; optional "
        "[password@]host[:port] or socket path (default: "
        "MPD_HOST/MPD_PORT, else localhost:6600)",
    )
    source.add_argument(
        "-s",
        "--scan",
        type=Path,
        metavar="DIR",
        help="read album artists from the tags of the music "
        "files under DIR (MP3, FLAC, Ogg, Opus, M4A)",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="look names up and show what would be followed; "
        "change nothing",
    )
    parser.add_argument(
        "-i",
        "--ignore",
        action="append",
        default=[],
        metavar="NAME",
        help="artist name to skip (repeatable); added to "
        "IMPORT_IGNORE_ARTISTS from .env",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one import and report what happened."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not arguments:
        parser.print_help()
        return 0
    rs.load_dotenv()
    args = parser.parse_args(arguments)
    ignore = (
        rs.split_comma_list(os.environ.get("IMPORT_IGNORE_ARTISTS"))
        + args.ignore
    )

    try:
        if args.scan is not None:
            try:
                import mutagen  # noqa: F401  (only checking it is available)
            except ImportError:
                rs.ensure_private_env(__file__, arguments)
            folder = args.scan.expanduser().resolve()
            names, unreadable = scan_album_artists(folder)
            source = "scan"
            print(
                f"Found {len(names)} artist name(s) in {folder}"
                + (
                    f" ({unreadable} unreadable file(s) skipped)"
                    if unreadable
                    else ""
                )
            )
        else:
            names = mpd_album_artists(args.mpd or None)
            source = "mpd"
            print(f"Found {len(names)} album artist(s) in MPD")
    except (SourceError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # A dry run before the first real import must not create the database.
    connection = rs.connect(
        ":memory:" if args.dry_run and not rs.DB_PATH.exists() else rs.DB_PATH
    )
    client = rs.DeezerClient()
    if args.dry_run:
        result = import_names(
            connection, client, names, source, ignore, dry_run=True
        )
    else:
        with rs.recorded_run(connection, f"import-{source}") as summary:
            result = import_names(connection, client, names, source, ignore)
            summary.update(
                {
                    key: (len(value) if isinstance(value, list) else value)
                    for key, value in result.items()
                }
            )
    verb = "Would follow" if args.dry_run else "Followed"
    print(
        f"\n{verb} {len(result['followed'])} new artist(s); "
        f"{result['known']} already known; {result['skipped']} skipped; "
        f"{len(result['unresolved'])} unresolved; "
        f"{len(result['errors'])} lookup failure(s)."
    )
    if result["unresolved"] and not args.dry_run:
        print(
            "See unresolved names with: "
            "python3 monitor_releases.py artists --unresolved"
        )
    if args.dry_run:
        print("Dry run: nothing was changed.")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
