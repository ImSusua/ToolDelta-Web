function loadDashboardStats() {
    var _fetch = function (url) {
        var fn = window.tdFetch || fetch;
        var p = fn(url);
        return p.then(function (r) { return r.json(); });
    };

    setDashStatsState('loading');

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

        var allFailed = !dash && !info && !market && !backups;
        if (allFailed) {
            setDashStatsState('error', { retry: loadDashboardStats });
            renderDashboardError();
            clearMainToggleBusy();
            return;
        }

        setDashStatsState('content');

        if (dash) {
            renderDashboardData(dash);
            var ds = document.getElementById('dashStats');
            if (ds) ds.setAttribute('aria-busy', 'false');
        } else {
            setDashStatsState('error', { retry: loadDashboardStats });
        }

        var running = !!(dash && dash.tooldelta && dash.tooldelta.running);
        updateStatusIcon('statusIcon', running);
        setDashText('statusText', running ? '运行中' : '已停止');
        if (dash && dash.panel && typeof dash.panel.plugins_count === 'number') {
            setDashText('pluginCount', dash.panel.plugins_count);
        } else if (!dash) {
            setDashText('pluginCount', '--');
        }

        if (info) {
            setDashText('infoPython', info.python_version || '--');
            setDashText('infoPlatform', info.platform || '--');
            setDashText('infoDir', info.tooldelta_dir || '--');
            renderInfoExists('infoExists', info.tooldelta_exists);
        }

        if (market && Array.isArray(market)) {
            setDashText('marketCount', market.length);
        } else {
            setDashText('marketCount', '--');
        }

        if (backups && Array.isArray(backups)) {
            setDashText('backupCount', backups.length);
        } else {
            setDashText('backupCount', '--');
        }

        clearMainToggleBusy();
    }).catch(function (e) {
        console.error('loadDashboardStats error', e);
        setDashStatsState('error', { retry: loadDashboardStats });
        renderDashboardError();
        clearMainToggleBusy();
    });
}

function setDashStatsState(state, opts) {
    var container = document.getElementById('dashStats');
    if (!container) return;
    opts = opts || {};

    if (state === 'loading') {
        container.setAttribute('aria-busy', 'true');
        return;
    }
    if (state === 'error') {
        container.setAttribute('aria-busy', 'false');
        return;
    }
    if (state === 'content') {
        container.setAttribute('aria-busy', 'false');
        return;
    }
}

function renderDashboardData(d) {
    var sys = (d && d.system) || {};
    var td = (d && d.tooldelta) || {};
    var panel = (d && d.panel) || {};

    if (isValidNumber(sys.cpu_percent)) {
        animateCount('dashCpu', sys.cpu_percent, '%');
    } else {
        setDashText('dashCpu', '--');
    }

    var memStr = fmtMB(sys.mem_used_mb) + ' / ' + fmtMB(sys.mem_total_mb) +
        ' (' + fmtPercent(sys.mem_percent) + ')';
    setDashText('dashMem', memStr);

    var diskStr = fmtGB(sys.disk_free_gb) + ' 剩余 (' + fmtPercent(sys.disk_percent) + ' 已用)';
    setDashText('dashDisk', diskStr);

    var running = !!(td && td.running);
    setDashText('dashToolStatus', running ? '运行中' : '已停止');
    updateStatusIcon('dashToolIcon', running);

    setDashText('dashWatchdog', panel.watchdog_enabled ? '开' : '关');

    if (isValidNumber(panel.connections_count)) {
        animateCount('dashConnections', panel.connections_count);
    } else {
        setDashText('dashConnections', '--');
    }

    if (isValidNumber(panel.scheduler_jobs_count)) {
        animateCount('dashSchedJobs', panel.scheduler_jobs_count);
    } else {
        setDashText('dashSchedJobs', '--');
    }

    pushPerfMetric(sys);
}

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

