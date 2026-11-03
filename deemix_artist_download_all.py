#!/usr/bin/env python3
"""Download artist releases using deemix's own discography selection.

Requires Python 3.10+ and a configured deemix CLI supporting artist URLs.
Automatically creates .deemix-artist-venv beside this script and installs
deezer-python there on first use. Later runs reuse it. Internet is required
for initial setup and artist lookup. Your existing deemix CLI stays separate.
Targets the installed legacy Python `deemix` executable, not a Docker command.
No credentials are handled by this script.

Default: matches the inspected WebUI's normal discography click / All tab.
Artist name searches use Deezer's strict mode, including --list.
Multiple exact-name matches are ranked by fan count; --pick overrides this.
With --list, --output and --bitrate are accepted but unused.
--discography: includes the separate featured releases tab as well.
--include-top: additionally requests deemix's native top-tracks collection.

Examples:
    python3 deemix_artist_download_all.py "Daft Punk" 13 -o ~/Music -b 320
    python3 deemix_artist_download_all.py 27 --discography --include-top
    python3 deemix_artist_download_all.py "Daft Punk" --dry-run

The inspected TypeScript CLI at commit 3ac2576 has an artist-array concat bug;
it needs fixing before using it. The locally installed legacy Python CLI
handles artist arrays correctly. Exit zero only means CLI processes returned
zero: deemix may log and suppress individual release/track failures.
"""

import argparse
import os
from itertools import islice
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import venv

# -I isolates third-party imports; explicitly allow this project's lock helper.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_safety import (OperationBusyError, inherited_lock_fds, operation_lock)

DEEZER_PYTHON_VERSION = "7.4.0"


