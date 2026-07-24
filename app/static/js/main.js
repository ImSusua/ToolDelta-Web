// ─── Toast 通知系统（升级版：进度条 / 点击关闭 / 堆叠上限 / 分类型时长） ───
var _TOAST_MAX = 5;            // 最多同时显示 5 条
var _TOAST_DURATIONS = { success: 3000, info: 3500, error: 6000, warning: 5000 };
// showToast(msg, type, opts) —— 兼容旧调用 showToast(msg, type)
// opts.action: { label: '重试', fn: function(){...} } 参考 figr.design toast retry button
function showToast(msg, type, opts) {
    type = type || 'info';
    opts = opts || {};
    var container = document.getElementById('toastContainer');
    if (!container) return;
    // 堆叠上限：超出移除最早的一条
    while (container.children.length >= _TOAST_MAX) {
        container.removeChild(container.firstChild);
    }
    var dur = _TOAST_DURATIONS[type] || 3500;
    // 带 action 的 toast 持续更久，给用户点击时间
    if (opts.action) dur = Math.max(dur, 8000);
    var t = document.createElement('div');
    t.className = 'toast ' + type;
    t.setAttribute('role', 'status');
    var html = '<span class="toast-msg">' + _escHtml(msg) + '</span>';
    // 可选 action 按钮（如重试），参考 figr.design toast retry button
    if (opts.action && opts.action.label) {
        html += '<button class="toast-action" type="button">' + _escHtml(opts.action.label) + '</button>';
    }
    html += '<button class="toast-close" aria-label="关闭">×</button><span class="toast-bar"></span>';
    t.innerHTML = html;
    // 点击/触摸关闭（但点击 action 按钮不关闭，由 action 自己处理）
    t.addEventListener('click', function(e) {
        if (e.target && e.target.classList && e.target.classList.contains('toast-action')) return;
        removeToast(t);
    });
    container.appendChild(t);
    // 归档到通知中心历史（toast 即时显示的同时持久化记录）
    try { if (window._archiveToast) _archiveToast(msg, type, opts); } catch(_) {}
    // 绑定 action 按钮
    if (opts.action && opts.action.fn) {
        var actionBtn = t.querySelector('.toast-action');
        if (actionBtn) actionBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            try { opts.action.fn(); } catch(_) {}
            removeToast(t);
        });
    }
    // 进度条动画
    var bar = t.querySelector('.toast-bar');
    if (bar) {
        // 强制重排后启动宽度过渡
        requestAnimationFrame(function() {
            bar.style.transition = 'width ' + dur + 'ms linear';
            bar.style.width = '0%';
        });
    }
    t._timer = setTimeout(function() { removeToast(t); }, dur);
    // 悬停暂停
    t.addEventListener('mouseenter', function() { if (t._timer) { clearTimeout(t._timer); t._timer = null; } if (bar) { bar.style.transition = 'none'; } });
    t.addEventListener('mouseleave', function() {
        if (bar) { var w = bar.offsetWidth; bar.style.transition = 'width ' + Math.max(500, dur) + 'ms linear'; bar.style.width = '0%'; }
        t._timer = setTimeout(function() { removeToast(t); }, Math.max(1000, dur));
    });
}
function removeToast(t) {
    if (!t || !t.parentNode || t._removed) return;
    t._removed = true; // 防止 click 与 mouseleave/timer 重复触发导致多次 setTimeout
    if (t._timer) { clearTimeout(t._timer); t._timer = null; }
    t.classList.add('toast-out');
    setTimeout(function() { if (t.parentNode) t.parentNode.removeChild(t); }, 200);
}
function _escHtml(s) {
    return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ─── 通用：带超时与错误归类的 fetch 封装 ───
// 所有模块应使用 tdFetch 替代原生 fetch，统一处理超时与错误提示。
// 返回 Promise<response>；网络/超时错误 reject 时附带 userMessage 字段。
window.tdFetch = function(url, opts, timeoutMs) {
    opts = opts || {};
    timeoutMs = timeoutMs || 15000; // 默认 15s 超时
    var ctrl = null;
    var timer = null;
    if (window.AbortController) {
        ctrl = new AbortController();
        opts.signal = ctrl.signal;
        timer = setTimeout(function() { ctrl.abort(); }, timeoutMs);
    }
    return fetch(url, opts).then(function(r) {
        if (timer) clearTimeout(timer);
        return r;
    }).catch(function(e) {
        if (timer) clearTimeout(timer);
        // 包装错误，附 userMessage 供调用方展示
        var msg;
        if (e && e.name === 'AbortError') msg = '请求超时，请检查网络后重试';
        else if (e && e.name === 'TypeError') msg = '网络连接失败';
        else msg = '请求失败';
        var err = new Error(msg);
        err.userMessage = msg;
        err.original = e;
        throw err;
    });
};

// ─── 通用：防抖与按钮锁 ───
// 防止用户快速重复点击触发多次相同请求。
// 用法：onclick="tdLock(this, function(){ ... return promise; })"
window.tdLock = function(btn, fn) {
    if (btn && btn.disabled) return;
    if (btn) { btn.disabled = true; }
    var p;
    try { p = fn(); } catch(e) { if (btn) btn.disabled = false; throw e; }
    if (p && typeof p.then === 'function') {
        p.then(function(){ if (btn) btn.disabled = false; }, function(){ if (btn) btn.disabled = false; });
    } else {
        if (btn) btn.disabled = false;
    }
    return p;
};

// ─── 通用：状态组件 renderState（参考 Vercel/Primer 四态：loading → content → error → empty） ──
// 统一所有列表/卡片的状态展示，避免散落实现不一致。
// 用法：
//   renderState(el, 'loading', { title: '加载中…' });
//   renderState(el, 'error',   { message: '加载插件失败', retry: loadPlugins, icon: '⚠️' });
//   renderState(el, 'empty',   { icon: '🧩', title: '暂无插件', hint: '点击添加', cta: '添加插件', ctaFn: showAddPluginModal });
//   renderState(el, 'skeleton',{ count: 3 });
// 兼容旧调用 renderErrorState(container, message, retryFn, opts)
window.renderState = function(container, state, opts) {
    if (!container) return;
    opts = opts || {};
    var esc = window._escHtml || function(s){ return String(s==null?'':s); };
    var html = '';
    if (state === 'loading') {
        html = '<div class="empty-state" aria-busy="true" aria-live="polite">' +
            '<span class="spinner" role="status" aria-label="加载中"></span>' +
            '<div class="empty-title" style="margin-top:12px">' + esc(opts.title || '加载中…') + '</div></div>';
        container.innerHTML = html;
        return;
    }
    if (state === 'skeleton') {
        var n = opts.count || 3;
        html = '<div class="skeleton-list" aria-busy="true" aria-live="polite">';
        for (var i = 0; i < n; i++) html += '<div class="skeleton-card"></div>';
        html += '</div>';
        container.innerHTML = html;
        return;
    }
    if (state === 'error') {
        html = '<div class="empty-state empty-state-error" role="alert">' +
            '<div class="icon" aria-hidden="true">' + esc(opts.icon || '⚠️') + '</div>' +
            '<div class="empty-title">' + esc(opts.message || opts.title || '加载失败') + '</div>' +
            '<p class="empty-hint">' + esc(opts.hint || '请检查网络后重试') + '</p>';
        if (typeof opts.retry === 'function') {
            var btnId = 'td_retry_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
            html += '<button class="btn btn-outline btn-sm" id="' + btnId + '">' + esc(opts.retryText || '重试') + '</button>';
            container.innerHTML = html + '</div>';
            setTimeout(function() {
                var b = document.getElementById(btnId);
                if (b) b.addEventListener('click', function() {
                    container.innerHTML = '<div class="empty-state" aria-busy="true" aria-live="polite"><span class="spinner" role="status" aria-label="加载中"></span><div class="empty-title" style="margin-top:12px">加载中…</div></div>';
                    try { opts.retry(); } catch(e) {}
                });
            }, 0);
            return;
        }
        container.innerHTML = html + '</div>';
        return;
    }
    if (state === 'empty') {
        html = '<div class="empty-state">' +
            '<div class="icon" aria-hidden="true">' + esc(opts.icon || '📭') + '</div>' +
            '<div class="empty-title">' + esc(opts.title || '暂无数据') + '</div>';
        if (opts.hint) html += '<p class="empty-hint">' + esc(opts.hint) + '</p>';
        if (opts.cta && typeof opts.ctaFn === 'function') {
            var id = 'td_cta_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
            html += '<button class="btn btn-outline btn-sm" id="' + id + '">' + esc(opts.cta) + '</button>';
            container.innerHTML = html + '</div>';
            setTimeout(function() {
                var b = document.getElementById(id);
                if (b) b.addEventListener('click', opts.ctaFn);
            }, 0);
            return;
        }
        container.innerHTML = html + '</div>';
        return;
    }
};

// 兼容旧 API：renderErrorState(container, message, retryFn, opts)
window.renderErrorState = function(container, message, retryFn, opts) {
    opts = opts || {};
    opts.message = message;
    opts.retry = retryFn;
    window.renderState(container, 'error', opts);
};

// ─── 通用：字段级内联校验（参考 shadcn/ui Form / Linear 字段提示） ───
// 用法：setFieldError(inputEl, '两次密码不一致') / setFieldError(inputEl, '')  // 清除
// 自动给 .form-group 追加 .field-error 提示文字，input 加 aria-invalid 红框。
window.setFieldError = function(input, msg) {
    if (!input) return;
    var group = input.closest('.form-group') || input.parentElement;
    input.setAttribute('aria-invalid', msg ? 'true' : 'false');
    if (!group) { if (!msg) return; }
    var err = group ? group.querySelector('.field-error') : null;
    if (!msg) { if (err) err.remove(); input.removeAttribute('aria-invalid'); return; }
    if (!err) {
        err = document.createElement('div');
        err.className = 'field-error'; err.setAttribute('role', 'alert');
        if (group) group.appendChild(err);
        var id = 'fe_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
        err.id = id;
        var old = input.getAttribute('aria-describedby') || '';
        input.setAttribute('aria-describedby', (old + ' ' + id).trim());
    }
    err.textContent = msg;
};


// ─── ToolDelta 启停 ───
function toggleTool() {
    var btn = document.getElementById('mainToggleBtn') || document.getElementById('consoleToggleBtn');
    if (!btn) return;
    var isRunning = btn.getAttribute('data-running') === 'true';
    btn.disabled = true;
    btn.textContent = '操作中...';
    var url = isRunning ? '/api/tool/stop' : '/api/tool/start';
    fetch(url, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) {
                showToast(isRunning ? '已停止' : '已启动', 'success');
                updateToggleState(!isRunning);
            } else {
                showToast('操作失败', 'error');
                btn.disabled = false;
                btn.textContent = isRunning ? '停止' : '启动';
            }
        })
        .catch(function() {
            showToast('请求失败', 'error');
            btn.disabled = false;
            btn.textContent = isRunning ? '停止' : '启动';
        });
}

