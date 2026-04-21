#!/usr/bin/env bash
# Claude Assist — minimal Claude Code statusline.
# Writes context-usage.json to .claude/state/ so the Assist 'i' button works.
# Install: add to ~/.claude/settings.json:
#   "statusLine": {"type": "command", "command": "/path/to/assist/bin/statusline.sh"}

input=$(cat)

# Parse context + session data
read -r pct tokens limit model duration_ms cost <<< "$(echo "$input" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    cw = d.get('context_window', {})
    cur = cw.get('current_usage', {}) or {}
    tokens = (cur.get('input_tokens', 0) +
              cur.get('cache_read_input_tokens', 0) +
              cur.get('cache_creation_input_tokens', 0))
    theoretical = cw.get('context_window_size', 200000)
    limit = int(theoretical * 0.8)
    pct = round(min((tokens / limit * 100) if limit > 0 else 0, 100), 1)
    model = d.get('model', {}).get('id', '') or ''
    duration_ms = int(d.get('cost', {}).get('total_duration_ms', 0) or 0)
    cost = round(float(d.get('cost', {}).get('total_cost_usd', 0) or 0), 4)
    print(pct, tokens, limit, model, duration_ms, cost)
except Exception as e:
    print('0 0 160000  0 0')
" 2>/dev/null)"

cwd="$(echo "$input" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d.get('workspace', {}).get('current_dir') or d.get('cwd', ''))
" 2>/dev/null)"

ts="$(date +%s)"
payload="{\"used_percentage\":${pct:-0},\"tokens\":${tokens:-0},\"limit\":${limit:-160000},\"model\":\"${model}\",\"duration_ms\":${duration_ms:-0},\"cost_usd\":${cost:-0},\"updated\":$ts}"

# Always write to global ~/.claude/state/ (fallback for get_claude_meta)
mkdir -p "$HOME/.claude/state" 2>/dev/null
echo "$payload" > "$HOME/.claude/state/context-usage.json" 2>/dev/null

# Also write to project-level state dir if it exists
if [[ -n "$cwd" && -d "$cwd/.claude/state" ]]; then
    echo "$payload" > "$cwd/.claude/state/context-usage.json" 2>/dev/null
fi

# Compact statusline output
pct_int="${pct%.*}"
if   [[ "${pct_int:-0}" -ge 80 ]]; then col=$'\033[38;5;203m'
elif [[ "${pct_int:-0}" -ge 50 ]]; then col=$'\033[38;5;215m'
else                                      col=$'\033[38;5;114m'
fi
printf "%s%s%%%s" "$col" "${pct:-0}" $'\033[0m'
[[ -n "$model" ]] && printf " \033[38;5;242m%s\033[0m" "${model##*-}"
printf '\n'
