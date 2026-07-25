// 控制台交互：命令发送 + 历史回溯(↑/↓) + Tab 自动补全(基于命令库) + 收藏快捷发送
// socket 重连配置：指数退避，避免移动端弱网下高频重连耗电
var socket = io({
    transports: ['websocket', 'polling'],  // websocket 优先，降级 polling（弱网更稳）
    reconnection: true,
    reconnectionAttempts: 20,              // 上限 20 次，避免无限重连耗电；超限后由 manualReconnect 兜底
    reconnectionDelay: 1000,          // 首次重连 1s
    reconnectionDelayMax: 10000,     // 退避上限 10s
    reconnectionJitter: 0.5          // 抖动 50%，避免多客户端同步重连压垮服务器
});
var body = document.getElementById('consoleBody');
var input = document.getElementById('consoleInput');
var statusEl = document.getElementById('consoleStatus');

var HISTORY_KEY = 'td_console_history';
var history = loadHistory();
var histIdx = -1;          // -1 表示正在输入新命令
var cmdLibrary = [];      // 来自 /api/commands 的所有命令 trigger，用于补全
// 命令历史下拉专用（内存维护，最近 50 条）：与 ↑/↓ 用的 history 分离，互不干扰
var _cmdHistory = history.slice(-50);

function loadHistory() {
    try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); }
    catch (e) { return []; }
}
function saveHistory(cmd) {
    if (!cmd) return;
    history = history.filter(function (c) { return c !== cmd; });
    history.push(cmd);
    if (history.length > 200) history = history.slice(-200);
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch (e) {}
}

// 维护命令历史下拉数组（去重 + 追加 + 上限 50）
function _pushCmdHistory(cmd) {
    if (!cmd) return;
    _cmdHistory = _cmdHistory.filter(function (c) { return c !== cmd; });
    _cmdHistory.push(cmd);
    if (_cmdHistory.length > 50) _cmdHistory = _cmdHistory.slice(-50);
}

// 日志级别检测：按优先级匹配关键字，返回外层 div 应加的 CSS 类名（不破坏内层 ANSI 着色）
function _detectLogLevel(text) {
    // 优先级：ERROR > WARN > DEBUG > INFO（更严重的优先标注）
    if (/\bERROR\b/i.test(text)) return 'log-error';
    if (/\bWARN(ING)?\b/i.test(text)) return 'log-warn';
    if (/\bDEBUG\b/i.test(text)) return 'log-debug';
    if (/\bINFO\b/i.test(text)) return 'log-info';
    return '';
}