function updateToggleState(running) {
    var btn1 = document.getElementById('mainToggleBtn');
    var btn2 = document.getElementById('consoleToggleBtn');
    [btn1, btn2].forEach(function(btn) {
        if (!btn) return;
        btn.disabled = false;
        btn.setAttribute('data-running', running ? 'true' : 'false');
        btn.textContent = running ? '停止' : '启动';
        btn.className = 'btn ' + (running ? 'btn-danger' : 'btn-success');
    });
    var sd = document.getElementById('sidebarStatus');
    if (sd) {
        sd.className = 'status-dot ' + (running ? 'running' : 'stopped');
        sd.title = running ? '运行中' : '已停止';
        // 同步无障碍标签，让屏幕阅读器能感知连接状态（不仅是颜色）
        sd.setAttribute('aria-label', running ? 'ToolDelta 运行中' : 'ToolDelta 已停止');
    }
}

// ─── 自定义确认弹窗（替代浏览器 confirm） + 输入确认弹窗 ───
var _confirmCallback = null;
var _promptCallback = null;
var _lastFocus = null;  // 打开弹窗前记录焦点，关闭后归还

function showConfirm(msg, callback, danger) {
    _lastFocus = document.activeElement;
    var msgEl = document.getElementById('confirmMessage');
    var okBtn = document.getElementById('confirmOkBtn');
    var modal = document.getElementById('confirmModal');
    if (!msgEl || !okBtn || !modal) return;
    msgEl.textContent = msg;
    okBtn.className = 'btn ' + (danger === false ? 'btn-primary' : 'btn-danger');
    okBtn.textContent = '确定';
    _confirmCallback = callback;
    modal.classList.add('active');
    setTimeout(function() { okBtn.focus(); }, 50);
}

