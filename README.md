# deemix-batch

Downloads an artist list with `deemix`, then moves each finished artist's
folder wherever you actually want it: a NAS mount, a MusicBrainz Picard
inbox, any other local folder.

> Use this only for content you're legally allowed to download with your own
> Deezer account. You're responsible for how you use it.

## What's actually happening here

Three scripts, each doing one job.

`deemix_artist_download_all.py` looks up an artist on Deezer and downloads
their whole discography through the `deemix` CLI. It's blocking, so it just
sits there until the download's done. First run, it builds its own private
venv (`.deemix-artist-venv`), so there's nothing to `pip install` yourself.

`download_artists.py` is the one you actually run. It reads `artists.txt`
and calls the downloader once per artist, one at a time, so a failure only
ever means one artist, not the whole batch. Every result gets logged to
`state/download_status.json`. Succeed, and the artist's line is recorded in `done.txt` before it is
removed from `artists.txt`, so an interrupted update cannot lose the artist.
Preview and list runs leave both lists and completion status unchanged. Fail, and it just stays in `artists.txt`
for next time. Nothing's blocked: you just run it again later once you've
figured out why.

`sync_to_dest.py` handles the last step, moving each finished artist folder
into `DEST_PATH`. It waits until nothing in the folder has changed for `--stable-for`
seconds. A shared operation lock prevents the batch downloader, standalone
downloader, track-repair scripts, and sync from running at the same time.
Old completion records cannot bypass the stability check.

## Setup

You'll need Python 3.10+ and a working `deemix` CLI, already logged in.
(`download_artists.py` and `sync_to_dest.py` are stdlib-only;
`deemix_artist_download_all.py` manages its own venv.) `DEST_PATH` has to
already exist too. This won't mount or create it for you.

Installing `deemix` gets you the CLI along with it:

```sh
pip install --user deemix
```

First time you run it against any URL, it'll ask you to paste your Deezer
ARL and save it to `~/.config/deemix/.arl` so you never have to enter it
again. To grab the ARL: log into deezer.com, open DevTools, go to
Application (or Storage) -> Cookies -> deezer.com, and copy the value of the
`arl` cookie.

```sh
cp .env.example .env      # fill in STAGING_DIR, DEST_PATH, DEST_SUBPATH
printf 'Some Artist\nAnother Artist\n' > artists.txt
```

`STAGING_DIR` is optional but saves you repeating the same path on both
scripts' command lines. Set it once in `.env` and neither script's own flag
(`-o` / `--source`) is required anymore, though passing one explicitly still
overrides it.

An `artists.txt` line can be an artist name or a Deezer artist ID. Blank
lines and lines starting with `#` are ignored.

## Usage

```sh
# Download everything in artists.txt, one artist at a time
python3 download_artists.py -- --bitrate flac --discography

# Show the child command, unbuffered output, and a 30-second wait heartbeat
python3 download_artists.py --debug -- --discography

# Move whatever's finished to DEST_PATH
python3 sync_to_dest.py --dry-run
python3 sync_to_dest.py
```

That's with `STAGING_DIR` set in `.env`. Without it, add `-o ~/deemix-downloads`
to the first command and `--source ~/deemix-downloads` to the second.

`--copy` on `sync_to_dest.py` copies instead of moving (keeps local copies).
Completion and sync history are kept under `state/`; sync logs go to
`logs/`. Sync history does not prevent later albums or repaired tracks from
being synced. Overlapping source/destination paths and symlink entries are
rejected; files are replaced atomically. Unchanged copies are reused.

`retry_failed_tracks.py -s` retries tracks in `errors.txt`.
`check_missing_tracks.py -s` checks existing album folders for missing tracks.
Both accept `-s /path/to/music`; bare `-s` uses `STAGING_DIR`. Repairs count
as successful only when the expected ISRC is readable in the original album
folder (requires `mutagen`, also used by deemix). An unavailable track remains
unresolved and can be retried on a later run. Failed batches, sync failures,
and unverified repairs return a nonzero exit status.

State writes are atomic. Completion updates are recoverable across
interruptions, and repeated completion entries are not duplicated. If another
operation owns the lock, the new command exits with a clear message instead
of touching its files. Wait for the existing operation to finish before retrying.

## Running automatically

`systemd/deemix-batch.service` + `deemix-batch.timer` run `sync_to_dest.py`
on a 15-minute timer (user-level systemd, no root needed):

```sh
./install.sh ~/deemix-downloads   # your real staging directory
```

This installs both units to `~/.config/systemd/user/`, substituting your
staging directory into `ExecStart`, then enables and starts the timer. It
requires `.env` to already exist (`cp .env.example .env` first).

To install by hand instead:

```sh
mkdir -p ~/.config/systemd/user
cp systemd/deemix-batch.{service,timer} ~/.config/systemd/user/
# then edit --source in ~/.config/systemd/user/deemix-batch.service yourself
systemctl --user daemon-reload
systemctl --user enable --now deemix-batch.timer
```

`download_artists.py` isn't timered. That one's meant to be run by hand,
whenever you've actually got artists worth downloading.

## Tests

Run the offline safety regression suite:

```sh
python3 -m unittest discover -s tests -v
```

## Contributing

Contributions are welcome!

- **Bug reports**: [Open an issue](https://github.com/bonelifer/deemix-batch/issues).
- **Everything else** (questions, feature requests, ideas, general discussion): [Use Discussions](https://github.com/bonelifer/deemix-batch/discussions).
- Pull requests are welcome for bug fixes or discussed features.

## Acknowledgments

- `merge_move()` in `sync_to_dest.py` is adapted from
  [deemix-bulk](https://github.com/mylegitches/deemix-bulk)'s
  `deemix_sync.py` (MIT License, Copyright (c) 2026 Adam Reue; full notice in
  [LICENSES/deemix-bulk-MIT.txt](LICENSES/deemix-bulk-MIT.txt)).
- Code review, bug fixes, and documentation assisted by [Claude](https://www.anthropic.com/claude).

## License

This project is licensed under the **GNU General Public License v3.0**.

See [LICENSE](LICENSE) for more information.
