/* 状态仪表盘前端逻辑：统一聚合拉取并填充首页全部统计卡 + 系统信息表。
 * 取消 index.html 内联轮询，全部由本文件 loadDashboardStats() 负责，避免双重轮询。
 * 数据来源：
 *   - /api/dashboard        → 系统资源 + ToolDelta 状态 + 看门狗/连接数/插件数/定时任务数
 *   - /api/system/info      → Python 版本 / 平台 / ToolDelta 目录 / 是否存在
 *   - /api/market/plugins    → 市场插件数
 *   - /api/backups          → 备份数量
 * 用 Promise.allSettled 并发拉取，任一接口失败不影响其他卡片（独立降级）。
 */

function loadDashboardStats() {
    // 优先使用 main.js 的 tdFetch（统一超时与错误归类）；不存在则回退原生 fetch
    var _fetch = function (url) {
        var fn = window.tdFetch || fetch;
        var p = fn(url);
        return p.then(function (r) { return r.json(); });
    };

    Promise.allSettled([
        _fetch('/api/dashboard'),
        _fetch('/api/system/info'),
        _fetch('/api/market/plugins'),
        _fetch('/api/backups')
    ]).then(function (results) {
        var dash = results[0].status === 'fulfilled' ? results[0].value : null;
        var info = results[1].status === 'fulfilled' ? results[1].value : null;
        var market = results[2].status === 'fulfilled' ? results[2].value : null;
        var backups = results[3].status === 'fulfilled' ? results[3].value : null;

        if (dash) {
            renderDashboardData(dash);
        } else {
            renderDashboardError();
        }

        // ── 顶部统计卡：运行状态 / 插件数（来自 /api/dashboard）──
        var running = !!(dash && dash.tooldelta && dash.tooldelta.running);
        updateStatusIcon('statusIcon', running);
        setDashText('statusText', running ? '运行中' : '已停止');
        if (dash && dash.panel && typeof dash.panel.plugins_count === 'number') {
            setDashText('pluginCount', dash.panel.plugins_count);
        } else if (!dash) {
            setDashText('pluginCount', '—');
        }

        // ── 系统信息表（来自 /api/system/info）──
        if (info) {
            setDashText('infoPython', info.python_version || '-');
            setDashText('infoPlatform', info.platform || '-');
            setDashText('infoDir', info.tooldelta_dir || '-');
            renderInfoExists('infoExists', info.tooldelta_exists);
        }

        // ── 市场插件数（来自 /api/market/plugins）──
        if (market && Array.isArray(market)) {
            setDashText('marketCount', market.length);
        }

        // ── 备份数量（来自 /api/backups）──
        if (backups && Array.isArray(backups)) {
            setDashText('backupCount', backups.length);
        }

        // 首次加载完成：清除 mainToggleBtn 的 aria-busy
        clearMainToggleBusy();
    }).catch(function (e) {
        // allSettled 不会 reject，此处仅为兜底（处理 then 内部异常）
        console.error('loadDashboardStats error', e);
        renderDashboardError();
        clearMainToggleBusy();
    });
}

/* 渲染 /api/dashboard 数据到 #dashStats 卡片区 */
function renderDashboardData(d) {
    var sys = (d && d.system) || {};
    var td = (d && d.tooldelta) || {};
    var panel = (d && d.panel) || {};

    // CPU 使用率（count-up 动画）
    if (typeof sys.cpu_percent === 'number') {
        animateCount('dashCpu', sys.cpu_percent, '%');
    } else {
        setDashText('dashCpu', fmtPercent(sys.cpu_percent));
    }

    // 内存使用（已用 / 总量，含百分比）
    setDashText(
        'dashMem',
        fmtMB(sys.mem_used_mb) + ' / ' + fmtMB(sys.mem_total_mb) +
        ' (' + fmtPercent(sys.mem_percent) + ')'
    );

    // 磁盘剩余（剩余空间，含已用百分比）
    setDashText(
        'dashDisk',
        fmtGB(sys.disk_free_gb) + ' 剩余 (' + fmtPercent(sys.disk_percent) + ')'
    );

    // ToolDelta 运行状态 + 状态点颜色
    var running = !!(td && td.running);
    setDashText('dashToolStatus', running ? '运行中' : '已停止');
    updateStatusIcon('dashToolIcon', running);

    // 看门狗开关
    setDashText('dashWatchdog', panel.watchdog_enabled ? '开' : '关');

    // 服务器连接数（count-up 动画）
    if (typeof panel.connections_count === 'number') {
        animateCount('dashConnections', panel.connections_count);
    } else {
        setDashText('dashConnections', panel.connections_count != null ? panel.connections_count : 0);
    }

    // 定时任务数（count-up 动画）
    if (typeof panel.scheduler_jobs_count === 'number') {
        animateCount('dashSchedJobs', panel.scheduler_jobs_count);
    } else {
        setDashText('dashSchedJobs', panel.scheduler_jobs_count != null ? panel.scheduler_jobs_count : 0);
    }
}

