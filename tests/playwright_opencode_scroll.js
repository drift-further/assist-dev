// Real OpenCode + browser regression; run against a throwaway Assist server.
// Start the server with ASSIST_ALLOWED_ORIGINS matching its test URL. Resume
// a long saved OpenCode conversation in your own t562-* pane (e.g. 160x60).
// This types/clears two drafts and submits two short no-tools test prompts.
//
// NODE_PATH=/path/to/node_modules ASSIST_URL=http://127.0.0.1:8199 \
// ASSIST_TEST_TARGET=t562-scroll:0.0 ASSIST_OPENCODE_SESSION=ses_... \
// node tests/playwright_opencode_scroll.js
// ASSIST_TOKEN_PATH defaults to ./auth_token; ASSIST_TEST_OUTPUT defaults to /tmp.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const base = (process.env.ASSIST_URL || '').replace(/\/$/, '');
const target = process.env.ASSIST_TEST_TARGET || '';
const session = process.env.ASSIST_OPENCODE_SESSION || '';
const out = process.env.ASSIST_TEST_OUTPUT || '/tmp';
const url = new URL(base);
assert(['127.0.0.1', 'localhost'].includes(url.hostname) && url.port && url.port !== '8089', 'Use a throwaway loopback server');
assert(/^t562-[^:]+:\d+\.\d+$/.test(target), 'Use your own t562-* test pane');
assert(/^ses_[A-Za-z0-9_-]+$/.test(session), 'Set the saved test conversation ID');
const token = fs.readFileSync(process.env.ASSIST_TOKEN_PATH || 'auth_token', 'utf8').trim();

