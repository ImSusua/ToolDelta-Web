// 看门狗模块前端逻辑

// 惰性查找：脚本加载时 main.js 可能尚未执行，_escHtml 为 undefined
function _esc(s) {
    return (window._escHtml || function(v) {
        return String(v == null ? '' : v).replace(/[&<>"']/g, function(m) {
            return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m];
        });
    })(s);
}

var _lastStatusJSON = null;
var _lastHealthy = null;
var _lastOk = Date.now();

function loadConfig() {
    fetch('/api/watchdog/config')
        .then(function (r) { return r.json(); })
        .then(function (cfg) {
            var setVal = function (id, v) { var el = document.getElementById(id); if (el) el.value = v; };
            var setChk = function (id, v) { var el = document.getElementById(id); if (el) el.checked = !!v; };
            setChk('cfg_enabled', cfg.enabled);
            setVal('cfg_check_interval', cfg.check_interval);
            setChk('cfg_auto_restart', cfg.auto_restart);
            setVal('cfg_max_restarts', cfg.max_restarts);
            setVal('cfg_restart_cooldown', cfg.restart_cooldown);
        })
        .catch(function () { showToast('加载配置失败', 'error'); });
}

function loadStatus() {
    fetch('/api/watchdog/status')
        .then(function (r) { return r.json(); })
        .then(function (s) {
            _lastOk = Date.now();
            var sig;
            try { sig = JSON.stringify(s); } catch (_) { sig = null; }
            if (sig !== null && sig === _lastStatusJSON) return;
            _lastStatusJSON = sig;
            renderStatus(s);
        })
        .catch(function () {
            renderStatusError();
        });
}

function renderStatus(s) {
    var statusEl = document.getElementById('statusArea');
    if (!statusEl) return;
    var state = s.healthy ? 'healthy' : (s.enabled ? 'degraded' : 'stopped');
    var pillCls = state === 'healthy' ? 'tag-enabled' : (state === 'degraded' ? 'tag-warning' : 'tag-disabled');
    var pillText = state === 'healthy' ? '正常' : (state === 'degraded' ? '异常' : '已停止');
    var maxEl = document.getElementById('cfg_max_restarts');
    var maxR = maxEl ? maxEl.value : '?';

    var html = '<div class="watchdog-status-card" data-state="' + state + '">' +
        '<span class="tag ' + pillCls + '">● ' + _esc(pillText) + '</span>' +
        '<div class="meta-text" style="margin-top:6px">重启次数 ' + _esc(s.restarts_count || 0) + ' / ' + _esc(maxR) +
        ' · 上次检查 ' + _esc(s.last_check || '—') + '</div>' +
        '<div class="meta-text">上次重启：' + _esc(s.last_restart || '—') + '</div>' +
        '<div class="meta-text">最近事件：' + _esc(s.last_event || '—') + '</div>' +
        '</div>';
    statusEl.innerHTML = html;

    var enableBtn = document.getElementById('enableWatchdogBtn');
    var disableBtn = document.getElementById('disableWatchdogBtn');
    if (s.enabled) {
        if (enableBtn) {
            enableBtn.disabled = true;
            enableBtn.className = 'btn btn-success active';
        }
        if (disableBtn) {
            disableBtn.disabled = false;
            disableBtn.className = 'btn btn-outline';
        }
    } else {
        if (enableBtn) {
            enableBtn.disabled = false;
            enableBtn.className = 'btn btn-outline';
        }
        if (disableBtn) {
            disableBtn.disabled = true;
            disableBtn.className = 'btn btn-danger active';
        }
    }

    var brief = document.getElementById('watchdogBrief');
    if (brief && _lastHealthy !== s.healthy) {
        brief.textContent = '健康状态：' + (s.healthy ? '正常' : '异常');
        _lastHealthy = s.healthy;
    }
}

function renderStatusError() {
    var statusEl = document.getElementById('statusArea');
    if (!statusEl) return;
    var sec = Math.floor((Date.now() - _lastOk) / 1000);
    if (window.renderState) {
        renderState(statusEl, 'error', {
            message: '连接失败', hint: '已 ' + sec + ' 秒未更新', retry: loadStatus
        });
    } else {
        statusEl.innerHTML = '<div class="empty-state empty-state-error" role="alert">' +
            '<div class="icon">⚠</div><div class="empty-title">连接失败</div>' +
            '<p class="empty-hint">已 ' + sec + ' 秒未更新</p>' +
            '<button class="btn btn-outline btn-sm" onclick="loadStatus()">重试</button></div>';
    }
}

function saveConfig() {
    var safeInt = function (id) { var v = parseInt(document.getElementById(id).value, 10); return isNaN(v) ? 0 : v; };
    var payload = {
        enabled: document.getElementById('cfg_enabled').checked,
        check_interval: safeInt('cfg_check_interval'),
        auto_restart: document.getElementById('cfg_auto_restart').checked,
        max_restarts: safeInt('cfg_max_restarts'),
        restart_cooldown: safeInt('cfg_restart_cooldown')
    };
    var saveBtn = document.getElementById('watchdogSaveBtn');
    var origText = saveBtn ? saveBtn.textContent : '';
    if (saveBtn) saveBtn.textContent = '保存中...';
    tdLock(saveBtn, function () {
        return fetch('/api/watchdog/set', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d && d.success) {
                    showToast('配置已保存', 'success');
                    _lastStatusJSON = null;
                    loadStatus();
                } else {
                    showToast('配置保存失败（参数不合法）', 'error');
                }
            })
            .catch(function () { showToast('请求失败', 'error'); });
    }).then(function () {
        if (saveBtn) saveBtn.textContent = origText;
    }, function () {
        if (saveBtn) saveBtn.textContent = origText;
    });
}

function enableWatchdog(btn) {
    tdLock(btn, function () {
        return fetch('/api/watchdog/enable', { method: 'POST' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d && d.success) {
                    showToast('看门狗已启用', 'success');
                    loadConfig();
                    _lastStatusJSON = null; loadStatus();
                } else { showToast('启用失败', 'error'); }
            })
            .catch(function () { showToast('请求失败', 'error'); });
    });
}

function disableWatchdog(btn) {
    tdLock(btn, function () {
        return fetch('/api/watchdog/disable', { method: 'POST' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d && d.success) {
                    showToast('看门狗已禁用', 'success');
                    loadConfig();
                    _lastStatusJSON = null; loadStatus();
                } else { showToast('禁用失败', 'error'); }
            })
            .catch(function () { showToast('请求失败', 'error'); });
    });
}

function checkNow(btn) {
    var origText = btn ? btn.textContent : '';
    if (btn) btn.textContent = '检查中...';
    tdLock(btn, function () {
        return fetch('/api/watchdog/check', { method: 'POST' })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d && d.success) {
                    showToast('检查完成', 'success');
                    _lastStatusJSON = null;
                    loadStatus();
                } else {
                    showToast((d && d.error) || '检查失败', 'error');
                }
            })
            .catch(function () { showToast('请求失败', 'error'); });
    }).then(function () {
        if (btn) btn.textContent = origText;
    }, function () {
        if (btn) btn.textContent = origText;
    });
}

loadConfig();
loadStatus();
if (window.TDPoll) {
    window.TDPoll.register(loadStatus, 3000);
} else {
    var _timer = setInterval(loadStatus, 3000);
    document.addEventListener('visibilitychange', function () {
        if (document.hidden) { if (_timer) { clearInterval(_timer); _timer = null; } }
        else if (!_timer) { _timer = setInterval(loadStatus, 3000); }
    });
}
