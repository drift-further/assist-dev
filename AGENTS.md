# Agent Instructions

Before starting work in this repository:

1. Read [`CLAUDE.md`](CLAUDE.md) completely. Its architecture, development, change-control, verification, and repository-ownership rules apply to all agents.
2. Discover the available skills before acting:
   - Project-local skills: `.claude/skills/*/SKILL.md`
   - Global skills: `~/.claude/skills/*/SKILL.md`
   - Canonical shared-skill sources: `/home/daniel/source/drift/drift-further_daic/.claude/skills/*/SKILL.md`
3. Load and follow every skill relevant to the task before making changes. Start with the core contracts named in `CLAUDE.md`, especially `architecture-contract`, `change-control`, and `validation-and-qa` when applicable.

Do not assume a file is canonical, generated, or safe to edit until the ownership guidance in `CLAUDE.md` and the relevant skills has been checked.
