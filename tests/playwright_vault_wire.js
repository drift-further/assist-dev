// Browser-wire proof for the vault transport. This intentionally has no source-code
// assertions: every verdict comes from runtime fetch calls or Playwright's
// recorded requests. Do not run in the networkless build sandbox.
//
// Run from the repo root on the host:
//   ASSIST_URL=http://127.0.0.1:8099 ASSIST_TOKEN="$(<auth_token)" \
//     node tests/playwright_vault_wire.js

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { chromium } = require('playwright');

const baseUrl = (process.env.ASSIST_URL || '').replace(/\/$/, '');
const assistToken = process.env.ASSIST_TOKEN || '';
const secret = process.env.VAULT_TEST_SECRET
    || 'vault-wire-' + crypto.randomBytes(12).toString('hex');
const vaultToken = '[$sudo]';
const ordinaryFavorite = {
    id: 'vault-wire-segment',
    handle: 'vault-wire-segment',
    text: 'ordinary wire-preview segment',
};
const ordinaryToken = '[' + ordinaryFavorite.handle + ']';
const previewComposer = vaultToken + ' ' + ordinaryToken;

if (!baseUrl || !assistToken) {
    throw new Error('ASSIST_URL and ASSIST_TOKEN are required');
}

(async () => {
    const browser = await chromium.launch({ headless: true });
    try {
        const context = await browser.newContext({
            viewport: { width: 390, height: 844 },
            serviceWorkers: 'block',
            extraHTTPHeaders: { 'X-Assist-Token': assistToken },
        });
        const page = await context.newPage();
        const traffic = [];
        const typeOutcomes = [];
        let failNextType = false;

        page.on('request', request => {
            traffic.push({
                method: request.method(),
                url: request.url(),
                body: request.postData() || '',
            });
        });

        // Record the outgoing /type request but do not let a test sentinel reach
        // whatever live tmux pane the phone last selected.
        await page.route('**/type', route => {
            const ok = !failNextType;
            failNextType = false;
            typeOutcomes.push(ok);
            route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify(ok
                    ? { ok: true, via: 'tmux', sent_chars: secret.length }
                    : { ok: false, error: 'wire proof forced failure' }),
            });
        });

        // Supply one deterministic ordinary segment through the same runtime
        // history response the UI normally consumes. The mixed composer then
        // renders the ~N ch control that drives WILL-SEND preview.
        await page.route('**/history', route => route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ history: [], favorites: [ordinaryFavorite] }),
        }));

        // The request event is recorded before this fulfillment. Keeping the
        // preview local to the throwaway browser also makes a leaked sentinel
        // observable without forwarding it to any server-side renderer.
        await page.route('**/segments/expand', route => route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
                expanded: previewComposer,
                tokens: [{ handle: ordinaryFavorite.handle, known: true }],
            }),
        }));

        // Draft behavior is part of the proof, but the fake target should not
        // leave a server-side draft behind after the run.
        await page.route('**/api/draft*', route => {
            const request = route.request();
            const posted = request.postData() ? JSON.parse(request.postData()) : {};
            route.fulfill({
                status: 200,
                contentType: 'application/json',
                body: JSON.stringify({
                    ok: true,
                    target: posted.target || 'vault-wire-test:0.0',
                    draft: {
                        text: posted.text || '',
                        attachments: posted.attachments || [],
                        enter_armed: posted.enter_armed !== false,
                        updated_at: Date.now() / 1000,
                    },
                }),
            });
        });

        await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
        await page.waitForFunction(() => typeof vaultPut === 'function');
        await page.evaluate(() => loadHistory());
        await page.evaluate(value => {
            if (!vaultPut('sudo', value)) throw new Error('vaultPut failed');
            _termTarget = 'vault-wire-test:0.0';

            // Scope the runtime fetch recorder to a synchronous UI action. This
            // avoids confusing Assist's background poll with a fetch issued by
            // the vault chip itself.
            const realFetch = window.fetch;
            window.__vaultFetchCalls = [];
            window.__recordVaultFetch = false;
            window.fetch = function(...args) {
                if (window.__recordVaultFetch) {
                    window.__vaultFetchCalls.push(String(args[0]));
                }
                return realFetch.apply(this, args);
            };
        }, secret);

        const input = page.locator('#text-input');
        const sendButton = page.locator('.btn-paste');
        const sheet = page.locator('.seg-sheet');

        await input.fill(previewComposer);
        await page.locator('.seg-chip.vault').waitFor({ state: 'visible' });
        await page.locator('.seg-count').waitFor({ state: 'visible' });

        const vaultChipFetches = await page.evaluate(() => {
            window.__vaultFetchCalls = [];
            window.__recordVaultFetch = true;
            document.querySelector('.seg-chip.vault').click();
            window.__recordVaultFetch = false;
            return window.__vaultFetchCalls.slice();
        });
        await sheet.waitFor({ state: 'visible' });
        const sheetTitle = await sheet.locator('.seg-sheet-title').innerText();
        const sheetText = await sheet.innerText();
        assert.equal(sheetTitle, vaultToken, 'vault chip did not open its own sheet');
        assert.match(sheetText, /stored in this browser, in plaintext/i);
        assert.match(sheetText, /A value is set\./);
        assert.deepEqual(vaultChipFetches, [], 'vault chip sheet issued fetch()');
        assert.equal(sheetText.includes(secret), false,
            'vault sheet rendered the stored value');
        await page.locator('.seg-sheet-close').click();

        // Exercise segment preview with both token kinds. The exact request
        // count and token-form body are the positive denominator for the
        // absence claim; the visible note states why this is not WILL-SEND.
        const previewStart = traffic.length;
        const expandResponse = page.waitForResponse(response =>
            response.request().method() === 'POST'
            && new URL(response.url()).pathname === '/segments/expand'
        );
        await page.locator('.seg-count').click();
        await expandResponse;
        await page.waitForFunction(expected => {
            const body = document.querySelector('.seg-sheet-body');
            return body && body.textContent === expected;
        }, previewComposer);
        const expandTraffic = traffic.slice(previewStart).filter(item =>
            item.method === 'POST' && new URL(item.url).pathname === '/segments/expand'
        );
        assert.equal(expandTraffic.length, 1,
            'WILL-SEND preview did not issue exactly one expansion request');
        const expandBody = JSON.parse(expandTraffic[0].body);
        assert.equal(expandBody.text, previewComposer,
            'WILL-SEND preview did not send the composer token text');
        assert.equal(expandTraffic[0].body.includes(secret), false,
            '/segments/expand received the vault value');
        assert.equal(await sheet.locator('.seg-sheet-title').innerText(), 'SEGMENT PREVIEW');
        assert.equal(await sheet.locator('.seg-sheet-body').innerText(), previewComposer);
        assert.match(await sheet.innerText(), /Vault tokens stay masked here/);
        await page.locator('.seg-sheet-close').click();

        // Let the ordinary typing debounce fire. It may persist the inert token,
        // but the resolved value must never appear in that PUT.
        await input.fill(vaultToken);
        await page.waitForTimeout(700);
        const successStart = traffic.length;
        const clearedDraft = page.waitForRequest(request =>
            request.method() === 'PUT'
            && new URL(request.url()).pathname === '/api/draft'
            && JSON.parse(request.postData() || '{}').text === ''
        );
        const reloadedHistory = page.waitForRequest(request =>
            request.method() === 'GET'
            && new URL(request.url()).pathname === '/history'
        );
        await sendButton.click();
        await Promise.all([clearedDraft, reloadedHistory]);
        await page.waitForFunction(() => typeof _sending !== 'undefined' && !_sending);

        const successTraffic = traffic.slice(successStart);
        const carryingSecret = successTraffic.filter(item =>
            (item.url + '\n' + item.body).includes(secret)
        );
        assert.equal(carryingSecret.length, 1, 'secret did not appear in exactly one request');

        const only = carryingSecret[0];
        assert.equal(only.method, 'POST');
        assert.equal(new URL(only.url).pathname, '/type');
        const typeBody = JSON.parse(only.body);
        assert.equal(typeBody.text, secret);
        assert.equal(typeBody.secret, true);
        assert.equal(typeBody.expand, false);
        assert.deepEqual(typeOutcomes, [true], 'success /type response was not exercised');

        const successDraftTraffic = successTraffic.filter(item =>
            new URL(item.url).pathname === '/api/draft'
        );
        assert(successDraftTraffic.length > 0,
            'successful send did not exercise draft cleanup');
        assert.equal(successDraftTraffic.some(item => item.body.includes(secret)), false,
            'draft cleanup traffic carried the vault value');

        const successHistoryTraffic = successTraffic.filter(item =>
            new URL(item.url).pathname === '/history'
        );
        assert(successHistoryTraffic.length > 0,
            'successful send did not exercise history reload');
        assert.equal(successHistoryTraffic.some(item =>
            (item.url + '\n' + item.body).includes(secret)
        ), false, 'history traffic carried the vault value');

        // Run the send again with a forced application-level failure. A secret
        // failure deliberately cannot schedule draft persistence, so there is
        // no draft request to use as a denominator. The intercepted failed
        // /type request and restored token textarea are positive runtime proof
        // that the guarded failure branch itself executed.
        await input.fill(vaultToken);
        await page.waitForTimeout(700);
        failNextType = true;
        const failureStart = traffic.length;
        await sendButton.click();
        await page.waitForFunction(() => typeof _sending !== 'undefined' && !_sending);
        await page.waitForTimeout(700);

        const failureTraffic = traffic.slice(failureStart);
        const failureTypeTraffic = failureTraffic.filter(item =>
            item.method === 'POST' && new URL(item.url).pathname === '/type'
        );
        assert.equal(failureTypeTraffic.length, 1,
            'failed-send pass did not issue exactly one /type request');
        const failureTypeBody = JSON.parse(failureTypeTraffic[0].body);
        assert.equal(failureTypeBody.text, secret);
        assert.equal(failureTypeBody.secret, true);
        assert.equal(failureTypeBody.expand, false);
        assert.deepEqual(typeOutcomes, [true, false],
            'the forced failure response was not consumed');
        assert.equal(await input.inputValue(), vaultToken,
            'failed send did not restore the unresolved token text');

        const failureDraftTraffic = failureTraffic.filter(item =>
            new URL(item.url).pathname === '/api/draft'
        );
        assert.equal(failureDraftTraffic.length, 0,
            'failed secret send issued a draft request');

        // Across both sends the sentinel has a positive count of two, and both
        // occurrences must be the recorded /type requests. This catches any
        // additional boundary—including preview, draft, or history—carrying it.
        const allSecretTraffic = traffic.filter(item =>
            (item.url + '\n' + item.body).includes(secret)
        );
        assert.equal(allSecretTraffic.length, 2,
            'secret appeared outside the two exercised /type sends');
        for (const item of allSecretTraffic) {
            assert.equal(item.method, 'POST');
            assert.equal(new URL(item.url).pathname, '/type');
        }

        // Acceptance 6 needs both detector directions in one browser run. Drive
        // the render cadence explicitly: changing the captured pane text alone
        // does not update the compact key.
        const quickButton = page.locator('#vault-quick-send');
        const ordinaryPasswordMention = [
            'Documentation note:',
            'This ordinary prose mentions a password but asks for nothing.',
        ].join('\n');
        await page.evaluate(content => {
            _termLatestContent = content;
            renderSmartActions(null);
        }, ordinaryPasswordMention);
        assert.equal(await quickButton.isVisible(), false,
            'vault key appeared for ordinary password prose');

        // The general composer detector accepts this real password prompt, but
        // the vault's narrower matcher must not bind $sudo to a remote account.
        // Calling the send function directly proves the refusal path ran; a UI
        // visibility check alone would not exercise its tap-time backstop.
        const remoteSshPrompt = [
            'Connecting to deploy@vps',
            "deploy@vps's password:",
        ].join('\n');
        const remoteStart = traffic.length;
        await page.evaluate(content => {
            _termLatestContent = content;
            renderSmartActions(null);
        }, remoteSshPrompt);
        assert.equal(await quickButton.isVisible(), false,
            'vault key offered $sudo for an ssh password prompt');
        await page.evaluate(() => vaultSendDefault());
        const remoteTypeTraffic = traffic.slice(remoteStart).filter(item =>
            item.method === 'POST' && new URL(item.url).pathname === '/type'
        );
        assert.equal(remoteTypeTraffic.length, 0,
            'direct quick-send call sent $sudo to an ssh password prompt');

        const livePasswordPrompt = [
            'sudo is waiting for authentication',
            '[sudo] password for user:',
        ].join('\n');
        await page.evaluate(content => {
            _termLatestContent = content;
            renderSmartActions(null);
        }, livePasswordPrompt);
        assert.equal(await quickButton.isVisible(), true,
            'vault key did not appear for a live password prompt');
        assert.equal(await quickButton.isEnabled(), true,
            'vault key was visible but unusable');
        assert.equal((await quickButton.innerText()).trim(), '$sudo',
            'visible vault key did not name the secret handle');
        assert.equal(await quickButton.evaluate(button =>
            button.scrollWidth <= button.clientWidth), true,
        'vault handle label was clipped inside the compact key');

        // Gate and destination must remain the same pane. Route input to a
        // synthetic split after the main-pane prompt was captured, require the
        // key to disappear, then call the send function directly to exercise
        // its independent tap-time refusal before restoring the main route.
        const divergedStart = traffic.length;
        await page.evaluate(() => {
            _splitPanes['vault-wire-test'] = {target: 'vault-wire-split:0.1'};
            _inputToSplit = true;
            renderSmartActions(null);
        });
        assert.equal(await quickButton.isVisible(), false,
            'vault key remained visible with input routed to a split pane');
        await page.evaluate(() => vaultSendDefault());
        const divergedTypeTraffic = traffic.slice(divergedStart).filter(item =>
            item.method === 'POST' && new URL(item.url).pathname === '/type'
        );
        assert.equal(divergedTypeTraffic.length, 0,
            'quick-send crossed from its gated main pane to the split target');
        await page.evaluate(() => {
            _inputToSplit = false;
            delete _splitPanes['vault-wire-test'];
            renderSmartActions(null);
        });
        assert.equal(await quickButton.isVisible(), true,
            'vault key did not return after restoring the gated main target');

        const composerBeforeQuickSend = await input.inputValue();
        const quickStart = traffic.length;
        const quickResponse = page.waitForResponse(response =>
            response.request().method() === 'POST'
            && new URL(response.url()).pathname === '/type'
        );
        await quickButton.click();
        await quickResponse;
        await page.waitForFunction(() => {
            const button = document.getElementById('vault-quick-send');
            return button && button.getClientRects().length === 0;
        });

        const quickTraffic = traffic.slice(quickStart).filter(item =>
            item.method === 'POST' && new URL(item.url).pathname === '/type'
        );
        assert.equal(quickTraffic.length, 1,
            'quick-send did not issue exactly one /type request');
        const quickTypeBody = JSON.parse(quickTraffic[0].body);
        assert.equal(quickTypeBody.text, secret);
        assert.equal(quickTypeBody.secret, true);
        assert.equal(quickTypeBody.expand, false);
        assert.deepEqual(typeOutcomes, [true, false, true],
            'quick-send success response was not consumed');
        assert.equal(await input.inputValue(), composerBeforeQuickSend,
            'quick-send changed the composer text');
        assert.equal(await quickButton.isVisible(), false,
            'quick-send key did not suppress the already-served prompt');

        // Acceptance 7 is driven through the real Actions > Settings UI. The
        // existing composer token supplies a second rendered view of the same
        // storage state, before and after the Forget confirmation.
        const storedChip = page.locator('.seg-chip.vault:not(.unknown)');
        await storedChip.waitFor({ state: 'visible' });
        assert.equal(await storedChip.count(), 1,
            'stored vault chip was not rendered as known before Forget');

        const settingsStart = traffic.length;
        await page.locator('#btn-actions').click();
        await page.locator('#actions-deck').waitFor({ state: 'visible' });
        await page.locator('.plus-settings').click();
        const settingsPanel = page.locator('#settings-panel');
        await settingsPanel.waitFor({ state: 'visible' });

        const vaultRow = settingsPanel.locator('.settings-vault-row')
            .filter({ hasText: '$sudo' });
        await vaultRow.waitFor({ state: 'visible' });
        const vaultRowText = await vaultRow.innerText();
        assert.match(vaultRowText, /\$sudo/);
        assert.match(vaultRowText, /stored/i);
        assert.equal((await settingsPanel.innerText()).includes(secret), false,
            'Settings rendered the stored vault value');

        let forgetPrompt = '';
        const forgetDialog = page.waitForEvent('dialog').then(async dialog => {
            forgetPrompt = dialog.message();
            await dialog.accept();
        });
        await Promise.all([
            forgetDialog,
            vaultRow.getByRole('button', { name: 'Forget', exact: true }).click(),
        ]);
        assert.equal(forgetPrompt, 'Forget $sudo from this browser?');

        const emptyVault = settingsPanel.locator('.settings-vault-empty');
        await emptyVault.waitFor({ state: 'visible' });
        assert.match(await emptyVault.innerText(), /No entries\./);
        assert.equal(await settingsPanel.locator('.settings-vault-row').count(), 0,
            'forgotten handle remained in Settings');

        await settingsPanel.locator('.cmd-panel-close').click();
        await settingsPanel.waitFor({ state: 'hidden' });
        const missingChip = page.locator('.seg-chip.vault.unknown');
        await missingChip.waitFor({ state: 'visible' });
        assert.equal(await page.locator('.seg-chip.vault').count(), 1,
            'composer did not retain exactly one vault token chip after Forget');

        await missingChip.click();
        await sheet.waitFor({ state: 'visible' });
        const missingSheetText = await sheet.innerText();
        assert.match(missingSheetText, /No value is set\./);
        assert.equal(missingSheetText.includes(secret), false,
            'forgotten value remained visible through the composer chip');
        await page.locator('.seg-sheet-close').click();

        const settingsSecretTraffic = traffic.slice(settingsStart).filter(item =>
            (item.url + '\n' + item.body).includes(secret)
        );
        // The listing, confirmation, empty state, and missing chip above are
        // the positive runtime denominator for this local-only traffic check.
        assert.equal(settingsSecretTraffic.length, 0,
            'Settings/Forget traffic carried the vault value');

        const completeSecretTraffic = traffic.filter(item =>
            (item.url + '\n' + item.body).includes(secret)
        );
        assert.equal(completeSecretTraffic.length, 3,
            'secret appeared outside the three exercised /type sends');
        for (const item of completeSecretTraffic) {
            assert.equal(item.method, 'POST');
            assert.equal(new URL(item.url).pathname, '/type');
        }

        await page.evaluate(() => vaultForgetAll());
        console.log('vault wire assertions passed');
    } finally {
        await browser.close();
    }
})().catch(error => {
    console.error(error.stack || error);
    process.exitCode = 1;
});
