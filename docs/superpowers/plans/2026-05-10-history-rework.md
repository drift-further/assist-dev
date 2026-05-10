# History Drawer Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bottom-drawer history easier to use by (a) splitting entries into Prompts vs Cmds via a frontend heuristic so shell-command noise stops drowning real prompts, and (b) adding a sticky live-filter input.

**Architecture:** Frontend-only change. Three tabs (Prompts / Cmds / Favs) replace the current two (History / Favs). A `classifyKind(text)` helper in `js/ui.js` decides which bucket each entry falls into at render time — no backend, no schema, no migration. A new sticky filter input above the list narrows the active tab live (substring, case-insensitive, with `<mark>` highlighting). Existing favorite-exclusion logic is unchanged.

**Tech Stack:** Plain ES6 (no bundler), HTML, CSS custom properties. Manual verification via Playwright (no test suite — assist-dev's `CLAUDE.md` is explicit: "Testing is manual"). Restart pattern: `karen assist restart` then verify via clean Playwright context (browser cache will mask JS/HTML/CSS changes otherwise).

**Spec reference:** `docs/superpowers/specs/2026-05-10-history-rework-design.md`

---

## File map

| File | Responsibility | Change type |
|------|----------------|-------------|
| `js/ui.js` | Classification helper, render logic, tab switching, count display, filter handler | Modify (~60 LOC added/changed) |
| `js/state.js` | Add `_filterText`; change `_activeHistTab` default | Modify (2 lines) |
| `index.html` | Replace 2-tab row with 3 tabs; add filter input element | Modify (~6 lines) |
| `css/drawers.css` | Filter input styling, `<mark>` highlight, three-tab spacing | Modify (~20 LOC added) |

No new files, no backend changes.

---

## Task 1: Add `classifyKind()` helper and state changes

**Goal:** Pure-function classification + the two state variables. Verify the helper in isolation before any UI wiring.

**Files:**
- Modify: `js/state.js:40-42` (add `_filterText`)
- Modify: `js/ui.js:30` (move `_activeHistTab` default to `'prompts'`)
- Modify: `js/ui.js` (add `classifyKind()` near top of file, after `isFav()`)

- [ ] **Step 1: Add `_filterText` to state.js**

Edit `js/state.js`. Find the `_history` / `_favorites` block at line 40:

```javascript
let lastAction = null;
let _history = [];
let _favorites = [];
```

Replace with:

```javascript
let lastAction = null;
let _history = [];
let _favorites = [];
let _filterText = '';
```

- [ ] **Step 2: Add `classifyKind()` to ui.js**

Edit `js/ui.js`. After the existing `isFav()` function (around line 28), add:

```javascript
const SHELL_CMDS = new Set([
    'cd', 'ls', 'pwd', 'echo', 'cat', 'grep', 'mkdir', 'rm', 'cp', 'mv',
    'git', 'make', 'npm', 'python', 'pip', 'docker', 'tmux', 'kill',
    'clear', 'exit', 'claude', 'bash', 'sudo', 'chmod', 'chown',
    'find', 'awk', 'sed', 'head', 'tail', 'wc', 'which', 'ps', 'df', 'du'
]);

function classifyKind(text) {
    if (!text) return 'prompt';
    const trimmed = text.trim();
    const firstWord = trimmed.split(/\s+/)[0];
    if (SHELL_CMDS.has(firstWord)) return 'command';
    if (trimmed.length < 5 && !/\s/.test(trimmed)) return 'command';
    return 'prompt';
}
```

- [ ] **Step 3: Change `_activeHistTab` default in ui.js**

Edit `js/ui.js`. Find line 30:

```javascript
let _activeHistTab = 'history';
```

Replace with:

```javascript
let _activeHistTab = 'prompts';
```

- [ ] **Step 4: Restart assist and verify in browser console**

```bash
karen assist restart
```

Open http://assist.drift via Playwright (clean context, no cache). In the console, run:

```javascript
classifyKind('cd HL7R*')          // → 'command'
classifyKind('ls -ltr')           // → 'command'
classifyKind('claude')            // → 'command'
classifyKind('y')                 // → 'command'
classifyKind('/clear')            // → 'prompt'
classifyKind('/investigate')      // → 'prompt'
classifyKind('Can you go ahead and write...') // → 'prompt'
classifyKind('')                  // → 'prompt'
```

Expected: every assertion above matches. If any returns the wrong kind, fix the heuristic before proceeding.

- [ ] **Step 5: Commit**

```bash
cd ~/source/drift/drift-further_assist-dev
git add js/state.js js/ui.js
git commit -m "feat(history): add classifyKind() helper and filter state"
```

---

## Task 2: Three-tab structure (HTML + tab switching + counts)

**Goal:** Replace the 2-tab row with 3 tabs. Tab counts should be correct. List rendering still ignores the filter (added in Task 3) but should respect tab classification.

**Files:**
- Modify: `index.html:79-82` (tab row)
- Modify: `js/ui.js:30-48` (`switchHistTab`, `updateHistTabCounts`)
- Modify: `js/ui.js:50-81` (`renderLists` — add `commands` branch)

- [ ] **Step 1: Replace the tab row in index.html**

Edit `index.html`. Find lines 79–82:

```html
<div class="drawer-tabs">
    <button class="drawer-tab active" data-tab="history" onclick="switchHistTab('history')">History</button>
    <button class="drawer-tab" data-tab="favs" onclick="switchHistTab('favs')">Favs</button>
</div>
```

Replace with:

```html
<div class="drawer-tabs">
    <button class="drawer-tab active" data-tab="prompts" onclick="switchHistTab('prompts')">Prompts</button>
    <button class="drawer-tab" data-tab="commands" onclick="switchHistTab('commands')">Cmds</button>
    <button class="drawer-tab" data-tab="favs" onclick="switchHistTab('favs')">Favs</button>
</div>
```

- [ ] **Step 2: Update `switchHistTab()` in ui.js**

Edit `js/ui.js`. Find the existing `switchHistTab()` (around lines 32–39):

```javascript
function switchHistTab(tab) {
    _activeHistTab = tab;
    document.querySelectorAll('.drawer-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    const clearBtn = document.getElementById('clear-btn');
    if (clearBtn) clearBtn.style.display = tab === 'history' ? '' : 'none';
    updateHistTabCounts();
    renderLists();
}
```

Replace with:

```javascript
function switchHistTab(tab) {
    _activeHistTab = tab;
    document.querySelectorAll('.drawer-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    const clearBtn = document.getElementById('clear-btn');
    if (clearBtn) clearBtn.style.display = tab === 'favs' ? 'none' : '';
    updateHistTabCounts();
    renderLists();
}
```

(Change: clear button hides on Favs tab, shows on both Prompts and Cmds — was previously showing only on the legacy `history` tab.)

- [ ] **Step 3: Update `updateHistTabCounts()` in ui.js**

Edit `js/ui.js`. Find the existing `updateHistTabCounts()` (around lines 41–48):

```javascript
function updateHistTabCounts() {
    const favTexts = new Set(_favorites.map(f => f.text));
    const histCount = _history.filter(h => !favTexts.has(h.text)).length;
    document.querySelectorAll('.drawer-tab').forEach(t => {
        if (t.dataset.tab === 'history') t.textContent = 'History' + (histCount ? ' ' + histCount : '');
        if (t.dataset.tab === 'favs') t.textContent = 'Favs' + (_favorites.length ? ' ' + _favorites.length : '');
    });
}
```

Replace with:

```javascript
function updateHistTabCounts() {
    const favTexts = new Set(_favorites.map(f => f.text));
    const nonFav = _history.filter(h => !favTexts.has(h.text));
    const promptCount = nonFav.filter(h => classifyKind(h.text) === 'prompt').length;
    const cmdCount = nonFav.filter(h => classifyKind(h.text) === 'command').length;
    document.querySelectorAll('.drawer-tab').forEach(t => {
        if (t.dataset.tab === 'prompts') t.textContent = 'Prompts' + (promptCount ? ' ' + promptCount : '');
        if (t.dataset.tab === 'commands') t.textContent = 'Cmds' + (cmdCount ? ' ' + cmdCount : '');
        if (t.dataset.tab === 'favs') t.textContent = 'Favs' + (_favorites.length ? ' ' + _favorites.length : '');
    });
}
```

- [ ] **Step 4: Update `renderLists()` in ui.js to handle three tabs (no filter yet)**

Edit `js/ui.js`. Find the existing `renderLists()` (around lines 50–81). Replace the whole function with:

```javascript
function renderLists() {
    let html = '';

    if (_activeHistTab === 'favs') {
        if (_favorites.length > 0) {
            for (const f of _favorites) {
                html += `<div class="list-item" onclick="loadText(this)" data-text="${escHtml(f.text)}">
                    <span class="list-item-text">${escHtml(f.display || f.text)}</span>
                    <button class="list-item-star fav" onclick="toggleFavorite('${escHtml(f.text).replace(/'/g, "\\'")}', event)">&#9733;</button>
                </div>`;
            }
        } else {
            html = '<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:10px;letter-spacing:1px;">No favorites yet</div>';
        }
    } else {
        const wantKind = _activeHistTab === 'commands' ? 'command' : 'prompt';
        const favTexts = new Set(_favorites.map(f => f.text));
        const filtered = _history.filter(h => !favTexts.has(h.text) && classifyKind(h.text) === wantKind);

        if (filtered.length > 0) {
            for (const h of filtered) {
                html += `<div class="list-item" onclick="loadText(this)" data-text="${escHtml(h.text)}">
                    <span class="list-item-text">${escHtml(h.display || h.text)}</span>
                    <button class="list-item-star unfav" onclick="toggleFavorite('${escHtml(h.text).replace(/'/g, "\\'")}', event)">&#9734;</button>
                </div>`;
            }
        } else {
            const emptyMsg = wantKind === 'command' ? 'No commands yet' : 'No prompts yet';
            html = `<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:10px;letter-spacing:1px;">${emptyMsg}</div>`;
        }
    }

    listArea.innerHTML = html;
}
```

- [ ] **Step 5: Restart assist and verify in browser**

```bash
karen assist restart
```

Open http://assist.drift via Playwright (clean context). Set viewport to 390x844 (phone). Open the bottom drawer (swipe up or via the bottom edge tab).

Expected:
- Three tabs visible: `Prompts N` / `Cmds M` / `Favs K` (with non-zero counts if you have history)
- Default active tab: `Prompts` (green border)
- Tap `Cmds` → list switches to entries like `cd HL7R*`, `ls -ltr`, `claude`
- Tap `Favs` → existing favorites list, Clear History button hides
- Tap `Prompts` → Clear History button shows again
- Tap a prompt → loads into main input, drawer closes (existing behavior)

If counts look wrong, double-check the favorite-exclusion logic and that classifyKind is reachable from `updateHistTabCounts`.

- [ ] **Step 6: Commit**

```bash
cd ~/source/drift/drift-further_assist-dev
git add index.html js/ui.js
git commit -m "feat(history): split History tab into Prompts and Cmds via classifier"
```

---

## Task 3: Filter input wiring

**Goal:** Add the sticky filter input above the list. Live filter, substring, case-insensitive. Filter persists across tab switches; cleared on drawer close. No highlighting yet (that's Task 4).

**Files:**
- Modify: `index.html:84` (insert filter input between drawer-header and list-area)
- Modify: `js/ui.js` (filter input listener; thread `_filterText` through `renderLists`)
- Modify: `js/ui.js` (`closeBottomDrawer` already exists in `js/ui.js` — extend to clear filter)
- Modify: `css/drawers.css` (style filter input)

- [ ] **Step 1: Add filter input to index.html**

Edit `index.html`. Find the drawer-bottom block (lines 76–87). Replace:

```html
<div class="drawer-bottom" id="drawer-bottom">
    <div class="drawer-header">
        <div class="drawer-tabs">
            <button class="drawer-tab active" data-tab="prompts" onclick="switchHistTab('prompts')">Prompts</button>
            <button class="drawer-tab" data-tab="commands" onclick="switchHistTab('commands')">Cmds</button>
            <button class="drawer-tab" data-tab="favs" onclick="switchHistTab('favs')">Favs</button>
        </div>
        <button class="drawer-close" onclick="closeBottomDrawer()">&times;</button>
    </div>
    <div class="list-area" id="list-area"></div>
    <button class="clear-btn" id="clear-btn" onclick="clearHistory()">Clear History</button>
