#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 (--positive-and-default-xdg-negative|--positive-only)" >&2
    exit 2
fi

case "$1" in
    --positive-and-default-xdg-negative|--positive-only) ;;
    *)
        echo "usage: $0 (--positive-and-default-xdg-negative|--positive-only)" >&2
        exit 2
        ;;
esac

: "${EFFORT510_ASSIST_ROOT:?missing EFFORT510_ASSIST_ROOT}"
: "${EFFORT510_CANONICAL_ASSIST:?missing EFFORT510_CANONICAL_ASSIST}"
: "${EFFORT510_ACTIVATION_ROOT:?missing EFFORT510_ACTIVATION_ROOT}"

install -d -m 0700 "$EFFORT510_ACTIVATION_ROOT"
release_run_root="$(mktemp -d "$EFFORT510_ACTIVATION_ROOT/handoff.XXXXXX")"
chmod 0700 "$release_run_root"

"$EFFORT510_ASSIST_ROOT/.venv/bin/python3" \
    "$EFFORT510_ASSIST_ROOT/tools/release/park_activation_handoff_harness.py" \
    --assist-root "$EFFORT510_ASSIST_ROOT" \
    --canonical "$EFFORT510_CANONICAL_ASSIST" \
    --run-root "$release_run_root" \
    "$1"
