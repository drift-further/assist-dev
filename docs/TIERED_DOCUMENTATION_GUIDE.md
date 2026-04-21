# Tiered Documentation System Guide

**Version**: 1.0
**Date**: 2026-01-28

This guide explains how to organize project documentation for optimal AI-assisted development with DAIC.

---

## Overview

The Tiered Documentation System organizes documentation into three access tiers based on frequency of use, reducing AI context consumption while maintaining comprehensive documentation.

| Tier | Purpose | Size Limit | When Loaded |
|------|---------|------------|-------------|
| **HOT** | Current session context | 200 lines max | Every session start |
| **WARM** | Architectural truth, patterns | ~500 lines each | On-demand |
| **COLD** | Historical archives | Unlimited | Rarely, when needed |

### Benefits

Based on Cortex TMS methodology:
- **60-70% token reduction** per query through focused context
- **70-75% fewer file reads** per session
- **Faster session startup** with clear current state
- **Better session continuity** across context windows

---

## HOT Tier: ACTIVE_CONTEXT.md

### Purpose

A single, compact document that captures the current state of work. Loaded automatically at every session start by the DAIC hook system.

### Location

```
your-project/
└── docs/
    └── ACTIVE_CONTEXT.md    <-- HOT tier (max 200 lines)
```

### Rules

1. **Maximum 200 lines** - enforced by session-start hook (warns if exceeded)
2. **Update at session boundaries** - not continuously during work
3. **Reference, don't duplicate** - link to detailed docs instead of copying content
4. **Delete resolved items** - keep it current, not historical

### Template

See `templates/ACTIVE_CONTEXT.template.md` for a starter template.

### What to Include

| Section | Lines | Content |
|---------|-------|---------|
| Header | 5 | Task name, branch, mode, last updated |
| Current Focus | 20 | What we're doing, decisions made, blockers |
| Quick References | 50 | Critical files, active specialists |
| Recent Context | 50 | Session history, recent changes |
| Navigation | 30 | Links to relevant WARM tier docs |
| Do Not Forget | 10 | Critical items that must persist |

### What NOT to Include

- Full implementation details (put in task files)
- Complete code examples (link to pattern docs)
- Historical decisions (move to archives)
- Anything that doesn't change frequently

---

## WARM Tier: On-Demand References

### Purpose

Architectural truth, patterns, and detailed documentation accessed when needed but not loaded every session.

### Recommended Structure

```
docs/
├── ACTIVE_CONTEXT.md           # HOT tier
├── PROMPTS.md                  # AI interaction templates
├── architecture/               # WARM tier
│   ├── README.md
│   └── [system]-architecture.md
├── catalogs/                   # WARM tier
│   ├── MASTER_CATALOG_INDEX.md
│   └── [domain]/
├── patterns/                   # WARM tier
│   └── CODE_PATTERNS.md
└── backlog/                    # Active tasks
    ├── BACKLOG_INDEX.md
    ├── [active-tasks].md
    └── archive/                # COLD tier
```

### WARM Tier Documents

| Document | Purpose | Size Target |
|----------|---------|-------------|
| `architecture/*.md` | System design, data flows | ~500 lines |
| `catalogs/*.md` | File indexes, component maps | ~300 lines |
| `patterns/*.md` | Code patterns with Do/Don't | ~400 lines |
| `PROMPTS.md` | AI interaction templates | ~300 lines |

---

## COLD Tier: Archives

### Purpose

Historical records preserved for reference but not actively loaded.

### Structure

```
docs/backlog/archive/
├── index.md              # Archive navigation
├── 2025-08/              # Month-based organization
│   └── [completed-task].md
├── 2025-09/
└── ...
```

### Archive Triggers

Move items to COLD tier when:
- Task status is `COMPLETED` or `CLOSED`
- No updates for 90+ days
- All phases of multi-phase task complete
- User explicitly requests archival

### Retrieval

Archived items retain full content. To reactivate:
1. Copy from `archive/YYYY-MM/` to active backlog
2. Update status from ARCHIVED to PENDING
3. Update `ACTIVE_CONTEXT.md` with new task

---

## PROMPTS.md: AI Interaction Templates

### Purpose

Standardized prompts for common AI interactions, ensuring consistent specialist engagement and session management.

### Recommended Templates

1. **Session Start** - Resume work, start new task
2. **Specialist Engagement** - Structured prompts per specialist type
3. **Investigation** - Gap analysis, performance issues
4. **Context Management** - Session handoff, context getting large
5. **Quick Operations** - Status checks, find documentation

### Example

```markdown
## Engage Build System Architect
\```
/doplan Engage build-system-architect for: [SPECIFIC_ISSUE]

Context:
- Current task: [TASK_NAME]
- Relevant files: [LIST_FILES]
- Specific question: [QUESTION]
\```
```

---

## Integration with DAIC

### Session Start Hook

The DAIC `session-start.py` hook automatically:
1. Checks for `docs/ACTIVE_CONTEXT.md`
2. Loads content into session context
3. Warns if file exceeds 200 lines
4. Provides guidance if file is missing

### Behavior

| Scenario | Hook Behavior |
|----------|---------------|
| File exists, < 200 lines | Loads content, continues normally |
| File exists, > 200 lines | Loads content + warning message |
| File missing | Info message with setup guidance (non-blocking) |
| Read error | Warning message (non-blocking) |

The hook **never fails** due to ACTIVE_CONTEXT.md issues - it always continues the session.

---

## Getting Started

### 1. Create ACTIVE_CONTEXT.md

```bash
# Copy the template
cp /path/to/daic-workflow/templates/ACTIVE_CONTEXT.template.md docs/ACTIVE_CONTEXT.md

# Edit with your current context
# Keep it under 200 lines!
```

### 2. Create PROMPTS.md (Optional)

Create standardized prompts for your project's common operations.

### 3. Organize Existing Docs

```
# Create WARM tier structure
mkdir -p docs/architecture docs/catalogs docs/patterns

# Create COLD tier archive
mkdir -p docs/backlog/archive
```

### 4. Maintain the System

- **Session Start**: Review ACTIVE_CONTEXT.md, update if stale
- **Session End**: Update with progress, decisions, next steps
- **Task Complete**: Archive to COLD tier
- **Weekly**: Check for stale items (> 90 days)

---

## Validation

### Manual Check

```bash
# Check HOT tier size
wc -l docs/ACTIVE_CONTEXT.md
# Should be < 200

# Check for stale backlog items
find docs/backlog -name "*.md" -mtime +90 -not -path "*/archive/*"
```

### Automated (Optional)

Projects can add validation scripts:
- Check HOT tier line limits
- Identify archive candidates
- Verify cross-references

---

## References

- **Cortex TMS**: Original tiered memory system concept
- **DAIC Workflow**: Discussion-Alignment-Implementation-Check methodology
- **Session Hooks**: `scripts/session-start.py` for integration details

---

*This guide is part of the DAIC Workflow Plugin. For questions, see the main README.md.*
