// container.js — Container panel: build, packages, extensions, status

var _containerPanelOpen = false;
var _containerConfig = null;
var _containerExtensions = [];
var _containerStatus = null;
var _ctrSections = { packages: false, extensions: false, build: false };
var _ctrBuildPollTimer = null;
var _ctrExtFormOpen = false;

// ================================================================
// Panel toggle
// ================================================================
function toggleContainerPanel() {
    _containerPanelOpen = !_containerPanelOpen;
    var panel = document.getElementById('container-panel');
    panel.classList.toggle('visible', _containerPanelOpen);
    if (_containerPanelOpen) {
        loadContainerStatus();
    }
}

// ================================================================
// Load status + config + extensions
// ================================================================
async function loadContainerStatus() {
    var statusEl = document.getElementById('ctr-status');
    if (statusEl) statusEl.innerHTML = '<div class="ctr-status-loading">Loading...</div>';

    try {
        var [statusResp, extResp] = await Promise.all([
            fetch('/api/container/status'),
            fetch('/api/container/extensions'),
        ]);
        var statusData = await statusResp.json();
        var extData = await extResp.json();

        if (statusData.ok) {
            _containerStatus = statusData;
            _containerConfig = statusData.config;
        }
        if (extData.ok) {
            _containerExtensions = extData.extensions;
        }

        renderContainerStatus();
        renderContainerPackages();
        renderContainerExtensions();
        renderContainerBuild();
    } catch (e) {
        if (statusEl) statusEl.innerHTML = '<div class="ctr-status-loading">Failed to load</div>';
    }
}

// ================================================================
// Render: Status section (always visible at top)
// ================================================================
function renderContainerStatus() {
    var el = document.getElementById('ctr-status');
    if (!el || !_containerConfig) return;

    var html = '';
    var img = _containerStatus ? _containerStatus.image : null;

    if (img) {
        html += '<div class="ctr-image-info">';
        html += '<span class="ctr-image-name">' + escHtml(img.name) + '</span>';
        html += '<span class="ctr-image-size">' + escHtml(img.size) + '</span>';
        html += '</div>';
    } else {
        html += '<div class="ctr-no-image">No image built yet</div>';
    }

    // Version summary from config
    var base = _containerConfig.base || {};
    html += '<div class="ctr-versions">';
    html += '<span>Node <span class="ctr-ver-val">' + escHtml(base.node_version || '?') + '</span></span>';
    html += '<span>Python <span class="ctr-ver-val">' + escHtml(base.python_version || '?') + '</span></span>';
    html += '<span>Claude <span class="ctr-ver-val">' + escHtml(base.claude_version || '?') + '</span></span>';
    html += '</div>';

    // Active containers
    var containers = (_containerStatus && _containerStatus.containers) || [];
    if (containers.length > 0) {
        html += '<div class="ctr-containers-hdr">Active Containers (' + containers.length + ')</div>';
        for (var i = 0; i < containers.length; i++) {
            var c = containers[i];
            html += '<div class="ctr-container-row">';
            html += '<span class="ctr-cname">' + escHtml(c.name) + '</span>';
            html += '<span class="ctr-cuptime">' + escHtml(c.running_for || c.status) + '</span>';
            html += '<button class="ctr-kill-btn" onclick="killContainer(\'' + escHtml(c.name) + '\')">Kill</button>';
            html += '</div>';
        }
    }

    el.innerHTML = html;
}