(async () => {
    const browser = await chromium.launch({headless:true});
    const context = await browser.newContext({
        viewport:{width:390,height:844}, hasTouch:true, isMobile:true, serviceWorkers:'block',
    });
    await context.addCookies([{
        name:'assist_auth', value:crypto.createHmac('sha256',token).update('assist-auth-v1').digest('hex'), url:base,
    }]);
    await context.addInitScript(value => localStorage.setItem('term_target', value), target);
    const page = await context.newPage();
    const errors = [], keys = [], measurements = [];
    let frames = 0, snapshots = 0, generic = false;
    page.on('pageerror', error => errors.push(error.message));
    page.on('websocket', ws => ws.on('framereceived', event => {
        try { if (JSON.parse(event.payload).target === target) frames++; } catch {}
    }));
    page.on('response', response => {
        if (response.url().includes('/terminal/opencode/transcript?') && response.ok()) snapshots++;
    });
    await page.route('**/*', async route => {
        const req = route.request();
        if (new URL(req.url()).origin !== base) return route.abort();
        if (req.method() === 'POST') {
            let data = {}; try { data = req.postDataJSON() || {}; } catch {}
            if ((data.target && data.target !== target) ||
                (data.session && data.session !== target.split(':')[0])) return route.abort();
            if (new URL(req.url()).pathname === '/key') {
                keys.push(data.keys);
                if (generic) return route.fulfill({json:{ok:true}});
            }
        }
        await route.continue();
    });
    const cdp = await context.newCDPSession(page);
    async function drag(selector, dx, dy) {
        const box = await page.locator(selector).boundingBox();
        const x = Math.round(box.x + box.width * .75);
        const y = Math.round(dy > 0 ? box.y + 65 : box.y + box.height - 45);
        await cdp.send('Input.dispatchTouchEvent', {type:'touchStart', touchPoints:[{x,y}]});
        for (let i = 1; i <= 10; i++) {
            await cdp.send('Input.dispatchTouchEvent', {type:'touchMove', touchPoints:[{x:x+dx*i/10,y:y+dy*i/10}]});
            await page.waitForTimeout(35);
        }
        await cdp.send('Input.dispatchTouchEvent', {type:'touchEnd',touchPoints:[]});
        await page.waitForTimeout(350);
    }
    async function measure(label, selector = '#term-display') {
        const data = await page.locator(selector).evaluate(el => ({
            top:el.scrollTop, left:el.scrollLeft, height:el.clientHeight,
            scrollHeight:el.scrollHeight, width:el.clientWidth, scrollWidth:el.scrollWidth,
            bottom:el.scrollHeight-el.clientHeight-el.scrollTop,
        }));
        measurements.push({label,...data,frames,snapshots,keyCount:keys.length});
        return data;
    }
    async function post(endpoint, data) {
        const response = await context.request.post(base + endpoint, {data:{...data,target}});
        assert(response.ok(), `${endpoint}: ${response.status()}`);
        assert((await response.json()).ok);
    }
    async function latest() {
        const count = keys.length;
        await page.locator('#term-tui-end').click();
        await page.waitForFunction(() => !_termPaused);
        await page.waitForTimeout(700);
        assert.equal(keys[count], 'End');
        assert((await measure('terminal-latest')).bottom < 2);
    }
    try {
        await page.goto(base);
        await page.waitForFunction(() => _termWsConnected && _paneInfo[_termTarget]?.agent_kind === 'opencode');
        await page.waitForTimeout(350);
        await latest();
        const initial = await measure('terminal-initial');
        assert(initial.scrollHeight-initial.height > 200, 'Use a tall capture');
        assert(initial.scrollWidth-initial.width > 200, 'Use a wide capture');
        const beforePanKeys = keys.length;
        await drag('#term-display', 0, 110);
        const vertical = await measure('terminal-touch-vertical');
        assert(initial.top-vertical.top > 60);
        await drag('#term-display', -160, 0);
        const panned = await measure('terminal-touch-horizontal');
        assert(panned.left > 100);
        assert.equal(keys.length, beforePanKeys, 'Native panning must not page the app');
        const button = await page.locator('#term-tui-end').boundingBox();
        assert(button.x >= 0 && button.x+button.width <= 390, 'Latest stays visible during horizontal panning');

        const draft = 'T562_FIX2_DRAFT_' + Date.now();
        await post('/type', {text:draft,enter:false,no_history:true});
        await page.waitForFunction(value => document.getElementById('term-content').innerText.includes(value), draft);
        const updated = await measure('terminal-live-frame-while-reading');
        assert(Math.abs(updated.top-panned.top) < 2, 'Live frame must not pull the reader down');
        assert.equal(updated.left, panned.left);
        await page.screenshot({path:path.join(out,'fix2-terminal-panned.png')});
        await post('/key', {keys:'ctrl+u'});
        await latest();
        assert.equal((await measure('terminal-reset-horizontal')).left, 0);

        // A one-line scroll must not be mistaken for following the tail.
        await page.locator('#term-display').hover();
        await page.mouse.wheel(0,-20);
        await page.waitForTimeout(200);
        const shortPan = await measure('terminal-short-pan');
        assert(Math.abs(shortPan.bottom-20) < 2);
        const shortDraft = draft + '_SHORT';
        await post('/type', {text:shortDraft,enter:false,no_history:true});
        await page.waitForFunction(value => document.getElementById('term-content').innerText.includes(value),shortDraft);
        assert(Math.abs((await measure('terminal-short-pan-after-frame')).top-shortPan.top) < 2);
        await post('/key', {keys:'ctrl+u'});
        await latest();

        // Native wheel movement inside the capture, then effort 258 paging
        // when another gesture starts at the top edge.
        await page.locator('#term-display').hover();
        await page.mouse.wheel(0,-120);
        await page.waitForTimeout(300);
        assert((await measure('terminal-wheel')).bottom > 80);
        await page.locator('#term-display').evaluate(el => {el.scrollTop=0;});
        await page.waitForTimeout(100);
        const beforeEdge = keys.length;
        await drag('#term-display',0,300);
        assert.equal(keys[beforeEdge], 'Page_Up');
        assert((await measure('terminal-edge-page')).top < 2);
        const beforeButtons = keys.length;
        await page.locator('#term-tui-nav button').nth(1).click();
        await page.waitForTimeout(200);
        assert.equal(keys[beforeButtons], 'Page_Down');
        await latest();

        // Effort 283's per-pane override still enables ordinary browser scroll.
        await page.locator('#term-tui-chip').click();
        await page.waitForFunction(() => !_paneTui[_termTarget]);
        const std = await measure('normal-override');
        const beforeStd = keys.length;
        await drag('#term-display',0,110);
        assert(std.top-(await measure('normal-override-pan')).top > 60);
        assert.equal(keys.length,beforeStd);
        await page.locator('#term-tui-chip').click();
        await page.waitForFunction(() => _paneTui[_termTarget]);
        await latest();

        await page.locator('#opencode-open').click();
        await page.waitForFunction(() => !document.getElementById('opencode-session').disabled);
        await page.locator('#opencode-session').selectOption(session);
        await page.waitForFunction(() => document.querySelectorAll('#opencode-messages article').length > 0);
        await page.waitForTimeout(200);
        assert((await measure('reader-default-tail','#opencode-scroll')).bottom < 2);
        const beforeReader = keys.length;
        await drag('#opencode-scroll',0,110);
        const reading = await measure('reader-native-pan','#opencode-scroll');
        assert(reading.bottom > 60);
        const marker = 'T562_FIX2_READING_' + Date.now();
        await post('/type', {text:'Reply exactly '+marker+'. Do not use tools.',enter:true,no_history:true});
        await page.waitForFunction(value => Array.from(document.querySelectorAll('#opencode-messages .assistant')).some(el => el.innerText.includes(value)),marker,{timeout:60000});
        const refreshed = await measure('reader-new-snapshot-while-reading','#opencode-scroll');
        assert(Math.abs(refreshed.top-reading.top) < 2);
        assert(refreshed.scrollHeight > reading.scrollHeight);
        assert.equal(keys.length,beforeReader, 'Reader gestures must not send terminal keys');
        await page.screenshot({path:path.join(out,'fix2-reader-held.png')});
        await page.locator('#opencode-scroll').hover();
        await page.mouse.wheel(0,refreshed.bottom-20);
        await page.waitForTimeout(200);
        const shortRead = await measure('reader-short-pan','#opencode-scroll');
        assert(Math.abs(shortRead.bottom-20) < 2);
        await page.waitForResponse(response => response.url().includes('/terminal/opencode/transcript?') && response.ok());
        await page.waitForTimeout(200);
        assert(Math.abs((await measure('reader-short-pan-after-refresh','#opencode-scroll')).top-shortRead.top) < 2);
        await page.locator('#opencode-latest').click();
        assert((await measure('reader-latest','#opencode-scroll')).bottom < 2);
        const tailMarker = 'T562_FIX2_TAIL_' + Date.now();
        await post('/type', {text:'Reply exactly '+tailMarker+'. Do not use tools.',enter:true,no_history:true});
        await page.waitForFunction(value => Array.from(document.querySelectorAll('#opencode-messages .assistant')).some(el => el.innerText.includes(value)),tailMarker,{timeout:60000});
        assert((await measure('reader-follows-new-snapshot','#opencode-scroll')).bottom < 2);
        await page.screenshot({path:path.join(out,'fix2-reader-latest.png')});

        // Synthetic non-OpenCode frames exercise the unchanged generic TUI
        // handlers. Their key requests are recorded and fulfilled locally.
        await page.locator('#opencode-terminal').click();
        await page.evaluate(() => {
            _termOpen=false;
            disconnectTerminalWs();
            clearInterval(_termPollTimer); _termPollTimer=null;
            clearTimeout(_renderTimer); _renderTimer=null; _pendingRender=null;
        });
        generic=true;
        for (const kind of ['codex',null]) {
            await page.evaluate(kind => {
                _termPaused=false;
                const info={..._paneInfo[_termTarget],agent_kind:kind,command:'test-tui',alternate_on:true};
                _doRender(Array.from({length:60},(_,i)=>'generic TUI row '+i).join('\n'),info,_termTarget);
            },kind);
            await page.waitForTimeout(200);
            const before = await measure('generic-'+kind);
            const start = keys.length;
            await drag('#term-display',-160,0);
            await drag('#term-display',0,110);
            const after = await measure('generic-'+kind+'-small-drags');
            assert.equal(after.top,before.top);
            assert.equal(after.left,before.left);
            assert.equal(keys.length,start);
            await drag('#term-display',0,300);
            assert.equal(keys[start],'Page_Up');
            const endStart=keys.length;
            await page.locator('#term-tui-end').click();
            await page.waitForTimeout(300);
            assert.deepEqual(keys.slice(endStart),['End',...Array(8).fill('Page_Down')]);
            assert.equal(await page.locator('#term-tui-end').innerText(),'End');
        }
        assert.deepEqual(errors,[]);
        const result={status:'passed',viewport:'390x844',target,session,frames,snapshots,measurements,errors};
        fs.writeFileSync(path.join(out,'fix2-scroll-check.json'),JSON.stringify(result,null,2)+'\n');
        console.log(JSON.stringify(result,null,2));
    } catch(error) {
        await page.screenshot({path:path.join(out,'fix2-scroll-failure.png')});
        console.error(JSON.stringify({measurements,keys,errors},null,2));
        throw error;
    } finally {
        await browser.close();
    }
})().catch(error => {console.error(error);process.exit(1);});
