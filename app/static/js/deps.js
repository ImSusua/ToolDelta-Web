// 依赖安装进度 UI：高斯模糊遮罩 + 安装方式选择 + 动画进度条 + 实时日志
// 性能优化：复用全局 socket（避免重复 long-polling 连接）、按钮防双击、fetch 超时。
(function () {
  var overlay = document.getElementById('depOverlay');
  if (!overlay) return;
  var bar = document.getElementById('depBar');
  var percent = document.getElementById('depPercent');
  var stage = document.getElementById('depStage');
  var mirror = document.getElementById('depMirror');
  var log = document.getElementById('depLog');
  var actions = document.getElementById('depActions');
  var choose = document.getElementById('depChoose');
  var run = document.getElementById('depRun');
  var title = document.getElementById('depTitle');
  var dialog = overlay.querySelector('.dep-card');

  // 安装完成后是否已自动隐藏过，避免 socket 重复推送时再次弹遮罩
  var _completed = false;
  // 安装按钮防双击锁
  var _installing = false;

  function showOverlay() { overlay.classList.add('active'); }
  function hideOverlay() { overlay.classList.remove('active'); }
  function showRun() {
    choose.style.display = 'none'; run.style.display = 'block';
    // 切到进度步骤时，aria-labelledby 指向 depTitle（进度步骤标题）
    if (dialog) dialog.setAttribute('aria-labelledby', 'depTitle');
  }
  function showChoose() {
    run.style.display = 'none';
    choose.style.display = 'block';
    actions.style.display = 'none';
    // 切到选择步骤时，aria-labelledby 指向 depTitleChoose（选择步骤标题），避免悬空引用
    if (dialog) dialog.setAttribute('aria-labelledby', 'depTitleChoose');
  }

  function applyDep(d) {
    if (!d || _completed) return;
    // 防御：若关键 DOM 元素缺失（如模板裁剪），跳过更新避免 TypeError
    if (!bar || !percent || !stage || !mirror) return;
    var p = Math.max(0, Math.min(100, d.progress || 0));
    bar.style.width = p + '%';
    percent.textContent = p + '%';
    stage.textContent = d.stage || (d.ready ? '依赖已就绪' : '准备中…');
    mirror.textContent = d.mirror || (d.status === 'installing' ? '测速中…' : '—');
    if (d.log_tail && d.log_tail.length) {
      if (log) {
        log.textContent = d.log_tail.join('\n');
        log.scrollTop = log.scrollHeight;
      }
    }
    if (d.ready) {
      _completed = true;
      hideOverlay();
    } else {
      showOverlay();
      showRun();
      if (actions) actions.style.display = (d.status === 'failed') ? 'flex' : 'none';
    }
  }

  // 返回「选择安装方式」卡片
  window.showDepChoose = function () {
    showChoose();
    showOverlay();
  };

  // 触发安装：mode = 'local' | 'online' | undefined（沿用上次 / 自动）
  window.triggerDepInstall = function (mode) {
    // 防双击：安装进行中再次点击直接忽略
    if (_installing) return;
    _installing = true;
    // 0.6s 后自动解锁，允许重试（失败场景）
    setTimeout(function () { _installing = false; }, 600);

    showRun();
    showOverlay();
    actions.style.display = 'none';
    stage.textContent = '正在启动安装…';
    if (title) title.textContent = '正在安装运行依赖';
    var opts = { method: 'POST' };
    if (mode) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify({ mode: mode });
    }
    // 使用 tdFetch 统一超时与错误归类（若不可用则回退原生 fetch）
    var f = (window.tdFetch || fetch)('/api/dependencies/install', opts, 30000);
    f.then(function (r) { return r.json(); })
      .then(applyDep)
      .catch(function (e) {
        var msg = (e && e.userMessage) ? e.userMessage : '触发安装失败，请刷新后重试';
        stage.textContent = msg;
      });
  };

  // 实时进度（socket）：延迟到 DOMContentLoaded 后注册，以便复用页面上已建立的 io 连接
  // （console.js 在 base.html 的 {% block scripts %} 中加载，晚于 deps.js；
  //  等 DOM 解析完成后 window.socket 已就绪，可复用同一 polling 连接，节省一条长轮询）
  function _registerDepSocket() {
    try {
      // 双重防御：window.socket 不可用且 io 全局未定义（如 socket.io.min.js 加载失败）时，
      // 静默降级为仅轮询拉取，避免抛 ReferenceError 阻断初始化
      var s;
      if (window.socket && window.socket.io) s = window.socket;
      else if (typeof window.io === 'function') s = window.io({ transports: ['polling'] });
      else return;
      s.on('dependency_progress', function (d) { applyDep(d); });
    } catch (e) { /* 静默：socket 不可用时降级为仅轮询拉取 */ }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _registerDepSocket);
  } else {
    setTimeout(_registerDepSocket, 0);
  }

  // 初始拉取状态，决定是否弹出遮罩 / 选择卡片
  // 使用 tdFetch 统一超时（若不可用则回退原生 fetch）
  var f2 = (window.tdFetch || fetch)('/api/dependencies', null, 8000);
  f2.then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.ready) { hideOverlay(); return; }
      if (d.status === 'installing') {
        applyDep(d);            // 已在安装中：直接显示进度
      } else {
        // idle / failed：展示「选择安装方式」卡片
        showOverlay();
        showChoose();
      }
    })
    .catch(function () { /* 未登录或非 JSON 响应：不弹遮罩 */ });
})();
