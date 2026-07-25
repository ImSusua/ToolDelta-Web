// 定时任务模块前端逻辑

function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function typeText(job) {
    if (job.type === 'interval') {
        return '每 ' + (job.interval || '?') + ' 秒';
    }
    if (job.type === 'daily') {
        var hh = String(job.hour === null || job.hour === undefined ? '--' : job.hour).padStart(2, '0');
        var mm = String(job.minute === null || job.minute === undefined ? '--' : job.minute).padStart(2, '0');
        return '每日 ' + hh + ':' + mm;
    }
    return job.type || '';
}

function loadJobs() {
    var body = document.getElementById('jobsBody');
    // 加载态：骨架屏（参考 main.js renderState 四态组件）
    if (body && window.renderState) {
        body.innerHTML = '<tr><td colspan="8">' +
            '<div class="skeleton-list" aria-busy="true" aria-live="polite">' +
            '<div class="skeleton-card"></div><div class="skeleton-card"></div><div class="skeleton-card"></div>' +
            '</div></td></tr>';
    }
    fetch('/api/scheduler/jobs')
        .then(function (r) { return r.json(); })
        .then(function (jobs) {
            if (!body) return;
            if (!jobs || jobs.length === 0) {
                // 空态：带 CTA 引导用户添加首个任务
                if (window.renderState) {
                    body.innerHTML = '<tr><td colspan="8" id="jobsEmptyCell"></td></tr>';
                    var cell = document.getElementById('jobsEmptyCell');
                    renderState(cell, 'empty', {
                        icon: '⏰', title: '暂无定时任务',
                        hint: '创建第一个任务即可按计划自动执行命令',
                        cta: '添加任务', ctaFn: function () { if (typeof openAdd === 'function') openAdd(); }
                    });
                } else {
                    body.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--ink-subtle)">暂无任务，点击右上角“添加任务”创建。</td></tr>';
                }
                return;
            }
            var html = '';
            jobs.forEach(function (job) {
                var enabled = !!job.enabled;
                // 通过 data-id 承载 job.id + 事件委托，避免 onchange/onclick 字符串拼接导致的 JS 字面量逃逸
                var eid = escapeHtml(job.id);
                html += '<tr>';
                html += '<td>' + escapeHtml(job.name) + '</td>';
                html += '<td>' + escapeHtml(typeText(job)) + '</td>';
                html += '<td><code style="font-size:12px">' + escapeHtml(job.command) + '</code></td>';
                html += '<td><label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px">'
                    + '<input type="checkbox" class="job-toggle" data-id="' + eid + '" ' + (enabled ? 'checked' : '') + ' style="accent-color:var(--primary)"> '
                    + (enabled ? '已启用' : '已关闭') + '</label></td>';
                html += '<td>' + escapeHtml(job.run_count || 0) + '</td>';
                html += '<td style="font-size:12px;color:var(--ink-subtle)">' + escapeHtml(job.last_run || '—') + '</td>';
                html += '<td style="font-size:12px;color:var(--ink-muted)">' + escapeHtml(job.next_run || '—') + '</td>';
                html += '<td><div style="display:flex;gap:6px;flex-wrap:wrap">'
                    + '<button class="btn btn-outline btn-sm job-edit" data-id="' + eid + '">编辑</button>'
                    + '<button class="btn btn-outline btn-sm job-run" data-id="' + eid + '">立即运行</button>'
                    + '<button class="btn btn-danger btn-sm job-delete" data-id="' + eid + '">删除</button>'
                    + '</div></td>';
                html += '</tr>';
            });
            body.innerHTML = html;
        })
        .catch(function () {
            if (body && window.renderState) {
                body.innerHTML = '<tr><td colspan="8" id="jobsErrorCell"></td></tr>';
                renderState(document.getElementById('jobsErrorCell'), 'error', {
                    message: '加载任务失败', retry: loadJobs
                });
            } else {
                showToast('加载任务失败', 'error');
            }
        });
}