function appendLine(html) {
    if (!body) return;
    // XSS 防护：先用正则过滤脚本、事件处理器、data URI 等明显危险内容（作为第一道防线）
    var safe = (html || '')
        .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '')
        .replace(/<img[^>]*onerror\s*=[^>]*>/gi, '')
        .replace(/<svg[^>]*onload\s*=[^>]*>/gi, '')
        .replace(/<[^>]*\bon[a-z]+\s*=\s*[^>]*/gi, function(m){return m.replace(/on\w+\s*=\s*/gi,'data-removed=');})
        .replace(/javascript\s*:/gi, 'blocked:')
        .replace(/data\s*:\s*[^;\s"']*/gi, 'blocked:');
    // 暂停滚动模式下：不判断 atBottom，强制不滚动到底部
    var atBottom = _pauseScroll ? false : (body.scrollHeight - body.scrollTop - body.clientHeight < 50);
    var div = document.createElement('div');
    // 安全清理：用 DOMParser 在离屏文档中解析 raw，再 sanitize 后将节点迁入 div，
    // 避免直接 div.innerHTML = raw 时 <img onerror> 等被触发
    var parsed = new DOMParser().parseFromString('<root>' + safe + '</root>', 'text/html');
    var root = (parsed.body && parsed.body.firstChild) ? parsed.body.firstChild : parsed.createElement('root');
    var tags = root.querySelectorAll('*');
    for (var i = tags.length - 1; i >= 0; i--) {
        var tag = tags[i];
        // 移除所有 on* 事件属性与可疑 href/src，防止 DOMParser 仍残留的事件句柄
        var attrs = tag.attributes;
        for (var j = attrs.length - 1; j >= 0; j--) {
            var an = attrs[j].name.toLowerCase();
            if (an.indexOf('on') === 0) {
                tag.removeAttribute(attrs[j].name);
            } else if (an === 'href' || an === 'src' || an === 'xlink:href') {
                var av = (attrs[j].value || '').trim();
                if (/^\s*(javascript|data)\s*:/i.test(av)) {
                    tag.removeAttribute(attrs[j].name);
                }
            }
        }
        // 非 span 标签：保留其文本内容，但剥离标签本身
        if (tag.tagName.toLowerCase() !== 'span') {
            var parent = tag.parentNode;
            while (tag.firstChild) {
                parent.insertBefore(tag.firstChild, tag);
            }
            parent.removeChild(tag);
        }
    }
    // 将清理后的子节点迁入 div（不直接 innerHTML=raw）
    while (root.firstChild) {
        div.appendChild(root.firstChild);
    }
    // 添加时间戳前缀（默认隐藏，由 .show-ts 控制可见性）
    var tsSpan = document.createElement('span');
    tsSpan.className = 'c-ts';
    tsSpan.textContent = _formatTs(new Date());
    div.insertBefore(tsSpan, div.firstChild);
    // 日志级别着色：仅在外层 div 加级别类，不影响内层 ANSI 着色
    var lvl = _detectLogLevel(div.textContent || '');
    if (lvl) div.classList.add(lvl);
    // 应用当前过滤状态（避免批量插入后再回流）
    if (_filterQuery) {
        var text = div.textContent || '';
        if (text.toLowerCase().indexOf(_filterQuery) === -1) {
            div.classList.add('is-filtered-out');
        }
    }
    // 批量插入缓冲：高频输出（如日志刷屏）时合并到下一帧统一插入，避免逐行重排卡顿
    if (_batchBuffer === null) {
        _batchBuffer = document.createDocumentFragment();
        _batchPending = { atBottom: atBottom, count: 0 };
        requestAnimationFrame(_flushBatch);
    }
    _batchBuffer.appendChild(div);
    _batchPending.count++;
    _batchPending.atBottom = _batchPending.atBottom && atBottom;
}
var _batchBuffer = null;
var _batchPending = null;
function _flushBatch() {
    if (!_batchBuffer || !_batchPending || !body) { _batchBuffer = null; _batchPending = null; return; }
    var frag = _batchBuffer;
    var info = _batchPending;
    _batchBuffer = null;
    _batchPending = null;
    body.appendChild(frag);
    // 行数上限：保留最近 1000 行。批量移除 50 行（而非逐行移除），减少 reflow 次数
    while (body.children.length > 1000) {
        var removeCount = Math.min(50, body.children.length - 1000);
        for (var i = 0; i < removeCount; i++) body.removeChild(body.firstChild);
    }
    if (info.atBottom) {
        body.scrollTop = body.scrollHeight;
    } else {
        // 用户未在底部（或暂停滚动）：累计未读，显示新消息提示
        // 暂停模式下不自动滚动但仍累计未读，便于用户感知有新消息到达
        _newMsgCount += info.count;
        if (_pillCount) _pillCount.textContent = _newMsgCount;
        if (_pill) _pill.style.display = '';
    }
}

/* ─── 工具栏：暂停滚动 / 时间戳 / 搜索过滤 ─────────────────── */

var _pauseScroll = false;       // 暂停自动滚动到最新
var _showTimestamps = false;    // 显示时间戳
var _showLineNumbers = false;   // 显示行号
var _filterQuery = '';          // 当前过滤关键词（小写）

// 格式化时间戳为 HH:MM:SS.mmm
function _formatTs(d) {
    var h = String(d.getHours()).padStart(2, '0');
    var m = String(d.getMinutes()).padStart(2, '0');
    var s = String(d.getSeconds()).padStart(2, '0');
    var ms = String(d.getMilliseconds()).padStart(3, '0');
    return h + ':' + m + ':' + s + '.' + ms;
}

// 暂停滚动：开启后新消息不滚动到底部，方便用户阅读历史
function togglePauseScroll() {
    _pauseScroll = !_pauseScroll;
    var btn = document.getElementById('pauseScrollBtn');
    var bar = document.querySelector('.console-bar');
    if (btn) {
        btn.setAttribute('aria-pressed', _pauseScroll ? 'true' : 'false');
        var lbl = btn.querySelector('.lbl');
        if (lbl) lbl.textContent = _pauseScroll ? '继续' : '暂停';
        btn.setAttribute('aria-label', _pauseScroll ? '继续自动滚动' : '暂停自动滚动');
    }
    if (bar) bar.classList.toggle('is-paused', _pauseScroll);
    // 继续滚动时立即跳到底部
    if (!_pauseScroll) {
        scrollToBottom();
    }
    if (typeof showToast === 'function') {
        showToast(_pauseScroll ? '已暂停自动滚动' : '已恢复自动滚动', 'info');
    }
}

// 时间戳开关：开启后在每行行首显示时间戳
function toggleTimestamps() {
    _showTimestamps = !_showTimestamps;
    var btn = document.getElementById('timestampBtn');
    if (btn) {
        btn.setAttribute('aria-pressed', _showTimestamps ? 'true' : 'false');
        btn.setAttribute('aria-label', _showTimestamps ? '隐藏时间戳' : '显示时间戳');
    }
    if (body) body.classList.toggle('show-ts', _showTimestamps);
}

// 行号开关：开启后在每行左侧显示行号（CSS counter 实现，自动随行增减重编号）
function toggleLineNumbers() {
    _showLineNumbers = !_showLineNumbers;
    var btn = document.getElementById('lineNumberBtn');
    if (btn) {
        btn.setAttribute('aria-pressed', _showLineNumbers ? 'true' : 'false');
        btn.setAttribute('aria-label', _showLineNumbers ? '隐藏行号' : '显示行号');
    }
    if (body) body.classList.toggle('show-lines', _showLineNumbers);
}

// 搜索过滤：仅显示包含关键词的行（实时，300ms 防抖）
var _filterTimer = null;
function _setupSearchFilter() {
    var inp = document.getElementById('consoleSearch');
    var clear = document.getElementById('consoleSearchClear');
    if (!inp) return;
    inp.addEventListener('input', function () {
        if (clear) clear.style.display = inp.value ? 'flex' : 'none';
        if (_filterTimer) clearTimeout(_filterTimer);
        _filterTimer = setTimeout(function () {
            _filterQuery = (inp.value || '').trim().toLowerCase();
            _applyFilter();
        }, 300);
    });
    // Esc 清除过滤
    inp.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            inp.value = '';
            if (clear) clear.style.display = 'none';
            _filterQuery = '';
            _applyFilter();
            e.preventDefault();
        }
    });
}

