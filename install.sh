#!/usr/bin/env bash
# Installs deemix-batch's systemd --user units (timer-driven sync_to_dest.py,
# and optionally the daily release monitor).
#
# Usage: ./install.sh [-r|--release-monitor] [staging-directory]
#   <staging-directory> is the local folder deemix downloads land in --
#   the same path you'd pass to download_artists.py -o. If omitted, it's
#   read from STAGING_DIR in .env instead.
#   -r/--release-monitor also installs release-monitor.timer, which runs
#   "monitor_releases.py check --download" once a day.
set -Eeuo pipefail

usage="Usage: $0 [-r|--release-monitor] [staging-directory]"
release_monitor=false
staging_dir=
for argument in "$@"; do
    case "$argument" in
        -r|--release-monitor) release_monitor=true ;;
        -h|--help) echo "$usage"; exit 0 ;;
        -*) echo "Unknown option: $argument" >&2; echo "$usage" >&2; exit 1 ;;
        *)
            if [ -n "$staging_dir" ]; then
                echo "$usage" >&2
                exit 1
            fi
            staging_dir=$argument
            ;;
    esac
done
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
user_unit_dir="$HOME/.config/systemd/user"

if [ ! -f "$script_dir/.env" ]; then
    echo "Error: $script_dir/.env not found." >&2
    echo "Run: cp .env.example .env, then fill in STAGING_DIR / DEST_PATH / DEST_SUBPATH first." >&2
    exit 1
fi

if [ -z "$staging_dir" ]; then
    staging_dir=$(python3 - "$script_dir/.env" <<'PYENV'
import sys
from pathlib import Path

for line in Path(sys.argv[1]).read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    key, value = line.split('=', 1)
    if key.strip() == 'STAGING_DIR':
        print(value.strip().strip('"').strip("'"))
        break
PYENV
    )
fi

if [ -z "$staging_dir" ]; then
    echo "Error: no staging directory given, and STAGING_DIR isn't set in .env." >&2
    echo "$usage" >&2
    exit 1
fi

if [ ! -d "$staging_dir" ]; then
    echo "Warning: staging directory does not exist yet: $staging_dir" >&2
fi

mkdir -p "$user_unit_dir"

# Render systemd arguments directly; shell/sed escaping is not unit-file escaping.
render_unit() {
python3 - "$script_dir" "$staging_dir" "$user_unit_dir/$1.service" "$1" <<'PYUNIT'
import os
from pathlib import Path
import sys
import tempfile

project = Path(sys.argv[1]).resolve()
staging = Path(sys.argv[2]).expanduser().resolve()
target = Path(sys.argv[3])
unit = sys.argv[4] if len(sys.argv) > 4 else 'deemix-batch'


def quote(value, command=False):
    """Quote one systemd value, preserving literal specifier/environment syntax."""
    text = str(value)
    if any(character in text for character in ('\n', '\r', '\0')):
        raise SystemExit('Paths must not contain line breaks or NUL characters')
    text = text.replace('%', '%%')
    if command:
        text = text.replace('$', '$$')
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


source = (project / 'systemd' / f'{unit}.service').read_text()
lines = []
for line in source.splitlines():
    if line.startswith('WorkingDirectory='):
        quote(project)  # Validate line breaks; this directive takes an unquoted path.
        line = 'WorkingDirectory=' + str(project).replace('%', '%%')
    elif line.startswith('ExecStart=') and unit == 'release-monitor':
        # Same staging folder the sync service watches; overrides .env.
        lines.append('Environment=' + quote(f'STAGING_DIR={staging}'))
        line = ('ExecStart=' + quote(project / 'monitor_releases.py', command=True)
                + ' check --download')
    elif line.startswith('ExecStart='):
        line = ('ExecStart=' + quote(project / 'sync_to_dest.py', command=True)
                + ' --source ' + quote(staging, command=True))
    lines.append(line)
temporary = None
try:
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                     dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write('\n'.join(lines) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
finally:
    if temporary is not None:
        temporary.unlink(missing_ok=True)
PYUNIT
}

render_unit deemix-batch
cp "$script_dir/systemd/deemix-batch.timer" "$user_unit_dir/deemix-batch.timer"
timers=(deemix-batch.timer)
if [ "$release_monitor" = true ]; then
    render_unit release-monitor
    cp "$script_dir/systemd/release-monitor.timer" \
        "$user_unit_dir/release-monitor.timer"
    timers+=(release-monitor.timer)
fi

systemctl --user daemon-reload
systemctl --user enable --now "${timers[@]}"

echo
echo "Installed and started. Next scheduled run(s):"
systemctl --user list-timers "${timers[@]}" --no-pager
