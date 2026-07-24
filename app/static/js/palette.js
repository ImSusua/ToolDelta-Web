// ─── 命令面板 Cmd+K（参考 Linear / Raycast / Vercel cmdk） ──
// 全局快捷键 Cmd/Ctrl+K 唤起，支持模糊搜索导航 + 快速操作。
// 各业务页可调用 TD_PALETTE.register({...}) 注入页面级命令。
window.TD_PALETTE = (function () {
    var registry = [];
    var isOpen = false;
    var selectedIdx = 0;
    var listEl = null, inputEl = null, overlayEl = null;

    // 注册一条命令：{ id, label, group, ico, hint, run, keywords }
    function register(cmd) {
        if (!cmd || !cmd.id || !cmd.label) return;
        // 去重：相同 id 后注册覆盖前注册
        for (var i = 0; i < registry.length; i++) {
            if (registry[i].id === cmd.id) { registry[i] = cmd; return; }
        }
        registry.push(cmd);
    }

    function _navCommands() {
        var navs = [];
        var links = document.querySelectorAll('.sidebar nav a[href]');
        links.forEach(function (a) {
            var href = a.getAttribute('href') || '';
            var txt = (a.textContent || '').trim();
            // 跳过纯空白
            if (!txt) return;
            // 提取图标 span
            var icoEl = a.querySelector('.nav-ico');
            var ico = icoEl ? icoEl.textContent.trim() : '🔗';
            navs.push({
                id: 'nav_' + href,
                label: txt,
                group: '导航',
                ico: ico,
                hint: href,
                keywords: txt + ' ' + href,
                run: function () { if (href) location.href = href; }
            });
        });
        return navs;
    }

    function _recentCommands() {
        var recent = (window.tdPref && tdPref.getRecent()) || [];
        var navs = _navCommands();
        var map = {};
        navs.forEach(function (n) { map[n.hint] = n; });
        return recent.map(function (h) {
            var n = map[h];
            if (!n) return null;
            return {
                id: 'recent_' + n.id,
                label: n.label,
                group: '最近访问',
                ico: n.ico,
                hint: n.hint,
                keywords: n.label + ' ' + n.hint,
                run: n.run
            };
        }).filter(Boolean);
    }

    function _actionCommands() {
        var acts = [];
        // 主题切换
        acts.push({
            id: 'act_toggle_theme', label: '切换主题', group: '动作', ico: '🌗', hint: '深色 / 浅色 / 系统',
            keywords: '主题 theme 切换 dark light system',
            run: function () { if (typeof toggleTheme === 'function') toggleTheme(); }
        });
        // 启动/停止 ToolDelta
        acts.push({
            id: 'act_toggle_tool', label: '启动 / 停止 ToolDelta', group: '动作', ico: '▶', hint: '切换运行状态',
            keywords: '启动 停止 toggle start stop 运行',
            run: function () { if (typeof toggleTool === 'function') toggleTool(); }
        });
        // 打开通知历史
        acts.push({
            id: 'act_open_notify', label: '查看通知历史', group: '动作', ico: '🔔', hint: '',
            keywords: '通知 历史 notify history bell',
            run: function () { if (typeof openNotifyPanel === 'function') openNotifyPanel(); }
        });
        // 刷新当前页
        acts.push({
            id: 'act_refresh', label: '刷新当前页', group: '动作', ico: '🔄', hint: '',
            keywords: '刷新 refresh reload',
            run: function () { location.reload(); }
        });
        // 聚焦页面搜索框
        acts.push({
            id: 'act_focus_search', label: '聚焦页面搜索框', group: '动作', ico: '🔍', hint: '',
            keywords: '搜索 search focus find',
            run: function () {
                var s = document.querySelector('input[type=text][id*=Search], input[type=text][id*=search], #pluginSearch, #searchInput, #presetSearch, #marketSearch, #commandSearch');
                if (s) { s.focus(); s.select(); }
            }
        });
        return acts;
    }

    function _all() {
        var recent = _recentCommands();
        var actions = _actionCommands();
        var navs = _navCommands();
        // 拼装：最近访问（动态）→ 动作 → 导航 → 自定义
        return recent.concat(actions).concat(navs).concat(registry);
    }

    // 模糊子序列匹配：输入 "tgldk" 命中 "ToolDelta"
    function _fuzzy(q, text) {
        q = (q || '').toLowerCase();
        text = (text || '').toLowerCase();
        if (!q) return true;
        var i = 0;
        for (var j = 0; j < text.length; j++) {
            if (text.charAt(j) === q.charAt(i)) i++;
            if (i >= q.length) return true;
        }
        return false;
    }

    // 高亮匹配字符
    function _highlight(text, q) {
        if (!q) return text;
        var esc = window._escHtml || String;
        var lo = text.toLowerCase();
        var ql = q.toLowerCase();
        var out = '';
        var qi = 0;
        for (var i = 0; i < text.length; i++) {
            if (qi < ql.length && lo.charAt(i) === ql.charAt(qi)) {
                out += '<mark>' + esc(text.charAt(i)) + '</mark>';
                qi++;
            } else {
                out += esc(text.charAt(i));
            }
        }
        return out;
    }

    function _filter(q) {
        var all = _all();
        if (!q) return all;
        var matched = [];
        for (var i = 0; i < all.length; i++) {
            var c = all[i];
            var hay = (c.label + ' ' + (c.keywords || '') + ' ' + (c.hint || '')).trim();
            if (_fuzzy(q, hay)) matched.push(c);
        }
        return matched;
    }

    function _group(results) {
        var groups = {};
        var order = [];
        results.forEach(function (c) {
            if (!groups[c.group]) { groups[c.group] = []; order.push(c.group); }
            groups[c.group].push(c);
        });
        return { groups: groups, order: order };
    }

    function _render() {
        if (!listEl) return;
        var q = (inputEl.value || '').trim();
        var results = _filter(q);
        if (results.length === 0) {
            listEl.innerHTML = '<div class="palette-empty">未找到匹配的命令</div>';
            selectedIdx = 0;
            return;
        }
        selectedIdx = Math.min(selectedIdx, results.length - 1);
        if (selectedIdx < 0) selectedIdx = 0;
        var grouped = _group(results);
        var esc = window._escHtml || String;
        var html = '';
        var flatIdx = 0;
        grouped.order.forEach(function (gname) {
            html += '<div class="palette-group-label">' + esc(gname) + '</div>';
            grouped.groups[gname].forEach(function (c) {
                var sel = (flatIdx === selectedIdx) ? 'true' : 'false';
                var label = _highlight(c.label, q);
                var hint = c.hint ? '<span class="palette-hint">' + esc(c.hint) + '</span>' : '';
                html += '<div class="palette-item" role="option" aria-selected="' + sel + '" data-idx="' + flatIdx + '">' +
                    '<span class="palette-ico" aria-hidden="true">' + esc(c.ico || '🔗') + '</span>' +
                    '<span class="palette-label">' + label + '</span>' + hint + '</div>';
                flatIdx++;
            });
        });
        listEl.innerHTML = html;
        // 绑定点击 + hover
        var items = listEl.querySelectorAll('.palette-item');
        items.forEach(function (it) {
            it.addEventListener('mouseenter', function () {
                var idx = parseInt(it.getAttribute('data-idx'), 10);
                _setSelected(idx);
            });
            it.addEventListener('click', function () {
                var idx = parseInt(it.getAttribute('data-idx'), 10);
                _runIdx(results, idx);
            });
        });
        // 滚动选中项到可见区
        _scrollToSelected();
        // 缓存 results 供键盘运行用
        listEl._results = results;
    }

    function _setSelected(idx) {
        var items = listEl.querySelectorAll('.palette-item');
        if (!items.length) return;
        if (idx < 0) idx = 0;
        if (idx >= items.length) idx = items.length - 1;
        items.forEach(function (it, i) { it.setAttribute('aria-selected', i === idx ? 'true' : 'false'); });
        selectedIdx = idx;
        _scrollToSelected();
    }

    function _scrollToSelected() {
        var sel = listEl.querySelector('.palette-item[aria-selected="true"]');
        if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest' });
    }

    function _runIdx(results, idx) {
        if (!results || !results[idx]) return;
        var c = results[idx];
        _close();
        // 延迟一帧执行，避免与 _close 的焦点归还冲突
        setTimeout(function () {
            try { c.run(); } catch (e) { if (typeof showToast === 'function') showToast('执行失败', 'error'); }
        }, 30);
    }

    function _open() {
        if (isOpen) return;
        if (!overlayEl) _ensureDOM();
        if (!overlayEl) return;
        isOpen = true;
        overlayEl.classList.add('active');
        _setInertExcept(overlayEl);
        if (inputEl) { inputEl.value = ''; }
        _render();
        setTimeout(function () { if (inputEl) inputEl.focus(); }, 50);
    }

    function _close() {
        if (!isOpen) return;
        isOpen = false;
        if (overlayEl) overlayEl.classList.remove('active');
        _clearInert();
    }

    function _ensureDOM() {
        overlayEl = document.getElementById('cmdPalette');
        if (!overlayEl) return;
        inputEl = document.getElementById('paletteInput');
        listEl = document.getElementById('paletteList');
        if (inputEl) {
            inputEl.addEventListener('input', function () {
                selectedIdx = 0;
                _render();
            });
            // 阻止输入时 Cmd+K 再次触发关闭
            inputEl.addEventListener('keydown', function (e) { e.stopPropagation(); });
        }
        if (listEl) {
            listEl.addEventListener('mousemove', function () {});
        }
        if (overlayEl) {
            // 点击空白背景关闭
            overlayEl.addEventListener('click', function (e) {
                if (e.target === overlayEl) _close();
            });
        }
    }

    function _onKeydown(e) {
        if (!isOpen) {
            // Cmd/Ctrl+K 打开
            if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K' || e.keyCode === 75)) {
                e.preventDefault();
                _ensureDOM();
                _open();
            }
            return;
        }
        // 打开后：
        if (e.key === 'Escape' || e.keyCode === 27) {
            e.preventDefault(); _close(); return;
        }
        if (e.key === 'ArrowDown' || e.keyCode === 40) {
            e.preventDefault();
            var results = (listEl && listEl._results) || [];
            _setSelected(Math.min(selectedIdx + 1, results.length - 1));
            return;
        }
        if (e.key === 'ArrowUp' || e.keyCode === 38) {
            e.preventDefault();
            _setSelected(Math.max(selectedIdx - 1, 0));
            return;
        }
        if (e.key === 'Enter' || e.keyCode === 13) {
            e.preventDefault();
            var res = (listEl && listEl._results) || [];
            _runIdx(res, selectedIdx);
            return;
        }
    }

    // ── 兼容 main.js 的 _setInertExcept / _clearInert（焦点冻结） ──
    function _setInertExcept(activeModal) {
        if (typeof window._setInertExcept === 'function') {
            window._setInertExcept(activeModal);
            return;
        }
        var siblings = document.body.children;
        for (var i = 0; i < siblings.length; i++) {
            var el = siblings[i];
            if (el === activeModal || el.contains(activeModal)) continue;
            if (el.tagName === 'SCRIPT' || el.tagName === 'LINK' || el.tagName === 'STYLE') continue;
            if (el.hasAttribute && !el.hasAttribute('inert')) {
                try { el.setAttribute('inert', ''); el.setAttribute('data-td-inert', '1'); } catch (e) {}
            }
        }
    }
    function _clearInert() {
        if (typeof window._clearInert === 'function') { window._clearInert(); return; }
        var marked = document.querySelectorAll('[data-td-inert="1"]');
        marked.forEach(function (el) { el.removeAttribute('inert'); el.removeAttribute('data-td-inert'); });
    }

    // 初始化
    document.addEventListener('keydown', _onKeydown);
    // DOM 就绪后挂载触发按钮事件 + 注入 DOM（若模板未挂载）
    function _init() {
        _ensureDOM();
        // 若模板未挂载 palette DOM，按需注入
        if (!document.getElementById('cmdPalette')) {
            var overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.id = 'cmdPalette';
            overlay.setAttribute('role', 'dialog');
            overlay.setAttribute('aria-modal', 'true');
            overlay.setAttribute('aria-label', '命令面板');
            overlay.innerHTML =
                '<div class="palette-box">' +
                '<div class="palette-input-wrap">' +
                '<input type="text" id="paletteInput" class="palette-input" placeholder="输入命令或搜索…" autocomplete="off" spellcheck="false" aria-label="搜索命令">' +
                '</div>' +
                '<ul id="paletteList" class="palette-list" role="listbox"></ul>' +
                '<div class="palette-foot"><kbd>↑↓</kbd>选择 <kbd>↵</kbd>执行 <kbd>esc</kbd>关闭 <kbd>⌘K</kbd>唤起</div>' +
                '</div>';
            document.body.appendChild(overlay);
            overlay.addEventListener('click', function (e) {
                if (e.target === overlay) _close();
            });
            _ensureDOM();
            if (inputEl) {
                inputEl.addEventListener('input', function () { selectedIdx = 0; _render(); });
                inputEl.addEventListener('keydown', function (e) { e.stopPropagation(); });
            }
        }
        // 悬浮触发按钮（仅移动端显示）
        var trigger = document.getElementById('paletteTrigger');
        if (trigger) {
            trigger.addEventListener('click', function () { _open(); });
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _init);
    } else { _init(); }

    return {
        register: register,
        open: function () { _ensureDOM(); _open(); },
        close: _close
    };
})();