// 应用过滤：遍历所有行，隐藏不匹配的
function _applyFilter() {
    if (!body) return;
    var children = body.children;
    for (var i = 0; i < children.length; i++) {
        var line = children[i];
        if (!_filterQuery) {
            line.classList.remove('is-filtered-out');
        } else {
            var text = (line.textContent || '').toLowerCase();
            line.classList.toggle('is-filtered-out', text.indexOf(_filterQuery) === -1);
        }
    }
    // 过滤后跳到底部（让用户看到最近的匹配）
    if (_filterQuery && !_pauseScroll) {
        body.scrollTop = body.scrollHeight;
    }
}

// 清除搜索过滤
function clearConsoleSearch() {
    var inp = document.getElementById('consoleSearch');
    var clear = document.getElementById('consoleSearchClear');
    if (inp) inp.value = '';
    if (clear) clear.style.display = 'none';
    _filterQuery = '';
    _applyFilter();
}

// 初始化工具栏（DOM 已渲染后调用）
_setupSearchFilter();

var _sendingCmd = false;
// 命令队列：替代原 120ms 硬锁（硬锁会丢弃连发命令），改为入队 + 100ms 间隔串行 flush
var _cmdQueue = [];

// 供移动端「发送」按钮调用：读取输入框并发送
function sendConsoleInput() {
    if (!input) return;
    var cmd = input.value;
    input.value = '';
    histIdx = -1;
    sendCommand(cmd);
    input.focus();
}

