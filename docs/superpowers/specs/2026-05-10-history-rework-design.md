# History Drawer Rework — Design

**Date:** 2026-05-10
**Status:** Spec, not yet implemented

## Problem

The bottom-drawer history (HISTORY 48 / FAVS 17 in the screenshot) has two pain points:

1. **No way to find old prompts.** The list is flat, recency-ordered, no filter. Scrolling to recover a long prompt sent days ago is tedious.
2. **Shell-command noise drowns out real prompts.** Trivial entries (`/clear`, `claude`, `cd HL7R*`, `ls -ltr`) get the same row treatment as multi-paragraph Claude prompts and bury them.

Per-project scoping and edit-before-resend were considered and explicitly out of scope for this iteration.

## Goal

Make finding and resending an old prompt fast — without changing the backend data model or losing any history.

## Design

### 1. Tab structure

The drawer's tab row becomes three tabs instead of two:

```
PROMPTS (N)   CMDS (N)   FAVS (N)
```

Default active tab on drawer open: **Prompts**. Active tab persists across drawer open/close (existing `_activeHistTab` state, just renamed default).

### 2. Classification heuristic (frontend-only)

Each entry is classified at render time. No backend change, no migration, no schema field added.

```
kind('command') if any of:
  - first whitespace-separated token ∈ SHELL_CMDS
  - text length < 5 chars and contains no spaces

else: kind = 'prompt'

SHELL_CMDS = {
  cd, ls, pwd, echo, cat, grep, mkdir, rm, cp, mv,
  git, make, npm, python, pip, docker, tmux, kill,
  clear, exit, claude, bash, sudo, chmod, chown,
  find, awk, sed, head, tail, wc, which, ps, df, du
}
```

**All slash-commands** (`/clear`, `/investigate`, `/loop`, etc.) → **Prompts**. They are deliberate Claude directives; even `/clear` is rare enough not to be noise. Keeping all slash entries in one bucket also avoids the harder "is this slash-command short or long" judgment.

### 3. Filter input

A sticky text input sits below the tab row, above the list area:

```
┌──────────────────────────────┐
│ PROMPTS 32  CMDS 14  FAVS 17 │
├──────────────────────────────┤
│ 🔍 Filter…                ✕ │
├──────────────────────────────┤
│ Does it allow me to chan… ☆ │
│ I'm trying to do automate ☆ │
│ Can you go ahead and wri… ☆ │
│ …                           │
├──────────────────────────────┤
│        CLEAR HISTORY         │
└──────────────────────────────┘
```

Behavior:
- Live, substring, case-insensitive match against entry `text`
- Filters whichever tab is currently active
- Filter text persists across tab switches (so flipping Prompts↔Cmds keeps the query)
- Matched substring receives a subtle background highlight (`<mark>` or equivalent span)
- Inline "✕" button on the right clears the filter
- **Does NOT autofocus** when the drawer opens (avoids the virtual keyboard popping up every time on phone)

### 4. Counts

Tab labels show **unfiltered** counts so the user always knows the bucket size:

- `PROMPTS (N)` = count of entries classified as prompt, minus those already in favorites
- `CMDS (N)` = count of entries classified as command, minus those already in favorites
- `FAVS (N)` = count of favorites (unchanged)

The list area renders the **filtered** subset of the active tab. If filter results are empty, show `No matches for "<query>"`. If the bucket itself is empty (no filter), show `No prompts yet` / `No commands yet`.

### 5. Clear button

`Clear History` button stays as wipe-all (existing `DELETE /history` endpoint, untouched). The classification fix already removes noise from the default Prompts view, so per-tab clear isn't justified for v1. Easy to add later by sending a `?kind=command` filter to the backend if it becomes useful.

### 6. Favorites

Favorites continue to be a per-text exclusion: anything in `_favorites` is hidden from Prompts and Cmds (existing logic). The Favs tab itself is unchanged in layout and behavior.

## Files changed

| File | Change |
|------|--------|
| `js/ui.js` | Add `classifyKind(text)` helper, refactor `renderLists()` for 3 tabs + filter, update `updateHistTabCounts()`, `switchHistTab()` |
| `js/state.js` | Add `_filterText = ''`, change default `_activeHistTab` from `'history'` to `'prompts'` |
| `index.html` | Tab row: replace `History`/`Favs` with `Prompts`/`Cmds`/`Favs` (data-tab values updated). Add filter `<input>` element. |
| `css/widgets.css` | Style for filter input, `<mark>` highlight, three-tab spacing |

**No backend changes**, no Python touched, no `routes/input.py` change, no schema/data migration. The existing `history.json` works as-is — classification happens at render time on the existing `text` field.

Estimated diff: ~80–120 lines across four files.

## Edge cases

- **Empty bucket (no filter)**: Show `No prompts yet` / `No commands yet` in the list area
- **Filter returns nothing**: Show `No matches for "<query>"`
- **All entries are favorited**: Prompts and Cmds buckets show their empty state
- **Tab switch with active filter**: Filter persists, results update for new tab
- **Drawer close + reopen**: Filter text is cleared (matches typical command-palette UX); active tab is preserved
- **Backwards compat**: Existing `history.json` entries have no `kind` field — that's fine, classification runs on every render

## Out of scope (deliberately, may revisit)

- Per-project filtering / scoping
- Dedup with frequency badges (Approach B from brainstorming)
- Spotlight-style single-list UX (Approach C)
- Per-item delete button
- Edit-before-resend in a textarea
- Smart ordering (frequency-weighted, pinned)
- Per-tab clear button

## Success criteria

After deploy, on a phone, with the existing 48-entry history:

1. Open drawer → first thing visible is the Prompts tab, ~30 entries (no shell noise)
2. Tap filter input, type `automate` → list narrows to entries containing "automate", with substring highlighted
3. Tap a prompt → loads into input, drawer closes (existing behavior preserved)
4. Switch to Cmds tab → see the `cd`, `ls`, `claude` entries that used to clutter the main list
5. Switch to Favs tab → unchanged from today
