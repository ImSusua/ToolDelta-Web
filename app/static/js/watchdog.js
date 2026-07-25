// 看门狗模块前端逻辑

// 转义服务端数据，防止注入到 innerHTML
function _esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// 状态 diff 缓存：避免每 3s 全量重写 statusArea 引起屏幕阅读器狂播与 DOM 抖动
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
            // diff：数据未变化则跳过重渲染，避免每 3s 全量 innerHTML 抖动与无谓 DOM 重排
            var sig;
            try { sig = JSON.stringify(s); } catch (_) { sig = null; }
            if (sig !== null && sig === _lastStatusJSON) return;
            _lastStatusJSON = sig;
            renderStatus(s);
        })
        .catch(function () {
            // 不再静默：显式错误态 + 重试按钮，告知用户已多久未更新
            renderStatusError();
        });
}

// 渲染状态卡片：状态药丸 + 关键指标，仅在健康状态变化时播报
function renderStatus(s) {
    var statusEl = document.getElementById('statusArea');
    if (!statusEl) return;
    // healthy=true 正常；enabled 但 !healthy 视为 degraded；未启用则 stopped
    var state = s.healthy ? 'healthy' : (s.enabled ? 'degraded' : 'stopped');
    var pillCls = state === 'healthy' ? 'tag-enabled' : (state === 'degraded' ? 'tag-warning' : 'tag-disabled');
    var pillText = state === 'healthy' ? '正常' : (state === 'degraded' ? '异常' : '已停止');
    // max_restarts 从配置表单读取（loadConfig 已填充），缺失则展示 ?
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

    // 启用/禁用按钮按状态互斥禁用，避免重复操作
    var enableBtn = document.getElementById('enableWatchdogBtn');
    var disableBtn = document.getElementById('disableWatchdogBtn');
    if (s.enabled) {
        if (enableBtn) enableBtn.disabled = true;
        if (disableBtn) disableBtn.disabled = false;
    } else {
        if (enableBtn) enableBtn.disabled = false;
        if (disableBtn) disableBtn.disabled = true;
    }

    // 仅在健康状态变化时更新 sr-only brief，触发屏幕阅读器播报
    var brief = document.getElementById('watchdogBrief');
    if (brief && _lastHealthy !== s.healthy) {
        brief.textContent = '健康状态：' + (s.healthy ? '正常' : '异常');
        _lastHealthy = s.healthy;
    }
}

// 错误态：显式提示已多久未更新 + 重试按钮
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
    // parseInt 失败时回退为 0，避免向服务端发送 NaN
    var safeInt = function (id) { var v = parseInt(document.getElementById(id).value, 10); return isNaN(v) ? 0 : v; };
    var payload = {
        enabled: document.getElementById('cfg_enabled').checked,
        check_interval: safeInt('cfg_check_interval'),
        auto_restart: document.getElementById('cfg_auto_restart').checked,
        max_restarts: safeInt('cfg_max_restarts'),
        restart_cooldown: safeInt('cfg_restart_cooldown')
    };
    // 保存按钮 loading 态 + 防重复点击
    var saveBtn = document.getElementById('watchdogSaveBtn');
    var origText = saveBtn ? saveBtn.textContent : '';
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中...'; }
    fetch('/api/watchdog/set', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (d && d.success) {
                showToast('配置已保存', 'success');
                // 强制刷新状态：清空 diff 缓存以确保重新渲染
                _lastStatusJSON = null;
                loadStatus();
            } else {
                showToast('配置保存失败（参数不合法）', 'error');
            }
        })
        .catch(function () { showToast('请求失败', 'error'); })
        .finally(function () { if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = origText; } });
}

function enableWatchdog(btn) {
    if (btn) { btn.disabled = true; }
    fetch('/api/watchdog/enable', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (d && d.success) {
                showToast('看门狗已启用', 'success');
                loadConfig();
                _lastStatusJSON = null; loadStatus();
            } else { showToast('启用失败', 'error'); }
        })
        .catch(function () { showToast('请求失败', 'error'); })
        .finally(function () {
            // 按钮禁用由 renderStatus 按状态决定，这里仅解除 loading
            if (btn) { btn.disabled = false; }
        });
}

function disableWatchdog(btn) {
    if (btn) { btn.disabled = true; }
    fetch('/api/watchdog/disable', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (d && d.success) {
                showToast('看门狗已禁用', 'success');
                loadConfig();
                _lastStatusJSON = null; loadStatus();
            } else { showToast('禁用失败', 'error'); }
        })
        .catch(function () { showToast('请求失败', 'error'); })
        .finally(function () {
            if (btn) { btn.disabled = false; }
        });
}

loadConfig();
loadStatus();
// 优先使用全局可见性感知轮询器；缺失时本地 setInterval 也响应 Page Visibility 暂停
if (window.TDPoll) {
    window.TDPoll.register(loadStatus, 3000);
} else {
    var _timer = setInterval(loadStatus, 3000);
    document.addEventListener('visibilitychange', function () {
        if (document.hidden) { if (_timer) { clearInterval(_timer); _timer = null; } }
        else if (!_timer) { _timer = setInterval(loadStatus, 3000); }
    });
}
