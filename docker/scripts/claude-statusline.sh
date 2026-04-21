#!/bin/bash
# Claude Code StatusLine for claude-mount containers
# Shows: context usage bar + percentage | model name

input=$(cat)

# Extract model name and context data in a single python call
read -r pct tokens limit model_name < <(echo "$input" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    model = data.get('model', {}).get('display_name', 'Claude')
    cw = data.get('context_window', {})
    current = cw.get('current_usage', {}) or {}
    tokens = current.get('input_tokens', 0) + current.get('cache_read_input_tokens', 0) + current.get('cache_creation_input_tokens', 0)
    usable = int(cw.get('context_window_size', 200000) * 0.8)
    pct = min((tokens / usable) * 100, 100.0) if tokens > 0 else 0
    print(f'{pct:.1f} {tokens} {usable} {model}')
except:
    print('0.0 0 160000 Claude')
" 2>/dev/null)

# Colors
GREEN="\033[38;5;114m"
ORANGE="\033[38;5;215m"
RED="\033[38;5;203m"
GRAY="\033[38;5;242m"
TEXT="\033[38;5;250m"
CYAN="\033[38;5;111m"
RESET="\033[0m"

# Color based on usage
pct_int=${pct%.*}
if [[ $pct_int -lt 50 ]]; then
    bar_color="$GREEN"
elif [[ $pct_int -lt 80 ]]; then
    bar_color="$ORANGE"
else
    bar_color="$RED"
fi

# Progress bar (10 blocks)
filled=$((pct_int / 10))
[[ $filled -gt 10 ]] && filled=10
empty=$((10 - filled))

bar="${bar_color}"
for ((i=0; i<filled; i++)); do bar+="█"; done
bar+="${GRAY}"
for ((i=0; i<empty; i++)); do bar+="░"; done
bar+="${RESET}"

tokens_k=$((tokens / 1000))
limit_k=$((limit / 1000))

echo -e "${bar} ${TEXT}${pct}% (${tokens_k}k/${limit_k}k) ${GRAY}│${RESET} ${CYAN}${model_name}${RESET}"
