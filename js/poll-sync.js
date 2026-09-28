// poll-sync.js — /poll response ordering and the pane-tail cache.
//
// Polls overlap: the 5 s loop, tab actions (js/tabs.js) and the return from a
// hidden page each fire one, and responses can land out of order. An older
// response applied after a newer one re-raised an answered prompt's popup,
// fired a spurious "done" and re-adopted a killed target. Each request takes a
// sequence number, and a response older than one already applied is dropped.
//
// /poll sends a pane's tail only when it changed since the `gen` this browser
// echoes back as ?since=, so the tails it already holds are kept here and
// filled back into the scan entries that omit them. Each tail carries the
// server's rev, and a lower rev never replaces a higher one, so a slow
// response cannot put an old tail back.

// Lines of a capture the smart-action detector reads (js/actions.js keeps
// `lines.slice(-60)`); routes/poll.py:SCAN_TAIL_LINES trims to the same number.
const DETECTION_TAIL_LINES = 60;

let _pollSeqIssued = 0;
let _pollSeqApplied = 0;
let _pollEpoch = '';
let _pollRev = -1;
const _pollTails = {};   // target -> {tail, rev}

function pollBegin() {
    return {
        seq: ++_pollSeqIssued,
        since: _pollRev >= 0 ? _pollEpoch + '.' + _pollRev : '',
    };
}

function pollAccept(seq) {
    if (seq <= _pollSeqApplied) return false;
    _pollSeqApplied = seq;
    return true;
}

function pollMergeScan(gen, scan) {
    const dot = (gen || '').lastIndexOf('.');
    const epoch = dot > 0 ? gen.slice(0, dot) : '';
    const rev = dot > 0 ? parseInt(gen.slice(dot + 1), 10) : NaN;
    if (epoch !== _pollEpoch) {
        // A restarted server numbers from scratch: nothing held is comparable.
        for (const k of Object.keys(_pollTails)) delete _pollTails[k];
        _pollEpoch = epoch;
        _pollRev = -1;
    }
    let missing = false;
    for (const entry of scan) {
        const held = _pollTails[entry.target];
        if (typeof entry.tail === 'string' && (!held || entry.tail_rev > held.rev)) {
            _pollTails[entry.target] = {tail: entry.tail, rev: entry.tail_rev};
        }
        const now = _pollTails[entry.target];
        if (now) {
            entry.tail = now.tail;
        } else {
            entry.tail = '';
            missing = true;
        }
    }
    if (missing || isNaN(rev)) _pollRev = -1;  // ask for everything next time
    else if (rev > _pollRev) _pollRev = rev;
}

// The last DETECTION_TAIL_LINES lines of a capture, ANSI-stripped: all
// detectSmartActions reads. Cutting before stripping keeps a 2,000-line
// capture from being stripped and split in full on every frame.
function detectionTail(content) {
    if (!content) return '';
    let i = content.length;
    for (let k = 0; k < DETECTION_TAIL_LINES; k++) {
        i = content.lastIndexOf('\n', i - 1);
        if (i <= 0) return stripAnsi(content);
    }
    return stripAnsi(content.slice(i + 1));
}