function closeConfirm(result) {
    var modal = document.getElementById('confirmModal');
    if (modal) modal.classList.remove('active');
    if (_confirmCallback) {
        _confirmCallback(result);
        _confirmCallback = null;
    }
    if (_lastFocus && _lastFocus.focus) _lastFocus.focus();
}

// 自定义输入弹窗（替代浏览器 prompt）
function showPrompt(title, placeholder, defaultValue, callback) {
    _lastFocus = document.activeElement;
    var modal = document.getElementById('promptModal');
    var titleEl = document.getElementById('promptTitle');
    var input = document.getElementById('promptInput');
    if (!modal || !titleEl || !input) return;
    titleEl.textContent = title;
    input.placeholder = placeholder || '';
    input.value = defaultValue || '';
    _promptCallback = callback;
    modal.classList.add('active');
    setTimeout(function() { input.focus(); input.select(); }, 50);
}

function closePrompt(result) {
    var modal = document.getElementById('promptModal');
    if (modal) modal.classList.remove('active');
    var input = document.getElementById('promptInput');
    var val = (result && input) ? input.value : null;
    if (_promptCallback) {
        _promptCallback(val);
        _promptCallback = null;
    }
    if (_lastFocus && _lastFocus.focus) _lastFocus.focus();
}

// ─── 模态框：Esc 关闭 + 背景点击关闭 + 焦点陷阱 ───
function _openModal(id) {
    _lastFocus = document.activeElement;
    var m = document.getElementById(id);
    if (!m) return;
    m.classList.add('active');
    // 标记背景为 inert：阻止屏幕阅读器与 Tab 访问背景内容
    _setInertExcept(m);
    var focusable = m.querySelectorAll('input:not([type=hidden]),select,textarea,button,a[href]');
    if (focusable.length) setTimeout(function() { focusable[0].focus(); }, 50);
}
function _closeModal(id) {
    var m = document.getElementById(id);
    if (!m) return;
    m.classList.remove('active');
    _clearInert();
    if (_lastFocus && _lastFocus.focus) _lastFocus.focus();
}
function closeModal(id) { _closeModal(id); }

