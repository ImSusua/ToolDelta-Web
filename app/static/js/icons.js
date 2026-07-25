/* ────────────────────────────────────────────────────────────────
   icons.js — 统一 SVG icon system（参考 lucide-react / octicons / geist-icons）
   线性图标，1.5px stroke，currentColor，24x24 viewBox。
   路径数据来源 lucide.dev（MIT 协议），保证视觉与原版一致。
   ──────────────────────────────────────────────────────────────── */
window.tdIcons = (function() {
  /* SVG 路径定义（每个图标是 paths 字符串，不含外层 <svg>） */
  var ICONS = {
    /* ── 导航（12） ── */
    'dashboard': '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    'terminal': '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
    'puzzle': '<path d="M19.439 7.85c-.049.322.059.848.394 1.417.247.42.6.708.993.908.404.205.84.375 1.224.575.494.254.728.662.728 1.162v2.5c0 .552-.448 1-1 1h-2c-.552 0-1-.448-1-1 0-.552-.448-1-1-1h-2c-.552 0-1 .448-1 1v3c0 .552-.448 1-1 1h-2c-.552 0-1-.448-1-1 0-.552-.448-1-1-1h-2c-.552 0-1 .448-1 1 0 .552-.448 1-1 1h-2c-.552 0-1-.448-1-1v-3c0-.552.448-1 1-1h2c.552 0 1-.448 1-1v-2c0-.552-.448-1-1-1h-2c-.552 0-1-.448-1-1v-2.5c0-.5.234-.908.728-1.162.384-.2.82-.37 1.224-.575.393-.2.746-.488.993-.908.335-.569.443-1.095.394-1.417a2.5 2.5 0 1 1 4.946-.747c.05.322-.059.848-.394 1.417-.247.42-.6.708-.993.908-.404.205-.84.375-1.224.575-.494.254-.728.662-.728 1.162v2.5c0 .552.448 1 1 1h2c.552 0 1-.448 1-1 0-.552.448-1 1-1h2c.552 0 1 .448 1 1 0 .552.448 1 1 1h2c.552 0 1-.448 1-1v-2.5c0-.5.234-.908.728-1.162.384-.2.82-.37 1.224-.575.393-.2.746-.488.993-.908.335-.569.443-1.095.394-1.417a2.5 2.5 0 1 1 4.946-.747z"/>',
    'shopping-cart': '<circle cx="8" cy="21" r="1"/><circle cx="19" cy="21" r="1"/><path d="M2.05 2.05h2l2.66 12.42a2 2 0 0 0 2 1.58h9.78a2 2 0 0 0 1.95-1.57l1.65-7.43H5.12"/>',
    'folder': '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    'keyboard': '<path d="M10 8h.01"/><path d="M12 12h.01"/><path d="M14 8h.01"/><path d="M16 12h.01"/><path d="M18 8h.01"/><path d="M6 8h.01"/><path d="M7 16h10"/><path d="M8 12h.01"/><rect width="20" height="16" x="2" y="4" rx="2"/>',
    'archive': '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
    'scroll-text': '<path d="M15 12h-5"/><path d="M15 8h-5"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/>',
    'globe': '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    'dog': '<path d="M10 5.172C10 3.782 8.423 2.679 6.5 3c-2.823.47-4.113 6.006-4 7 .08.703 1.725 1.722 3.656 1 1.261-.472 1.96-1.45 2.344-2.5"/><path d="M14.267 5.172c0-1.39 1.577-2.493 3.5-2.172 2.823.47 4.113 6.006 4 7-.08.703-1.725 1.722-3.656 1-1.261-.472-1.855-1.45-2.239-2.5"/><path d="M8 14v.5"/><path d="M16 14v.5"/><path d="M11.25 16.25h1.5L12 17l-.75-.75Z"/><path d="M4.5 13.5c1.5-1 3.5-1 5 .5 1 1 1 2.5 2 2.5s1-1.5 2-2.5c1.5-1.5 3.5-1.5 5-.5"/><path d="M5 8a3 3 0 0 1 2.5-2.5c1.5 0 2.5 1 3.5 1s2-1 3.5-1A3 3 0 0 1 19 8c0 1.5-1 2.5-2 3.5-1.5 1.5-2 3-2 5v2a6 6 0 0 1-6 6c-2 0-3-1-3-3v-4c0-2-.5-3.5-2-5C5 10.5 4 9.5 4.5 8Z"/>',
    'clock': '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    'settings': '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',

    /* ── 其他 emoji 替换（3） ── */
    'bell': '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    'package': '<path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    'wifi-off': '<path d="M12 20h.01"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/><path d="M5 12.859a10 10 0 0 1 5.17-2.69"/><path d="M19 12.859a10 10 0 0 0-2.007-1.523"/><path d="M2 8.82a15 15 0 0 1 4.177-2.643"/><path d="M22 8.82a15 15 0 0 0-11.288-3.764"/><path d="m2 2 20 20"/>',

    /* ── 主题切换（3） ── */
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    'moon': '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    'monitor': '<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',

    /* ── 认证（4） ── */
    'user': '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    'lock': '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    'eye': '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
    'eye-off': '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"/><path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/><path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143"/><path d="m2 2 20 20"/>',

    /* ── 通用反馈（5） ── */
    'copy': '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    'check': '<polyline points="20 6 9 17 4 12"/>',
    'x': '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    'alert-triangle': '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" x2="12" y1="9" y2="13"/><line x1="12" x2="12.01" y1="17" y2="17"/>',
    'info': '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',

    /* ── 表单 / 按钮（12） ── */
    'chevron-down': '<path d="m6 9 6 6 6-6"/>',
    'arrow-up': '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
    'refresh-cw': '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    'play': '<polygon points="6 3 20 12 6 21 6 3"/>',
    'stop': '<rect width="18" height="18" x="3" y="3" rx="2"/>',
    'plus': '<path d="M5 12h14"/><path d="M12 5v14"/>',
    'trash': '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>',
    'edit': '<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"/>',
    'download': '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>',
    'upload': '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>',
    'search': '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    'star': '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
    'command': '<path d="M15 6v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3"/>',
    'list': '<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M8 6h13"/>'
  };

  /* 主题切换器 data-current → icon name 映射 */
  var THEME_ICON_MAP = { dark: 'moon', light: 'sun', system: 'monitor' };

  function get(name, opts) {
    opts = opts || {};
    var size = opts.size || 18;
    var cls = opts.class ? ' class="' + opts.class + '"' : '';
    var strokeW = opts.strokeWidth || 1.5;
    var paths = ICONS[name];
    if (!paths) return '';
    return '<svg xmlns="http://www.w3.org/2000/svg" width="' + size + '" height="' + size +
      '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' + strokeW +
      '" stroke-linecap="round" stroke-linejoin="round"' + cls +
      ' aria-hidden="true" focusable="false">' + paths + '</svg>';
  }

  /* 渲染单个 [data-icon] 元素 */
  function renderIcon(el) {
    var name = el.getAttribute('data-icon');
    var sizeAttr = el.getAttribute('data-icon-size');
    var size = sizeAttr ? parseInt(sizeAttr, 10) : 18;
    var cls = el.getAttribute('data-icon-class') || '';
    el.innerHTML = get(name, { size: size, class: cls });
  }

  /* 渲染主题切换器内部 .theme-icon（依据 .theme-toggle[data-current]） */
  function renderThemeIcon(toggle) {
    if (!toggle) return;
    var mode = toggle.getAttribute('data-current') || 'dark';
    var name = THEME_ICON_MAP[mode] || 'moon';
    var box = toggle.querySelector('.theme-icon');
    if (box) box.innerHTML = get(name, { size: 16 });
  }

  /* 批量渲染页面中所有 [data-icon] 元素 + 主题图标 */
  function renderAll() {
    document.querySelectorAll('[data-icon]').forEach(renderIcon);
    document.querySelectorAll('.theme-toggle').forEach(renderThemeIcon);
  }

  /* 监听 .theme-toggle 的 data-current 变化，动态更新图标
     （setTheme() 切换主题时无需手动调用 renderAll） */
  function watchThemeToggle() {
    var toggles = document.querySelectorAll('.theme-toggle');
    toggles.forEach(function(toggle) {
      if (toggle._tdIconObserved) return;
      toggle._tdIconObserved = true;
      var obs = new MutationObserver(function() { renderThemeIcon(toggle); });
      obs.observe(toggle, { attributes: true, attributeFilter: ['data-current'] });
    });
  }

  /* 暴露 tdIcon(name) 便捷别名 —— 等价于 tdIcons.get(name) */
  function tdIcon(name, opts) { return get(name, opts); }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() {
      renderAll();
      watchThemeToggle();
    });
  } else {
    renderAll();
    watchThemeToggle();
  }

  return { get: get, renderAll: renderAll, ICONS: ICONS, THEME_ICON_MAP: THEME_ICON_MAP };
})();

/* 便捷别名：window.tdIcon(name) → SVG 字符串 */
window.tdIcon = function(name, opts) { return window.tdIcons.get(name, opts); };
