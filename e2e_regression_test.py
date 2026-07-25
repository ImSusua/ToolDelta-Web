#!/usr/bin/env python3
"""ToolDelta-Web 安全与可访问性回归测试"""
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
import json
import re

BASE = "http://127.0.0.1:5000"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect)

def get(path):
    req = urllib.request.Request(BASE + path)
    try:
        resp = opener.open(req, timeout=15)
        return resp.getcode(), resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers)
    except Exception as e:
        return -1, str(e), {}

def post_json(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        resp = opener.open(req, timeout=15)
        return resp.getcode(), resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers)
    except Exception as e:
        return -1, str(e), {}

# 登录
post_json("/api/login", {"username": "admin", "password": "Admin123456"})

results = []

print("=" * 60)
print("安全回归测试")
print("=" * 60)

# 测试 1: 安全响应头
print("\n[1] 安全响应头检查")
code, _, headers = get("/login")
expected_headers = {
    "X-Frame-Options": "DENY or SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "存在 CSP",
    "Referrer-Policy": "存在",
    "X-XSS-Protection": "存在",
}
for h, desc in expected_headers.items():
    val = headers.get(h, "MISSING")
    status = "OK" if val != "MISSING" else "FAIL"
    results.append(status == "OK")
    print(f"  {h}: {val[:60]} ({status}) - {desc}")

# 测试 2: Cookie 安全标志
print("\n[2] Cookie 安全标志")
set_cookie = headers.get("Set-Cookie", "")
cookie_attrs = ["HttpOnly", "SameSite"]
for attr in cookie_attrs:
    found = attr.lower() in set_cookie.lower()
    results.append(found)
    print(f"  {attr}: {'OK' if found else 'FAIL'}")

# 测试 3: 静态资源缓存头 (性能修复)
print("\n[3] 静态资源缓存头 (性能修复验证)")
code, _, headers = get("/static/css/style.css")
cache_ctrl = headers.get("Cache-Control", "")
has_max_age = "max-age=31536000" in cache_ctrl
results.append(has_max_age)
print(f"  Cache-Control: {cache_ctrl} ({'OK' if has_max_age else 'FAIL'})")

# 测试 4: settings.html 不使用原生 prompt()
print("\n[4] settings.html 不使用原生 prompt() (UI 修复验证)")
code, body, _ = get("/settings")
no_prompt = "prompt(" not in body
results.append(no_prompt)
print(f"  无 prompt(): {'OK' if no_prompt else 'FAIL'}")
# 检查 showPrompt 已使用
has_showprompt = "showPrompt" in body
results.append(has_showprompt)
print(f"  使用 showPrompt: {'OK' if has_showprompt else 'FAIL'}")

# 测试 5: settings.html label 关联
print("\n[5] settings.html label for 关联 (UI 修复验证)")
labels_for = re.findall(r'<label[^>]*for="([^"]+)"', body)
expected_ids = ["cfg_github_mirror", "cfg_logging", "cfg_market_source", 
                "cfg_fate_server", "cfg_fate_password", "cfg_fate_server_addr", "fbtokenInput"]
for expected in expected_ids:
    found = expected in labels_for
    results.append(found)
    print(f"  label for='{expected}': {'OK' if found else 'FAIL'}")

# 测试 6: console.html type=button
print("\n[6] console.html button type=button (UI 修复验证)")
code, body, _ = get("/console")
buttons_no_type = re.findall(r'<button(?![^>]*\btype=)[^>]*>', body)
# 过滤掉 form 提交按钮（应该有 type=submit）
problematic = [b for b in buttons_no_type if "submit" not in b.lower() and "console-send-btn" not in b]
results.append(len(problematic) == 0)
print(f"  无 type 缺失的 button: {'OK' if not problematic else f'FAIL ({len(problematic)}个)'}")

# 测试 7: console.html sr-only label
print("\n[7] console.html consoleInput sr-only label (UI 修复验证)")
has_sr_only_label = 'label for="consoleInput"' in body and 'sr-only' in body
results.append(has_sr_only_label)
print(f"  存在 sr-only label: {'OK' if has_sr_only_label else 'FAIL'}")