function updateStatusIcon(id, state) {
    var dot = document.getElementById(id);
    if (!dot) return;

    dot.classList.remove('status-running', 'status-stopped', 'status-starting', 'status-stopping');

    if (state === 'starting') {
        dot.textContent = '◌';
        dot.style.color = 'var(--warning)';
        dot.classList.add('status-starting');
        dot.setAttribute('aria-label', '启动中');
    } else if (state === 'stopping') {
        dot.textContent = '◌';
        dot.style.color = 'var(--warning)';
        dot.classList.add('status-stopping');
        dot.setAttribute('aria-label', '停止中');
    } else if (state) {
        dot.textContent = '●';
        dot.style.color = 'var(--success)';
        dot.classList.add('status-running');
        dot.setAttribute('aria-label', '运行中');
    } else {
        dot.textContent = '■';
        dot.style.color = 'var(--danger)';
        dot.classList.add('status-stopped');
        dot.setAttribute('aria-label', '已停止');
    }
}

function updateSidebarStatusDot(state) {
    var sd = document.getElementById('sidebarStatus');
    var stText = document.getElementById('sidebarStatusText');
    if (!sd) return;

    sd.className = 'status-dot ' + state;
    var labels = {
        running: 'ToolDelta 运行中',
        stopped: 'ToolDelta 已停止',
        starting: 'ToolDelta 启动中',
        stopping: 'ToolDelta 停止中'
    };
    var label = labels[state] || '状态未知';
    sd.title = label;
    sd.setAttribute('aria-label', label);
    if (stText) stText.textContent = label;
}

function renderInfoExists(id, exists) {
    var el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = exists
        ? '<span class="tag tag-enabled">存在</span>'
        : '<span class="tag tag-disabled">不存在</span>';
}

function clearMainToggleBusy() {
    var btn = document.getElementById('mainToggleBtn');
    if (btn) btn.setAttribute('aria-busy', 'false');
}

function isValidNumber(v) {
    return typeof v === 'number' && !isNaN(v) && isFinite(v);
}

function setDashText(id, value) {
    var el = document.getElementById(id);
    if (el && value !== undefined) {
        var prev = el.textContent;
        var next = String(value);
        el.textContent = next;
        el.classList.remove('loading-text');
        if (prev && prev !== next && prev !== '—' && prev !== '--' && prev !== '检测中...' && prev !== '加载失败') {
            triggerStatPulse(el);
        }
    }
}

function triggerStatPulse(el) {
    if (!el) return;
    el.classList.remove('is-pulse');
    void el.offsetWidth;
    el.classList.add('is-pulse');
    setTimeout(function () { el.classList.remove('is-pulse'); }, 650);
}

var _countTimers = {};
var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function animateCount(id, target, suffix) {
    var el = document.getElementById(id);
    if (!el) return;
    suffix = suffix || '';
    var prevValue = parseFloat(el.textContent) || 0;
    if (isNaN(target) || prevValue === target) { el.textContent = target + suffix; return; }
    if (_countTimers[id]) { cancelAnimationFrame(_countTimers[id]); delete _countTimers[id]; }
    if (prefersReducedMotion) {
        el.textContent = target + suffix;
        triggerStatPulse(el);
        return;
    }
    var startTime = null, dur = 300;
    function step(ts) {
        if (!document.getElementById(id)) { delete _countTimers[id]; return; }
        if (startTime === null) startTime = ts;
        var p = Math.min(1, (ts - startTime) / dur);
        var eased = 1 - Math.pow(1 - p, 3);
        var v = prevValue + (target - prevValue) * eased;
        el.textContent = (Number.isInteger(target) ? Math.round(v) : v.toFixed(1)) + suffix;
        if (p < 1) {
            _countTimers[id] = requestAnimationFrame(step);
        } else {
            el.textContent = target + suffix;
            delete _countTimers[id];
            triggerStatPulse(el);
        }
    }
    _countTimers[id] = requestAnimationFrame(step);
}

