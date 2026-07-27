// 日志增强前端：分级 / 搜索 / 过滤 / 导出 / 按来源筛选
// showToast 由 main.js 全局提供。

var allLogLines = [];
var _showLogLineNumbers = false;

// 惰性获取 tdFetch（main.js 可能尚未执行，加载时捕获为 undefined）
function _tdFetch(url, opts) {
    return (window.tdFetch || fetch)(url, opts);
}

// filterLogs 防抖定时器（避免 oninput 每字符全量重建 DOM）
var _filterTimer = null;
function debounceFilter() {
    clearTimeout(_filterTimer);
    _filterTimer = setTimeout(filterLogs, 200);
}

// 清屏时间戳：30s 内阻止 TDPoll 自动 loadLogs 把清空的日志拉回来
var _clearedAt = 0;

// 纯字符串 escapeHtml（参考 main.js 的 _escHtml，避免 document.createElement 开销）
function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (m) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m];
    });
}

// 清屏前确认（优先使用 main.js 的 showConfirm 弹窗，缺省直接清屏）
function confirmClear() {
    if (typeof window.showConfirm === 'function') {
        window.showConfirm('确认清空当前显示的日志？', function (ok) { if (ok) clearDisplay(); });
    } else {
        clearDisplay();
    }
}

// 文件大小格式化（支持 B/KB/MB/GB）
function fmtSize(b) {
    b = Number(b) || 0;
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(2) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
}

// 填充来源下拉框（含「全部来源」）
function loadSources() {
    var dateSel = document.getElementById("logDate");
    var date = dateSel ? dateSel.value : "today";
    var params = (date && date !== "today") ? ("?date=" + encodeURIComponent(date)) : "";
    _tdFetch("/api/logs/sources" + params)
        .then(function (r) { return r.json(); })
        .then(function (sources) {
            var sel = document.getElementById("logSource");
            if (!sel) return;
            var current = sel.value;
            var html = '<option value="">全部来源</option>';
            (sources || []).forEach(function (s) {
                html += '<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + "</option>";
            });
            sel.innerHTML = html;
            sel.value = current;
        })
        .catch(function (e) {
            if (typeof showToast === "function") showToast("加载来源失败: " + e, "error");
        });
}

// 加载日志（调用增强 API，按级别/来源/关键字/日期过滤）
function loadLogs() {
    if (_clearedAt && Date.now() - _clearedAt < 30000) return;
    var dateSel = document.getElementById("logDate");
    var date = dateSel ? dateSel.value : "today";
    if (date === "today") date = "";

    var levelEl = document.getElementById("logLevel");
    var sourceEl = document.getElementById("logSource");
    var keywordEl = document.getElementById("logFilter");

    var level = levelEl ? levelEl.value : "";
    var source = sourceEl ? sourceEl.value : "";
    var keyword = keywordEl ? keywordEl.value.trim() : "";

    var params = new URLSearchParams();
    if (date) params.set("date", date);
    if (level) params.set("level", level);
    if (source) params.set("source", source);
    if (keyword) params.set("keyword", keyword);
    params.set("limit", "500");

    _tdFetch("/api/logs/query?" + params.toString())
        .then(function (r) { return r.json(); })
        .then(function (d) {
            allLogLines = d.lines || [];
            _clearedAt = 0;
            var countEl = document.getElementById("logCount");
            if (countEl) countEl.textContent = allLogLines.length + " 行";
            filterLogs();
        })
        .catch(function (e) {
            if (typeof showToast === "function") showToast("加载日志失败: " + e, "error");
        });
}

// ─── 搜索高亮：用 mark 标签包裹匹配文本 ───
function _highlightLogText(node, query) {
    if (!query || !node) return;
    if (node.nodeType === Node.TEXT_NODE) {
        var text = node.textContent;
        var lowerText = text.toLowerCase();
        var idx = lowerText.indexOf(query);
        if (idx === -1) return;
        var frag = document.createDocumentFragment();
        var lastIdx = 0;
        while (idx !== -1) {
            if (idx > lastIdx) {
                frag.appendChild(document.createTextNode(text.slice(lastIdx, idx)));
            }
            var mark = document.createElement('mark');
            mark.textContent = text.slice(idx, idx + query.length);
            frag.appendChild(mark);
            lastIdx = idx + query.length;
            idx = lowerText.indexOf(query, lastIdx);
        }
        if (lastIdx < text.length) {
            frag.appendChild(document.createTextNode(text.slice(lastIdx)));
        }
        node.parentNode.replaceChild(frag, node);
    } else if (node.nodeType === Node.ELEMENT_NODE) {
        if (node.tagName === 'MARK' || node.classList.contains('log-ln')) return;
        var children = Array.prototype.slice.call(node.childNodes);
        for (var i = 0; i < children.length; i++) {
            _highlightLogText(children[i], query);
        }
    }
}

function _clearLogHighlights(container) {
    if (!container) return;
    var marks = container.querySelectorAll('mark');
    for (var i = marks.length - 1; i >= 0; i--) {
        var mark = marks[i];
        var parent = mark.parentNode;
        while (mark.firstChild) {
            parent.insertBefore(mark.firstChild, mark);
        }
        parent.removeChild(mark);
    }
    container.normalize();
}

// ─── 复制单行日志 ───
function _copyLogLine(lineEl) {
    if (!lineEl) return;
    var clone = lineEl.cloneNode(true);
    var ln = clone.querySelector('.log-ln');
    if (ln) ln.remove();
    var text = clone.textContent || '';
    _copyToClipboard(text.trim(), '已复制该行');
}