// 给除指定模态外的所有顶层交互元素加 inert（背景冻结）
function _setInertExcept(activeModal) {
    var siblings = document.body.children;
    for (var i = 0; i < siblings.length; i++) {
        var el = siblings[i];
        if (el === activeModal || el.contains(activeModal)) continue;
        if (el.tagName === 'SCRIPT' || el.tagName === 'LINK' || el.tagName === 'STYLE') continue;
        if (el.hasAttribute && !el.hasAttribute('inert')) {
            try { el.setAttribute('inert', ''); el.setAttribute('data-td-inert', '1'); } catch(e) {}
        }
    }
}
function _clearInert() {
    var marked = document.querySelectorAll('[data-td-inert="1"]');
    marked.forEach(function(el) {
        el.removeAttribute('inert');
        el.removeAttribute('data-td-inert');
    });
}

// 兼容旧调用：点击 .modal-overlay 背景关闭（点击内容不关闭）
document.addEventListener('click', function(e) {
    if (e.target.classList && e.target.classList.contains('modal-overlay')) {
        e.target.classList.remove('active');
        if (_lastFocus && _lastFocus.focus) _lastFocus.focus();
    }
});

// Esc 关闭最顶层弹窗 + 焦点陷阱
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' || e.keyCode === 27) {
        // 找最顶层 active 的 modal-overlay
        var modals = document.querySelectorAll('.modal-overlay.active');
        if (modals.length) {
            var top = modals[modals.length - 1];
            // 确认弹窗走专用回调
            if (top.id === 'confirmModal') { closeConfirm(false); }
            else if (top.id === 'promptModal') { closePrompt(false); }
            else { top.classList.remove('active'); if (_lastFocus && _lastFocus.focus) _lastFocus.focus(); }
            e.preventDefault();
        }
        // 没有弹窗时，移动端关闭侧边栏
        else if (document.body.classList.contains('sidebar-open')) {
            document.body.classList.remove('sidebar-open');
        }
        return;
    }
    // 焦点陷阱：Tab 在弹窗内循环
    if (e.key === 'Tab' || e.keyCode === 9) {
        var active = document.querySelector('.modal-overlay.active');
        if (!active) return;
        var f = active.querySelectorAll('input:not([type=hidden]),select,textarea,button,a[href],[tabindex]:not([tabindex="-1"])');
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) {
            last.focus(); e.preventDefault();
        } else if (!e.shiftKey && document.activeElement === last) {
            first.focus(); e.preventDefault();
        }
    }
});

