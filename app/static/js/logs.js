// 日志增强前端：分级 / 搜索 / 过滤 / 导出 / 按来源筛选
// showToast 由 main.js 全局提供。

var allLogLines = [];

// 兼容 tdFetch（main.js 注入的统一 fetch 封装，带 token/重试），缺省回退原生 fetch
var tdFetch = window.tdFetch || fetch;

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
    tdFetch("/api/logs/sources" + params)
        .then(function (r) { return r.json(); })
        .then(function (sources) {
            var sel = document.getElementById("logSource");
            if (!sel) return;
            var current = sel.value;
            // 一次构建字符串再赋值，避免 innerHTML += 触发多次重解析
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
    // 清屏后 30s 内阻止 loadLogs（含 TDPoll 自动轮询）恢复，避免清空被立即覆盖
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

    tdFetch("/api/logs/query?" + params.toString())
        .then(function (r) { return r.json(); })
        .then(function (d) {
            allLogLines = d.lines || [];
            // 重置清屏门控：成功拉取后允许后续轮询正常工作
            _clearedAt = 0;
            var countEl = document.getElementById("logCount");
            if (countEl) countEl.textContent = allLogLines.length + " 行";
            filterLogs();
        })
        .catch(function (e) {
            if (typeof showToast === "function") showToast("加载日志失败: " + e, "error");
        });
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
    body.innerHTML = lines.map(function (l) {
        var cls = "line-output";
        if (l.level === "ERROR") cls = "line-err";
        else if (l.level === "WARN") cls = "line-warn";
        else if (l.level === "INFO") cls = "line-system";
        var text = "[" + l.time + "][" + l.level + "][" + l.source + "] " + l.message;
        // 同时挂 log-line 基类（提供 padding/字体/hover）和分级配色类
        return '<div class="log-line ' + cls + '">' + escapeHtml(text) + "</div>";
    }).join("");
    body.scrollTop = body.scrollHeight;
    // 过滤后同步计数：显示「过滤后 / 总数 行」
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
    tdFetch("/api/logs/files")
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

// 初始化
loadDates();
loadSources();
loadLogs();
if (window.TDPoll) { window.TDPoll.register(loadLogs, 5000); }
else { setInterval(loadLogs, 5000); }
