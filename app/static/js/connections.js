// 服务器连接配置前端逻辑

// 连接列表缓存：避免编辑时再次拉取全列表
var _connCache = [];

// 统一转义：优先使用 main.js 的 _escHtml，并补充反斜杠与正斜杠转义
function escapeHtml(s) {
    return (window._escHtml || function (x) {
        return String(x == null ? '' : x).replace(/[&<>"'\/\\]/g, function (m) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '/': '&#47;', '\\': '&#92;' }[m];
        });
    })(s);
}

// 渲染连接列表（基于 _connCache），供加载与本地撤销复用
function renderList() {
    var tbody = document.getElementById('connTableBody');
    var empty = document.getElementById('connEmpty');
    if (!tbody) return;
    if (!_connCache.length) {
        tbody.innerHTML = '';
        if (empty) empty.style.display = 'block';
        return;
    }
    if (empty) empty.style.display = 'none';
    // 安全：使用 data-* 属性承载 id（data-attr 经过 HTML 实体编码），事件委托派发
    // 避免 onclick="fn('...')" 字符串拼接导致的属性上下文注入（XSS）
    tbody.innerHTML = _connCache.map(function (c) {
        var addr = (c.host || '') + ':' + (c.port != null ? c.port : '');
        var def = c.is_default
            ? '<span class="badge-default">默认</span>'
            : '<span class="badge-normal">—</span>';
        var nameEsc = escapeHtml(c.name || '');
        var defaultBtn;
        if (c.is_default) {
            defaultBtn = '<button class="btn btn-sm btn-outline" disabled aria-disabled="true">默认</button>';
        } else {
            defaultBtn = '<button class="btn btn-sm btn-outline" data-action="default" data-id="' + escapeHtml(c.id || '') + '" aria-label="设为连接 ' + nameEsc + ' 为默认">设为默认</button>';
        }
        return '<tr>' +
            '<td>' + nameEsc + '</td>' +
            '<td>' + escapeHtml(addr) + '</td>' +
            '<td>' + escapeHtml(c.protocol || '') + '</td>' +
            '<td>' + def + '</td>' +
            '<td style="white-space:nowrap">' +
                '<button class="btn btn-sm btn-primary" data-action="edit" data-id="' + escapeHtml(c.id || '') + '" aria-label="编辑连接 ' + nameEsc + '">编辑</button> ' +
                defaultBtn + ' ' +
                '<button class="btn btn-sm btn-danger" data-action="delete" data-id="' + escapeHtml(c.id || '') + '" aria-label="删除连接 ' + nameEsc + '">删除</button>' +
            '</td>' +
        '</tr>';
    }).join('');
}

// 事件委托：tbody 上统一监听 click，依据 data-action 派发
// 减少监听器数量（10 行 vs 30 个），同时避免 inline onclick 的 XSS 风险
(function _bindConnTableDelegation() {
    function bind() {
        var tbody = document.getElementById('connTableBody');
        if (!tbody || tbody.__tdBound) return;
        tbody.__tdBound = true;
        tbody.addEventListener('click', function (e) {
            var btn = e.target.closest && e.target.closest('button[data-action]');
            if (!btn) return;
            var action = btn.getAttribute('data-action');
            var id = btn.getAttribute('data-id');
            if (!action || id == null) return;
            // 反查 _connCache 获取真实 id（id 来源唯一为后端，避免任何 HTML 转义歧义）
            var conn = _connCache.find(function (c) { return String(c.id) === String(id); });
            var realId = conn ? conn.id : id;
            if (action === 'edit') openForm(realId);
            else if (action === 'default') setDefault(realId, btn);
            else if (action === 'delete') removeConnection(realId);
        });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
    else bind();
})();

function loadConnections() {
    var tbody = document.getElementById('connTableBody');
    var empty = document.getElementById('connEmpty');
    if (empty) empty.style.display = 'none';
    // skeleton 占位：在 tbody 内渲染骨架行，保留表格结构
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="5" style="padding:18px"><div class="skeleton-list" aria-busy="true" aria-live="polite"><div class="skeleton-card"></div><div class="skeleton-card"></div><div class="skeleton-card"></div></div></td></tr>';
    }
    var f = window.tdFetch || fetch;
    f('/api/connections')
        .then(function (r) { return r.json(); })
        .then(function (list) {
            _connCache = list || [];
            renderList();
        })
        .catch(function () {
            // 失败：在 tbody 内显示错误 + 重试按钮
            if (tbody) {
                var retryId = 'connRetry_' + Date.now();
                tbody.innerHTML = '<tr><td colspan="5"><div class="empty-state empty-state-error" role="alert" style="padding:24px">' +
                    '<div class="icon" aria-hidden="true">⚠️</div>' +
                    '<div class="empty-title">加载失败</div>' +
                    '<button class="btn btn-outline btn-sm" id="' + retryId + '">重试</button>' +
                    '</div></td></tr>';
                setTimeout(function () {
                    var b = document.getElementById(retryId);
                    if (b) b.addEventListener('click', loadConnections);
                }, 0);
            } else if (typeof showToast === 'function') {
                showToast('加载连接失败', 'error');
            }
        });
}