// ─── 全局快捷键：/ 聚焦搜索框（非输入态时） ───
document.addEventListener('keydown', function(e) {
    if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
    var tag = (document.activeElement && document.activeElement.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    var search = document.querySelector('input[type=text][id*=Search], input[type=text][id*=search], #pluginSearch, #searchInput, #presetSearch, #marketSearch, #commandSearch');
    if (search) { search.focus(); e.preventDefault(); }
});

// ─── Tab 切换（含 aria-selected） ───
document.addEventListener('click', function(e) {
    var tabBtn = e.target.closest('.tab-btn');
    if (!tabBtn) return;
    var tabId = tabBtn.getAttribute('data-tab');
    var parent = tabBtn.closest('.modal') || tabBtn.closest('.card') || document;
    parent.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); b.setAttribute('aria-selected', 'false'); });
    tabBtn.classList.add('active');
    tabBtn.setAttribute('aria-selected', 'true');
    parent.querySelectorAll('.tab-content').forEach(function(c) { c.classList.remove('active'); });
    var target = document.getElementById(tabId);
    if (target) target.classList.add('active');
});

// ─── 通用：按钮禁用防双击 + 自动恢复（参考 calmops button loading state） ───
// loading 状态：按钮内嵌 spinner + 灰度，保留原文本避免布局抖动
function withGuard(btn, fn) {
    if (!btn) return fn();
    if (btn.disabled) return;
    var orig = btn.textContent;
    btn.disabled = true;
    btn.classList.add('btn-loading');
    btn.textContent = orig; // 保留原文本，spinner 由 CSS::before 注入
    var p = fn();
    if (p && p.then) {
        p.then(function() { btn.disabled = false; btn.classList.remove('btn-loading'); btn.textContent = orig; })
         .catch(function() { btn.disabled = false; btn.classList.remove('btn-loading'); btn.textContent = orig; });
    } else {
        btn.disabled = false; btn.classList.remove('btn-loading'); btn.textContent = orig;
    }
    return p;
}

// ─── 通用：视图状态持久化（参考 Linear 表格筛选状态保留 / Notion 列配置记忆） ───
// 各页面调用 tdPref.bind(pageKey, inputEl, stateKey) 即可双向持久化 input/select 值，
// 刷新后自动恢复，避免管理员反复重新筛选。同时提供「最近访问」用于命令面板默认置顶。
window.tdPref = (function(){
    var PREFIX = 'td_pref_';
    function _k(page, key) { return PREFIX + page + '_' + key; }
    function get(page, key, def) {
        try { var v = localStorage.getItem(_k(page, key)); return v === null ? def : v; }
        catch (e) { return def; }
    }
    function set(page, key, val) {
        try { localStorage.setItem(_k(page, key), String(val)); } catch (e) {}
    }
    function bind(page, el, key) {
        if (!el) return;
        var saved = get(page, key, null);
        if (saved !== null && saved !== undefined) el.value = saved;
        var evt = (el.tagName === 'SELECT') ? 'change' : 'input';
        el.addEventListener(evt, function() { set(page, key, el.value); });
        // 兼容 select 也响应 change（上面 evt 已覆盖）
        if (el.tagName === 'SELECT') el.addEventListener('change', function() { set(page, key, el.value); });
    }
    // ── 最近访问页面（最多 5 个），供命令面板默认展示 ──
    var RECENT_KEY = 'td_recent';
    function pushRecent(href) {
        if (!href) return;
        try {
            var arr = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
            arr = [href].concat(arr.filter(function(h){ return h !== href; }));
            arr = arr.slice(0, 5);
            localStorage.setItem(RECENT_KEY, JSON.stringify(arr));
        } catch (e) {}
    }
    function getRecent() {
        try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); }
        catch (e) { return []; }
    }
    return { get: get, set: set, bind: bind, pushRecent: pushRecent, getRecent: getRecent };
})();