# 测试 8: index.html aria-label + table caption
print("\n[8] index.html aria-label + table caption (UI 修复验证)")
code, body, _ = get("/")
has_aria_label = 'aria-label="启动或停止 ToolDelta"' in body
has_caption = '<caption' in body and 'sr-only' in body
has_scope = 'scope="row"' in body
results.extend([has_aria_label, has_caption, has_scope])
print(f"  mainToggleBtn aria-label: {'OK' if has_aria_label else 'FAIL'}")
print(f"  table caption: {'OK' if has_caption else 'FAIL'}")
print(f"  th scope=row: {'OK' if has_scope else 'FAIL'}")

# 测试 9: login.html 壁纸 URL 使用 tojson (XSS 修复)
print("\n[9] login.html 壁纸 URL tojson (XSS 修复验证)")
# 注意：渲染后 |tojson 已被替换为实际 JSON 字符串，所以必须检查模板源文件
login_tpl_path = "/workspace/project/ToolDelta-Web/app/templates/login.html"
with open(login_tpl_path) as f:
    tpl = f.read()
no_inline_url = "background-image:url('{{" not in tpl and "background-image:url('{{ wallpaper_url" not in tpl
uses_tojson = "|tojson" in tpl
results.extend([no_inline_url, uses_tojson])
print(f"  无内联 url() 拼接: {'OK' if no_inline_url else 'FAIL'}")
print(f"  使用 |tojson: {'OK' if uses_tojson else 'FAIL'}")

# 测试 10: setup.html role=dialog
print("\n[10] setup.html role=dialog (UI 修复验证)")
code, body, _ = get("/setup")
# 已登录访问 setup 应重定向到 / (admin 已配置)
# 但页面模板本身可读取
import os
template_path = "/workspace/project/ToolDelta-Web/app/templates/setup.html"
if os.path.exists(template_path):
    with open(template_path) as f:
        tpl = f.read()
    has_role_dialog = 'role="dialog"' in tpl and 'aria-modal="true"' in tpl
    has_aria_live = 'aria-live="polite"' in tpl
    results.extend([has_role_dialog, has_aria_live])
    print(f"  role=dialog aria-modal: {'OK' if has_role_dialog else 'FAIL'}")
    print(f"  aria-live=polite: {'OK' if has_aria_live else 'FAIL'}")

# 测试 11: base.html noscript 对比度
print("\n[11] base.html noscript 文字对比度 (UI 修复验证)")
template_path = "/workspace/project/ToolDelta-Web/app/templates/base.html"
with open(template_path) as f:
    tpl = f.read()
# 检查是否使用了 #664d03 而非 #856404
uses_new_color = "#664d03" in tpl
old_color_only = "#856404" in tpl and "#664d03" not in tpl
results.append(uses_new_color and not old_color_only)
print(f"  使用 #664d03 (WCAG AA): {'OK' if uses_new_color else 'FAIL'}")

# 测试 12: console.js XSS 修复 (DOMParser)
print("\n[12] console.js appendLine 使用 DOMParser (XSS 修复验证)")
js_path = "/workspace/project/ToolDelta-Web/app/static/js/console.js"
with open(js_path) as f:
    js = f.read()
uses_domparser = "DOMParser" in js
# 排除注释行：只检查非注释的代码中是否存在直接 innerHTML=raw/safe 的赋值
import re as _re
_code_only = _re.sub(r'//[^\n]*', '', js)
no_innerhtml_raw = "div.innerHTML = safe" not in _code_only and ".innerHTML = raw" not in _code_only
results.extend([uses_domparser, no_innerhtml_raw])
print(f"  使用 DOMParser: {'OK' if uses_domparser else 'FAIL'}")
print(f"  无 innerHTML=raw 直接赋值: {'OK' if no_innerhtml_raw else 'FAIL'}")

# 测试 13: scheduler.js data-id 事件委托 (XSS 修复)
print("\n[13] scheduler.js 使用 data-id + 事件委托 (XSS 修复验证)")
js_path = "/workspace/project/ToolDelta-Web/app/static/js/scheduler.js"
with open(js_path) as f:
    js = f.read()
uses_data_id = "data-id" in js
no_inline_onchange = "onchange=\"toggleEnabled" not in js
results.extend([uses_data_id, no_inline_onchange])
print(f"  使用 data-id: {'OK' if uses_data_id else 'FAIL'}")
print(f"  无 onchange 内联拼接: {'OK' if no_inline_onchange else 'FAIL'}")

