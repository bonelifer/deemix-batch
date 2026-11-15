# TODO

## Release monitor

- **Self-hosted Muspy as an extra source.** [blindndangerous/muspy](https://github.com/blindndangerous/muspy)
  is a Django 6 rewrite of Muspy that can be self-hosted (Postgres, RabbitMQ,
  Redis and two Celery workers). It reports MusicBrainz releases, often before
  Deezer has them, and has its own release pages, feeds and email notices.
  Planned as a separate homelab stack first. Later, `monitor_releases.py`
  could read its `/api/v1/releases/?artist_mbid=` endpoint to flag releases
  Deezer doesn't have yet (matched by artist MusicBrainz ID, which
  `import_artists.py --scan` already stores).
- **Beets as an artist source.** Once the music library rebuild is done,
  `import_artists.py` could read artists from a `beet export`.