function _copyToClipboard(text, successMsg) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
            if (typeof showToast === 'function' && successMsg) showToast(successMsg, 'success');
        }).catch(function () {
            _fallbackCopy(text, successMsg);
        });
    } else {
        _fallbackCopy(text, successMsg);
    }
}

function _fallbackCopy(text, successMsg) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
        document.execCommand('copy');
        if (typeof showToast === 'function' && successMsg) showToast(successMsg, 'success');
    } catch (e) {
        if (typeof showToast === 'function') showToast('复制失败', 'error');
    }
    document.body.removeChild(ta);
}

// 行号切换
function toggleLogLineNumbers() {
    _showLogLineNumbers = !_showLogLineNumbers;
    var btn = document.getElementById('logLineNumBtn');
    if (btn) {
        btn.setAttribute('aria-pressed', _showLogLineNumbers ? 'true' : 'false');
    }
    var body = document.getElementById('logBody');
    if (body) body.classList.toggle('show-log-lines', _showLogLineNumbers);
    filterLogs();
}

// 客户端渲染：按级别配色，并叠加关键字高亮过滤
function filterLogs() {
    var keywordEl = document.getElementById("logFilter");
    var q = keywordEl ? keywordEl.value.trim().toLowerCase() : "";

    var lines = allLogLines.filter(function (l) {
        if (!q) return true;
        var hay = ((l.message || "") + " " + (l.source || "") + " " + (l.level || "")).toLowerCase();
        return hay.indexOf(q) !== -1;
    });

    var body = document.getElementById("logBody");
    if (!body) return;
    
    _clearLogHighlights(body);
    
    var html = lines.map(function (l, idx) {
        var cls = "line-output";
        if (l.level === "ERROR") cls = "line-err";
        else if (l.level === "WARN") cls = "line-warn";
        else if (l.level === "INFO") cls = "line-system";
        else if (l.level === "DEBUG") cls = "line-debug";
        var text = "[" + l.time + "][" + l.level + "][" + l.source + "] " + l.message;
        var lnHtml = _showLogLineNumbers ? ('<span class="log-ln" data-idx="' + idx + '">' + (idx + 1) + '</span>') : '';
        return '<div class="log-line ' + cls + '">' + lnHtml + escapeHtml(text) + "</div>";
    }).join("");
    body.innerHTML = html;
    
    // 添加搜索高亮
    if (q) {
        var lineEls = body.querySelectorAll('.log-line');
        for (var i = 0; i < lineEls.length; i++) {
            _highlightLogText(lineEls[i], q);
        }
    }
    
    // 绑定行号点击事件
    if (_showLogLineNumbers) {
        var lnEls = body.querySelectorAll('.log-ln');
        for (var j = 0; j < lnEls.length; j++) {
            (function (lnEl) {
                lnEl.addEventListener('click', function (e) {
                    e.stopPropagation();
                    var line = lnEl.parentNode;
                    _copyLogLine(line);
                });
            })(lnEls[j]);
        }
    }
    
    body.scrollTop = body.scrollHeight;
    var countEl = document.getElementById("logCount");
    if (countEl) countEl.textContent = lines.length + " / " + allLogLines.length + " 行";
}

// 刷新：重新拉取日期列表、来源列表与日志
function refreshLogs() {
    loadDates();
    loadSources();
    loadLogs();
}

// 清屏（保留现有功能）。记录清屏时间，阻止后续轮询自动恢复
function clearDisplay() {
    var body = document.getElementById("logBody");
    if (body) body.innerHTML = "";
    allLogLines = [];
    _clearedAt = Date.now();
    var countEl = document.getElementById("logCount");
    if (countEl) countEl.textContent = "0 行";
}

// 导出：基于当前过滤条件下载 logs_export.txt
function doExport() {
    var dateSel = document.getElementById("logDate");
    var date = dateSel ? dateSel.value : "today";
    if (date === "today") date = "";

    var levelEl = document.getElementById("logLevel");
    var sourceEl = document.getElementById("logSource");
    var keywordEl = document.getElementById("logFilter");

    var level = levelEl ? levelEl.value : "";
    var source = sourceEl ? sourceEl.value : "";
    var keyword = keywordEl ? keywordEl.value.trim() : "";

    var params = new URLSearchParams();
    if (date) params.set("date", date);
    if (level) params.set("level", level);
    if (source) params.set("source", source);
    if (keyword) params.set("keyword", keyword);

    var url = "/api/logs/export?" + params.toString();
    window.open(url, "_blank");
    if (typeof showToast === "function") showToast("已导出日志", "success");
}

// 加载可用日期列表（保留现有功能，调用 /api/logs/files）
function loadDates() {
    _tdFetch("/api/logs/files")
        .then(function (r) { return r.json(); })
        .then(function (files) {
            var sel = document.getElementById("logDate");
            if (!sel) return;
            var current = sel.value;
            var html = '<option value="today">今天</option>';
            (files || []).forEach(function (f) {
                html += '<option value="' + escapeHtml(f.date) + '">' + escapeHtml(f.date) + " (" +
                    fmtSize(f.size) + ")</option>";
            });
            sel.innerHTML = html;
            sel.value = current;
        })
        .catch(function () { /* 网络抖动忽略，下次刷新重试 */ });
}

// 初始化（延迟到 DOMContentLoaded 后，确保 main.js 的 TDPoll 已就绪）
document.addEventListener('DOMContentLoaded', function() {
    loadDates();
    loadSources();
    loadLogs();
    if (window.TDPoll) { window.TDPoll.register(loadLogs, 5000); }
    else { setInterval(loadLogs, 5000); }
});