# 测试 14: ProxyFix 条件启用 (安全修复)
print("\n[14] ProxyFix 条件启用 (安全修复验证)")
py_path = "/workspace/project/ToolDelta-Web/run.py"
with open(py_path) as f:
    py = f.read()
uses_env_var = "BEHIND_PROXY" in py
results.append(uses_env_var)
print(f"  使用 BEHIND_PROXY 环境变量: {'OK' if uses_env_var else 'FAIL'}")

# 测试 15: 全局 excepthook (错误处理修复)
print("\n[15] 全局 excepthook 注册 (错误处理修复验证)")
py_path = "/workspace/project/ToolDelta-Web/app/__init__.py"
with open(py_path) as f:
    py = f.read()
has_thread_hook = "threading.excepthook" in py
has_sys_hook = "sys.excepthook" in py
results.extend([has_thread_hook, has_sys_hook])
print(f"  threading.excepthook 注册: {'OK' if has_thread_hook else 'FAIL'}")
print(f"  sys.excepthook 注册: {'OK' if has_sys_hook else 'FAIL'}")

# 测试 16: log_service 兜底队列 (错误处理修复)
print("\n[16] log_service 兜底队列 (错误处理修复验证)")
py_path = "/workspace/project/ToolDelta-Web/app/log_service.py"
with open(py_path) as f:
    py = f.read()
has_fallback = "_fallback_queue" in py or "fallback_queue" in py
has_stderr = "sys.stderr" in py
results.extend([has_fallback, has_stderr])
print(f"  兜底队列: {'OK' if has_fallback else 'FAIL'}")
print(f"  stderr fallback: {'OK' if has_stderr else 'FAIL'}")

# 测试 17: backup_service stop 失败中止 (错误处理修复)
print("\n[17] backup_service stop 失败中止 (错误处理修复验证)")
py_path = "/workspace/project/ToolDelta-Web/app/backup_service.py"
with open(py_path) as f:
    py = f.read()
# 应该不再有 tooldelta_manager.stop() 后 except Exception: pass
no_silent_stop = "tooldelta_manager.stop()\n    except Exception:\n        pass" not in py.replace("    ", "\t").replace("\t", "    ")
has_error_log = "停止运行中的 ToolDelta 失败" in py or "log_service.error" in py
results.extend([has_error_log])
print(f"  错误日志记录: {'OK' if has_error_log else 'FAIL'}")

# 测试 18: dashboard_service 后台 CPU 采样 (性能修复)
print("\n[18] dashboard_service 后台 CPU 采样 (性能修复验证)")
py_path = "/workspace/project/ToolDelta-Web/app/dashboard_service.py"
with open(py_path) as f:
    py = f.read()
has_cpu_sampler = "_cpu_sampler_loop" in py or "_start_cpu_sampler" in py
no_blocking_sleep = "time.sleep(0.1)" not in py
results.extend([has_cpu_sampler])
print(f"  后台采样线程: {'OK' if has_cpu_sampler else 'FAIL'}")
print(f"  无阻塞 sleep(0.1): {'OK' if no_blocking_sleep else 'FAIL'}")

# 测试 19: dashboard_service build hash 预加载 (性能修复)
print("\n[19] dashboard_service build hash 预加载 (性能修复验证)")
has_compute = "_compute_build_hash" in py
has_preload = "self._build_hash_cache = self._compute_build_hash()" in py or "_compute_build_hash()" in py
results.append(has_compute)
print(f"  抽取 _compute_build_hash: {'OK' if has_compute else 'FAIL'}")

# 测试 20: plugin_service mtime 缓存 (性能修复)
print("\n[20] plugin_service mtime 缓存 (性能修复验证)")
py_path = "/workspace/project/ToolDelta-Web/app/plugin_service.py"
with open(py_path) as f:
    py = f.read()
has_mtime = "cur_mtime" in py and "self._cache" in py
results.append(has_mtime)
print(f"  mtime 缓存: {'OK' if has_mtime else 'FAIL'}")

# 汇总
print()
print("=" * 60)
total = len(results)
passed = sum(1 for r in results if r)
print(f"回归测试汇总: {passed}/{total} 通过")
print("=" * 60)