// 事件委托：在 #jobsBody 上集中处理按钮点击 / 勾选切换
(function () {
    var body = document.getElementById('jobsBody');
    if (!body) return;
    body.addEventListener('click', function (e) {
        var t = e.target;
        while (t && t !== body) {
            if (t.classList) {
                if (t.classList.contains('job-edit')) {
                    openEdit(t.getAttribute('data-id') || '');
                    return;
                }
                if (t.classList.contains('job-run')) {
                    runNow(t.getAttribute('data-id') || '', t);
                    return;
                }
                if (t.classList.contains('job-delete')) {
                    removeJob(t.getAttribute('data-id') || '');
                    return;
                }
            }
            t = t.parentNode;
        }
    });
    body.addEventListener('change', function (e) {
        var t = e.target;
        if (t && t.classList && t.classList.contains('job-toggle')) {
            toggleEnabled(t.getAttribute('data-id') || '', t);
        }
    });
})();

function toggleEnabled(id, checkbox) {
    // 兼容旧调用 toggleEnabled(id, boolean) 与新调用 toggleEnabled(id, element)
    var isEl = checkbox && typeof checkbox.checked === 'boolean';
    var enable = isEl ? checkbox.checked : !!checkbox;
    var prev = enable;
    if (isEl) checkbox.disabled = true;
    fetch('/api/scheduler/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id, enabled: enable })
    })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (isEl) checkbox.disabled = false;
            if (d && d.success) {
                showToast(enable ? '已启用' : '已禁用', 'success');
                loadJobs();
            } else {
                if (isEl) checkbox.checked = prev;
                showToast('操作失败: ' + (d && (d.message || d.error) || '未知错误'), 'error');
                loadJobs();
            }
        })
        .catch(function () {
            if (isEl) { checkbox.checked = prev; checkbox.disabled = false; }
            showToast('网络请求失败，已回滚', 'error');
            loadJobs();
        });
}

function runNow(id, btn) {
    // 防重复点击 + loading 文案（按钮由 onclick 传入 this）
    var origText = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '运行中...'; }
    fetch('/api/scheduler/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id })
    })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (d && d.success) { showToast('已立即执行', 'success'); loadJobs(); }
            else { showToast((d && (d.message || d.error)) || '执行失败', 'error'); }
        })
        .catch(function () { showToast('请求失败', 'error'); })
        .finally(function () { if (btn) { btn.disabled = false; btn.textContent = origText; } });
}

function removeJob(id) {
    showConfirm('确定删除该定时任务吗？', function (ok) {
        if (!ok) return;
        fetch('/api/scheduler/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: id })
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.success) { showToast('已删除', 'success'); loadJobs(); }
                else { showToast(d.message || '删除失败', 'error'); }
            })
            .catch(function () { showToast('请求失败', 'error'); });
    }, true);
}

// ─── 表单 ───────────────────────────────────────────

function syncTypeFields() {
    var type = document.getElementById('job_type').value;
    var isInterval = type === 'interval';
    document.getElementById('row_interval').style.display = isInterval ? 'block' : 'none';
    document.getElementById('row_daily').style.display = isInterval ? 'none' : 'block';
}

function openAdd() {
    document.getElementById('jobModalTitle').textContent = '添加任务';
    document.getElementById('jobSaveBtn').textContent = '添加';
    document.getElementById('job_id').value = '';
    document.getElementById('job_name').value = '';
    document.getElementById('job_type').value = 'interval';
    document.getElementById('job_interval').value = '3600';
    document.getElementById('job_hour').value = '4';
    document.getElementById('job_minute').value = '0';
    document.getElementById('job_command').value = '';
    document.getElementById('job_enabled').checked = false;
    syncTypeFields();
    if (window._openModal) _openModal('jobModal');
    else document.getElementById('jobModal').classList.add('active');
}