def ensure_environment(argv: list[str]) -> None:
    """Bootstrap a private interpreter, without activating/changing CLI PATH."""
    script = Path(__file__).resolve()
    environment = script.parent / ".deemix-artist-venv"
    if Path(sys.prefix).resolve() == environment.resolve():
        return
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if environment.exists() and not (environment / "pyvenv.cfg").is_file():
        raise RuntimeError(f"Refusing to modify an unrelated directory: {environment}")
    if not python.exists():
        print(f"Creating private Python environment: {environment}", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    probe = [str(python), "-I", "-c",
             "from deezer import Client; from importlib.metadata import version; "
             f"assert version('deezer-python') == {DEEZER_PYTHON_VERSION!r}"]
    if subprocess.run(probe, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        pip_probe = [str(python), "-I", "-m", "pip", "--version"]
        if subprocess.run(pip_probe, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode:
            print("Repairing pip in the private environment...", flush=True)
            try:
                subprocess.run([str(python), "-I", "-m", "ensurepip", "--upgrade"],
                               check=True)
                subprocess.run(pip_probe, check=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise RuntimeError(
                    f"Could not restore pip in {environment}. Ensure this Python "
                    "installation includes venv/ensurepip support; on Ubuntu "
                    "install the matching python3-venv package, then rerun."
                ) from exc
        print("Installing deezer-python in the private environment...", flush=True)
        subprocess.run([str(python), "-I", "-m", "pip", "install",
                        "--disable-pip-version-check", "--no-input",
                        f"deezer-python=={DEEZER_PYTHON_VERSION}"],
                       check=True)
        subprocess.run(probe, check=True)
    # -I ignores ambient Python package paths. No shell activation, PATH change,
    # or working-directory change: ./music still means the caller's ./music.
    os.execv(str(python), [str(python), "-I", str(script), *argv])


def nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def positive(value: str) -> int:
    number = nonnegative(value)
    if number == 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def describe_artist(artist) -> str:
    # Avoid lazy detail requests for every search result if nb_fan is absent.
    fans = vars(artist).get("nb_fan")
    label = f"{fans:,}" if isinstance(fans, int) else "unknown"
    return f"{artist.name} (ID: {artist.id}, fans: {label})"


def resolve_artist(client, name_or_id: str, pick: int | None = None) -> tuple[int, str]:
    query = name_or_id.strip()
    if not query:
        raise ValueError("Artist name cannot be empty")
    if re.fullmatch(r"[0-9]+", query):
        artist_id = int(query)
        if artist_id <= 0:
            raise ValueError("Artist ID must be positive")
        artist = client.get_artist(artist_id)
        return int(artist.id), str(artist.name)

    print(f"Searching Deezer for {query!r}...", flush=True)
    # Iterate across pages, but fetch only enough to display/select a result.
    matches = list(islice(client.search_artists(query, strict=True), max(10, (pick + 1) if pick is not None else 10)))
    if not matches:
        raise ValueError(f"No artist found for {query!r}")
    for index, artist in enumerate(matches[:10]):
        print(f"  [{index}] {describe_artist(artist)}")
    if pick is None:
        exact = [i for i, artist in enumerate(matches)
                 if artist.name.strip().casefold() == query.casefold()]
        if not exact:
            raise ValueError("No exact match in the first ten results. "
                             "Use --pick N or an artist ID to choose explicitly.")
        if len(exact) == 1:
            pick = exact[0]
        else:
            fan_counts = {}
            for index in exact:
                fans = vars(matches[index]).get("nb_fan")
                if not isinstance(fans, int):
                    # Search responses may omit fan counts; obtain details only
                    # for candidates needed to resolve an exact-name tie.
                    fans = getattr(client.get_artist(matches[index].id), "nb_fan", None)
                if not isinstance(fans, int) or fans < 0:
                    raise ValueError("Cannot compare fan counts for exact matches. "
                                     "Use --pick N or an artist ID.")
                fan_counts[index] = fans
            pick = max(exact, key=fan_counts.__getitem__)
            print(f"  ({len(exact)} exact matches; chose the most-followed "
                  f"with {fan_counts[pick]:,} fans; equal counts use search order)")
    if pick >= len(matches):
        raise ValueError(f"Pick {pick} is out of range ({len(matches)} results)")
    chosen = matches[pick]
    print(f"Using [{pick}] {chosen.name} (ID: {chosen.id})")
    return int(chosen.id), str(chosen.name)


def get_artist_urls(artist_id: int, include_top: bool = False,
                    discography: bool = False) -> list[str]:
    base = f"https://www.deezer.com/artist/{artist_id}"
    urls = [base + "/discography" if discography else base]
    if include_top:
        urls.append(base + "/top_track")
    return urls


def list_artists(client, name_or_id: str, limit: int = 100,
                 fetch_fans: bool = False) -> None:
    query = name_or_id.strip()
    if not query:
        raise ValueError("Artist name cannot be empty")
    print(f"Artist matches for {query!r}:", flush=True)
    if re.fullmatch(r"[0-9]+", query):
        artist_id = int(query)
        if artist_id <= 0:
            raise ValueError("Artist ID must be positive")
        artist = client.get_artist(artist_id)
        if fetch_fans:
            print(f"  {describe_artist(artist)}")
        else:
            print(f"  {artist.name} (ID: {artist.id})")
        return
    count = 0
    for index, artist in enumerate(islice(client.search_artists(query, strict=True), limit)):
        if fetch_fans and not isinstance(vars(artist).get("nb_fan"), int):
            artist = client.get_artist(artist.id)
        print(f"  [{index}] {describe_artist(artist)}", flush=True)
        count += 1
    if not count:
        raise ValueError(f"No artist found for {query!r}")
    print(f"{count} match(es). Use artist IDs to download multiple matches.")
    if count == limit:
        print(f"Listing limit {limit} reached; increase --limit to look for more.")


def download_with_deemix(urls: list[str], output_dir: str | None,
                         bitrate: str, executable: str,
                         dry_run: bool = False) -> int:
    base = [executable, "--bitrate", bitrate]
    if output_dir is not None:
        base += ["--path", str(Path(output_dir).expanduser().resolve())]
    failures = 0
    for url in urls:
        command = base + [url]
        print(shlex.join(command), flush=True)
        if dry_run:
            continue
        try:
            subprocess.run(command, check=True, pass_fds=inherited_lock_fds())
        except subprocess.CalledProcessError as exc:
            failures += 1
            print(f"CLI failed for {url} (exit {exc.returncode})", file=sys.stderr)
        except OSError as exc:
            print(f"Cannot execute deemix: {exc}", file=sys.stderr)
            return failures + 1
    return failures


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("artists", nargs="+", help="Artist names or Deezer IDs")
    parser.add_argument("-o", "--output", help="Download directory")
    parser.add_argument("-b", "--bitrate", default="320",
                        choices=["128", "320", "flac"],
                        help="Audio quality to download (default: 320)")
    parser.add_argument("--include-top", action="store_true",
                        help="Also download the native top-tracks collection")
    parser.add_argument("--pick", type=nonnegative, default=None,
                        help="Override exact-name matching; index applies to EVERY name")
    parser.add_argument("--discography", action=argparse.BooleanOptionalAction, default=False,
                        help="Include featured releases via /discography "
                             "(--no-discography explicitly turns it back off, "
                             "e.g. if download_artists.py defaulted it on from .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve artists and print commands; do not download")
    parser.add_argument("--list", action="store_true",
                        help="List artist search matches and IDs; do not download")
    parser.add_argument("--fans", action="store_true",
                        help="With --list, fetch missing fan counts (extra API requests)")
    parser.add_argument("--limit", type=positive, default=None,
                        help="Requires --list; maximum matches displayed (default: 100)")
    parser.add_argument("--deemix", default="deemix", metavar="EXECUTABLE",
                        help="deemix executable name or path")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if args.fans and not args.list:
        parser.error("--fans requires --list")
    if args.limit is not None and not args.list:
        parser.error("--limit requires --list")
    if args.list:
        for option, enabled in (("--pick", args.pick is not None),
                                ("--dry-run", args.dry_run),
                                ("--include-top", args.include_top),
                                ("--discography", args.discography)):
            if enabled:
                parser.error(f"{option} cannot be combined with --list")
    if args.limit is None:
        args.limit = 100
    executable = shutil.which(str(Path(args.deemix).expanduser()))
    if executable is None and not args.dry_run and not args.list:
        parser.error(f"deemix executable not found: {args.deemix}")

    try:
        ensure_environment(argv)
        from deezer import Client
        from deezer.exceptions import DeezerAPIException
        from httpx import RequestError
    except (OSError, RuntimeError, subprocess.CalledProcessError, ImportError) as exc:
        print(f"Private Python environment setup failed: {exc}", file=sys.stderr)
        return 1

    if args.pick is not None and not args.list:
        names = [item for item in args.artists
                 if not re.fullmatch(r"[0-9]+", item.strip())]
        if len(names) > 1:
            print(f"Warning: --pick {args.pick} overrides exact matching for "
                  f"all {len(names)} artist names. Use an artist ID to select "
                  "a specific artist unambiguously.", file=sys.stderr)

    failures = 0
    urls = []
    client = Client()
    with client:
        for item in args.artists:
            try:
                if args.list:
                    list_artists(client, item, args.limit, args.fans)
                    continue
                artist_id, artist_name = resolve_artist(client, item, args.pick)
                print(f"Selected {artist_name} (ID: {artist_id})")
                urls.extend(get_artist_urls(artist_id, args.include_top,
                                            args.discography))
            except (ValueError, DeezerAPIException, RequestError) as exc:
                failures += 1
                print(f"Could not resolve {item!r}: {exc}", file=sys.stderr)

    if args.list:
        return 1 if failures else 0

    unique_urls = list(dict.fromkeys(urls))
    failures += download_with_deemix(unique_urls, args.output, args.bitrate,
                                    executable or args.deemix, args.dry_run)
    if failures:
        print(f"Finished with {failures} resolution/CLI failure(s).", file=sys.stderr)
        return 1
    if args.dry_run:
        print("Preview complete; no download commands were executed.")
    else:
        print("CLI commands finished. Check deemix output for skipped/failed tracks.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Coordinate standalone downloads with the batch, repairs, and sync."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or "--help" in arguments or "-h" in arguments:
        return run(arguments)
    try:
        with operation_lock():
            return run(arguments)
    except (OperationBusyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