// ─── 通用：复制到剪贴板并给反馈（参考 GitHub 复制按钮 + Vercel 一键复制 token） ───
// 用法：tdCopy('文本', '已复制 token') 或 tdCopy(textEl.value, '已复制路径')
window.tdCopy = function(text, successMsg) {
    function _fallback() {
        var ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); } catch(e) {}
        document.body.removeChild(ta);
    }
    var done = false;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function(){ done = true; }).catch(_fallback).then(function(){
            if (typeof showToast === 'function') showToast(successMsg || '已复制', 'success');
        });
    } else {
        _fallback();
        if (typeof showToast === 'function') showToast(successMsg || '已复制', 'success');
    }
};

// ─── 通用：可见性感知的轮询注册器 ───
// 各页面模块（dashboard/scheduler/watchdog/logs/status）共用此机制，
// 页面隐藏或网络离线时统一暂停所有轮询，可见/恢复时统一恢复，节省移动端后台电量。
window.TDPoll = (function(){
    var registry = []; // {fn, interval, timer}
    var _offline = !navigator.onLine;
    function shouldSkip() {
        // 页面隐藏 或 网络离线 时跳过本次轮询
        return document.hidden || _offline;
    }
    function tick(reg) {
        if (shouldSkip()) return;
        try { reg.fn(); } catch(e) { /* 静默：避免单次异常中断整个轮询链 */ }
    }
    function start(reg) {
        if (reg.timer) return;
        reg.timer = setInterval(function(){ tick(reg); }, reg.interval);
    }
    function stop(reg) {
        if (reg.timer) { clearInterval(reg.timer); reg.timer = null; }
    }
    document.addEventListener('visibilitychange', function() {
        if (document.hidden) {
            registry.forEach(stop);
        } else {
            registry.forEach(function(reg){ start(reg); if (!_offline) reg.fn(); });
        }
    });
    // 网络离线/恢复事件：暂停/恢复轮询并提示用户
    function _updateOfflineBanner(offline) {
        var b = document.getElementById('offlineBanner');
        if (b) {
            if (offline) { b.removeAttribute('hidden'); b.classList.add('visible'); }
            else { b.classList.remove('visible'); setTimeout(function(){ b.setAttribute('hidden',''); }, 300); }
        }
    }
    window.addEventListener('online', function() {
        _offline = false;
        registry.forEach(function(reg){ start(reg); reg.fn(); });
        _updateOfflineBanner(false);
        if (typeof showToast === 'function') showToast('网络已恢复', 'success');
    });
    window.addEventListener('offline', function() {
        _offline = true;
        registry.forEach(stop);
        _updateOfflineBanner(true);
        if (typeof showToast === 'function') showToast('网络已断开，部分功能不可用', 'warning');
    });
    // 初始离线状态：页面加载时若已离线则显示横幅
    if (_offline) _updateOfflineBanner(true);
    return {
        register: function(fn, interval) {
            var reg = { fn: fn, interval: interval, timer: null };
            registry.push(reg);
            // 离线时不启动定时器，等恢复后由 online 事件启动
            if (!_offline) start(reg);
            return reg;
        },
        unregister: function(reg) {
            stop(reg);
            var i = registry.indexOf(reg);
            if (i >= 0) registry.splice(i, 1);
        }
    };
})();