function openEdit(id) {
    fetch('/api/scheduler/jobs')
        .then(function (r) { return r.json(); })
        .then(function (jobs) {
            var job = jobs.find(function (j) { return j.id === id; });
            if (!job) { showToast('任务不存在', 'error'); return; }
            document.getElementById('jobModalTitle').textContent = '编辑任务';
            document.getElementById('jobSaveBtn').textContent = '保存';
            document.getElementById('job_id').value = job.id;
            document.getElementById('job_name').value = job.name || '';
            document.getElementById('job_type').value = job.type || 'interval';
            document.getElementById('job_interval').value = job.interval || 3600;
            document.getElementById('job_hour').value = job.hour === null || job.hour === undefined ? 4 : job.hour;
            document.getElementById('job_minute').value = job.minute === null || job.minute === undefined ? 0 : job.minute;
            document.getElementById('job_command').value = job.command || '';
            document.getElementById('job_enabled').checked = !!job.enabled;
            syncTypeFields();
            if (window._openModal) _openModal('jobModal');
            else document.getElementById('jobModal').classList.add('active');
        })
        .catch(function () { showToast('加载失败', 'error'); });
}

function closeJobModal() {
    closeModal('jobModal');
}

function submitJob() {
    var type = document.getElementById('job_type').value;
    // ── 表单校验（使用 main.js setFieldError，缺失时静默跳过） ──
    var nameEl = document.getElementById('job_name');
    var intervalEl = document.getElementById('job_interval');
    var hourEl = document.getElementById('job_hour');
    var minuteEl = document.getElementById('job_minute');
    var clearErr = function (el) { if (el && window.setFieldError) setFieldError(el, ''); };
    clearErr(nameEl); clearErr(intervalEl); clearErr(hourEl); clearErr(minuteEl);
    if (!nameEl.value.trim()) {
        if (window.setFieldError) setFieldError(nameEl, '名称不能为空');
        nameEl.focus(); return;
    }
    if (type === 'interval') {
        var iv = parseInt(intervalEl.value, 10);
        if (isNaN(iv) || iv < 1) {
            if (window.setFieldError) setFieldError(intervalEl, '间隔必须 ≥ 1 秒');
            intervalEl.focus(); return;
        }
    } else if (type === 'daily') {
        var h = parseInt(hourEl.value, 10);
        var m = parseInt(minuteEl.value, 10);
        if (isNaN(h) || h < 0 || h > 23) {
            if (window.setFieldError) setFieldError(hourEl, '小时 0-23');
            hourEl.focus(); return;
        }
        if (isNaN(m) || m < 0 || m > 59) {
            if (window.setFieldError) setFieldError(minuteEl, '分钟 0-59');
            minuteEl.focus(); return;
        }
    }

    var payload = {
        id: document.getElementById('job_id').value || undefined,
        name: nameEl.value,
        type: type,
        command: document.getElementById('job_command').value,
        enabled: document.getElementById('job_enabled').checked
    };
    // parseInt 失败时回退为 0，避免向服务端发送 NaN
    var safeInt = function (id) { var v = parseInt(document.getElementById(id).value, 10); return isNaN(v) ? 0 : v; };
    if (type === 'interval') {
        payload.interval = safeInt('job_interval');
    } else {
        payload.hour = safeInt('job_hour');
        payload.minute = safeInt('job_minute');
    }

    var isEdit = !!payload.id;
    var url = isEdit ? '/api/scheduler/update' : '/api/scheduler/add';

    // 保存按钮 loading 态，防重复提交
    var saveBtn = document.getElementById('jobSaveBtn');
    var origText = saveBtn ? saveBtn.textContent : '';
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中...'; }

    fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (d && d.success) {
                showToast(isEdit ? '已保存' : '已添加', 'success');
                closeJobModal();
                loadJobs();
            } else {
                showToast((d && (d.message || d.error)) || '保存失败', 'error');
            }
        })
        .catch(function () { showToast('请求失败', 'error'); })
        .finally(function () { if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = origText; } });
}

// 类型切换实时更新表单字段显隐
var _typeEl = document.getElementById('job_type');
if (_typeEl) _typeEl.addEventListener('change', syncTypeFields);

loadJobs();
// 优先使用全局可见性感知轮询器；缺失时本地 setInterval 也响应 Page Visibility 暂停
if (window.TDPoll) {
    window.TDPoll.register(loadJobs, 5000);
} else {
    var _timer = setInterval(loadJobs, 5000);
    document.addEventListener('visibilitychange', function () {
        if (document.hidden) { if (_timer) { clearInterval(_timer); _timer = null; } }
        else if (!_timer) { _timer = setInterval(loadJobs, 5000); }
    });
}