</div>
```

With:

```html
<div class="drawer-bottom" id="drawer-bottom">
    <div class="drawer-header">
        <div class="drawer-tabs">
            <button class="drawer-tab active" data-tab="prompts" onclick="switchHistTab('prompts')">Prompts</button>
            <button class="drawer-tab" data-tab="commands" onclick="switchHistTab('commands')">Cmds</button>
            <button class="drawer-tab" data-tab="favs" onclick="switchHistTab('favs')">Favs</button>
        </div>
        <button class="drawer-close" onclick="closeBottomDrawer()">&times;</button>
    </div>
    <div class="filter-row">
        <input type="text" id="hist-filter" class="hist-filter" placeholder="Filter…" autocomplete="off" autocapitalize="off" spellcheck="false">
        <button class="hist-filter-clear" id="hist-filter-clear" onclick="clearHistFilter()" aria-label="Clear filter">&times;</button>
    </div>
    <div class="list-area" id="list-area"></div>
    <button class="clear-btn" id="clear-btn" onclick="clearHistory()">Clear History</button>
</div>
```

- [ ] **Step 2: Add filter input listener and `clearHistFilter()` in ui.js**

Edit `js/ui.js`. After the `loadText()` function (around line 87), add:

```javascript
function clearHistFilter() {
    _filterText = '';
    const el = document.getElementById('hist-filter');
    if (el) el.value = '';
    document.getElementById('hist-filter-clear').style.display = 'none';
    renderLists();
}