function openForm(id) {
    var modal = document.getElementById('connModal');
    var title = document.getElementById('connModalTitle');
    if (!modal) return;
    var openModal = function () {
        if (window._openModal) _openModal('connModal');
        else modal.classList.add('active');
    };
    if (id) {
        title.textContent = '编辑连接';
        // 优先使用缓存，避免重复请求
        var conn = _connCache.find(function (x) { return x.id === id; });
        if (conn) {
            _fillConnForm(conn);
            openModal();
            return;
        }
        // 缓存未命中（如页面刷新后直接编辑）：回退到 fetch
        var ff = window.tdFetch || fetch;
        ff('/api/connections')
            .then(function (r) { return r.json(); })
            .then(function (list) {
                _connCache = list || [];
                conn = _connCache.find(function (x) { return x.id === id; });
                if (!conn) return;
                _fillConnForm(conn);
                openModal();
            })
            .catch(function () { if (typeof showToast === 'function') showToast('加载连接信息失败', 'error'); });
        return;
    }
    title.textContent = '添加连接';
    _fillConnForm(null);
    openModal();
}

// 填充表单：conn 为 null 时清空
function _fillConnForm(conn) {
    document.getElementById('connId').value = conn ? (conn.id || '') : '';
    document.getElementById('connName').value = conn ? (conn.name || '') : '';
    document.getElementById('connHost').value = conn ? (conn.host || '') : '';
    document.getElementById('connPort').value = conn ? (conn.port != null ? conn.port : '') : '';
    document.getElementById('connProtocol').value = conn ? (conn.protocol || 'tcp') : 'tcp';
    var tokenEl = document.getElementById('connToken');
    tokenEl.value = conn ? (conn.token || '') : '';
    // 重置 Token 显隐状态为隐藏
    tokenEl.type = 'password';
    var tg = document.querySelector('#connModal .pw-toggle');
    if (tg) {
        tg.setAttribute('aria-pressed', 'false');
        tg.textContent = '显示';
        tg.setAttribute('aria-label', '显示 Token');
    }
    document.getElementById('connNote').value = conn ? (conn.note || '') : '';
    // 清除可能的字段错误标记
    if (window.setFieldError) {
        setFieldError(document.getElementById('connName'), '');
        setFieldError(document.getElementById('connHost'), '');
        setFieldError(document.getElementById('connPort'), '');
    }
}

function closeForm() {
    closeModal('connModal');
}

// 密码/Token 显隐切换
function togglePwVisibility(inputId, btn) {
    var input = document.getElementById(inputId);
    if (!input) return;
    var isPw = input.type === 'password';
    input.type = isPw ? 'text' : 'password';
    btn.setAttribute('aria-pressed', isPw ? 'true' : 'false');
    btn.textContent = isPw ? '隐藏' : '显示';
    btn.setAttribute('aria-label', isPw ? '隐藏 Token' : '显示 Token');
}