/* 加载失败：状态卡显示「加载失败 [重试]」+ 错误 toast（带重试 action） */
function renderDashboardError() {
    var el = document.getElementById('statusText');
    if (el) {
        el.classList.remove('loading-text');
        el.innerHTML = '加载失败 <button type="button" class="dash-retry-btn" ' +
            'style="margin-left:8px;padding:2px 10px;font-size:12px;border:1px solid var(--hairline);' +
            'background:var(--surface-2);color:var(--ink);border-radius:var(--r-sm);cursor:pointer">重试</button>';
        var btn = el.querySelector('.dash-retry-btn');
        if (btn) btn.addEventListener('click', function () { loadDashboardStats(); });
    }
    if (typeof showToast === 'function') {
        showToast('加载仪表盘数据失败', 'error', { action: { label: '重试', fn: loadDashboardStats } });
    }
}

/* 状态点：● + 成功/危险色 + aria-label（替代旧的 ON/OFF 文本） */
function updateStatusIcon(id, running) {
    var dot = document.getElementById(id);
    if (!dot) return;
    dot.textContent = '●';
    dot.style.color = running ? 'var(--success)' : 'var(--danger)';
    dot.setAttribute('aria-label', running ? '运行中' : '已停止');
}

/* ToolDelta 是否存在：用 tag 徽标替代纯文本 */
function renderInfoExists(id, exists) {
    var el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = exists
        ? '<span class="tag tag-enabled">存在</span>'
        : '<span class="tag tag-disabled">不存在</span>';
}

/* 首次加载完成后清除 mainToggleBtn 的 aria-busy */
function clearMainToggleBusy() {
    var btn = document.getElementById('mainToggleBtn');
    if (btn) btn.setAttribute('aria-busy', 'false');
}

/* ─── 辅助函数 ───────────────────────────────── */

function setDashText(id, value) {
    var el = document.getElementById(id);
    if (el && value !== undefined && value !== null) {
        el.textContent = String(value);
    }
}

// 数字 count-up 动画：从当前值过渡到目标值（300ms，requestAnimationFrame 驱动）
var _countTimers = {};
function animateCount(id, target, suffix) {
    var el = document.getElementById(id);
    if (!el) return;
    suffix = suffix || '';
    // 解析当前显示值（支持 "75.0%" / "1.2 GB / 8.0 GB (15.0%)" 这种复合文本时跳过动画）
    var prevValue = parseFloat(el.textContent) || 0;
    if (isNaN(target) || prevValue === target) { el.textContent = target + suffix; return; }
    // 清理旧动画句柄（避免元素被复用时多个动画叠加）
    if (_countTimers[id]) { cancelAnimationFrame(_countTimers[id]); delete _countTimers[id]; }
    var startTime = null, dur = 300;
    function step(ts) {
        // 元素可能已被移除（SPA 场景 / 页面切换），动画句柄需自清理避免泄漏
        if (!document.getElementById(id)) { delete _countTimers[id]; return; }
        if (startTime === null) startTime = ts;
        var p = Math.min(1, (ts - startTime) / dur);
        var eased = 1 - Math.pow(1 - p, 3); // easeOutCubic
        var v = prevValue + (target - prevValue) * eased;
        // 整数 vs 小数
        el.textContent = (Number.isInteger(target) ? Math.round(v) : v.toFixed(1)) + suffix;
        if (p < 1) {
            _countTimers[id] = requestAnimationFrame(step);
        } else {
            el.textContent = target + suffix;
            delete _countTimers[id];
        }
    }
    _countTimers[id] = requestAnimationFrame(step);
}

function fmtPercent(v) {
    if (v === undefined || v === null || isNaN(v)) return '0.0%';
    return Number(v).toFixed(1) + '%';
}

function fmtMB(mb) {
    if (mb === undefined || mb === null || isNaN(mb)) return '0 MB';
    if (mb >= 1024) return (mb / 1024).toFixed(1) + ' GB';
    return mb + ' MB';
}

function fmtGB(gb) {
    if (gb === undefined || gb === null || isNaN(gb)) return '0.0 GB';
    return Number(gb).toFixed(2) + ' GB';
}

// 首次调用 + 每 5 秒轮询（统一走 TDPoll：页面隐藏/离线时自动暂停）
// 不再用裸 setInterval 作 fallback，避免 main.js 加载延迟时双重轮询。
loadDashboardStats();
if (window.TDPoll) { window.TDPoll.register(loadDashboardStats, 5000); }