// ================================================================
// Render: Packages section
// ================================================================
function renderContainerPackages() {
    var body = document.getElementById('ctr-body-packages');
    if (!body || !_containerConfig) return;

    var pkgs = _containerConfig.packages || {};
    var html = '';

    // Global pip
    html += '<div class="ctr-pkg-section">';
    html += '<div class="ctr-pkg-section-label">Global pip (baked into image)</div>';
    html += '<div class="ctr-tags" id="ctr-tags-pip">';
    var pip = pkgs.pip || [];
    for (var i = 0; i < pip.length; i++) {
        html += '<span class="ctr-tag">' + escHtml(pip[i]);
        html += '<button class="ctr-tag-x" onclick="ctrRemovePkg(\'pip\',' + i + ')">&times;</button></span>';
    }
    html += '</div>';
    html += '<div class="ctr-pkg-add">';
    html += '<input class="ctr-pkg-input" id="ctr-add-pip" placeholder="package name" onkeydown="if(event.key===\'Enter\')ctrAddPkg(\'pip\')">';
    html += '<button class="ctr-pkg-add-btn" onclick="ctrAddPkg(\'pip\')">Add</button>';
    html += '</div></div>';

    // Project pip (from project settings)
    html += '<div class="ctr-pkg-section">';
    html += '<div class="ctr-pkg-section-label">Project pip (installed at startup)</div>';
    html += '<div class="ctr-tags" id="ctr-tags-projpip">';
    var projPip = (_projectSettings && _projectSettings.packages) ? (_projectSettings.packages.pip || []) : [];
    for (var j = 0; j < projPip.length; j++) {
        html += '<span class="ctr-tag">' + escHtml(projPip[j]);
        html += '<button class="ctr-tag-x" onclick="ctrRemoveProjPkg(' + j + ')">&times;</button></span>';
    }
    html += '</div>';
    html += '<div class="ctr-pkg-add">';
    html += '<input class="ctr-pkg-input" id="ctr-add-projpip" placeholder="package name" onkeydown="if(event.key===\'Enter\')ctrAddProjPkg()">';
    html += '<button class="ctr-pkg-add-btn" onclick="ctrAddProjPkg()">Add</button>';
    html += '</div></div>';

    // System apt
    html += '<div class="ctr-pkg-section">';
    html += '<div class="ctr-pkg-section-label">System apt (baked into image)</div>';
    html += '<div class="ctr-tags" id="ctr-tags-system">';
    var sys = pkgs.system || [];
    for (var k = 0; k < sys.length; k++) {
        html += '<span class="ctr-tag">' + escHtml(sys[k]);
        html += '<button class="ctr-tag-x" onclick="ctrRemovePkg(\'system\',' + k + ')">&times;</button></span>';
    }
    html += '</div>';
    html += '<div class="ctr-pkg-add">';
    html += '<input class="ctr-pkg-input" id="ctr-add-system" placeholder="package name" onkeydown="if(event.key===\'Enter\')ctrAddPkg(\'system\')">';
    html += '<button class="ctr-pkg-add-btn" onclick="ctrAddPkg(\'system\')">Add</button>';
    html += '</div></div>';

    body.innerHTML = html;
}

// ================================================================
// Render: Extensions section
// ================================================================
function renderContainerExtensions() {
    var body = document.getElementById('ctr-body-extensions');
    if (!body) return;

    var html = '';
    for (var i = 0; i < _containerExtensions.length; i++) {
        var ext = _containerExtensions[i];
        var togCls = ext.enabled ? 'ctr-ext-toggle active' : 'ctr-ext-toggle';
        html += '<div class="ctr-ext">';
        html += '<button class="' + togCls + '" onclick="ctrToggleExt(\'' + escHtml(ext.id) + '\',' + !ext.enabled + ')">' + (ext.enabled ? 'ON' : 'OFF') + '</button>';
        html += '<span class="ctr-ext-name">' + escHtml(ext.name) + '</span>';
        if (ext.builtin) {
            html += '<span class="ctr-ext-badge">built-in</span>';
        } else {
            html += '<button class="ctr-ext-del" onclick="ctrDeleteExt(\'' + escHtml(ext.id) + '\')">&times;</button>';
        }
        html += '</div>';
    }

    // Add custom button / form
    if (_ctrExtFormOpen) {
        html += '<div class="ctr-ext-form" id="ctr-ext-form">';
        html += '<input class="ctr-pkg-input" id="ctr-ext-name" placeholder="Extension name">';
        html += '<input class="ctr-pkg-input" id="ctr-ext-archive" placeholder="Archive path (optional)">';
        html += '<textarea class="ctr-ext-cmds" id="ctr-ext-install" placeholder="Install commands (one per line)"></textarea>';
        html += '<div class="ctr-pkg-add">';
        html += '<button class="ctr-pkg-add-btn" onclick="ctrSaveNewExt()">Save</button>';
        html += '<button class="ctr-pkg-add-btn" onclick="_ctrExtFormOpen=false;renderContainerExtensions()">Cancel</button>';
        html += '</div></div>';
    } else {
        html += '<button class="ctr-ext-add-btn" onclick="_ctrExtFormOpen=true;renderContainerExtensions()">+ Add Custom Extension</button>';
    }

    body.innerHTML = html;
}