function sendCommand(cmd) {
    cmd = (cmd || '').trim();
    if (!cmd) return;
    // socket 未连接时拒绝发送并提示，避免命令静默丢失
    if (!socket.connected) {
        appendLine('<span class="c-err">⚠ 未连接到服务器，命令未发送。请等待重连后重试。</span>');
        return;
    }
    // echo 与历史保存立即执行（即时反馈）；实际 emit 入队串行发送，避免连发丢失
    appendLine('<span class="c-cmd">$ ' + escapeHtml(cmd) + '</span>');
    saveHistory(cmd);
    _pushCmdHistory(cmd);
    _cmdQueue.push(cmd);
    _flushCmdQueue();
}

// 串行 flush 命令队列：每条命令间隔 100ms，避免服务端突发拥塞
function _flushCmdQueue() {
    if (_sendingCmd) return;
    if (_cmdQueue.length === 0) return;
    var cmd = _cmdQueue.shift();
    _sendingCmd = true;
    socket.emit('console_command', cmd);
    setTimeout(function () { _sendingCmd = false; _flushCmdQueue(); }, 100);
}

function moveCursorEnd(el) { setTimeout(function () { el.selectionStart = el.selectionEnd = el.value.length; }, 0); }

// Tab 自动补全：先匹配命令库 trigger，再退而求其次匹配历史命令
function complete() {
    var val = input.value;
    if (!val) return;
    var parts = val.split(/\s+/);
    var prefix = parts[parts.length - 1];
    if (!prefix) return;
    var cands = [];
    cmdLibrary.forEach(function (t) {
        if (t && t.indexOf(prefix) === 0 && cands.indexOf(t) === -1) cands.push(t);
    });
    if (cands.length === 0) {
        history.forEach(function (t) {
            if (t && t.indexOf(prefix) === 0 && cands.indexOf(t) === -1) cands.push(t);
        });
    }
    if (cands.length === 0) return;
    if (cands.length === 1) {
        parts[parts.length - 1] = cands[0];
        input.value = parts.join(' ');
    } else {
        var common = cands[0];
        for (var i = 1; i < cands.length; i++) {
            while (cands[i].indexOf(common) !== 0 && common.length) common = common.slice(0, -1);
        }
        if (common.length > prefix.length) {
            parts[parts.length - 1] = common;
            input.value = parts.join(' ');
        } else {
            appendLine('<span style="color:#aaa;">候选 (' + cands.length + '): ' +
                cands.slice(0, 14).join('  ') + (cands.length > 14 ? ' …' : '') + '</span>');
        }
    }
    moveCursorEnd(input);
}

if (body) body.innerHTML = '<div class="c-loading"><span class="spinner" style="vertical-align:middle;margin-right:6px"></span>正在加载输出...</div>';

if (body) {
    fetch('/api/tool/output?tail=200&html=1')
        .then(function (r) { return r.json(); })
        .then(function (d) {
            body.innerHTML = '';
            if (d.lines && d.lines.length) d.lines.forEach(appendLine);
        })
        .catch(function () { body.innerHTML = '<div class="c-err">⚠ 获取历史输出失败，请刷新重试</div>'; });
}

// 拉取统一命令库用于 Tab 补全（静态扫描 + 运行时注册）
fetch('/api/commands')
    .then(function (r) { return r.json(); })
    .then(function (d) {
        var arr = Array.isArray(d) ? d : (d.plugins || []);
        var lib = [];
        arr.forEach(function (p) {
            (p.commands || []).forEach(function (c) {
                (c.triggers || []).forEach(function (t) {
                    if (t && lib.indexOf(t) === -1) lib.push(t);
                });
            });
        });
        cmdLibrary = lib;
    })
    .catch(function () {
        cmdLibrary = [];
        // 补全库加载失败时在控制台提示一次（不阻塞使用）
        appendLine('<span class="c-hint">ℹ 命令补全库加载失败，Tab 补全不可用</span>');
    });

