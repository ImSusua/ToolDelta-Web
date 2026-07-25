#!/usr/bin/env python3
"""ToolDelta-Web 认证后端到端测试 (使用正确 API 端点)"""
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
import json
import time

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

USERNAME = "admin"
PASSWORD = "Admin123456"

print("=" * 60)
print("阶段 1: 创建/登录管理员账号")
print("=" * 60)
# 先尝试 setup（仅首次配置可用）；若已配置则改走 /api/login
code, body, _ = post_json("/api/setup", {"username": USERNAME, "password": PASSWORD})
print(f"  POST /api/setup -> HTTP {code} body={body[:120]}")
if code != 200 or '"success":true' not in body:
    # 已配置：直接登录
    print(f"  setup 不可用(已配置或被拦截)，改走 /api/login")
    code, body, _ = post_json("/api/login", {"username": USERNAME, "password": PASSWORD})
    print(f"  POST /api/login -> HTTP {code} body={body[:120]}")
print(f"  Cookie 数: {len(list(cj))}")
for c in list(cj):
    print(f"    Cookie: {c.name}={c.value[:30]}...")

print()
print("=" * 60)
print("阶段 2: 已认证访问所有页面")
print("=" * 60)
page_results = {}
for path in ["/", "/console", "/plugins", "/market", "/backup",
             "/commands", "/connections", "/files", "/logs",
             "/scheduler", "/settings", "/watchdog"]:
    code, body, _ = get(path)
    status = "OK" if code == 200 else f"FAIL(code={code})"
    page_results[path] = code
    print(f"  {path} -> HTTP {code} ({status}) {len(body)}B")

ok_count = sum(1 for c in page_results.values() if c == 200)
print(f"\n  通过率: {ok_count}/{len(page_results)}")

print()
print("=" * 60)
print("阶段 3: 已认证访问敏感 API (role=admin)")
print("=" * 60)
api_results = {}
api_paths = ["/api/dashboard", "/api/system/info", "/api/launcher/config",
             "/api/logs", "/api/logs/files", "/api/scheduler/jobs",
             "/api/watchdog/config", "/api/watchdog/status",
             "/api/plugins", "/api/backups", "/api/market/plugins",
             "/api/logs/query", "/api/logs/sources", "/api/status",
             "/api/commands"]
for path in api_paths:
    code, body, _ = get(path)
    short_body = body[:60].replace("\n", " ")
    status = "OK" if code == 200 else f"FAIL(code={code})"
    api_results[path] = code
    print(f"  {path} -> HTTP {code} ({status}) body={short_body}")

ok_count = sum(1 for c in api_results.values() if c == 200)
print(f"\n  通过率: {ok_count}/{len(api_results)}")

print()
print("=" * 60)
print("阶段 4: 关键功能验证")
print("=" * 60)

# 4.1 dashboard 返回字段
code, body, _ = get("/api/dashboard")
if code == 200:
    try:
        d = json.loads(body)
        info = d.get("info", d)
        if isinstance(info, dict):
            print(f"  /api/dashboard 字段: {list(info.keys())[:10]}")
    except Exception as e:
        print(f"  /api/dashboard 解析失败: {e}")

# 4.2 system info 字段
code, body, _ = get("/api/system/info")
if code == 200:
    try:
        d = json.loads(body)
        info = d.get("info", d)
        if isinstance(info, dict):
            print(f"  /api/system/info 字段: {list(info.keys())}")
            print(f"    python_version: {info.get('python_version', 'N/A')}")
            print(f"    tooldelta_dir: {info.get('tooldelta_dir', 'N/A')[:60]}")
    except Exception as e:
        print(f"  /api/system/info 解析失败: {e}")

# 4.3 launcher config 字段
code, body, _ = get("/api/launcher/config")
if code == 200:
    try:
        d = json.loads(body)
        data = d.get("data", d)
        if isinstance(data, dict):
            print(f"  /api/launcher/config 字段: {list(data.keys())[:10]}")
    except Exception as e:
        print(f"  /api/launcher/config 解析失败: {e}")

# 4.4 /api/tool/output
code, body, _ = get("/api/tool/output?tail=10&html=1")
print(f"  /api/tool/output -> HTTP {code} body={body[:80]}")

# 4.5 /api/status
code, body, _ = get("/api/status")
print(f"  /api/status -> HTTP {code} body={body[:80]}")

print()
print("=" * 60)
print("阶段 5: 登出后再次访问应重定向")
print("=" * 60)
# 清空 cookie 模拟登出
cj.clear()
code, body, _ = get("/api/dashboard")
status = "OK" if code in (301, 302) else f"FAIL(code={code})"
print(f"  登出后 /api/dashboard -> HTTP {code} ({status})")

print()
print("=" * 60)
print("阶段 6: 错误密码登录失败")
print("=" * 60)
code, body, _ = post_json("/api/login", {"username": USERNAME, "password": "wrong"})
print(f"  错误密码登录 -> HTTP {code} body={body[:100]}")

print()
print("=" * 60)
print("阶段 7: 重新登录验证")
print("=" * 60)
code, body, _ = post_json("/api/login", {"username": USERNAME, "password": PASSWORD})
print(f"  正确密码登录 -> HTTP {code} body={body[:100]}")

print()
print("=" * 60)
print(f"总结: 页面 {sum(1 for c in page_results.values() if c == 200)}/{len(page_results)}, API {sum(1 for c in api_results.values() if c == 200)}/{len(api_results)}")
print("=" * 60)