function fmtPercent(v) {
    if (!isValidNumber(v)) return '--';
    return Number(v).toFixed(1) + '%';
}

function fmtMB(mb) {
    if (!isValidNumber(mb)) return '--';
    if (mb >= 1024) return (mb / 1024).toFixed(1) + ' GB';
    return mb + ' MB';
}

function fmtGB(gb) {
    if (!isValidNumber(gb)) return '--';
    return Number(gb).toFixed(2) + ' GB';
}

loadDashboardStats();
if (window.TDPoll) { window.TDPoll.register(loadDashboardStats, 5000); }

var _perfHistory = [];
var _perfMaxPoints = 60;
var _perfChartW = 600, _perfChartH = 160;

function pushPerfMetric(sys) {
    var cpu = isValidNumber(sys.cpu_percent) ? Math.max(0, Math.min(100, sys.cpu_percent)) : null;
    var mem = isValidNumber(sys.mem_percent) ? Math.max(0, Math.min(100, sys.mem_percent)) : null;
    _perfHistory.push({ cpu: cpu, mem: mem });
    if (_perfHistory.length > _perfMaxPoints) _perfHistory.shift();
    renderPerfChart();
    pushSparkline(sys);
}

function cubicBezierPath(points, valueKey, closeArea) {
    var n = points.length;
    if (n === 0) return '';

    function getY(i) {
        var v = points[i][valueKey];
        if (v === null || v === undefined || isNaN(v)) return null;
        return _perfChartH - (v / 100) * _perfChartH;
    }

    function getX(i) {
        if (n === 1) return _perfChartW;
        return i * (_perfChartW / (n - 1));
    }

    var validPoints = [];
    for (var i = 0; i < n; i++) {
        var y = getY(i);
        if (y !== null) {
            validPoints.push({ x: getX(i), y: y, idx: i });
        }
    }

    if (validPoints.length === 0) return '';
    if (validPoints.length === 1) {
        var py = validPoints[0].y;
        var lineD = 'M 0 ' + py.toFixed(1) + ' L ' + _perfChartW + ' ' + py.toFixed(1);
        if (closeArea) return lineD + ' L ' + _perfChartW + ' ' + _perfChartH + ' L 0 ' + _perfChartH + ' Z';
        return lineD;
    }

    var d = 'M ' + validPoints[0].x.toFixed(1) + ' ' + validPoints[0].y.toFixed(1);

    for (var j = 0; j < validPoints.length - 1; j++) {
        var p0 = validPoints[j];
        var p1 = validPoints[j + 1];
        var prevP = j > 0 ? validPoints[j - 1] : p0;
        var nextP = j < validPoints.length - 2 ? validPoints[j + 2] : p1;

        var smoothing = 0.2;
        var cp1x = p0.x + (p1.x - prevP.x) * smoothing;
        var cp1y = p0.y + (p1.y - prevP.y) * smoothing;
        var cp2x = p1.x - (nextP.x - p0.x) * smoothing;
        var cp2y = p1.y - (nextP.y - p0.y) * smoothing;

        d += ' C ' + cp1x.toFixed(1) + ' ' + cp1y.toFixed(1) +
             ', ' + cp2x.toFixed(1) + ' ' + cp2y.toFixed(1) +
             ', ' + p1.x.toFixed(1) + ' ' + p1.y.toFixed(1);
    }

    if (closeArea) {
        d += ' L ' + _perfChartW + ' ' + _perfChartH + ' L 0 ' + _perfChartH + ' Z';
    }
    return d;
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

    var hasAnyData = _perfHistory.some(function(p) {
        return p.cpu !== null || p.mem !== null;
    });

    if (!hasAnyData) {
        cpuLine.setAttribute('d', '');
        memLine.setAttribute('d', '');
        cpuArea.setAttribute('d', '');
        memArea.setAttribute('d', '');
        if (empty) empty.style.display = 'flex';
        return;
    }
    if (empty) empty.style.display = 'none';
    if (sub) sub.textContent = '最近 ' + (n * 5) + ' 秒';

    cpuLine.setAttribute('d', cubicBezierPath(_perfHistory, 'cpu', false));
    memLine.setAttribute('d', cubicBezierPath(_perfHistory, 'mem', false));
    cpuArea.setAttribute('d', cubicBezierPath(_perfHistory, 'cpu', true));
    memArea.setAttribute('d', cubicBezierPath(_perfHistory, 'mem', true));

    var lastValidCpu = null, lastValidMem = null, lastX = 0;
    for (var k = n - 1; k >= 0; k--) {
        if (lastValidCpu === null && _perfHistory[k].cpu !== null) {
            lastValidCpu = _perfHistory[k].cpu;
        }
        if (lastValidMem === null && _perfHistory[k].mem !== null) {
            lastValidMem = _perfHistory[k].mem;
        }
        if (lastValidCpu !== null && lastValidMem !== null) break;
    }

    var lastIdx = n - 1;
    lastX = n > 1 ? lastIdx * (_perfChartW / (n - 1)) : _perfChartW;

    var cpuDot = document.getElementById('cpuDot');
    var memDot = document.getElementById('memDot');
    if (cpuDot && lastValidCpu !== null) {
        cpuDot.setAttribute('cx', lastX.toFixed(1));
        cpuDot.setAttribute('cy', (_perfChartH - (lastValidCpu / 100) * _perfChartH).toFixed(1));
    } else if (cpuDot) {
        cpuDot.setAttribute('cx', -10);
    }
    if (memDot && lastValidMem !== null) {
        memDot.setAttribute('cx', lastX.toFixed(1));
        memDot.setAttribute('cy', (_perfChartH - (lastValidMem / 100) * _perfChartH).toFixed(1));
    } else if (memDot) {
        memDot.setAttribute('cx', -10);
    }
}