// 输入框默认 placeholder（断开时改为「未连接...」，连接恢复时还原）
var _INPUT_PLACEHOLDER = '输入命令...（↑/↓ 翻历史，Tab 补全）';
// 进程状态（由 main.js updateToggleState 同步），用于 disconnect 时给出更准确的提示
window._tdProcessRunning = false;
socket.on('connect', function () {
    if (statusEl) { statusEl.textContent = '已连接'; statusEl.className = 'status-conn connected'; }
    var bar = document.querySelector('.console-bar');
    if (bar) bar.classList.add('is-connected');
    // 连接恢复：启用发送按钮 + 还原 placeholder
    var sendBtn = document.getElementById('console-send-btn');
    if (sendBtn) sendBtn.disabled = false;
    if (input) input.placeholder = _INPUT_PLACEHOLDER;
});
socket.on('disconnect', function () {
    // 进程运行中但实时通道断开：明确告知「进程仍在运行」，避免用户误以为启动失败
    if (window._tdProcessRunning) {
        if (statusEl) { statusEl.textContent = '实时通道断开（进程运行中）'; statusEl.className = 'status-conn disconnected'; }
        if (input) input.placeholder = '实时通道断开，命令暂不可发送...';
    } else {
        if (statusEl) { statusEl.textContent = '已断开'; statusEl.className = 'status-conn disconnected'; }
        if (input) input.placeholder = '未连接...';
    }
    var bar = document.querySelector('.console-bar');
    if (bar) bar.classList.remove('is-connected');
    // 断开时禁用发送按钮 + 提示未连接，避免用户继续输入被静默丢弃
    var sendBtn = document.getElementById('console-send-btn');
    if (sendBtn) sendBtn.disabled = true;
});
// 移动端弱网：重连尝试提示（带尝试次数显示）
// 弱网下 connect_error 高频触发，对 statusEl 的 DOM 更新做节流，避免每秒多次重排
// 参考 Pterodactyl v1.12.0：对不可恢复错误（认证失败 401/403）停止重连，避免无限重连耗电
var _reconnShown = false;
var _reconnAttempts = 0;
var _reconnRaf = null;
var _reconnFatal = false; // 不可恢复错误标志：认证失效后不再重连
socket.on('connect_error', function (err) {
    // 认证类错误：token 过期/会话失效，重连无意义，直接停止并提示重新登录
    var desc = (err && (err.description || err.message)) || '';
    // socket.io polling 401/403 时,HTTP 状态码位于 err.context.xhr.status
    var status = (err && err.context && err.context.xhr && err.context.xhr.status)
              || (err && err.status) || 0;
    if (status === 401 || status === 403 || /auth|forbidden|unauthorized|401|403/i.test(desc)) {
        if (!_reconnFatal) {
            _reconnFatal = true;
            socket.io.opts.reconnection = false; // 关闭自动重连
            if (socket.io.conn) socket.io.conn.close(); // 中止进行中的重连
            if (statusEl) { statusEl.textContent = '认证失效'; statusEl.className = 'status-conn disconnected'; }
            showToast('登录已失效，请刷新页面重新登录', 'error');
        }
        return;
    }
    _reconnAttempts++;
    if (!_reconnShown) {
        _reconnShown = true;
        showToast('连接中…若持续失败请检查网络', 'warning');
    }
    // rAF 合并：同一帧内多次 connect_error 只更新一次 DOM
    if (_reconnRaf || !statusEl) return;
    _reconnRaf = requestAnimationFrame(function () {
        _reconnRaf = null;
        statusEl.textContent = '重连中(' + _reconnAttempts + ')';
        statusEl.className = 'status-conn disconnected';
    });
});
socket.on('reconnect', function () {
    if (_reconnRaf) { cancelAnimationFrame(_reconnRaf); _reconnRaf = null; }
    if (_reconnShown) { _reconnShown = false; showToast('已重新连接', 'success'); }
    _reconnAttempts = 0;
});
// 重连 20 次仍失败：停止自动重连，提供「重试」按钮供用户手动恢复
socket.io.on('reconnect_failed', function () {
    // 进程仍在运行但实时通道彻底失败：明确告知用户两种状态分离，避免误判
    if (window._tdProcessRunning) {
        if (statusEl) statusEl.innerHTML = '实时通道失败（进程运行中） <button class="btn btn-sm btn-outline" onclick="manualReconnect()">重试</button>';
        if (input) input.placeholder = '实时通道失败，命令暂不可发送，点击重试恢复...';
    } else {
        if (statusEl) statusEl.innerHTML = '连接失败 <button class="btn btn-sm btn-outline" onclick="manualReconnect()">重试</button>';
        if (input) input.placeholder = '未连接...';
    }
    var sendBtn = document.getElementById('console-send-btn');
    if (sendBtn) sendBtn.disabled = true;
});
function manualReconnect() {
    // 重置所有重连状态,避免上次失败状态残留导致重试按钮立即被冲掉、计数器累加误导
    _reconnAttempts = 0;
    _reconnShown = false;
    _reconnFatal = false;
    if (window.socket && socket.io && socket.io.opts) socket.io.opts.reconnection = true;
    if (statusEl) { statusEl.textContent = '重连中…'; statusEl.className = 'status-conn connecting'; }
    if (window.socket) socket.connect();
}

