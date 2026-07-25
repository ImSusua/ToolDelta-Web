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
            // 数据已渲染：清除骨架屏 aria-busy
            var ds = document.getElementById('dashStats');
            if (ds) ds.setAttribute('aria-busy', 'false');
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

    // 推送性能采样点到趋势图
    pushPerfMetric(sys);
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
        var prev = el.textContent;
        var next = String(value);
        el.textContent = next;
        // 清除 loading-text 占位样式（shimmer 动画停止）
        el.classList.remove('loading-text');
        // 数值变化时短暂高亮（仅在已有真实值且新值不同时触发，避免首次加载闪烁）
        if (prev && prev !== next && prev !== '—' && prev !== '检测中...' && prev !== '加载失败') {
            triggerStatPulse(el);
        }
    }
}

/* stat-value 数值变化脉冲：短暂变绿后回退，用于感知数据更新（参考 Linear/Vercel 数据刷新反馈） */
function triggerStatPulse(el) {
    if (!el) return;
    el.classList.remove('is-pulse');
    // 强制 reflow 让动画重新触发
    void el.offsetWidth;
    el.classList.add('is-pulse');
    setTimeout(function () { el.classList.remove('is-pulse'); }, 650);
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
            // 数值变化完成时触发脉冲（与 setDashText 一致的更新反馈）
            triggerStatPulse(el);
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

/* ─── 性能趋势图(纯 SVG 折线,无依赖) ─────────────────────
   保留最近 MAX_POINTS 个采样点(5s 一次 × 60 = 5 分钟趋势),
   每次轮询后 push 一个点并重绘。
   - viewBox 600×160,固定坐标系,y=0 是顶部(100%),y=160 是底部(0%)
   - 用 SVG path 平滑折线,区域填充用渐变
   - 单点也兼容(避免首次只有 1 点时画不出来的问题) */
var _perfHistory = [];
var _perfMaxPoints = 60;  // 5 分钟历史
var _perfChartW = 600, _perfChartH = 160;

function pushPerfMetric(sys) {
    var cpu = (typeof sys.cpu_percent === 'number') ? Math.max(0, Math.min(100, sys.cpu_percent)) : 0;
    var mem = (typeof sys.mem_percent === 'number') ? Math.max(0, Math.min(100, sys.mem_percent)) : 0;
    _perfHistory.push({ cpu: cpu, mem: mem });
    if (_perfHistory.length > _perfMaxPoints) _perfHistory.shift();
    renderPerfChart();
    // 同步更新 stat-card 内嵌迷你 sparkline（保留最近 6 个采样点）
    pushSparkline(sys);
}

function renderPerfChart() {
    var n = _perfHistory.length;
    var cpuLine = document.getElementById('cpuLine');
    var memLine = document.getElementById('memLine');
    var cpuArea = document.getElementById('cpuArea');
    var memArea = document.getElementById('memArea');
    var empty = document.getElementById('perfEmpty');
    var sub = document.getElementById('perfChartSub');
    if (!cpuLine) return;
    if (n === 0) {
        if (empty) empty.style.display = 'block';
        return;
    }
    if (empty) empty.style.display = 'none';
    if (sub) sub.textContent = '最近 ' + (n * 5) + ' 秒';

    // x 轴:点均匀分布;只有 1 个点时画成水平短横线
    var stepX = (n > 1) ? (_perfChartW / (n - 1)) : 0;
    function toPath(points, valueKey, closeArea) {
        if (n === 1) {
            // 单点:画一条贯穿的水平线
            var y = _perfChartH - (points[0][valueKey] / 100) * _perfChartH;
            var lineD = 'M 0 ' + y + ' L ' + _perfChartW + ' ' + y;
            if (closeArea) return lineD + ' L ' + _perfChartW + ' ' + _perfChartH + ' L 0 ' + _perfChartH + ' Z';
            return lineD;
        }
        var d = 'M 0 ' + (_perfChartH - (points[0][valueKey] / 100) * _perfChartH);
        for (var i = 1; i < n; i++) {
            var x = i * stepX;
            var y = _perfChartH - (points[i][valueKey] / 100) * _perfChartH;
            d += ' L ' + x.toFixed(1) + ' ' + y.toFixed(1);
        }
        if (closeArea) {
            d += ' L ' + _perfChartW + ' ' + _perfChartH + ' L 0 ' + _perfChartH + ' Z';
        }
        return d;
    }
    cpuLine.setAttribute('d', toPath(_perfHistory, 'cpu', false));
    memLine.setAttribute('d', toPath(_perfHistory, 'mem', false));
    cpuArea.setAttribute('d', toPath(_perfHistory, 'cpu', true));
    memArea.setAttribute('d', toPath(_perfHistory, 'mem', true));

    // 最新点高亮
    var lastIdx = n - 1;
    var lastX = (n > 1) ? (lastIdx * stepX) : _perfChartW;
    var cpuDot = document.getElementById('cpuDot');
    var memDot = document.getElementById('memDot');
    if (cpuDot) {
        cpuDot.setAttribute('cx', lastX.toFixed(1));
        cpuDot.setAttribute('cy', (_perfChartH - (_perfHistory[lastIdx].cpu / 100) * _perfChartH).toFixed(1));
    }
    if (memDot) {
        memDot.setAttribute('cx', lastX.toFixed(1));
        memDot.setAttribute('cy', (_perfChartH - (_perfHistory[lastIdx].mem / 100) * _perfChartH).toFixed(1));
    }
}

/* ─── stat-card 迷你 sparkline（最近 6 个采样点的迷你折线，无依赖）─── */
var _sparkHistory = { cpu: [], mem: [], disk: [] };
var _sparkMaxPoints = 6;
var _sparkW = 80, _sparkH = 24;

function pushSparkline(sys) {
    var cpu = (typeof sys.cpu_percent === 'number') ? Math.max(0, Math.min(100, sys.cpu_percent)) : 0;
    var mem = (typeof sys.mem_percent === 'number') ? Math.max(0, Math.min(100, sys.mem_percent)) : 0;
    var disk = (typeof sys.disk_percent === 'number') ? Math.max(0, Math.min(100, sys.disk_percent)) : 0;
    _sparkHistory.cpu.push(cpu);
    _sparkHistory.mem.push(mem);
    _sparkHistory.disk.push(disk);
    if (_sparkHistory.cpu.length > _sparkMaxPoints) _sparkHistory.cpu.shift();
    if (_sparkHistory.mem.length > _sparkMaxPoints) _sparkHistory.mem.shift();
    if (_sparkHistory.disk.length > _sparkMaxPoints) _sparkHistory.disk.shift();
    renderSparkline('sparkCpu', _sparkHistory.cpu);
    renderSparkline('sparkMem', _sparkHistory.mem);
    renderSparkline('sparkDisk', _sparkHistory.disk);
}

function renderSparkline(svgId, points) {
    var svg = document.getElementById(svgId);
    if (!svg) return;
    var polyline = svg.querySelector('.spark-line');
    var dot = svg.querySelector('.spark-dot');
    if (!polyline) return;
    var n = points.length;
    if (n === 0) {
        polyline.setAttribute('points', '');
        if (dot) dot.setAttribute('cx', -1);
        return;
    }
    // 上下留 2px padding，避免折线贴边被裁切
    var pad = 2;
    var usableH = _sparkH - pad * 2;
    var stepX = (n > 1) ? (_sparkW / (n - 1)) : 0;
    var pts = [];
    for (var i = 0; i < n; i++) {
        var x = (n > 1) ? (i * stepX) : (_sparkW / 2);
        var y = pad + (1 - points[i] / 100) * usableH;
        pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    polyline.setAttribute('points', pts.join(' '));
    if (dot) {
        var lastX = (n > 1) ? ((n - 1) * stepX) : (_sparkW / 2);
        var lastY = pad + (1 - points[n - 1] / 100) * usableH;
        dot.setAttribute('cx', lastX.toFixed(1));
        dot.setAttribute('cy', lastY.toFixed(1));
    }
}

/* ─── perf-chart hover tooltip（mousemove 计算最近数据点并显示）─── */
function initPerfChartHover() {
    var chart = document.getElementById('perfChart');
    var wrap = chart ? chart.parentElement : null;
    var hoverLine = document.getElementById('perfHoverLine');
    var tooltip = document.getElementById('perfTooltip');
    if (!chart || !wrap || !hoverLine || !tooltip) return;

    function hideTooltip() {
        hoverLine.style.opacity = '0';
        tooltip.classList.remove('is-visible');
    }

    function handleMove(e) {
        var n = _perfHistory.length;
        if (n === 0) { hideTooltip(); return; }
        var rect = chart.getBoundingClientRect();
        if (rect.width === 0) return;
        var x = e.clientX - rect.left;
        if (x < 0 || x > rect.width) { hideTooltip(); return; }
        // 像素 x → viewBox x（图表 viewBox 600 宽）
        var vbX = (x / rect.width) * _perfChartW;
        // 计算最近的数据点索引
        var idx;
        if (n === 1) {
            idx = 0;
        } else {
            var stepX = _perfChartW / (n - 1);
            idx = Math.round(vbX / stepX);
            if (idx < 0) idx = 0;
            if (idx > n - 1) idx = n - 1;
        }
        // 更新垂直 hover 指示线（viewBox 坐标）
        var lineX = (n === 1) ? _perfChartW : idx * (_perfChartW / (n - 1));
        hoverLine.setAttribute('x1', lineX.toFixed(1));
        hoverLine.setAttribute('x2', lineX.toFixed(1));
        hoverLine.setAttribute('y1', 0);
        hoverLine.setAttribute('y2', _perfChartH);
        hoverLine.style.opacity = '1';
        // 更新 tooltip 内容 + 位置（像素坐标，相对 wrap）
        var px = (lineX / _perfChartW) * rect.width;
        var point = _perfHistory[idx];
        var secsAgo = (n - 1 - idx) * 5;
        tooltip.innerHTML =
            '<div class="pt-time">T-' + secsAgo + 's</div>' +
            '<div class="pt-row"><span class="pt-dot pt-cpu"></span>CPU ' + point.cpu.toFixed(1) + '%</div>' +
            '<div class="pt-row"><span class="pt-dot pt-mem"></span>内存 ' + point.mem.toFixed(1) + '%</div>';
        tooltip.style.left = px + 'px';
        tooltip.style.top = '8px';
        tooltip.classList.add('is-visible');
        // 边缘钳制：避免 tooltip 超出 wrap 边界（wrap 有 overflow:hidden）
        var tw = tooltip.offsetWidth;
        if (tw > 0) {
            var halfW = tw / 2;
            if (px - halfW < 0) px = halfW;
            if (px + halfW > rect.width) px = rect.width - halfW;
            tooltip.style.left = px + 'px';
        }
    }

    chart.addEventListener('mousemove', handleMove);
    chart.addEventListener('mouseleave', hideTooltip);
}

// 脚本以 defer 加载，DOM 已解析完成，可直接初始化 hover
initPerfChartHover();
