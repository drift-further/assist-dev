#!/bin/bash
# Link project agents to home directory

if [ -d "/workspace/.claude/agents" ] && [ "$(ls -A /workspace/.claude/agents 2>/dev/null)" ]; then
    echo "🔗 Linking project agents..."
    for agent in /workspace/.claude/agents/*; do
        if [ -f "$agent" ]; then
            agent_name=$(basename "$agent")
            agent_base="${agent_name%.md}"
            ln -sf "$agent" "/home/developer/.claude/agents/project-${agent_base}.md"
            echo "   ✓ Linked project agent: $agent_name"
        fi
    done
fi