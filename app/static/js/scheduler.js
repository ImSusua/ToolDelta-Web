let jobs = [];

// 转义函数：加载时 main.js 可能尚未执行，用惰性查找
function _esc(s) {
    return (window._escHtml || function(v) {
        return String(v == null ? '' : v).replace(/[&<>"']/g, function(m) {
            return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m];
        });
    })(s);
}
function escapeHtml(s) { return _esc(s); }

// 时间格式化（秒级时间戳或ISO字符串）
function fmtTime(ts) {
    if (!ts) return '—';
    var d = typeof ts === 'number' ? new Date(ts < 1e12 ? ts * 1000 : ts) : new Date(ts);
    if (isNaN(d.getTime())) return String(ts);
    var p = function(n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

async function apiCall(url, method, body) {
    var opts = { method: method || 'GET', headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    var f = window.tdFetch || fetch;
    var r = await f(url, opts);
    var ct = r.headers.get('content-type') || '';
    if (ct.indexOf('application/json') === -1) throw new Error('服务器响应格式错误');
    var d = await r.json();
    if (d.success === false) throw new Error(d.message || '操作失败');
    return d;
}

async function loadJobs() {
    var tbody = document.getElementById('jobsBody');
    try {
        jobs = await apiCall('/api/scheduler/jobs');
        if (!Array.isArray(jobs)) jobs = [];
        if (jobs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#888">暂无任务，点击"添加任务"创建</td></tr>';
            return;
        }
        tbody.innerHTML = jobs.map(function(j) {
            var typeLabel = j.type === 'interval' ? '间隔' : j.type === 'daily' ? '每日' : 'Cron';
            var lastRunBadge = j.last_error
                ? '<span class="badge badge-danger" title="' + escapeHtml(j.last_error) + '">失败</span>'
                : '';
            var toggle = '<label class="switch"><input type="checkbox" ' + (j.enabled ? 'checked' : '') + ' onchange="toggleJob(\'' + j.id + '\', this.checked)"><span class="slider"></span></label>';
            return '<tr>' +
                '<td><strong>' + escapeHtml(j.name) + '</strong></td>' +
                '<td>' + typeLabel + '</td>' +
                '<td><code>' + escapeHtml(j.command) + '</code></td>' +
                '<td>' + toggle + '</td>' +
                '<td>' + (j.run_count || 0) + '</td>' +
                '<td>' + lastRunBadge + ' ' + (j.last_run ? fmtTime(j.last_run) : '—') + '</td>' +
                '<td>' + (j.enabled ? (j.next_run ? fmtTime(j.next_run) : '<span style="color:#888">—</span>') : '<span style="color:#888">未启用</span>') + '</td>' +
                '<td>' +
                    '<button class="btn btn-sm btn-outline" onclick="withGuard(this, function(){runNow(\'' + j.id + '\')})">运行</button> ' +
                    '<button class="btn btn-sm btn-outline" onclick="openEdit(\'' + j.id + '\')">编辑</button> ' +
                    '<button class="btn btn-sm btn-danger" onclick="withGuard(this, function(){deleteJob(\'' + j.id + '\')})">删除</button>' +
                '</td>' +
            '</tr>';
        }).join('');
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#e5484d">加载失败: ' + escapeHtml(e.message) + '</td></tr>';
    }
}

function switchType() {
    var t = document.getElementById('job_type').value;
    document.getElementById('row_interval').style.display = t === 'interval' ? '' : 'none';
    document.getElementById('row_daily').style.display = t === 'daily' ? '' : 'none';
    document.getElementById('row_cron').style.display = t === 'cron' ? '' : 'none';
}

function setCronPreset(expr) {
    document.getElementById('job_cron').value = expr;
}

function openAdd() {
    document.getElementById('jobModalTitle').textContent = '添加任务';
    document.getElementById('job_id').value = '';
    document.getElementById('job_name').value = '';
    document.getElementById('job_type').value = 'interval';
    document.getElementById('job_interval').value = '3600';
    document.getElementById('job_hour').value = '4';
    document.getElementById('job_minute').value = '0';
    document.getElementById('job_cron').value = '0 * * * *';
    document.getElementById('job_command').value = '';
    document.getElementById('job_enabled').checked = false;
    switchType();
    document.getElementById('jobModal').classList.add('active');
    document.body.classList.add('modal-open');
    setTimeout(function() { document.getElementById('job_name').focus(); }, 100);
}

function openEdit(id) {
    var j = jobs.find(function(x) { return x.id === id; });
    if (!j) return;
    document.getElementById('jobModalTitle').textContent = '编辑任务';
    document.getElementById('job_id').value = j.id;
    document.getElementById('job_name').value = j.name || '';
    document.getElementById('job_type').value = j.type || 'interval';
    document.getElementById('job_interval').value = j.interval || 3600;
    document.getElementById('job_hour').value = (j.hour !== null && j.hour !== undefined) ? j.hour : 4;
    document.getElementById('job_minute').value = (j.minute !== null && j.minute !== undefined) ? j.minute : 0;
    document.getElementById('job_cron').value = j.cron || '0 * * * *';
    document.getElementById('job_command').value = j.command || '';
    document.getElementById('job_enabled').checked = !!j.enabled;
    switchType();
    document.getElementById('jobModal').classList.add('active');
    document.body.classList.add('modal-open');
}

function closeJobModal() {
    document.getElementById('jobModal').classList.remove('active');
    document.body.classList.remove('modal-open');
}

async function submitJob() {
    var id = document.getElementById('job_id').value;
    var name = document.getElementById('job_name').value.trim();
    var type = document.getElementById('job_type').value;
    var command = document.getElementById('job_command').value.trim();
    if (!name) { showToast('请输入任务名称', 'warning'); return; }
    if (!command) { showToast('请输入命令', 'warning'); return; }
    var body = {name: name, type: type, command: command, enabled: document.getElementById('job_enabled').checked};
    if (type === 'interval') {
        body.interval = parseInt(document.getElementById('job_interval').value) || 0;
    } else if (type === 'daily') {
        body.hour = parseInt(document.getElementById('job_hour').value);
        body.minute = parseInt(document.getElementById('job_minute').value);
    } else {
        body.cron = document.getElementById('job_cron').value.trim();
        if (!body.cron) { showToast('请输入 Cron 表达式', 'warning'); return; }
    }
    try {
        if (id) {
            body.id = id;
            await apiCall('/api/scheduler/update', 'POST', body);
            showToast('任务已更新', 'success');
        } else {
            await apiCall('/api/scheduler/add', 'POST', body);
            showToast('任务已创建', 'success');
        }
        closeJobModal();
        loadJobs();
    } catch (e) { showToast(e.message, 'error'); }
}

async function toggleJob(id, enabled) {
    try {
        await apiCall('/api/scheduler/update', 'POST', {id: id, enabled: enabled});
        loadJobs();
    } catch (e) { showToast(e.message, 'error'); loadJobs(); }
}

async function deleteJob(id) {
    if (!confirm('确定要删除该任务吗？')) return;
    try {
        await apiCall('/api/scheduler/delete', 'POST', {id: id});
        showToast('任务已删除', 'success');
        loadJobs();
    } catch (e) { showToast(e.message, 'error'); }
}

async function runNow(id) {
    try {
        await apiCall('/api/scheduler/run', 'POST', {id: id});
        showToast('已触发执行', 'success');
        loadJobs();
    } catch (e) { showToast(e.message, 'error'); }
}

function handleEnterKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        var target = e.target;
        if (target.tagName === 'INPUT' && target.type !== 'textarea') {
            e.preventDefault();
            var type = document.getElementById('job_type').value;
            if (target.id === 'job_name') {
                document.getElementById('job_command').focus();
            } else if (target.id === 'job_command' || target.enterkeyhint === 'done') {
                var btn = document.getElementById('jobSaveBtn');
                withGuard(btn, submitJob);
            } else if (type === 'interval' && target.id === 'job_interval') {
                document.getElementById('job_command').focus();
            } else if (type === 'daily') {
                if (target.id === 'job_hour') document.getElementById('job_minute').focus();
                else if (target.id === 'job_minute') document.getElementById('job_command').focus();
            } else if (type === 'cron' && target.id === 'job_cron') {
                document.getElementById('job_command').focus();
            }
        }
    }
}

document.addEventListener('DOMContentLoaded', function() {
    loadJobs();
    document.getElementById('job_type').addEventListener('change', switchType);
    document.getElementById('jobSaveBtn').addEventListener('click', function() {
        withGuard(this, submitJob);
    });
    document.getElementById('jobModal').addEventListener('click', function(e) {
        if (e.target === this) closeJobModal();
    });
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && document.getElementById('jobModal').classList.contains('active')) {
            closeJobModal();
        }
        if (document.getElementById('jobModal').classList.contains('active')) {
            handleEnterKey(e);
        }
    });
    document.querySelectorAll('.cron-preset').forEach(function(btn) {
        btn.addEventListener('click', function() {
            setCronPreset(this.getAttribute('data-cron'));
        });
    });
    if (window.TDPoll) { window.TDPoll.register(loadJobs, 5000); }
    else { setInterval(loadJobs, 5000); }
});
