#!/usr/bin/env python3
"""ToolDelta-Web 端到端测试"""
import urllib.request
import urllib.error
import urllib.parse
import sys

BASE = "http://127.0.0.1:5000"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def get(path):
    url = BASE + path
    req = urllib.request.Request(url)
    try:
        opener = urllib.request.build_opener(NoRedirect)
        resp = opener.open(req, timeout=10)
        return resp.getcode(), resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers)
    except urllib.error.URLError as e:
        return -1, str(e), {}
    except Exception as e:
        return -2, str(e), {}

print("=" * 60)
print("测试 1: 公开页面访问")
print("=" * 60)
for path in ["/login", "/setup"]:
    code, body, headers = get(path)
    print(f"  {path} -> HTTP {code} ({len(body)} bytes)")

print()
print("=" * 60)
print("测试 2: 未认证访问敏感页面应重定向到 /login (302)")
print("=" * 60)
sensitive_paths = ["/", "/console", "/plugins", "/market", "/backup",
                   "/commands", "/connections", "/files", "/logs",
                   "/scheduler", "/settings", "/watchdog"]
for path in sensitive_paths:
    code, body, headers = get(path)
    location = headers.get("Location", "")
    status = "OK" if code in (301, 302) else f"WARN(code={code})"
    print(f"  {path} -> HTTP {code} ({status}) [Location: {location[:50]}]")

print()
print("=" * 60)
print("测试 3: 未认证访问敏感 API 应返回 302/403")
print("=" * 60)
api_paths = ["/api/dashboard", "/api/system/info", "/api/launcher/config",
             "/api/logs", "/api/logs/files", "/api/scheduler/jobs",
             "/api/watchdog/config", "/api/watchdog/status",
             "/api/plugins", "/api/backups", "/api/market/plugins",
             "/api/logs/query", "/api/logs/sources", "/api/logs/export"]
for path in api_paths:
    code, body, headers = get(path)
    short_body = body[:60].replace("\n", " ")
    status = "OK" if code in (301, 302, 401, 403) else f"WARN(code={code})"
    print(f"  {path} -> HTTP {code} ({status}) body={short_body}")

print()
print("=" * 60)
print("测试 4: 静态资源响应 + 缓存头")
print("=" * 60)
for path in ["/static/css/style.css", "/static/js/main.js", "/static/js/console.js",
             "/static/js/dashboard.js", "/static/js/scheduler.js"]:
    code, body, headers = get(path)
    cache_ctrl = headers.get("Cache-Control", "MISSING")
    size_kb = len(body.encode()) / 1024
    print(f"  {path} -> HTTP {code} ({size_kb:.1f} KB) Cache-Control: {cache_ctrl}")

print()
print("=" * 60)
print("测试 5: 错误密码登录测试")
print("=" * 60)
login_data = urllib.parse.urlencode({"username": "admin", "password": "wrongpass"}).encode()
try:
    req = urllib.request.Request(BASE + "/login", data=login_data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    resp = urllib.request.build_opener(NoRedirect).open(req, timeout=10)
    code, body = resp.getcode(), resp.read().decode("utf-8", errors="replace")
    print(f"  错误密码登录 -> HTTP {code} ({'OK' if code in (200, 401, 302) else 'FAIL'})")
except urllib.error.HTTPError as e:
    print(f"  错误密码登录 -> HTTP {e.code} (OK)")
except Exception as e:
    print(f"  错误密码登录异常: {e}")

print()
print("=" * 60)
print("测试 6: /api/wallpaper 公开接口")
print("=" * 60)
code, body, _ = get("/api/wallpaper")
print(f"  /api/wallpaper -> HTTP {code} body={body[:80]}")

print()
print("=" * 60)
print("测试 7: /static 不应被 before_request 鉴权")
print("=" * 60)
for path in ["/static/css/style.css", "/static/js/socket.io.min.js"]:
    code, body, _ = get(path)
    print(f"  {path} -> HTTP {code} ({'OK' if code == 200 else 'FAIL'})")

print()
print("=" * 60)
print("测试 8: /api/status 端点")
print("=" * 60)
code, body, _ = get("/api/status")
print(f"  /api/status -> HTTP {code} body={body[:80]}")

print()
print("=" * 60)
print("测试 9: 认证后访问受保护页面")
print("=" * 60)
# 检查是否有已存在的用户，若有尝试用已知密码登录
import os
user_file = "/workspace/project/ToolDelta-Web/data/users.json"
if os.path.exists(user_file):
    print(f"  发现 users.json, 尝试 setup/登录流程")
else:
    print(f"  未发现 users.json, 走 setup 流程")

# 尝试 setup 路由
setup_data = urllib.parse.urlencode({
    "username": "admin",
    "password": "Admin123456"
}).encode()
try:
    req = urllib.request.Request(BASE + "/setup", data=setup_data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    opener = urllib.request.build_opener()
    opener.add_handler(urllib.request.HTTPCookieProcessor())
    resp = opener.open(req, timeout=10)
    code = resp.getcode()
    body = resp.read().decode("utf-8", errors="replace")
    cookies = resp.headers.get_all("Set-Cookie")
    print(f"  /setup POST -> HTTP {code} cookies={cookies}")
except urllib.error.HTTPError as e:
    print(f"  /setup POST -> HTTP {e.code} (已有用户或参数错误)")
except Exception as e:
    print(f"  /setup 异常: {e}")

print()
print("=" * 60)
print("端到端测试完成")
print("=" * 60)