// ─── 全局错误捕获：未处理异常与 Promise 拒收 ───
// 参考 GitHub Sentry/生产级 web 应用最佳实践：
// 静默失败让用户困惑，捕获后给出友好提示并保留控制台原始错误便于调试。
window.addEventListener('error', function(e) {
    // 忽略资源加载错误（由 onload/onerror 各自处理），只捕获 JS 运行时错误
    if (!e || !e.message) return;
    try { console.error('[TD Uncaught]', e.message, e.error); } catch(_) {}
    // 避免循环报错：showToast 内部异常不再触发 toast
    try { showToast('发生未知错误，请刷新页面重试', 'error'); } catch(_) {}
});
window.addEventListener('unhandledrejection', function(e) {
    var reason = e && e.reason;
    var msg = (reason && (reason.message || reason.userMessage)) || 'Promise 未捕获拒绝';
    try { console.error('[TD UnhandledRejection]', reason); } catch(_) {}
    // 用户级错误（tdFetch 包装的 userMessage）才提示，技术性拒绝静默
    if (reason && reason.userMessage) {
        try { showToast(reason.userMessage, 'error'); } catch(_) {}
    }
});

// ─── 状态轮询 ───
// 统一使用 TDPoll：页面隐藏时自动暂停（省电），可见时恢复；
// 仅当页面存在状态指示元素时才注册，避免在 settings 等页面空跑。
function checkStatus() {
    fetch('/api/status')
        .then(function(r) { return r.json(); })
        .then(function(d) {
            updateToggleState(d.running);
        })
        .catch(function() {});
}
// 按需注册：仅当页面有 sidebar 状态点或 toggle 按钮时才轮询
if (document.getElementById('sidebarStatus') ||
    document.getElementById('mainToggleBtn') ||
    document.getElementById('consoleToggleBtn')) {
    window.TDPoll.register(checkStatus, 3000);
    setTimeout(checkStatus, 500);
}

// ─── 可访问性增强：自动为动态元素加 ARIA 属性 ───
// spinner 与 loading-text 在多处模板出现，统一在此注入语义。
// 性能：控制台/日志页 DOM 高频变更，故仅在页面就绪时扫描一次，
// 不再使用 MutationObserver 监听整棵 body 子树（避免高频刷屏时持续触发回调）。
(function(){
    function enhanceA11y() {
        var spinners = document.querySelectorAll('.spinner:not([role])');
        spinners.forEach(function(s) {
            s.setAttribute('role', 'status');
            s.setAttribute('aria-label', '加载中');
        });
        var loaders = document.querySelectorAll('.loading-text:not([aria-live])');
        loaders.forEach(function(l) { l.setAttribute('aria-live', 'polite'); });
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', enhanceA11y);
    } else { enhanceA11y(); }
    // 首次渲染后再次扫描一次（覆盖脚本动态注入的初始元素）
    setTimeout(enhanceA11y, 300);
})();

// ─── 导航进度条（参考 NProgress / GitHub 顶部进度条 / Vercel 导航反馈） ───
// 全页刷新场景下消除"白屏闪烁"，弱网移动端感知更顺畅。
// 拦截同源 <a> 点击：启动进度条；DOMContentLoaded 推进到 100% 并淡出。
(function(){
    var bar = document.createElement('div');
    bar.id = 'tdNavProgress';
    bar.style.cssText = 'position:fixed;top:0;left:0;height:2px;width:0;background:var(--primary);z-index:10000;opacity:0;transition:width .25s ease, opacity .3s ease;pointer-events:none;border-radius:0 2px 2px 0;box-shadow:0 0 8px var(--primary)';
    // 移动端安全区域：进度条不遮挡 status bar
    bar.style.top = 'env(safe-area-inset-top, 0px)';
    document.documentElement.appendChild(bar);
    var w = 0, timer = null, done = false;
    function _set(p) { w = p; bar.style.width = p + '%'; bar.style.opacity = '1'; }
    window._navStart = function() {
        done = false; _set(0);
        if (timer) clearInterval(timer);
        timer = setInterval(function() {
            // 缓慢推进到 85%，模拟真实进度感
            _set(Math.min(w + Math.random() * 12 + 3, 85));
        }, 220);
    };
    window._navDone = function() {
        if (done) return; done = true;
        if (timer) { clearInterval(timer); timer = null; }
        _set(100);
        setTimeout(function(){ bar.style.opacity = '0'; }, 200);
        setTimeout(function(){ bar.style.width = '0%'; }, 500);
    };
    // 同源导航链接点击触发
    document.addEventListener('click', function(e) {
        var a = e.target.closest && e.target.closest('a[href]');
        if (!a) return;
        if (a.target === '_blank' || e.metaKey || e.ctrlKey || e.shiftKey) return;
        var href = a.getAttribute('href');
        if (!href || href.charAt(0) === '#' || href.indexOf('javascript:') === 0) return;
        if (a.hasAttribute('download')) return;
        try {
            if (a.origin === location.origin && a.pathname !== location.pathname) {
                window._navStart();
                // 记录最近访问
                if (window.tdPref) tdPref.pushRecent(a.pathname);
            }
        } catch(_) {}
    });
    // 首次加载完成时若有进度条则结束
    if (document.readyState === 'complete') window._navDone();
    else window.addEventListener('load', window._navDone);
    // 兜底：8 秒未完成也强制结束，避免卡在 85%
    setTimeout(function() { if (!done) window._navDone(); }, 8000);
})();