// ================================================================
// Render: Build section
// ================================================================
function renderContainerBuild() {
    var body = document.getElementById('ctr-body-build');
    if (!body || !_containerConfig) return;

    var base = _containerConfig.base || {};
    var net = _containerConfig.network || {};
    var res = _containerConfig.resources || {};
    var html = '';

    // Node version
    html += '<div class="ctr-build-row"><span>Node version</span>';
    html += '<select class="ctr-select" id="ctr-node" onchange="ctrSaveBuild(\'base\',\'node_version\',this.value)">';
    ['18', '20', '22'].forEach(function(v) {
        var sel = (base.node_version === v) ? ' selected' : '';
        html += '<option value="' + v + '"' + sel + '>' + v + '</option>';
    });
    html += '</select></div>';

    // Python version
    html += '<div class="ctr-build-row"><span>Python version</span>';
    html += '<select class="ctr-select" id="ctr-python" onchange="ctrSaveBuild(\'base\',\'python_version\',this.value)">';
    ['3.10', '3.11', '3.12', '3.13', '3'].forEach(function(v) {
        var sel = (base.python_version === v) ? ' selected' : '';
        html += '<option value="' + v + '"' + sel + '>' + v + '</option>';
    });
    html += '</select></div>';

    // Claude version
    html += '<div class="ctr-build-row"><span>Claude version</span>';
    html += '<input class="ctr-input" id="ctr-claude-ver" value="' + escHtml(base.claude_version || 'latest') + '" onblur="ctrSaveBuild(\'base\',\'claude_version\',this.value)"></div>';

    html += '<hr class="ctr-build-divider">';

    // Bind address
    html += '<div class="ctr-build-row"><span>Bind address</span>';
    html += '<select class="ctr-select" id="ctr-bind" onchange="ctrSaveBuild(\'network\',\'bind_address\',this.value)">';
    var bindOpts = [['127.0.0.1', 'Local only'], ['0.0.0.0', 'Open']];
    for (var b = 0; b < bindOpts.length; b++) {
        var bsel = (net.bind_address === bindOpts[b][0]) ? ' selected' : '';
        html += '<option value="' + bindOpts[b][0] + '"' + bsel + '>' + bindOpts[b][1] + '</option>';
    }
    html += '</select></div>';

    // LAN toggle
    var lanCls = net.allow_lan ? 'ctr-ext-toggle active' : 'ctr-ext-toggle';
    html += '<div class="ctr-build-row"><span>Allow LAN</span>';
    html += '<button class="' + lanCls + '" id="ctr-lan-toggle" onclick="ctrToggleLan()">' + (net.allow_lan ? 'ON' : 'OFF') + '</button></div>';

    html += '<hr class="ctr-build-divider">';

    // Memory
    html += '<div class="ctr-build-row"><span>Memory</span>';
    html += '<input class="ctr-input" id="ctr-memory" value="' + escHtml(res.memory || '16g') + '" onblur="ctrSaveBuild(\'resources\',\'memory\',this.value)"></div>';

    // CPUs
    html += '<div class="ctr-build-row"><span>CPUs</span>';
    html += '<input class="ctr-input" id="ctr-cpus" value="' + escHtml(res.cpus || '4') + '" onblur="ctrSaveBuild(\'resources\',\'cpus\',this.value)"></div>';

    html += '<hr class="ctr-build-divider">';

    // Rebuild button
    html += '<button class="ctr-rebuild-btn" id="ctr-rebuild-btn" onclick="triggerBuild()">Rebuild Image</button>';

    // Build log area
    html += '<pre class="ctr-build-log hidden" id="ctr-build-log"></pre>';

    body.innerHTML = html;
}

