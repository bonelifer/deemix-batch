#!/usr/bin/bash
# Run the offline test suite in a throwaway container, on the oldest
# supported Python (3.10) and on the newest, the same way CI does.
#
# Usage: tests/docker-test.sh [python-version ...]   (default: 3.10 3)
# The project is mounted read-only; nothing is written to the host.
set -Eeuo pipefail

project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
versions=("$@")
if [ "${#versions[@]}" -eq 0 ]; then
    versions=(3.10 3)
fi

for version in "${versions[@]}"; do
    echo "=== Python ${version} ==="
    docker run --rm --network bridge \
        --name "deemix-batch-tests-${version//./-}-$$" \
        -v "${project}:/src:ro" -w /tmp \
        "python:${version}-slim" \
        bash -c 'mkdir /tmp/project && cd /src \
            && tar --exclude=./state --exclude=./.env --exclude=./music \
                --exclude=./.release-monitor-venv --exclude=./.deemix-artist-venv \
                -cf - . | tar -xf - -C /tmp/project \
            && cd /tmp/project \
            && pip install --quiet --disable-pip-version-check mutagen \
            && python -m py_compile *.py tests/*.py \
            && bash -n install.sh \
            && python -m unittest discover -s tests'
done