// ─── 通知中心：Toast 历史归档（参考 Linear 通知中心 / GitHub 通知下拉） ───
// showToast 自动写入历史，侧边栏铃铛显示未读数，点击展开历史面板。
var _TOAST_HISTORY = [];
var _TOAST_HISTORY_MAX = 50;
var _NOTIFY_UNREAD = 0;
function _archiveToast(msg, type, opts) {
    var item = { msg: msg, type: type || 'info', ts: Date.now(), action: (opts && opts.action && opts.action.label) || null };
    _TOAST_HISTORY.unshift(item);
    if (_TOAST_HISTORY.length > _TOAST_HISTORY_MAX) _TOAST_HISTORY.length = _TOAST_HISTORY_MAX;
    // 错误/警告类记入未读，info/success 不打扰
    if (type === 'error' || type === 'warning') _NOTIFY_UNREAD++;
    _updateNotifyBadge();
}
function _updateNotifyBadge() {
    var badge = document.getElementById('notifyBadge');
    if (!badge) return;
    if (_NOTIFY_UNREAD > 0) {
        badge.textContent = _NOTIFY_UNREAD > 99 ? '99+' : _NOTIFY_UNREAD;
        badge.style.display = '';
        badge.setAttribute('aria-label', _NOTIFY_UNREAD + ' 条未读通知');
    } else {
        badge.style.display = 'none';
        badge.removeAttribute('aria-label');
    }
}
function _fmtTs(ts) {
    var d = new Date(ts);
    var now = Date.now();
    var diff = (now - ts) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    var pad = function(n) { return n < 10 ? '0' + n : '' + n; };
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}
function openNotifyPanel() {
    var modal = document.getElementById('notifyModal');
    if (!modal) return;
    _renderNotifyList();
    _openModal('notifyModal');
    _NOTIFY_UNREAD = 0;
    _updateNotifyBadge();
}
function _renderNotifyList() {
    var list = document.getElementById('notifyList');
    if (!list) return;
    if (_TOAST_HISTORY.length === 0) {
        list.innerHTML = '<div class="empty-state" style="padding:40px 20px"><div class="icon" aria-hidden="true">🔔</div><div class="empty-title">暂无通知</div><p class="empty-hint">操作产生的通知会显示在这里</p></div>';
        return;
    }
    var esc = window._escHtml || String;
    list.innerHTML = _TOAST_HISTORY.map(function(it) {
        var cls = 'notify-item notify-' + it.type;
        var ico = it.type === 'error' ? '⚠️' : (it.type === 'warning' ? '⚡' : (it.type === 'success' ? '✓' : 'ℹ'));
        var actHtml = it.action ? '<button class="notify-action" type="button">' + esc(it.action) + '</button>' : '';
        return '<div class="' + cls + '">' +
            '<span class="notify-ico" aria-hidden="true">' + ico + '</span>' +
            '<div class="notify-body"><div class="notify-msg">' + esc(it.msg) + '</div>' +
            '<div class="notify-ts">' + _fmtTs(it.ts) + '</div></div>' +
            actHtml + '</div>';
    }).join('');
}
window._archiveToast = _archiveToast;