function submitForm() {
    // 前端字段校验
    var nameEl = document.getElementById('connName');
    var hostEl = document.getElementById('connHost');
    var portEl = document.getElementById('connPort');
    var name = nameEl.value.trim();
    var host = hostEl.value.trim();
    var port = parseInt(portEl.value, 10);
    var firstErr = null;
    if (!name) { if (window.setFieldError) setFieldError(nameEl, '请输入名称'); firstErr = firstErr || nameEl; }
    else if (window.setFieldError) setFieldError(nameEl, '');
    if (!host) { if (window.setFieldError) setFieldError(hostEl, '请输入地址'); firstErr = firstErr || hostEl; }
    else if (window.setFieldError) setFieldError(hostEl, '');
    if (isNaN(port) || port < 1 || port > 65535) { if (window.setFieldError) setFieldError(portEl, '端口 1-65535'); firstErr = firstErr || portEl; }
    else if (window.setFieldError) setFieldError(portEl, '');
    if (firstErr) { firstErr.focus(); return; }

    var payload = {
        id: document.getElementById('connId').value || undefined,
        name: name,
        host: host,
        port: port,
        protocol: document.getElementById('connProtocol').value,
        token: document.getElementById('connToken').value,
        note: document.getElementById('connNote').value,
    };
    var isEdit = !!payload.id;
    var url = isEdit ? '/api/connections/update' : '/api/connections/add';
    var body = isEdit ? {
        id: payload.id, name: payload.name, host: payload.host, port: payload.port,
        protocol: payload.protocol, token: payload.token, note: payload.note,
    } : payload;
    var saveBtn = document.querySelector('#connModal .btn-primary');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中...'; }
    var f = window.tdFetch || fetch;
    f(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (d.success) {
                showToast(isEdit ? '已更新' : '已添加', 'success');
                closeForm();
                loadConnections();
            } else {
                showToast(d.error || '失败', 'error');
            }
        })
        .catch(function (e) {
            showToast((e && e.userMessage) || '请求失败', 'error');
        })
        .finally(function () {
            if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = '保存'; }
        });
}

// 测试连接（后端若无对应 API，仅提示失败，不影响主流程）
function testConnection(evt) {
    var host = document.getElementById('connHost').value.trim();
    var port = parseInt(document.getElementById('connPort').value, 10);
    if (!host || isNaN(port)) { showToast('请先填写地址和端口', 'warning'); return; }
    var btn = (evt && evt.currentTarget) || (typeof event !== 'undefined' && event.target);
    if (btn) { btn.disabled = true; var oldText = btn.textContent; btn.textContent = '测试中...'; }
    try {
        var f = window.tdFetch || fetch;
        f('/api/connections/test', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ host: host, port: port })
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d && d.success) showToast('连接成功', 'success');
                else showToast('连接失败: ' + (d && d.error || '无法连接'), 'error');
            })
            .catch(function () { showToast('请求失败', 'error'); })
            .finally(function () { if (btn) { btn.disabled = false; btn.textContent = oldText; } });
    } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = oldText; }
        showToast('请求失败', 'error');
    }
}

function removeConnection(id) {
    var conn = _connCache.find(function (c) { return c.id === id; });
    var name = conn ? conn.name : id;
    if (window.showConfirm) {
        showConfirm('确定删除连接「' + name + '」吗？此操作不可撤销。', function (ok) {
            if (!ok) return;
            var snapshot = _connCache.slice();
            var idx = _connCache.findIndex(function (c) { return c.id === id; });
            if (idx >= 0) _connCache.splice(idx, 1);
            renderList();
            var f = window.tdFetch || fetch;
            f('/api/connections/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: id }),
            })
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    if (d && d.success) {
                        showToast('已删除「' + name + '」', 'success');
                    } else {
                        if (idx >= 0 && snapshot[idx]) _connCache.splice(idx, 0, snapshot[idx]);
                        renderList();
                        showToast('删除失败: ' + (d && d.error || '未知错误'), 'error');
                    }
                })
                .catch(function () {
                    if (idx >= 0 && snapshot[idx]) _connCache.splice(idx, 0, snapshot[idx]);
                    renderList();
                    showToast('网络请求失败，已恢复', 'error');
                });
        }, true);
    } else {
        // 兼容回退：无自定义确认弹窗时直接删除
        var f = window.tdFetch || fetch;
        f('/api/connections/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id }),
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d && d.success) { showToast('已删除', 'success'); loadConnections(); }
                else { showToast(d.error || '失败', 'error'); }
            })
            .catch(function () { showToast('请求失败', 'error'); });
    }
}

function setDefault(id, btn) {
    if (btn) { btn.disabled = true; var oldText = btn.textContent; btn.textContent = '...'; }
    var f = window.tdFetch || fetch;
    f('/api/connections/default', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id }),
    })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (d.success) {
                showToast('已设为默认', 'success');
                loadConnections();
            } else {
                showToast(d.error || '失败', 'error');
            }
        })
        .catch(function () {
            showToast('请求失败', 'error');
        })
        .finally(function () {
            if (btn) { btn.disabled = false; btn.textContent = oldText; }
        });
}

loadConnections();