// console_output 批处理：50ms 内的多次输出合并为一次 flush，减少高频刷屏时的 reflow
var _outputBuffer = [];
var _outputTimer = null;
socket.on('console_output', function (data) {
    _outputBuffer.push(data);
    if (!_outputTimer) {
        _outputTimer = setTimeout(function () {
            var lines = _outputBuffer;
            _outputBuffer = [];
            _outputTimer = null;
            lines.forEach(function (d) { appendLine(d.data_html || d.data || ''); });
        }, 50);
    }
});

// 新消息提示：用户向上滚动时累计未读消息数，显示 pill
var _newMsgCount = 0;
var _scrollBtn = document.getElementById('scrollBottomBtn');
var _scrollFloatBtn = document.getElementById('scrollFloatBtn');
var _pill = document.getElementById('newMsgPill');
var _pillCount = document.getElementById('newMsgCount');
function _isAtBottom() {
    if (!body) return true;
    return body.scrollHeight - body.scrollTop - body.clientHeight < 50;
}
if (body) {
    // scroll 节流（rAF）：高频滚动时避免每帧多次回调造成卡顿
    var _scrollRaf = null;
    body.addEventListener('scroll', function () {
        if (_scrollRaf) return;
        _scrollRaf = requestAnimationFrame(function () {
            _scrollRaf = null;
            if (_isAtBottom()) {
                _newMsgCount = 0;
                if (_pill) _pill.style.display = 'none';
                if (_scrollBtn) _scrollBtn.style.display = 'none';
                if (_scrollFloatBtn) _scrollFloatBtn.style.display = 'none';
            } else {
                // 用户主动向上滚动：显示工具栏按钮 + 右下角浮动「回到最新」
                if (_scrollBtn) _scrollBtn.style.display = '';
                if (_scrollFloatBtn) _scrollFloatBtn.style.display = '';
            }
        });
    }, { passive: true });
}
function scrollToBottom() {
    if (!body) return;
    body.scrollTop = body.scrollHeight;
    _newMsgCount = 0;
    if (_pill) _pill.style.display = 'none';
    if (_scrollFloatBtn) _scrollFloatBtn.style.display = 'none';
}
if (_pill) _pill.addEventListener('click', scrollToBottom);

function copyAllConsole() {
    if (!body) return;
    // 仅遍历元素子节点（跳过空白文本节点），避免每行多出空行
    var text = '';
    for (var i = 0; i < body.children.length; i++) {
        text += body.children[i].textContent + '\n';
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
            showToast('已复制全部输出', 'success');
        }).catch(function () { showToast('复制失败', 'error'); });
    } else {
        // 回退：使用临时 textarea（旧浏览器 / 非 https 环境）
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); showToast('已复制全部输出', 'success'); }
        catch (e) { showToast('复制失败', 'error'); }
        document.body.removeChild(ta);
    }
}