(function attachHistFilter() {
    const el = document.getElementById('hist-filter');
    if (!el) return;
    el.addEventListener('input', () => {
        _filterText = el.value;
        document.getElementById('hist-filter-clear').style.display = el.value ? '' : 'none';
        renderLists();
    });
})();
```

- [ ] **Step 3: Update `renderLists()` to apply filter**

Edit `js/ui.js`. Find the `renderLists()` function (just rewritten in Task 2). Replace with:

```javascript
function renderLists() {
    let html = '';
    const filterLower = _filterText.toLowerCase();
    const matchesFilter = (text) => !filterLower || text.toLowerCase().includes(filterLower);

    if (_activeHistTab === 'favs') {
        const items = _favorites.filter(f => matchesFilter(f.text));
        if (items.length > 0) {
            for (const f of items) {
                html += `<div class="list-item" onclick="loadText(this)" data-text="${escHtml(f.text)}">
                    <span class="list-item-text">${escHtml(f.display || f.text)}</span>
                    <button class="list-item-star fav" onclick="toggleFavorite('${escHtml(f.text).replace(/'/g, "\\'")}', event)">&#9733;</button>
                </div>`;
            }
        } else {
            const msg = _filterText ? `No matches for "${escHtml(_filterText)}"` : 'No favorites yet';
            html = `<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:10px;letter-spacing:1px;">${msg}</div>`;
        }
    } else {
        const wantKind = _activeHistTab === 'commands' ? 'command' : 'prompt';
        const favTexts = new Set(_favorites.map(f => f.text));
        const items = _history.filter(h =>
            !favTexts.has(h.text) &&
            classifyKind(h.text) === wantKind &&
            matchesFilter(h.text)
        );

        if (items.length > 0) {
            for (const h of items) {
                html += `<div class="list-item" onclick="loadText(this)" data-text="${escHtml(h.text)}">
                    <span class="list-item-text">${escHtml(h.display || h.text)}</span>
                    <button class="list-item-star unfav" onclick="toggleFavorite('${escHtml(h.text).replace(/'/g, "\\'")}', event)">&#9734;</button>
                </div>`;
            }
        } else {
            const emptyBucketMsg = wantKind === 'command' ? 'No commands yet' : 'No prompts yet';
            const msg = _filterText ? `No matches for "${escHtml(_filterText)}"` : emptyBucketMsg;
            html = `<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:10px;letter-spacing:1px;">${msg}</div>`;
        }
    }

    listArea.innerHTML = html;
}
```

- [ ] **Step 4: Clear filter on drawer close**

Edit `js/ui.js`. Find the existing `closeBottomDrawer()` function (around lines 129–135):

```javascript
function closeBottomDrawer() {
    const drawer = document.getElementById('drawer-bottom');
    drawer.classList.remove('open', 'half');
    if (!document.getElementById('drawer-left').classList.contains('open')) {
        document.getElementById('drawer-overlay').classList.remove('visible');
    }
}
```

Replace with:

```javascript
function closeBottomDrawer() {
    const drawer = document.getElementById('drawer-bottom');
    drawer.classList.remove('open', 'half');
    if (!document.getElementById('drawer-left').classList.contains('open')) {
        document.getElementById('drawer-overlay').classList.remove('visible');
    }
    if (typeof clearHistFilter === 'function') clearHistFilter();
}
```

- [ ] **Step 5: Style the filter input in css/drawers.css**

Edit `css/drawers.css`. After the `.drawer-tab` block (around line 146), add:

```css
.filter-row {
    position: relative;
    padding: 6px 8px;
    border-bottom: 1px solid rgba(0, 255, 65, 0.15);
}

