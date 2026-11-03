#!/usr/bin/env bash
# Installs deemix-batch's systemd --user units (timer-driven sync_to_dest.py).
#
# Usage: ./install.sh [staging-directory]
#   <staging-directory> is the local folder deemix downloads land in --
#   the same path you'd pass to download_artists.py -o. If omitted, it's
#   read from STAGING_DIR in .env instead.
set -Eeuo pipefail

if [ "$#" -gt 1 ]; then
    echo "Usage: $0 [staging-directory]" >&2
    exit 1
fi

staging_dir=${1-}
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
    echo "Usage: $0 [staging-directory]" >&2
    exit 1
fi

if [ ! -d "$staging_dir" ]; then
    echo "Warning: staging directory does not exist yet: $staging_dir" >&2
fi

mkdir -p "$user_unit_dir"

# Render systemd arguments directly; shell/sed escaping is not unit-file escaping.
python3 - "$script_dir" "$staging_dir" "$user_unit_dir/deemix-batch.service" <<'PYUNIT'
import os
from pathlib import Path
import sys
import tempfile

project = Path(sys.argv[1]).resolve()
staging = Path(sys.argv[2]).expanduser().resolve()
target = Path(sys.argv[3])


def quote(value, command=False):
    """Quote one systemd value, preserving literal specifier/environment syntax."""
    text = str(value)
    if any(character in text for character in ('\n', '\r', '\0')):
        raise SystemExit('Paths must not contain line breaks or NUL characters')
    text = text.replace('%', '%%')
    if command:
        text = text.replace('$', '$$')
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


source = (project / 'systemd/deemix-batch.service').read_text()
lines = []
for line in source.splitlines():
    if line.startswith('WorkingDirectory='):
        quote(project)  # Validate line breaks; this directive takes an unquoted path.
        line = 'WorkingDirectory=' + str(project).replace('%', '%%')
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
cp "$script_dir/systemd/deemix-batch.timer" "$user_unit_dir/deemix-batch.timer"

systemctl --user daemon-reload
systemctl --user enable --now deemix-batch.timer

echo
echo "Installed and started. Next scheduled run:"
systemctl --user list-timers deemix-batch.timer --no-pager