// ================================================================
// Package management — global (config)
// ================================================================
async function ctrAddPkg(type) {
    var input = document.getElementById('ctr-add-' + type);
    if (!input) return;
    var pkg = input.value.trim();
    if (!pkg) return;

    var current = (_containerConfig.packages && _containerConfig.packages[type]) || [];
    if (current.indexOf(pkg) >= 0) { showFlash('error', 'Already added'); return; }

    var updated = current.concat([pkg]);
    var patch = { packages: {} };
    patch.packages[type] = updated;

    try {
        var resp = await fetch('/api/container/config', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
        });
        var data = await resp.json();
        if (data.ok) {
            _containerConfig = data.config;
            input.value = '';
            renderContainerPackages();
            showFlash('ok', 'Added — rebuild needed');
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

async function ctrRemovePkg(type, idx) {
    var current = (_containerConfig.packages && _containerConfig.packages[type]) || [];
    current = current.slice();
    current.splice(idx, 1);
    var patch = { packages: {} };
    patch.packages[type] = current;

    try {
        var resp = await fetch('/api/container/config', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
        });
        var data = await resp.json();
        if (data.ok) {
            _containerConfig = data.config;
            renderContainerPackages();
            showFlash('ok', 'Removed — rebuild needed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

// ================================================================
// Package management — project-level pip
// ================================================================
function ctrAddProjPkg() {
    var input = document.getElementById('ctr-add-projpip');
    if (!input) return;
    var pkg = input.value.trim();
    if (!pkg) return;

    var current = (_projectSettings && _projectSettings.packages) ? (_projectSettings.packages.pip || []) : [];
    if (current.indexOf(pkg) >= 0) { showFlash('error', 'Already added'); return; }

    var updated = current.concat([pkg]);
    _saveProjectSetting('packages', 'pip', updated);
    input.value = '';
    // Update local state and re-render
    if (_projectSettings && _projectSettings.packages) {
        _projectSettings.packages.pip = updated;
    }
    renderContainerPackages();
}

function ctrRemoveProjPkg(idx) {
    var current = (_projectSettings && _projectSettings.packages) ? (_projectSettings.packages.pip || []) : [];
    current = current.slice();
    current.splice(idx, 1);
    _saveProjectSetting('packages', 'pip', current);
    if (_projectSettings && _projectSettings.packages) {
        _projectSettings.packages.pip = current;
    }
    renderContainerPackages();
}

// ================================================================
// Extension management
// ================================================================
async function ctrToggleExt(extId, enabled) {
    try {
        var resp = await fetch('/api/container/extensions/' + encodeURIComponent(extId), {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: enabled }),
        });
        var data = await resp.json();
        if (data.ok) {
            _containerExtensions = data.extensions;
            renderContainerExtensions();
            showFlash('ok', (enabled ? 'Enabled' : 'Disabled') + ' — rebuild needed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

async function ctrDeleteExt(extId) {
    if (!confirm('Delete this extension?')) return;
    try {
        var resp = await fetch('/api/container/extensions/' + encodeURIComponent(extId), {
            method: 'DELETE',
        });
        var data = await resp.json();
        if (data.ok) {
            _containerExtensions = data.extensions;
            renderContainerExtensions();
            showFlash('ok', 'Deleted');
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

async function ctrSaveNewExt() {
    var nameEl = document.getElementById('ctr-ext-name');
    var archiveEl = document.getElementById('ctr-ext-archive');
    var installEl = document.getElementById('ctr-ext-install');
    if (!nameEl) return;

    var name = nameEl.value.trim();
    if (!name) { showFlash('error', 'Name is required'); return; }

    var install = (installEl.value || '').split('\n').map(function(l) { return l.trim(); }).filter(Boolean);
    var payload = {
        name: name,
        archive: archiveEl.value.trim() || null,
        install: install,
        enabled: true,
    };

    try {
        var resp = await fetch('/api/container/extensions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        var data = await resp.json();
        if (data.ok) {
            _containerExtensions = data.extensions;
            _ctrExtFormOpen = false;
            renderContainerExtensions();
            showFlash('ok', 'Extension added');
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

// ================================================================
// Build config save
// ================================================================
async function ctrSaveBuild(section, key, value) {
    var patch = {};
    patch[section] = {};
    patch[section][key] = value;

    try {
        var resp = await fetch('/api/container/config', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
        });
        var data = await resp.json();
        if (data.ok) {
            _containerConfig = data.config;
            showFlash('ok', 'Saved');
        }
    } catch (e) {
        showFlash('error', 'Save failed');
    }
}

function ctrToggleLan() {
    var el = document.getElementById('ctr-lan-toggle');
    if (!el) return;
    var current = el.classList.contains('active');
    var newVal = !current;
    el.classList.toggle('active', newVal);
    el.textContent = newVal ? 'ON' : 'OFF';
    ctrSaveBuild('network', 'allow_lan', newVal);
}

// ================================================================
// Build trigger + polling
// ================================================================
async function triggerBuild() {
    var btn = document.getElementById('ctr-rebuild-btn');
    var logEl = document.getElementById('ctr-build-log');
    if (btn) btn.disabled = true;
    if (logEl) {
        logEl.classList.remove('hidden');
        logEl.textContent = 'Starting build...\n';
    }

    try {
        var resp = await fetch('/api/container/build', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });
        var data = await resp.json();
        if (data.ok) {
            showFlash('ok', 'Build started');
            _startBuildPoll();
        } else {
            showFlash('error', data.error || 'Failed');
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        showFlash('error', 'Offline');
        if (btn) btn.disabled = false;
    }
}

function _startBuildPoll() {
    if (_ctrBuildPollTimer) clearInterval(_ctrBuildPollTimer);
    _ctrBuildPollTimer = setInterval(_pollBuildStatus, 2000);
}

async function _pollBuildStatus() {
    try {
        var resp = await fetch('/api/container/build/status');
        var data = await resp.json();
        if (!data.ok) return;

        var logEl = document.getElementById('ctr-build-log');
        if (logEl && data.log) {
            logEl.textContent = data.log.join('\n');
            logEl.scrollTop = logEl.scrollHeight;
        }

        if (!data.active) {
            clearInterval(_ctrBuildPollTimer);
            _ctrBuildPollTimer = null;
            var btn = document.getElementById('ctr-rebuild-btn');
            if (btn) btn.disabled = false;

            if (data.success) {
                showFlash('ok', 'Build completed');
                loadContainerStatus();
            } else {
                showFlash('error', 'Build failed');
            }
        }
    } catch (e) {
        // ignore polling errors
    }
}

// ================================================================
// Kill container
// ================================================================
async function killContainer(name) {
    if (!confirm('Kill container ' + name + '?')) return;
    try {
        var resp = await fetch('/api/container/kill/' + encodeURIComponent(name), {
            method: 'POST',
        });
        var data = await resp.json();
        if (data.ok) {
            showFlash('ok', 'Killed ' + name);
            loadContainerStatus();
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

// ================================================================
// Collapsible sections toggle
// ================================================================
function toggleCtrSection(name) {
    _ctrSections[name] = !_ctrSections[name];
    var body = document.getElementById('ctr-body-' + name);
    var arrow = document.getElementById('ctr-arrow-' + name);
    if (body) body.classList.toggle('hidden', !_ctrSections[name]);
    if (arrow) arrow.classList.toggle('open', _ctrSections[name]);
}