.hist-filter {
    width: 100%;
    box-sizing: border-box;
    background: rgba(0, 0, 0, 0.4);
    color: var(--text);
    border: 1px solid rgba(0, 255, 65, 0.25);
    border-radius: 4px;
    padding: 8px 28px 8px 10px;
    font: 12px 'JetBrains Mono', 'Fira Code', monospace;
    outline: none;
}

.hist-filter:focus {
    border-color: var(--green);
    background: rgba(0, 255, 65, 0.05);
}

.hist-filter::placeholder {
    color: var(--text-muted);
    opacity: 0.6;
}

.hist-filter-clear {
    position: absolute;
    right: 14px;
    top: 50%;
    transform: translateY(-50%);
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 16px;
    cursor: pointer;
    display: none;
    padding: 4px 6px;
    line-height: 1;
}

.hist-filter-clear:active {
    color: var(--green);
}
```

- [ ] **Step 6: Restart assist and verify**

```bash
karen assist restart
```

Open http://assist.drift via Playwright (clean context). Phone viewport 390x844. Open the drawer.

Expected:
- Filter input visible below the tab row, above the list. Placeholder text reads "Filter…"
- Input does NOT autofocus on drawer open (no virtual keyboard pop-up)
- Type `automate` → list narrows to entries containing "automate" (case-insensitive)
- "✕" clear button appears at the right of the input when text is present; tap it → input clears, full list returns
- Switch from Prompts to Cmds with text in the filter → filter persists, Cmds list filtered with same query
- Empty filter result → list area shows `No matches for "<query>"`
- Close drawer (swipe down or ✕) → reopen → filter is empty again, full list shows

- [ ] **Step 7: Commit**

```bash
cd ~/source/drift/drift-further_assist-dev
git add index.html js/ui.js css/drawers.css
git commit -m "feat(history): add live filter input with sticky drawer position"
```

---

## Task 4: Match highlighting + render polish

**Goal:** Highlight the matched substring in each visible row when a filter is active. Subtle background, mobile-readable.

**Files:**
- Modify: `js/ui.js` (`renderLists()` — wrap matched substring in `<mark>`)
- Modify: `css/drawers.css` (style for `.list-item-text mark`)

- [ ] **Step 1: Add `highlightMatch()` helper in ui.js**

Edit `js/ui.js`. Right above `renderLists()`, add:

```javascript
function highlightMatch(text, query) {
    const escaped = escHtml(text);
    if (!query) return escaped;
    const queryLower = query.toLowerCase();
    const textLower = text.toLowerCase();
    const idx = textLower.indexOf(queryLower);
    if (idx === -1) return escaped;
    // Recompute indices on the escaped string by walking through plain text positions.
    // Simpler: split plain text, escape each segment, join with <mark>.
    const before = text.slice(0, idx);
    const match = text.slice(idx, idx + query.length);
    const after = text.slice(idx + query.length);
    return escHtml(before) + '<mark>' + escHtml(match) + '</mark>' + escHtml(after);
}
```

- [ ] **Step 2: Wire `highlightMatch()` into `renderLists()`**

Edit `js/ui.js`. Update `renderLists()` (last rewritten in Task 3). In both render branches, replace the `<span class="list-item-text">${escHtml(...)}</span>` with `<span class="list-item-text">${highlightMatch(..., _filterText)}</span>`. Final function:

```javascript
function renderLists() {
    let html = '';
    const filterLower = _filterText.toLowerCase();
    const matchesFilter = (text) => !filterLower || text.toLowerCase().includes(filterLower);

    if (_activeHistTab === 'favs') {
        const items = _favorites.filter(f => matchesFilter(f.text));
        if (items.length > 0) {
            for (const f of items) {
                const display = f.display || f.text;
                html += `<div class="list-item" onclick="loadText(this)" data-text="${escHtml(f.text)}">
                    <span class="list-item-text">${highlightMatch(display, _filterText)}</span>
                    <button class="list-item-star fav" onclick="toggleFavorite('${escHtml(f.text).replace(/'/g, "\\'")}', event)">&#9733;</button>
                </div>`;
            }
        } else {
            const msg = _filterText ? `No matches for "${escHtml(_filterText)}"` : 'No favorites yet';
            html = `<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:10px;letter-spacing:1px;">${msg}</div>`;
        }
    } else {
        const wantKind = _activeHistTab === 'commands' ? 'command' : 'prompt';
        const favTexts = new Set(_favorites.map(f => f.text));
        const items = _history.filter(h =>
            !favTexts.has(h.text) &&
            classifyKind(h.text) === wantKind &&
            matchesFilter(h.text)
        );

        if (items.length > 0) {
            for (const h of items) {
                const display = h.display || h.text;
                html += `<div class="list-item" onclick="loadText(this)" data-text="${escHtml(h.text)}">
                    <span class="list-item-text">${highlightMatch(display, _filterText)}</span>
                    <button class="list-item-star unfav" onclick="toggleFavorite('${escHtml(h.text).replace(/'/g, "\\'")}', event)">&#9734;</button>
                </div>`;
            }
        } else {
            const emptyBucketMsg = wantKind === 'command' ? 'No commands yet' : 'No prompts yet';
            const msg = _filterText ? `No matches for "${escHtml(_filterText)}"` : emptyBucketMsg;
            html = `<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:10px;letter-spacing:1px;">${msg}</div>`;
        }
    }

    listArea.innerHTML = html;
}
```

- [ ] **Step 3: Style the `<mark>` highlight in css/drawers.css**

Edit `css/drawers.css`. After the `.list-item-text` rule (around line 241), add:

```css
.list-item-text mark {
    background: rgba(0, 212, 255, 0.25);
    color: var(--cyan);
    padding: 0 1px;
    border-radius: 2px;
}
```

- [ ] **Step 4: Restart assist and verify highlight rendering**

```bash
karen assist restart
```

Open http://assist.drift via Playwright (clean context). Phone viewport. Open drawer.

Expected:
- Type `auto` in filter → matching prompts show `auto` substring with cyan-tinted background
- Highlight matches even when the substring spans the truncated display (the substring is searched against the original text, but the visible row truncates with ellipsis — so a match deep in a long prompt may not visually show the highlight on screen; this is acceptable, it still filters correctly)
- No XSS injection: paste a prompt containing `<script>alert(1)</script>` into the input, send it, then filter for it. The history entry must render as literal text, not execute.

- [ ] **Step 5: Commit**

```bash
cd ~/source/drift/drift-further_assist-dev
git add js/ui.js css/drawers.css
git commit -m "feat(history): highlight matched substring in filtered results"
```

---

## Task 5: Final mobile verification + spec sign-off

**Goal:** Walk the full success-criteria checklist from the spec on a phone-sized Playwright session. Confirm nothing regressed in the existing favorite/clear/swipe behavior.

**Files:** None modified — verification only.

- [ ] **Step 1: Restart assist (idempotent — done if no JS edits since Task 4)**

```bash
karen assist restart
```

- [ ] **Step 2: Playwright walkthrough on phone viewport**

Use the playwright-skill to drive a clean browser context at viewport 390x844 against http://assist.drift. Run through the spec's success criteria:

1. Open drawer (swipe up from bottom edge, or tap bottom tab) → first tab visible is `Prompts`, default-active, count > 0 (assuming history exists)
2. Tap filter input → keyboard does NOT auto-pop until tap (verify by snapshotting before tapping)
3. Type `automate` → list narrows to entries containing "automate", substring highlighted in cyan
4. Tap a prompt row → text loads into main input, drawer closes (existing behavior preserved)
5. Reopen drawer → filter is empty, full Prompts list returns (filter clears on close per Task 3 step 4)
6. Tap `Cmds` tab → list switches to entries like `cd HL7R*`, `ls -ltr`, `claude`. Counts on tab labels are non-zero
7. Tap `Favs` tab → existing favorites list, Clear History button hidden
8. Tap `Prompts` again → Clear History button visible again
9. Star an entry → next render moves it to Favs and decrements Prompts count
10. Swipe drawer down halfway → snaps to half-height (existing gesture unchanged); swipe further → closes

- [ ] **Step 3: Smoke-test no regressions in adjacent features**

- Open the left drawer (keys panel) → still works
- Open the +menu → items still function
- Send a new prompt → it appears in history on next drawer open
- Send `cd somewhere` → it appears under Cmds, not Prompts

- [ ] **Step 4: Update spec status and commit**

Edit `docs/superpowers/specs/2026-05-10-history-rework-design.md`. Change the header line:

```markdown
**Status:** Spec, not yet implemented
```

To:

```markdown
**Status:** Implemented 2026-05-10
```

Then:

```bash
cd ~/source/drift/drift-further_assist-dev
git add docs/superpowers/specs/2026-05-10-history-rework-design.md
git commit -m "docs: mark history-rework spec as implemented"
```

---

## Self-review notes

- **Spec coverage:** All 7 design sections in the spec map to tasks here. Tab structure → Task 2. Classification → Task 1. Filter input → Task 3. Counts → Task 2 step 3. Clear button (unchanged behavior) → Task 2 step 2 (display logic adjusted). Mobile no-autofocus → Task 3 step 1 (no `autofocus` attribute on the `<input>`). Favorites unchanged → Task 2 step 4 keeps the existing exclusion.
- **Edge cases:** Empty bucket message → Task 2 step 4. Empty filter results message → Task 3 step 3. Filter persists across tab switches → Task 3 step 3 (no clear on `switchHistTab`). Filter clears on drawer close → Task 3 step 4. Backwards compat (existing `history.json` has no `kind` field) → classification runs on every render, so this is implicit.
- **Type/name consistency:** `_filterText` referenced in Task 1 step 1, used in Tasks 3 and 4. `classifyKind()` defined in Task 1 step 2, used in Tasks 2 and 3. `clearHistFilter()` defined in Task 3 step 2, called in Task 3 step 4. Tab data-tab values: `prompts` / `commands` / `favs` consistent in HTML (Task 2 step 1) and switching logic (Task 2 step 2). All consistent.
- **Out of scope confirmed:** No backend route changes, no `routes/input.py` edits, no `history.json` schema change, no per-project filter, no dedup, no per-item delete.