var _sparkHistory = { cpu: [], mem: [], disk: [] };
var _sparkMaxPoints = 6;
var _sparkW = 80, _sparkH = 24;

function pushSparkline(sys) {
    var cpu = isValidNumber(sys.cpu_percent) ? Math.max(0, Math.min(100, sys.cpu_percent)) : null;
    var mem = isValidNumber(sys.mem_percent) ? Math.max(0, Math.min(100, sys.mem_percent)) : null;
    var disk = isValidNumber(sys.disk_percent) ? Math.max(0, Math.min(100, sys.disk_percent)) : null;
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
    var validPts = points.filter(function(p) { return p !== null && !isNaN(p); });
    if (n === 0 || validPts.length === 0) {
        polyline.setAttribute('points', '');
        if (dot) dot.setAttribute('cx', -1);
        return;
    }
    var pad = 2;
    var usableH = _sparkH - pad * 2;
    var stepX = (n > 1) ? (_sparkW / (n - 1)) : 0;
    var pts = [];
    for (var i = 0; i < n; i++) {
        if (points[i] === null || isNaN(points[i])) continue;
        var x = (n > 1) ? (i * stepX) : (_sparkW / 2);
        var y = pad + (1 - points[i] / 100) * usableH;
        pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    polyline.setAttribute('points', pts.join(' '));
    if (dot) {
        var lastVal = null, lastI = n - 1;
        for (var j = n - 1; j >= 0; j--) {
            if (points[j] !== null && !isNaN(points[j])) {
                lastVal = points[j];
                lastI = j;
                break;
            }
        }
        if (lastVal !== null) {
            var lastX = (n > 1) ? (lastI * stepX) : (_sparkW / 2);
            var lastY = pad + (1 - lastVal / 100) * usableH;
            dot.setAttribute('cx', lastX.toFixed(1));
            dot.setAttribute('cy', lastY.toFixed(1));
        } else {
            dot.setAttribute('cx', -1);
        }
    }
}

function initPerfChartHover() {
    var chart = document.getElementById('perfChart');
    var wrap = chart ? chart.parentElement : null;
    var hoverLine = document.getElementById('perfHoverLine');
    var tooltip = document.getElementById('perfTooltip');
    if (!chart || !wrap || !hoverLine || !tooltip) return;

    var isTouching = false;

    function hideTooltip() {
        if (isTouching) return;
        hoverLine.style.opacity = '0';
        tooltip.classList.remove('is-visible');
    }

    function getEventPos(e) {
        var touch = e.touches && e.touches[0];
        if (touch) {
            return { clientX: touch.clientX, clientY: touch.clientY };
        }
        var changedTouch = e.changedTouches && e.changedTouches[0];
        if (changedTouch && (e.type === 'touchend')) {
            return { clientX: changedTouch.clientX, clientY: changedTouch.clientY };
        }
        return { clientX: e.clientX, clientY: e.clientY };
    }

    function handleMove(e) {
        var pos = getEventPos(e);
        if (!pos) return;
        var n = _perfHistory.length;
        if (n === 0) { hideTooltip(); return; }
        var rect = chart.getBoundingClientRect();
        if (rect.width === 0) return;
        var x = pos.clientX - rect.left;
        if (x < 0 || x > rect.width) { hideTooltip(); return; }
        var vbX = (x / rect.width) * _perfChartW;
        var idx;
        if (n === 1) {
            idx = 0;
        } else {
            var stepX = _perfChartW / (n - 1);
            idx = Math.round(vbX / stepX);
            if (idx < 0) idx = 0;
            if (idx > n - 1) idx = n - 1;
        }

        var lineX = (n === 1) ? _perfChartW : idx * (_perfChartW / (n - 1));
        hoverLine.setAttribute('x1', lineX.toFixed(1));
        hoverLine.setAttribute('x2', lineX.toFixed(1));
        hoverLine.setAttribute('y1', 0);
        hoverLine.setAttribute('y2', _perfChartH);
        hoverLine.style.opacity = '1';

        var px = (lineX / _perfChartW) * rect.width;
        var point = _perfHistory[idx];
        var secsAgo = (n - 1 - idx) * 5;

        var cpuStr = point.cpu !== null && !isNaN(point.cpu) ? point.cpu.toFixed(1) + '%' : '--';
        var memStr = point.mem !== null && !isNaN(point.mem) ? point.mem.toFixed(1) + '%' : '--';

        tooltip.innerHTML =
            '<div class="pt-time">T-' + secsAgo + 's</div>' +
            '<div class="pt-row"><span class="pt-dot pt-cpu"></span>CPU ' + cpuStr + '</div>' +
            '<div class="pt-row"><span class="pt-dot pt-mem"></span>内存 ' + memStr + '</div>';
        tooltip.style.left = px + 'px';
        tooltip.style.top = '8px';
        tooltip.classList.add('is-visible');

        var tw = tooltip.offsetWidth;
        if (tw > 0) {
            var halfW = tw / 2;
            if (px - halfW < 0) px = halfW;
            if (px + halfW > rect.width) px = rect.width - halfW;
            tooltip.style.left = px + 'px';
        }
    }

    function handleTouchStart(e) {
        isTouching = true;
        e.preventDefault();
        handleMove(e);
    }

    function handleTouchMove(e) {
        if (!isTouching) return;
        e.preventDefault();
        handleMove(e);
    }

    function handleTouchEnd(e) {
        isTouching = false;
        setTimeout(hideTooltip, 1500);
    }

    chart.addEventListener('mousemove', handleMove);
    chart.addEventListener('mouseleave', hideTooltip);
    chart.addEventListener('touchstart', handleTouchStart, { passive: false });
    chart.addEventListener('touchmove', handleTouchMove, { passive: false });
    chart.addEventListener('touchend', handleTouchEnd);
    chart.addEventListener('touchcancel', handleTouchEnd);
}

window.updateStatusIcon = updateStatusIcon;
window.updateSidebarStatusDot = updateSidebarStatusDot;

initPerfChartHover();
