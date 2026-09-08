// OpenCode's saved conversation beside its interactive tmux terminal.
// This reader never sends input, guesses a conversation, or resizes the pane.
const OpenCodeOutput = (() => {
    const panel = document.getElementById('opencode-output');
    const terminal = document.getElementById('term-display');
    const button = document.getElementById('opencode-open');
    const picker = document.getElementById('opencode-session');
    const messages = document.getElementById('opencode-messages');
    const scroll = document.getElementById('opencode-scroll');
    const status = document.getElementById('opencode-status');
    const older = document.getElementById('opencode-older');
    const panes = new Map();
    let target = null;
    let opened = false;
    let epoch = 0;
    let timer = null;
    let controller = null;
    let following = true;
    let loadingOlder = false;

    function stopRequest() {
        epoch++;
        clearTimeout(timer);
        timer = null;
        if (controller) controller.abort();
        controller = null;
    }

    function note(message, error = false) {
        status.textContent = message;
        status.classList.toggle('error', error);
    }

    function resetMessages() {
        messages.replaceChildren();
        document.getElementById('opencode-meta').textContent = '';
        document.getElementById('opencode-identity').textContent = '';
        older.hidden = true;
        following = true;
        loadingOlder = false;
        scroll.scrollTop = 0;
    }

    function close() {
        opened = false;
        stopRequest();
        _termPaused = false;
        _termHasNew = false;
        document.getElementById('term-new-output').classList.remove('visible');
        panel.classList.add('hidden');
        terminal.classList.remove('opencode-hidden');
    }

    function onTargetChange() {
        close();
        button.classList.add('hidden');
        terminal.classList.remove('has-opencode');
        target = null;
    }

    function onFrame(frameTarget, info, detected) {
        if (frameTarget !== _termTarget) return false;
        if (opened && target !== frameTarget) onTargetChange();
        const available = info && info.agent_kind === 'opencode';
        button.classList.toggle('hidden', !available);
        terminal.classList.toggle('has-opencode', !!available);
        if (!available) {
            panes.delete(frameTarget);
            if (opened) close();
        }
        if (opened && detected && !detected.passive && !detected.notifyOnly) {
            close();
            showFlash('sent', 'OpenCode needs attention in Terminal');
        }
        return opened;
    }

    async function request(path, params, requestEpoch) {
        controller = new AbortController();
        const response = await fetch('/terminal/opencode/' + path + '?' + new URLSearchParams(params), {
            signal: controller.signal, cache: 'no-store',
        });
        let data;
        try { data = await response.json(); }
        catch (error) {
            if (error.name === 'AbortError') throw error;
            throw new Error('Assist could not refresh Output. Retry or use Terminal.');
        }
        if (requestEpoch !== epoch || !opened || target !== _termTarget) return null;
        if (!response.ok || !data.ok) {
            const error = new Error(data.message || 'Output is unavailable. Retry or use Terminal.');
            error.code = data.error;
            throw error;
        }
        return data;
    }

    async function open() {
        target = _termTarget;
        if (!target || _paneInfo[target]?.agent_kind !== 'opencode') return;
        if (typeof clearSelection === 'function') clearSelection();
        opened = true;
        terminal.classList.add('opencode-hidden');
        panel.classList.remove('hidden');
        resetMessages();
        await loadSessions();
    }

    async function loadSessions() {
        stopRequest();
        const requestEpoch = epoch;
        picker.disabled = true;
        picker.replaceChildren(new Option('Choose conversation…', ''));
        note('Loading recent conversations…');
        try {
            const data = await request('sessions', {target}, requestEpoch);
            if (!data) return;
            const previous = panes.get(target);
            const entry = previous && previous.generation === data.generation
                ? previous : {generation: data.generation, session: '', limit: 50};
            if (!data.sessions.some(session => session.id === entry.session)) entry.session = '';
            if (!entry.session) resetMessages();
            if (panes.size >= 32 && !panes.has(target)) panes.delete(panes.keys().next().value);
            panes.set(target, entry);
            for (const session of data.sessions) {
                // Include a short ID to distinguish duplicate titles in the native picker.
                picker.add(new Option(session.title + ' · ' + session.id.slice(-6), session.id));
            }
            picker.value = entry.session;
            picker.disabled = false;
            if (entry.session) await refresh();
            else note(data.sessions.length
                ? 'Choose the conversation shown in this pane.'
                : `No conversations in this folder among OpenCode's ${data.recent_limit} most recent sessions.`);
        } catch (error) {
            if (requestEpoch === epoch && error.name !== 'AbortError') {
                note(error.message + (messages.children.length ? ' Showing the previous snapshot.' : ''), true);
            }
        }
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function updateMessage(node, message) {
        const signature = JSON.stringify(message);
        if (node._opencodeSignature === signature) return;
        const expanded = new Set(Array.from(node.querySelectorAll('details[open]')).map(d => d.dataset.part));
        node._opencodeSignature = signature;
        node.className = 'opencode-message ' + message.role;
        node.replaceChildren(element('div', 'opencode-role', message.role === 'user' ? 'You' : 'OpenCode'));
        message.parts.forEach((part, index) => {
            if (part.type === 'reasoning' || part.type === 'tool') {
                const detail = element('details', 'opencode-detail');
                detail.dataset.part = String(index);
                detail.open = expanded.has(String(index));
                const label = part.type === 'reasoning' ? 'Reasoning' : [part.title, part.status].filter(Boolean).join(' · ');
                detail.append(element('summary', '', label), element('pre', 'opencode-text', part.text));
                node.append(detail);
            } else {
                node.append(element('pre', 'opencode-text ' + part.type,
                    part.type === 'file' ? 'Attachment: ' + part.text : part.text));
            }
        });
        if (!message.parts.length) node.append(element('p', 'opencode-empty', 'No saved text yet.'));
    }

    function render(data) {
        const selection = window.getSelection();
        if (selection && !selection.isCollapsed && scroll.contains(selection.anchorNode)) {
            note('Updates paused while text is selected.');
            return;
        }
        const wasFollowing = following;
        const top = scroll.scrollTop;
        const height = scroll.scrollHeight;
        const viewportTop = scroll.getBoundingClientRect().top;
        const anchor = Array.from(messages.children).find(node => node.getBoundingClientRect().bottom > viewportTop);
        const anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;
        const nodes = new Map(Array.from(messages.children).map(node => [node.dataset.message, node]));
        const keep = new Set();
        data.messages.forEach((message, index) => {
            keep.add(message.id);
            let node = nodes.get(message.id);
            if (!node) {
                node = element('article', 'opencode-message');
                node.dataset.message = message.id;
            }
            updateMessage(node, message);
            if (messages.children[index] !== node) messages.insertBefore(node, messages.children[index] || null);
        });
        for (const node of Array.from(messages.children)) {
            if (!keep.has(node.dataset.message)) node.remove();
        }
        const session = data.session;
        document.getElementById('opencode-meta').textContent =
            [session.agent, session.model, session.variant].filter(Boolean).join(' · ') || 'Saved conversation';
        document.getElementById('opencode-identity').textContent = session.id;
        older.hidden = !data.has_older;
        older.disabled = false;
        const time = new Date(data.captured_at * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        note(`Snapshot ${time} · ${data.shown}/${data.total} messages` +
            (data.clipped ? ' · Some output clipped' : '') + (data.reverted ? ' · Reverted messages hidden' : ''));
        if (wasFollowing && !loadingOlder) scroll.scrollTop = scroll.scrollHeight;
        else if (anchor && anchor.isConnected) scroll.scrollTop = top + anchor.getBoundingClientRect().top - anchorTop;
        else if (loadingOlder) scroll.scrollTop = top + scroll.scrollHeight - height;
        following = wasFollowing && !loadingOlder;
        loadingOlder = false;
        document.getElementById('opencode-latest').classList.toggle('unfollowed', !following);
    }

    async function refresh() {
        stopRequest();
        if (!opened || document.hidden) return;
        const entry = panes.get(target);
        if (!entry || !entry.session) return;
        const requestEpoch = epoch;
        let retry = true;
        try {
            const data = await request('transcript', {
                target, generation: entry.generation, session_id: entry.session, limit: entry.limit,
            }, requestEpoch);
            if (data) render(data);
        } catch (error) {
            if (requestEpoch !== epoch || error.name === 'AbortError') return;
            note(error.message + (messages.children.length ? ' Showing the previous snapshot.' : ''), true);
            retry = !['cli_missing', 'cli_unavailable', 'invalid_export', 'output_too_large'].includes(error.code);
            if (['pane_changed', 'pane_gone', 'not_opencode', 'session_mismatch', 'different_store', 'remote_session'].includes(error.code)) {
                panes.delete(target);
                resetMessages();
                picker.value = '';
                picker.disabled = true;
                return;
            }
        } finally {
            if (retry && requestEpoch === epoch && opened && !document.hidden && panes.get(target)?.session) {
                timer = setTimeout(refresh, 3000);
            }
        }
    }

    button.addEventListener('click', open);
    document.getElementById('opencode-terminal').addEventListener('click', () => {
        close();
        // Paint the latest raw capture immediately, including a static dialog.
        _doRender(_termLatestContent, _paneInfo[_termTarget], _termTarget);
    });
    document.getElementById('opencode-refresh').addEventListener('click', () => {
        // Also refresh the picker: /new and renamed conversations need discovery.
        loadSessions();
    });
    picker.addEventListener('change', () => {
        stopRequest();
        const entry = panes.get(target);
        if (!entry) return;
        entry.session = picker.value;
        entry.limit = 50;
        resetMessages();
        note(entry.session ? 'Loading saved output…' : 'Choose the conversation shown in this pane.');
        refresh();
    });
    older.addEventListener('click', () => {
        const entry = panes.get(target);
        if (!entry) return;
        entry.limit = Math.min(500, entry.limit + 50);
        loadingOlder = true;
        following = false;
        older.disabled = true;
        refresh();
    });
    document.getElementById('opencode-latest').addEventListener('click', () => {
        following = true;
        scroll.scrollTop = scroll.scrollHeight;
        document.getElementById('opencode-latest').classList.remove('unfollowed');
    });
    scroll.addEventListener('scroll', () => {
        // Only rounding slack: a short scroll up must also survive refresh.
        following = scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop < 2;
        document.getElementById('opencode-latest').classList.toggle('unfollowed', !following);
    }, {passive:true});
    document.addEventListener('visibilitychange', () => {
        if (!opened) return;
        if (document.hidden) stopRequest();
        else if (panes.get(target)?.session) refresh();
        else loadSessions();
    });
    return {onFrame, onTargetChange};
})();
