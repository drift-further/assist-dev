#!/bin/bash
# Generic CLI proxy — forwards commands from inside the container
# to the host Assist server, which runs them against ASSIST_CLI_BIN.
#
# This script is name-agnostic. Symlink/install it under whatever name the
# host CLI is conventionally invoked as (e.g. /usr/local/bin/karen,
# /usr/local/bin/mycli). $0 is used in usage/error messages so each
# install presents itself as the configured command.
#
# Wire-format: POST {"args": [...], "files": [{"name", "data"}]} to
# http://$ASSIST_PROXY_HOST:$ASSIST_PROXY_PORT/api/cli-proxy
# Response: {"stdout": "...", "stderr": "...", "returncode": N}
#
# File-upload convention: -f/--file <path> args are base64-encoded and
# sent in the "files" array; the original arg becomes a __PROXY_FILE_N__
# token that the host resolves to a real path before invoking the CLI.

HOST="${ASSIST_PROXY_HOST:-10.0.0.101}"
PORT="${ASSIST_PROXY_PORT:-8089}"
CMD_NAME="$(basename "$0")"

if [ $# -eq 0 ]; then
    echo "Usage: $CMD_NAME <subcommand> [args...]"
    echo "Proxied to host CLI via Assist at $HOST:$PORT"
    exit 1
fi

# Scan args for -f/--file flags and collect files to upload.
# Replace file paths in args with placeholder tokens.
ARGS=()
FILES_JSON=""
FILE_IDX=0

while [[ $# -gt 0 ]]; do
    case $1 in
        -f|--file)
            if [ -z "$2" ]; then
                echo "Error: $1 requires a file path" >&2
                exit 1
            fi
            FPATH="$2"
            if [ ! -f "$FPATH" ]; then
                echo "Error: file not found: $FPATH" >&2
                exit 1
            fi
            FNAME=$(basename "$FPATH")
            B64=$(base64 -w0 "$FPATH")
            if [ -n "$FILES_JSON" ]; then
                FILES_JSON="$FILES_JSON,"
            fi
            FILES_JSON="$FILES_JSON{\"name\":\"$FNAME\",\"data\":\"$B64\"}"
            ARGS+=("$1")
            ARGS+=("__PROXY_FILE_${FILE_IDX}__")
            FILE_IDX=$((FILE_IDX + 1))
            shift 2
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done

# Build JSON args array
ARGS_JSON="["
FIRST=true
for arg in "${ARGS[@]}"; do
    if [ "$FIRST" = true ]; then
        FIRST=false
    else
        ARGS_JSON="$ARGS_JSON,"
    fi
    escaped=$(echo "$arg" | sed 's/\\/\\\\/g; s/"/\\"/g')
    ARGS_JSON="$ARGS_JSON\"$escaped\""
done
ARGS_JSON="$ARGS_JSON]"

if [ -n "$FILES_JSON" ]; then
    BODY="{\"args\": $ARGS_JSON, \"files\": [$FILES_JSON]}"
else
    BODY="{\"args\": $ARGS_JSON}"
fi

RESPONSE=$(curl -s -X POST "http://$HOST:$PORT/api/cli-proxy" \
    -H "Content-Type: application/json" \
    -d "$BODY" 2>&1)

if [ $? -ne 0 ]; then
    echo "Error: cannot reach Assist at $HOST:$PORT" >&2
    exit 1
fi

if command -v jq &>/dev/null; then
    STDOUT=$(echo "$RESPONSE" | jq -r '.stdout // empty')
    STDERR=$(echo "$RESPONSE" | jq -r '.stderr // empty')
    RC=$(echo "$RESPONSE" | jq -r '.returncode // 1')
    ERROR=$(echo "$RESPONSE" | jq -r '.error // empty')
else
    STDOUT=$(echo "$RESPONSE" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('stdout',''),end='')" 2>/dev/null)
    STDERR=$(echo "$RESPONSE" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('stderr',''),end='')" 2>/dev/null)
    RC=$(echo "$RESPONSE" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('returncode',1))" 2>/dev/null)
    ERROR=$(echo "$RESPONSE" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('error',''),end='')" 2>/dev/null)
fi

[ -n "$ERROR" ] && echo "Error: $ERROR" >&2
[ -n "$STDOUT" ] && echo "$STDOUT"
[ -n "$STDERR" ] && echo "$STDERR" >&2
exit "${RC:-1}"