if (input) {
    input.addEventListener('keydown', function (e) {
        // 兼容 Android 键盘：部分输入法回车键 e.key 为 'Enter' 但部分为 keyCode 13
        if (e.key === 'Enter' || e.keyCode === 13) {
            var cmd = input.value;
            input.value = '';
            histIdx = -1;
            sendCommand(cmd);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (!history.length) return;
            if (histIdx === -1) histIdx = history.length - 1;
            else if (histIdx > 0) histIdx--;
            input.value = history[histIdx];
            moveCursorEnd(input);
        } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (histIdx === -1) return;
            if (histIdx < history.length - 1) { histIdx++; input.value = history[histIdx]; }
            else { histIdx = -1; input.value = ''; }
            moveCursorEnd(input);
        } else if (e.key === 'Tab') {
            e.preventDefault();
            complete();
        }
    });
    input.focus();
}

function escapeHtml(s) {
    // 同时转义 & < > " ' 以兼容元素内容与属性上下文
    return (s || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}
// 渲染收藏的命令为快捷发送 chips（点击即发送到控制台）
function renderFavs() {
    var el = document.getElementById('favStrip');
    if (!el) return;
    fetch('/api/favorites')
        .then(function (r) { return r.json(); })
        .then(function (d) {
            var cmds = d.commands || [];
            if (!cmds.length) {
                el.innerHTML = '<span class="fav-empty">暂无收藏命令 · 在「命令参考」页点 ★ 即可收藏，这里一键发送</span>';
                return;
            }
            // 通过 data-cmd 属性承载命令 + 事件委托，避免 onclick 字符串拼接导致的 XSS
            el.innerHTML = cmds.map(function (c) {
                return '<button type="button" class="fav-chip" data-cmd="' + escapeHtml(c) + '">' + escapeHtml(c) + '</button>';
            }).join('');
        })
        .catch(function () { el.innerHTML = ''; });
}
// 事件委托：点击 favStrip 内的 .fav-chip 时取出 data-cmd 并发送
(function () {
    var el = document.getElementById('favStrip');
    if (!el) return;
    el.addEventListener('click', function (e) {
        var btn = e.target;
        while (btn && btn !== el) {
            if (btn.classList && btn.classList.contains('fav-chip')) {
                var cmd = btn.getAttribute('data-cmd') || '';
                if (cmd) sendCommand(cmd);
                return;
            }
            btn = btn.parentNode;
        }
    });
})();

function clearConsole() { if (body) body.innerHTML = ''; }

/* ─── 命令历史下拉（最近 10 条，选择填入输入框） ─────────────── */
function toggleCmdHistory() {
    var dd = document.getElementById('cmdHistoryDropdown');
    if (!dd) return;
    if (!dd.hidden) { _closeCmdHistory(); return; }
    _renderCmdHistory();
    dd.hidden = false;
    var btn = document.getElementById('cmdHistoryBtn');
    if (btn) btn.setAttribute('aria-expanded', 'true');
}
function _closeCmdHistory() {
    var dd = document.getElementById('cmdHistoryDropdown');
    var btn = document.getElementById('cmdHistoryBtn');
    if (dd) dd.hidden = true;
    if (btn) btn.setAttribute('aria-expanded', 'false');
}
function _renderCmdHistory() {
    var dd = document.getElementById('cmdHistoryDropdown');
    if (!dd) return;
    if (!_cmdHistory.length) {
        dd.innerHTML = '<div class="cmd-history-empty">暂无历史命令</div>';
        return;
    }
    // 最近 10 条，倒序展示（最新在最上）
    var recent = _cmdHistory.slice(-10).reverse();
    var html = '';
    for (var i = 0; i < recent.length; i++) {
        html += '<button type="button" class="cmd-history-item">' + escapeHtml(recent[i]) + '</button>';
    }
    dd.innerHTML = html;
    var items = dd.querySelectorAll('.cmd-history-item');
    for (var j = 0; j < items.length; j++) {
        (function (item, cmd) {
            item.addEventListener('click', function () {
                if (input) { input.value = cmd; input.focus(); moveCursorEnd(input); }
                _closeCmdHistory();
            });
        })(items[j], recent[j]);
    }
}
// 点击外部关闭下拉
document.addEventListener('click', function (e) {
    var dd = document.getElementById('cmdHistoryDropdown');
    var btn = document.getElementById('cmdHistoryBtn');
    if (!dd || dd.hidden) return;
    if (dd.contains(e.target) || (btn && btn.contains(e.target))) return;
    _closeCmdHistory();
});

renderFavs();
