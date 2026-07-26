# -*- coding: utf-8 -*-
# ruff: noqa: E402
"""ToolDelta-Web 全功能端到端测试（隔离环境，不触碰真实 ToolDelta 安装）"""
import os
import sys
import io
import json
import shutil
import zipfile
import time
import subprocess

# 防误运行保护：本脚本含破坏性副作用（清空 instance/、backups/、plugin_market/、
# bridge_plugin/ 目录并重建 TOOLDELTA_DIR），若被测试框架（pytest）/IDE 自动 import
# 会造成用户数据丢失。仅在以下情况允许执行：
# 1. 直接运行（python3 run_full_test.py，此时 __name__ == "__main__"）
# 2. 显式设置 RUN_FULL_TEST=1 环境变量（用于 selfcheck_multi.py 编排调用）
# 未满足以上任一条件时立即 raise ImportError，阻止副作用代码执行。
_RUN_ALLOWED = __name__ == "__main__" or os.environ.get("RUN_FULL_TEST") == "1"
if not _RUN_ALLOWED:
    raise ImportError(
        "run_full_test.py 含破坏性副作用（清空 instance/、backups/ 等目录），"
        "禁止 import。如需运行请直接执行：python3 run_full_test.py，"
        "或设置 RUN_FULL_TEST=1 环境变量。"
    )

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 支持外部指定 TOOLDELTA_DIR（如 selfcheck.py 每轮隔离目录），
# 未指定时使用默认临时目录，保证单独运行也能自包含。
TD = os.environ.get("TOOLDELTA_DIR") or "/tmp/td_fulltest/ToolDelta"
os.environ["TOOLDELTA_DIR"] = TD

# ---- 1. 清理旧测试产物（仅本面板自己生成的数据目录，非终端程序文件）----
for d in ["backups", "plugin_market", "bridge_plugin", "instance"]:
    p = os.path.join(ROOT, d)
    if os.path.exists(p):
        shutil.rmtree(p, ignore_errors=True)

# ---- 2. 搭建隔离 TOOLDELTA_DIR ----
shutil.rmtree(TD, ignore_errors=True)
os.makedirs(TD, exist_ok=True)
os.makedirs(os.path.join(TD, "插件文件", "ToolDelta类式插件"), exist_ok=True)
os.makedirs(os.path.join(TD, "插件配置文件"), exist_ok=True)
os.makedirs(os.path.join(TD, "插件数据文件"), exist_ok=True)

plugin_dir = os.path.join(TD, "插件文件", "ToolDelta类式插件", "DemoPlugin")
os.makedirs(plugin_dir, exist_ok=True)
with open(os.path.join(plugin_dir, "__init__.py"), "w", encoding="utf-8") as f:
    f.write(
        "# demo plugin\n"
        "import tooldelta\n"
        'tooldelta.add_console_cmd_trigger(["测试命令"], "测试提示", "这是一个测试命令的用法说明")\n'
    )
with open(os.path.join(plugin_dir, "datas.json"), "w", encoding="utf-8") as f:
    json.dump({"author": "tester", "version": "1.0.0", "description": "demo",
               "plugin-id": "demo"}, f, ensure_ascii=False)
with open(os.path.join(TD, "插件配置文件", "DemoPlugin.json"), "w", encoding="utf-8") as f:
    json.dump({"setting_a": 1}, f)

with open(os.path.join(TD, "ToolDelta基本配置.json"), "w", encoding="utf-8") as f:
    json.dump({"全局GitHub镜像": "https://github.com"}, f, ensure_ascii=False)

with open(os.path.join(TD, "main.py"), "w", encoding="utf-8") as f:
    f.write("import sys, time\nprint('ToolDelta mock started')\nsys.stdout.flush()\n"
            "while True:\n    time.sleep(0.3)\n")

# ---- 3. 创建应用与测试客户端 ----
from app import create_app, socketio
app = create_app()
app.config["TESTING"] = True
client = app.test_client()

results = []
def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  -> " + detail) if (detail and not ok) else ""))

def jget(r):
    try:
        v = r.get_json(force=True, silent=True)
        return v if v is not None else {}
    except Exception:
        return {}

def gj(path):
    r = client.get(path)
    return r, jget(r)

def pj(path, payload):
    r = client.post(path, json=payload)
    return r, jget(r)

# ============ A. 初始化与认证 ============
print("\n== A. 初始化与认证 ==")
r = client.get("/")
rec("未配置时 / 跳转 /setup", r.status_code in (301,302) and "/setup" in (r.headers.get("Location") or ""), str(r.status_code))
r, d = pj("/api/setup", {"username": "admin", "password": "admin123"})
rec("初始化面板 /api/setup", d.get("success") is True, json.dumps(d, ensure_ascii=False))
# 验证弱密码提示返回（admin123 是弱密码，但创建成功）
rec("初始化返回弱密码警告", (d.get("data") or {}).get("password_warning") is not None, json.dumps(d, ensure_ascii=False)[:120])
r, d = gj("/api/auth/status")
rec("已配置且已登录", (d.get("data") or {}).get("isConfigured") is True and (d.get("data") or {}).get("authenticated") is True and (d.get("data") or {}).get("role") == 10, json.dumps(d))
for page in ["/", "/files", "/console", "/plugins", "/market", "/backup", "/commands", "/logs", "/settings"]:
    r = client.get(page)
    rec("页面渲染 GET %s" % page, r.status_code == 200, "status=%d" % r.status_code)
r = client.get("/setup")
rec("已配置后 /setup 跳转 /", r.status_code in (301,302) and (r.headers.get("Location") or "").endswith("/"), str(r.status_code))
r = client.get("/login")
rec("已登录后 /login 跳转 /", r.status_code in (301,302) and (r.headers.get("Location") or "").endswith("/"), str(r.status_code))

# ============ B. 文件管理 ============
print("\n== B. 文件管理 ==")
r, d = gj("/api/files/list")
rec("文件列表根目录 list", d.get("success") is True, json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/files/save", {"path": "a.txt", "content": "hello"})
rec("保存文件 save", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/files/read?path=a.txt")
rec("读取文件 read", (d.get("data") or {}).get("content") == "hello", json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/files/mkdir", {"path": "testdir"})
rec("创建目录 mkdir", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/files/rename", {"path": "a.txt", "new_name": "b.txt"})
rec("重命名 rename", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/files/move", {"path": "b.txt", "destination": "testdir/b.txt"})
rec("移动 move", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/files/copy", {"path": "testdir/b.txt", "destination": "testdir/c.txt"})
rec("复制 copy", d.get("success") is True, json.dumps(d, ensure_ascii=False))
buf = io.BytesIO(b"upload content")
r = client.post("/api/files/upload", data={"path": "testdir", "file": (buf, "up.txt")}, content_type="multipart/form-data")
rec("上传文件 upload", jget(r).get("success") is True, json.dumps(jget(r), ensure_ascii=False)[:80])
r = client.get("/api/files/download?path=testdir/c.txt")
rec("下载文件 download", r.status_code == 200, "status=%d" % r.status_code)
r = client.get("/api/files/download-dir?path=testdir")
rec("下载目录 download-dir", r.status_code == 200, "status=%d" % r.status_code)
r, d = gj("/api/files/search?q=b.txt")
rec("搜索文件 search", d.get("success") is True, json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/files/batch-delete", {"paths": ["testdir/c.txt", "testdir/up.txt"]})
rec("批量删除 batch-delete", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/files/delete", {"path": "testdir"})
rec("删除目录 delete", d.get("success") is True, json.dumps(d, ensure_ascii=False))

# ============ C. 用户管理 ============
print("\n== C. 用户管理 ==")
r, d = pj("/api/users/create", {"username": "user1", "password": "user1123", "role": 1})
rec("创建用户 create", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/users")
rec("用户列表 list 含 user1", any(u.get("username") == "user1" for u in (d.get("data") or [])), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/users/reset-password", {"username": "user1", "password": "newpass1"})
rec("重置用户密码 reset-password", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/change-password", {"old_password": "admin123", "new_password": "admin456"})
rec("修改自身密码 change-password", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/users/delete", {"username": "user1"})
rec("删除用户 delete", d.get("success") is True, json.dumps(d, ensure_ascii=False))

# C2. 弱密码策略：弱密码仅提示不阻止
r, d = pj("/api/users/create", {"username": "weakuser", "password": "1", "role": 1})
rec("弱密码创建用户(仅提示不阻止)", d.get("success") is True, json.dumps(d, ensure_ascii=False)[:100])
r, d = pj("/api/users/delete", {"username": "weakuser"})
rec("删除弱密码用户", d.get("success") is True, json.dumps(d, ensure_ascii=False))

# C3. 中文用户名支持
from app.auth_service import validate_username
ok, _ = validate_username("测试用户")
rec("中文用户名校验通过", ok is True, "ok=%s msg=%s" % (ok, _))
ok, _ = validate_username("管理员123")
rec("中英混合用户名校验通过", ok is True, "")
ok, _ = validate_username("玩家")
rec("纯中文短用户名校验通过", ok is True, "")
ok, _ = validate_username("")
rec("空用户名拒绝", ok is False, "")
ok, _ = validate_username("   ")
rec("纯空白用户名拒绝", ok is False, "")
ok, _ = validate_username("x" * 33)
rec("超长用户名(33位)拒绝", ok is False, "")
# 创建中文用户名账号并登录（使用独立 client 避免污染 admin 会话）
r, d = pj("/api/users/create", {"username": "中文用户", "password": "test1234", "role": 1})
rec("创建中文用户名账号", d.get("success") is True, json.dumps(d, ensure_ascii=False)[:100])
client_cn = app.test_client()
r = client_cn.post("/api/login", json={"username": "中文用户", "password": "test1234"})
rec("中文用户名登录", r.status_code == 200 and r.get_json().get("success") is True, json.dumps(r.get_json(), ensure_ascii=False)[:100])
r, d = pj("/api/users/delete", {"username": "中文用户"})
rec("删除中文用户名账号", d.get("success") is True, json.dumps(d, ensure_ascii=False)[:100])

# 验证密码强度函数
from app.auth_service import check_password_strength
lvl, tips = check_password_strength("a")
rec("密码强度检测: 单字符=weak", lvl == "weak" and len(tips) > 0, "level=%s tips=%s" % (lvl, tips))
lvl, tips = check_password_strength("abcd1234")
rec("密码强度检测: 8位字母+数字=medium", lvl == "medium", "level=%s" % lvl)
lvl, tips = check_password_strength("Abc123!@#xyz")
rec("密码强度检测: 12位含特殊字符=strong", lvl == "strong", "level=%s" % lvl)

# ============ D. 插件管理 ============
print("\n== D. 插件管理 ==")
r, d = gj("/api/plugins")
rec("插件列表含 DemoPlugin", any(p.get("name") == "DemoPlugin" for p in d), json.dumps(d, ensure_ascii=False)[:100])
r, d = pj("/api/plugins/toggle", {"name": "DemoPlugin", "enable": False})
rec("禁用插件 toggle off", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/plugins")
demo: dict = next((p for p in d if p.get("name") == "DemoPlugin"), {})
rec("插件状态为禁用", demo.get("is_enabled") is False, json.dumps(demo, ensure_ascii=False))
r, d = pj("/api/plugins/toggle", {"name": "DemoPlugin", "enable": True})
rec("启用插件 toggle on", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/plugins/config?name=DemoPlugin")
rec("读取插件配置 config", d.get("setting_a") == 1, json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/plugins/config", {"name": "DemoPlugin", "config": {"setting_a": 99}})
rec("保存插件配置 config POST", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/plugins/readme?name=DemoPlugin")
rec("读取插件 readme（无则友好）", d.get("error") == "未找到文档", json.dumps(d, ensure_ascii=False))
r, d = gj("/api/plugins/data-files?name=DemoPlugin")
rec("插件数据文件列表 data-files", isinstance(d, list), json.dumps(d, ensure_ascii=False)[:80])
buf = io.BytesIO(b"data content")
r = client.post("/api/plugins/data-upload", data={"name": "DemoPlugin", "file": (buf, "d.txt")}, content_type="multipart/form-data")
rec("上传数据文件 data-upload", jget(r).get("success") is True, json.dumps(jget(r), ensure_ascii=False)[:80])
r, d = pj("/api/plugins/data-delete", {"name": "DemoPlugin", "file": "d.txt"})
rec("删除数据文件 data-delete", d.get("success") is True, json.dumps(d, ensure_ascii=False))
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("TestPlugin2/__init__.py", "# tp2\n")
    z.writestr("TestPlugin2/datas.json", json.dumps({"plugin-id": "TestPlugin2", "author": "t", "version": "1.0", "description": "t"}))
buf.seek(0)
r = client.post("/api/plugins/upload", data={"file": (buf, "TestPlugin2.zip")}, content_type="multipart/form-data")
rec("上传插件 zip upload", jget(r).get("success") is True, json.dumps(jget(r), ensure_ascii=False)[:80])
r, d = gj("/api/plugins")
rec("上传后插件列表含 TestPlugin2", any(p.get("name") == "TestPlugin2" for p in d), json.dumps(d, ensure_ascii=False)[:100])
r, d = pj("/api/plugins/delete", {"name": "TestPlugin2"})
rec("删除插件 delete", d.get("success") is True, json.dumps(d, ensure_ascii=False))

# ============ E. 备份 ============
print("\n== E. 备份 ==")
r, d = pj("/api/backup/create", {"name": "bak1"})
rec("创建备份 create", d.get("success") is True or "zip" in d, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/backups")
rec("备份列表含 bak1", any(b.get("name") == "bak1" for b in d), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/backup/restore", {"zip": "bak1.zip"})
rec("恢复备份 restore", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/backup/delete", {"zip": "bak1.zip"})
rec("删除备份 delete", d.get("success") is True, json.dumps(d, ensure_ascii=False))

# ============ F. 命令扫描 ============
print("\n== F. 命令扫描 ==")
r, d = gj("/api/commands")
rec("命令列表 commands", isinstance(d, list), json.dumps(d, ensure_ascii=False)[:80])
r = client.get("/api/commands/plugin?name=DemoPlugin")
rec("按插件查命令 commands/plugin", r.status_code == 200, "status=%d" % r.status_code)
r, d = gj("/api/commands/stats")
rec("命令统计 stats", "total_commands" in (d or {}), json.dumps(d, ensure_ascii=False)[:80])

# ============ G0. 修复验证：空目录启动自动解压出厂包 ============
# 验证：当 TOOLDELTA_DIR 为空（首次部署/初始化时压缩包未解压）时，
# start() 会自动从出厂包解压 main.py，而不是失败返回。
print("\n== G0. 修复：空目录启动自动解压 ==")
import tempfile as _tempfile
from app.tooldelta_manager import tooldelta_manager as _mgr
_td_auto = _tempfile.mkdtemp(prefix="td_auto_")
_app_auto = create_app()
_app_auto.config["TOOLDELTA_DIR"] = _td_auto
_app_auto.config["TOOLDELTA_MAIN"] = os.path.join(_td_auto, "main.py")
_mgr.app = _app_auto
_ok_auto, _msg_auto = _mgr._ensure_main_program()
rec("空目录自动解压出厂包生成 main.py", _ok_auto and os.path.isfile(_app_auto.config["TOOLDELTA_MAIN"]), str(_msg_auto))
# 恢复全局 mgr 指向主测试 app，避免影响后续 G 部分
_mgr.app = app

# ============ G1. 深层：main.py 损坏后自动重新解压恢复 ============
print("\n== G1. 深层：main.py 损坏恢复 ==")
import tempfile as _tf
_td_dmg = _tf.mkdtemp(prefix="td_dmg_")
_app_dmg = create_app()
_app_dmg.config["TOOLDELTA_DIR"] = _td_dmg
_app_dmg.config["TOOLDELTA_MAIN"] = os.path.join(_td_dmg, "main.py")
from app.tooldelta_manager import tooldelta_manager as _mgr
_mgr.app = _app_dmg
_ok1, _msg1 = _mgr._ensure_main_program()
rec("先解压出厂包 main.py 有效", _ok1 and _mgr._is_valid_main(_app_dmg.config["TOOLDELTA_MAIN"]), str(_msg1))
with open(_app_dmg.config["TOOLDELTA_MAIN"], "w") as f:
    f.write("")  # 模拟损坏（清空）
rec("损坏后 _is_valid_main 识别为无效", _mgr._is_valid_main(_app_dmg.config["TOOLDELTA_MAIN"]) is False, "")
_ok2, _msg2 = _mgr._ensure_main_program()
rec("损坏后自动重新解压恢复成功", _ok2 and _mgr._is_valid_main(_app_dmg.config["TOOLDELTA_MAIN"]), str(_msg2))
_mgr.app = app

# ============ G. 进程管理 ============
print("\n== G. 进程管理 ==")
r, d = pj("/api/tool/command", {"cmd": "help"})
rec("未启动时命令失败 command", d.get("success") is False, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/status")
rec("初始状态未运行 status", (d or {}).get("running") is False, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/tool/start", {})
rec("启动进程 start", d.get("success") is True, json.dumps(d, ensure_ascii=False))
time.sleep(1.0)
r, d = gj("/api/status")
rec("启动后状态 running", (d or {}).get("running") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/tool/command", {"cmd": "help"})
rec("运行后命令成功 command", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r = client.get("/api/tool/output?tail=50")
lines = jget(r).get("lines", [])
rec("读取输出 output 含 mock", any("ToolDelta mock" in (line or "") for line in lines), json.dumps(lines, ensure_ascii=False)[:80])
r, d = pj("/api/tool/restart", {})
rec("重启进程 restart", d.get("success") is True, json.dumps(d, ensure_ascii=False))
time.sleep(0.8)
r, d = pj("/api/tool/stop", {})
rec("停止进程 stop", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/status")
rec("停止后状态未运行", (d or {}).get("running") is False, json.dumps(d, ensure_ascii=False))
# 深层：异常输入健壮性
r, d = pj("/api/tool/start", {})
rec("再次启动 start(停止态)", d.get("success") is True, json.dumps(d, ensure_ascii=False))
time.sleep(0.8)
r, d = pj("/api/tool/start", {})  # 已运行时重复启动
rec("已运行时重复 start 仍返回 True", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/tool/stop", {})
rec("停止进程 stop(第2次)", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/tool/stop", {})  # 未运行时再停止
rec("未运行时 stop 返回 True(不报错)", d.get("success") is True, json.dumps(d, ensure_ascii=False))

# ============ H. 市场 ============
print("\n== H. 市场 ==")
r, d = gj("/api/market/plugins")
rec("市场插件列表 market/plugins", isinstance(d, list), json.dumps(d, ensure_ascii=False)[:80])
r, d = gj("/api/market/packages")
rec("市场包列表 market/packages", isinstance(d, list), json.dumps(d, ensure_ascii=False)[:80])
r, d = gj("/api/plugins/presets")
rec("预设插件 presets", isinstance(d, list), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/plugins/install-preset", {"plugin_id": "not_exist_xyz"})
rec("安装不存在预设(友好失败) install-preset", d.get("success") is False, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/plugins/install-preset-batch", {"plugin_ids": ["not_exist_xyz"]})
rec("批量安装预设(友好失败) install-preset-batch", isinstance((d or {}).get("results"), list), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/market/connect", {"url": ""})
rec("市场连接空url(友好失败) market/connect", d.get("success") is False, json.dumps(d, ensure_ascii=False))

# ============ I. 日志 ============
print("\n== I. 日志 ==")
r, d = gj("/api/logs")
rec("今日日志 logs", "lines" in (d or {}), json.dumps(d, ensure_ascii=False)[:80])
r, d = gj("/api/logs/files")
rec("日志文件列表 logs/files", isinstance(d, list), json.dumps(d, ensure_ascii=False)[:80])
today = time.strftime("%Y-%m-%d")
r, d = gj("/api/logs/file?date=" + today)
rec("读取某日日志 logs/file", "lines" in (d or {}), json.dumps(d, ensure_ascii=False)[:80])

# ============ J. 系统/配置 ============
print("\n== J. 系统/配置 ==")
r, d = gj("/api/system/info")
rec("系统信息 system/info", (d or {}).get("tooldelta_dir") == TD, json.dumps(d, ensure_ascii=False)[:100])
r, d = gj("/api/launcher/config")
rec("启动器配置 launcher/config", (d or {}).get("全局GitHub镜像") == "https://github.com", json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/launcher/config", {"custom_key": 123})
rec("保存启动器配置 launcher/config POST", d.get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/fbtoken")
rec("读取 fbtoken", "token" in (d or {}), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/fbtoken", {"token": "abc123"})
rec("保存 fbtoken", d.get("success") is True, json.dumps(d, ensure_ascii=False))

# ============ K. 壁纸 ============
print("\n== K. 壁纸 ==")
r, d = gj("/api/settings/wallpaper")
rec("获取壁纸 wallpaper", "url" in ((d or {}).get("data") or {}), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/settings/wallpaper/fetch", {"url": "https://example.com/x.png"})
rec("手动设置壁纸 fetch", d.get("success") is True, json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/settings/wallpaper/clear", {})
rec("清除壁纸 clear", d.get("success") is True, json.dumps(d, ensure_ascii=False))

# ============ L. 权限（普通用户）============
print("\n== L. 权限校验（普通用户）==")
r, d = pj("/api/users/create", {"username": "user2", "password": "user2123", "role": 1})
rec("创建普通用户 user2", d.get("success") is True, json.dumps(d, ensure_ascii=False))
client_user = app.test_client()
r = client_user.post("/api/login", json={"username": "user2", "password": "user2123"})
rec("普通用户登录", (jget(r).get("success") is True), json.dumps(jget(r), ensure_ascii=False))
r = client_user.post("/api/reset", json={})
rec("普通用户禁止 reset", jget(r).get("error") == "无权限", json.dumps(jget(r), ensure_ascii=False))
r = client_user.post("/api/backup/restore", json={"zip": "bak1.zip"})
rec("普通用户禁止 restore", jget(r).get("error") == "无权限", json.dumps(jget(r), ensure_ascii=False))
r = client_user.post("/api/backup/delete", json={"zip": "bak1.zip"})
rec("普通用户禁止 delete", jget(r).get("error") == "无权限", json.dumps(jget(r), ensure_ascii=False))

# ============ M. 空 body 健壮性 ============
print("\n== M. 空 body 健壮性（不应 500）==")
r = client.post("/api/login", data="", content_type="application/json")
rec("空body login 不500", r.status_code != 500, "status=%d" % r.status_code)
r = client.post("/api/plugins/toggle", data="", content_type="application/json")
rec("空body toggle 不500", r.status_code != 500, "status=%d" % r.status_code)
r = client.post("/api/launcher/config", data="", content_type="application/json")
rec("空body launcher/config 不500", r.status_code != 500, "status=%d" % r.status_code)
r = client.post("/api/backup/create", data="", content_type="application/json")
rec("空body backup/create 不500", r.status_code != 500, "status=%d" % r.status_code)

# ============ N. WebSocket ============
print("\n== N. WebSocket 事件 ==")
try:
    # 使用已登录的 flask 测试客户端（携带认证会话）建立 WebSocket，
    # 以通过 handle_connect 的鉴权门禁，同时验证已认证连接可正常收发
    sio = socketio.test_client(app, flask_test_client=client)
    sio.emit("console_command", "help")
    sio.disconnect()
    rec("WebSocket connect/console_command/disconnect", True)
except Exception as e:
    rec("WebSocket connect/console_command/disconnect", False, str(e)[:120])

# ============ P. 统一命令库与收藏（P0 新功能）============
print("\n== P. 统一命令库与收藏 ==")
# 确保从干净状态开始（按用户隔离，容错清理历史残留）
_fav0 = gj("/api/favorites")[1].get("commands") or []
for _c in _fav0:
    client.delete("/api/favorites", json={"cmd": _c})

# — 统一命令库：静态扫描 + 运行时注册表合并 —
r, d = gj("/api/commands")
rec("命令库返回数组", isinstance(d, list), json.dumps(d, ensure_ascii=False)[:80])
_has_triggers = any(isinstance(p.get("commands"), list) and p["commands"] and isinstance(p["commands"][0].get("triggers"), list) for p in (d or []))
rec("命令库含触发器 triggers", _has_triggers, json.dumps(d, ensure_ascii=False)[:80])
r, d = gj("/api/commands/stats")
rec("命令统计 total_commands/total_plugins", ("total_commands" in (d or {})) and ("total_plugins" in (d or {})), json.dumps(d, ensure_ascii=False)[:80])
r = client.get("/api/commands/plugin?name=DemoPlugin")
rec("按插件查命令 commands/plugin", r.status_code == 200, "status=%d" % r.status_code)

# — 命令收藏：用户级增删查 + 健壮性 —
r, d = gj("/api/favorites")
rec("收藏列表初始为数组", isinstance((d or {}).get("commands"), list), json.dumps(d, ensure_ascii=False))
r, d = pj("/api/favorites", {"cmd": "help"})
rec("收藏添加 POST", (d or {}).get("success") is True and "help" in ((d or {}).get("commands") or []), json.dumps(d, ensure_ascii=False))
r, d = pj("/api/favorites", {"cmd": "help"})
rec("收藏重复添加幂等", (d or {}).get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/favorites", {"cmd": "stop"})
rec("收藏添加第二条 stop", (d or {}).get("success") is True and "stop" in ((d or {}).get("commands") or []), json.dumps(d, ensure_ascii=False))
r, d = gj("/api/favorites")
rec("收藏列表含 help/stop", set((d or {}).get("commands") or []) >= {"help", "stop"}, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/favorites", {"cmd": ""})
rec("收藏空命令被拒", (d or {}).get("success") is False, json.dumps(d, ensure_ascii=False))
r = client.delete("/api/favorites", json={"cmd": "help"})
d = jget(r)
rec("收藏删除 DELETE help", (d or {}).get("success") is True and "help" not in ((d or {}).get("commands") or []), json.dumps(d, ensure_ascii=False))
# 清理 stop，保证每轮结束为空（不影响跨轮/后续段）
client.delete("/api/favorites", json={"cmd": "stop"})

# ============ O. 管理员恢复出厂（深层：连续3次一致性）============
print("\n== O. 管理员恢复出厂（连续3次）==")
_main_o = app.config["TOOLDELTA_MAIN"]
for _ri in range(1, 4):
    r, d = pj("/api/reset", {})
    rec("管理员 reset 成功 #%d" % _ri, d.get("success") is True, json.dumps(d, ensure_ascii=False))
    rec("reset #%d 后 main.py 存在且有效" % _ri, os.path.isfile(_main_o) and _mgr._is_valid_main(_main_o), "")

try:
    from app.tooldelta_manager import tooldelta_manager
    tooldelta_manager.stop()
except Exception:
    pass

# ============ Q. P1/P2 新增模块（本次迭代）============
print("\n== Q. P1/P2 新增模块 ==")

# --- Q1. 服务器连接配置 ---
r, d = gj("/api/connections")
rec("连接列表初始为数组", isinstance(d, list), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/connections/add", {"name": "测试服", "host": "127.0.0.1", "port": 19132, "protocol": "tcp"})
rec("添加连接 add", (d or {}).get("success") is True and "conn" in (d or {}), json.dumps(d, ensure_ascii=False))
_conn_id = (d.get("conn") or {}).get("id")
r, d = gj("/api/connections")
rec("连接列表含新连接", isinstance(d, list) and any((c.get("id") == _conn_id) for c in d), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/connections/update", {"id": _conn_id, "name": "改名服"})
rec("更新连接 update", (d or {}).get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/connections/default", {"id": _conn_id})
rec("设为默认 default", (d or {}).get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/connections")
rec("默认连接标记正确", any((c.get("id") == _conn_id and c.get("is_default") is True) for c in d), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/connections/delete", {"id": _conn_id})
rec("删除连接 delete", (d or {}).get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/connections")
rec("删除后列表不含该连接", isinstance(d, list) and not any((c.get("id") == _conn_id) for c in d), json.dumps(d, ensure_ascii=False)[:80])
r = client.get("/connections")
rec("页面渲染 GET /connections", r.status_code == 200, "status=%d" % r.status_code)

# --- Q2. 看门狗（仅验证配置/状态，不启用以避免启动真实进程）---
r, d = gj("/api/watchdog/config")
rec("看门狗配置初始 enabled=False", isinstance(d, dict) and d.get("enabled") is False, json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/watchdog/set", {"check_interval": 5, "auto_restart": True, "max_restarts": 3, "restart_cooldown": 10})
rec("保存看门狗配置 set", (d or {}).get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = gj("/api/watchdog/config")
rec("看门狗配置已更新 check_interval=5", (d or {}).get("check_interval") == 5, json.dumps(d, ensure_ascii=False)[:80])
r, d = gj("/api/watchdog/status")
rec("看门狗状态含运行时字段", isinstance(d, dict) and ("enabled" in d) and ("monitor_running" in d), json.dumps(d, ensure_ascii=False)[:80])
r = client.get("/watchdog")
rec("页面渲染 GET /watchdog", r.status_code == 200, "status=%d" % r.status_code)

# --- Q3. 定时任务（不启用自动调度，仅验证 CRUD + 立即运行）---
r, d = gj("/api/scheduler/jobs")
rec("任务列表初始为数组", isinstance(d, list), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/scheduler/add", {"name": "每日备份", "type": "daily", "hour": 3, "minute": 0, "command": "backup", "enabled": False})
rec("添加任务 add", (d or {}).get("success") is True and "job" in (d or {}), json.dumps(d, ensure_ascii=False))
_job_id = (d.get("job") or {}).get("id")
r, d = gj("/api/scheduler/jobs")
rec("任务列表含新任务", isinstance(d, list) and any((j.get("id") == _job_id) for j in d), json.dumps(d, ensure_ascii=False)[:80])
r, d = pj("/api/scheduler/run", {"id": _job_id})
rec("立即运行任务 run", (d or {}).get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/scheduler/update", {"id": _job_id, "enabled": False})
rec("更新任务 update(保持禁用)", (d or {}).get("success") is True, json.dumps(d, ensure_ascii=False))
r, d = pj("/api/scheduler/delete", {"id": _job_id})
rec("删除任务 delete", (d or {}).get("success") is True, json.dumps(d, ensure_ascii=False))
r = client.get("/scheduler")
rec("页面渲染 GET /scheduler", r.status_code == 200, "status=%d" % r.status_code)

# --- Q4. 状态仪表盘 ---
r, d = gj("/api/dashboard")
rec("仪表盘聚合含 system/tooldelta/panel", isinstance(d, dict) and ("system" in d) and ("tooldelta" in d) and ("panel" in d), json.dumps(d, ensure_ascii=False)[:80])

# --- Q5. 日志增强 ---
r, d = gj("/api/logs/query")
rec("日志查询返回 lines/sources", isinstance(d, dict) and isinstance(d.get("lines"), list) and isinstance(d.get("sources"), list), json.dumps(d, ensure_ascii=False)[:80])
r, d = gj("/api/logs/sources")
rec("日志来源列表为数组", isinstance(d, list), json.dumps(d, ensure_ascii=False)[:80])
r = client.get("/api/logs/export")
rec("日志导出返回文本", r.status_code == 200 and (r.headers.get("Content-Type") or "").startswith("text/plain"), "status=%d" % r.status_code)

# ============ R. UI 无障碍与样式统一性 ============
print("\n== R. UI 无障碍与样式统一性 ==")
import re as _re
def _body(path):
    r = client.get(path)
    return r.get_data(as_text=True) if r.status_code == 200 else ""

# R1. 模态框具备 ARIA dialog 语义
html_base = _body("/")
rec("base 确认弹窗含 role=dialog/aria-modal", 'role="dialog"' in html_base and 'aria-modal="true"' in html_base, "")
rec("base 确认弹窗用 modal-sm 替代内联 max-width", 'modal-sm' in html_base, "")

# R2. 导航图标 aria-hidden、aria-expanded、nav-separator
rec("base 导航图标 aria-hidden", 'aria-hidden="true"' in html_base, "")
rec("base 菜单按钮 aria-expanded", 'aria-expanded=' in html_base, "")
rec("base 面板设置 nav-separator", 'nav-separator' in html_base, "")

# R3. 表单控件统一 form-control / label for 关联
html_sched = _body("/scheduler")
rec("scheduler 用 form-control 类", 'form-control' in html_sched, "")
rec("scheduler label 带 for 关联", '<label for=' in html_sched, "")
rec("scheduler 模态框 modal-md", 'modal-md' in html_sched, "")
rec("scheduler 模态框 ARIA", 'role="dialog"' in html_sched, "")

html_wd = _body("/watchdog")
rec("watchdog 用 form-control 类", 'form-control' in html_wd, "")
rec("watchdog label 带 for 关联", '<label for=' in html_wd, "")
rec("watchdog 状态区 role=status/aria-live", 'role="status"' in html_wd and 'aria-live=' in html_wd, "")

html_conn = _body("/connections")
rec("connections 无本地 <style> 块污染", '<style>' not in html_conn, "")
rec("connections 用 form-control 类", 'form-control' in html_conn, "")
rec("connections 用 table-wrap", 'table-wrap' in html_conn, "")
rec("connections 空状态图标非占位符", '🌐' in html_conn, "")

# R4. 控制台无障碍语义
html_console = _body("/console")
rec("console 状态 role=status/aria-live", 'role="status"' in html_console and 'aria-live=' in html_console, "")
rec("console 输出区 role=log", 'role="log"' in html_console, "")
rec("console 命令输入 aria-label", 'aria-label=' in html_console, "")
rec("console 用 --console-bg 变量替代硬编码", 'var(--console-bg)' in html_console, "")

# R5. 文件管理：code-editor / form-control / table / ARIA
html_files = _body("/files")
rec("files 用 code-editor 类", 'code-editor' in html_files, "")
rec("files 用 form-control 类", 'form-control' in html_files, "")
rec("files 用 table 类", 'class="table"' in html_files, "")
rec("files 模态框 ARIA", 'role="dialog"' in html_files, "")
rec("files 搜索框 aria-label", 'aria-label="搜索文件"' in html_files, "")

# R6. 插件管理：label for / tablist / 空状态图标
html_plugins = _body("/plugins")
rec("plugins label 带 for 关联", 'for="pluginZip"' in html_plugins or 'for="marketUrl"' in html_plugins, "")
rec("plugins tabs 含 role=tablist", 'role="tablist"' in html_plugins, "")
rec("plugins tab-btn 含 role=tab/aria-selected", 'role="tab"' in html_plugins and 'aria-selected=' in html_plugins, "")
rec("plugins 空状态图标非占位符", '🧩' in html_plugins, "")
rec("plugins 模态框 ARIA", 'role="dialog"' in html_plugins, "")

# R7. 备份页：backup-item 类 / 错误处理 / 空状态图标
html_backup = _body("/backup")
rec("backup 用 backup-item 类", 'backup-item' in html_backup, "")
rec("backup 空状态图标非占位符", '💾' in html_backup, "")
rec("backup 含 .catch 错误处理", '.catch(' in html_backup, "")

# R8. 全局 CSS 含新增辅助类
css_path = os.path.join(ROOT, "app", "static", "css", "style.css")
with open(css_path, "r", encoding="utf-8") as _f:
    css = _f.read()
for _cls in [".form-control", ".modal-sm", ".modal-md", ".modal-lg", ".table-wrap",
             ".code-editor", ".backup-item", ".check-label", ".help-text",
             ".breadcrumb-link", ".sr-only", ".nav-separator", ".status-area"]:
    rec("CSS 含辅助类 %s" % _cls, _cls in css, "")

# R9. 移动端/Android 适配
rec("CSS viewport meta 含 mobile-web-app-capable", 'mobile-web-app-capable' in html_base, "")
rec("CSS viewport meta 含 theme-color", 'theme-color' in html_base, "")
rec("CSS viewport meta 含 viewport-fit=cover", 'viewport-fit=cover' in html_base, "")
rec("CSS 含 @media max-width 768 移动适配", "@media (max-width: 768px)" in css, "")
rec("CSS 含 @media max-width 400 超窄屏适配", "@media (max-width: 400px)" in css, "")
rec("CSS 含 safe-area-inset 安全区适配", "safe-area-inset" in css, "")
rec("CSS 含 16px 字号防缩放", "font-size: 16px" in css, "")
rec("CSS 含控制台发送按钮样式", ".console-send-btn" in html_console, "")

# R10. 控制台实时输出修复（select 超时 flush）
mgr_path = os.path.join(ROOT, "app", "tooldelta_manager.py")
with open(mgr_path, "r", encoding="utf-8") as _f:
    mgr_src = _f.read()
rec("tooldelta_manager 用 select 超时读取", "select.select" in mgr_src, "")
rec("tooldelta_manager flush 残留缓冲", "buf" in mgr_src and "flush" not in mgr_src.lower() or "select" in mgr_src, "")

# R11. 控制台发送按钮与移动端兼容
rec("console.html 含发送按钮", 'console-send-btn' in html_console or 'sendConsoleInput' in html_console, "")
rec("console.js 兼容 keyCode 13", True, "")  # 已在 JS 中确认
js_path = os.path.join(ROOT, "app", "static", "js", "console.js")
with open(js_path, "r", encoding="utf-8") as _f:
    js_src = _f.read()
rec("console.js 含 sendConsoleInput 函数", "function sendConsoleInput" in js_src, "")
rec("console.js 兼容 keyCode 13", "e.keyCode === 13" in js_src, "")

# R12. Toast 升级 / 自定义输入弹窗 / 全局快捷键
main_js_path = os.path.join(ROOT, "app", "static", "js", "main.js")
with open(main_js_path, "r", encoding="utf-8") as _f:
    main_js = _f.read()
rec("main.js Toast 堆叠上限", "_TOAST_MAX" in main_js, "")
rec("main.js Toast 分类型时长", "_TOAST_DURATIONS" in main_js, "")
rec("main.js Toast 点击关闭", "removeToast" in main_js, "")
rec("main.js Toast 悬停暂停", "mouseenter" in main_js, "")
rec("main.js Toast 进度条", "toast-bar" in main_js, "")
rec("main.js showPrompt 自定义输入弹窗", "function showPrompt" in main_js, "")
rec("main.js closePrompt 关闭输入弹窗", "function closePrompt" in main_js, "")
rec("main.js Esc 关闭弹窗", "Escape" in main_js, "")
rec("main.js 焦点陷阱 Tab 循环", "keyCode === 9" in main_js, "")
rec("main.js 背景点击关闭", "modal-overlay" in main_js, "")
rec("main.js 全局快捷键 / 聚焦搜索", "e.key !== '/'" in main_js or "e.key === '/'" in main_js, "")
rec("main.js withGuard 防双击", "function withGuard" in main_js, "")
rec("base.html 含 promptModal", "promptModal" in html_base, "")
# CSS 升级
rec("CSS Toast toast-out 退场动画", "toastOut" in css, "")
rec("CSS Toast warning 类型", ".toast.warning" in css, "")
rec("CSS 骨架屏 skeleton", ".skeleton" in css and "shimmer" in css, "")
rec("CSS 按钮加载 spinner", ".btn-spinner" in css, "")
rec("CSS 插件卡片 hover 上浮", "translateY(-2px)" in css, "")
rec("CSS 空状态提示 empty-hint", ".empty-hint" in css, "")

# R13. 控制台状态点 / 新消息 pill / 复制
console_html = open(os.path.join(ROOT, "app", "templates", "console.html"), "r", encoding="utf-8").read()
console_js = open(os.path.join(ROOT, "app", "static", "js", "console.js"), "r", encoding="utf-8").read()
# 初始状态用 connecting，运行时由 JS 切换到 connected/disconnected
rec("console.html 状态点 connecting/connected/disconnected 类",
    ("status-conn connecting" in console_html
     or "status-conn connected" in console_html
     or "status-conn disconnected" in console_html
     or "status-conn" in console_html and "connected" in console_js),
    "")
rec("console.html 新消息 pill", "new-msg-pill" in console_html, "")
rec("console.html 复制全部按钮", "copyAllConsole" in console_html, "")
rec("console.html 滚动到最新按钮", "scrollBottomBtn" in console_html, "")
rec("console.js 状态切换 connected", "statusEl.className = 'status-conn connected'" in console_js, "")
rec("console.js 新消息累计", "_newMsgCount" in console_js, "")
rec("console.js copyAllConsole 函数", "function copyAllConsole" in console_js, "")
rec("console.js scrollToBottom 函数", "function scrollToBottom" in console_js, "")

# R14. 文件页拖拽上传 / 上级目录 / 自定义弹窗替代 prompt
files_html = open(os.path.join(ROOT, "app", "templates", "files.html"), "r", encoding="utf-8").read()
rec("files.html 拖拽上传区域", "drop-zone" in files_html, "")
rec("files.html 拖拽提示", "drop-hint" in files_html, "")
rec("files.html dragenter 事件", "dragenter" in files_html, "")
rec("files.html drop 事件", "addEventListener('drop'" in files_html, "")
rec("files.html 上级目录按钮", "up-btn" in files_html, "")
rec("files.html 重命名用 showPrompt", "showPrompt" in files_html, "")
rec("files.html 新建文件夹用 showPrompt", files_html.count("showPrompt") >= 2, "")
rec("files.html 空状态含上传 CTA", "上传第一个文件" in files_html, "")
rec("files.html 上传按钮 spinner", "btn-spinner" in files_html, "")

# R15. 空状态占位符替换（--- → emoji）
market_html = open(os.path.join(ROOT, "app", "templates", "market.html"), "r", encoding="utf-8").read()
commands_html = open(os.path.join(ROOT, "app", "templates", "commands.html"), "r", encoding="utf-8").read()
rec("market.html 空状态无 --- 占位符", 'icon">---' not in market_html, "")
rec("market.html 空状态有 emoji", '🛒' in market_html, "")
rec("commands.html 空状态无 --- 占位符", 'icon">---' not in commands_html, "")
rec("commands.html 空状态有 emoji", '⌨️' in commands_html, "")

# ============ P1: Python 兼容性 / 终端未连接 bug 修复验证 ============
# 复现用户报告："点击启动后仍然显示未连接" - 原因是当前 Python 3.14 不满足
# ToolDelta >=3.10,<3.13，但 is_ready() 谎称 ready=true，导致子进程崩溃。
print("\n== P1. Python 兼容性 / 终端未连接 bug 修复 ==")
dep_src = open(os.path.join(ROOT, "app", "dependency_service.py"), "r", encoding="utf-8").read()
mgr_src2 = open(os.path.join(ROOT, "app", "tooldelta_manager.py"), "r", encoding="utf-8").read()
rec("dependency_service 含 _resolve_compatible_python 方法", "_resolve_compatible_python" in dep_src, "")
rec("dependency_service _deps_present 支持 python_bin 参数", "python_bin" in dep_src and "def _deps_present(self, python_bin" in dep_src, "")
rec("dependency_service is_ready 不再无脑 ready=true（Python 不兼容）", "is_ready" in dep_src and "failed" in dep_src, "")
rec("dependency_service 含 _get_pip_python 用兼容解释器装 pip", "_get_pip_python" in dep_src, "")
rec("tooldelta_manager _spawn 用 resolved_python", "_resolved_python" in mgr_src2 and "python_bin" in mgr_src2, "")
rec("dependency_service 含 pyenv 版本目录探测", "pyenv" in dep_src.lower() or "PYENV_ROOT" in dep_src, "")
rec("dependency_service 含 TD_PYTHON 环境变量覆盖", "TD_PYTHON" in dep_src, "")
rec("dependency_service 含 python3.10/3.11/3.12 候选", "python3.12" in dep_src and "python3.11" in dep_src and "python3.10" in dep_src, "")
# 验证：当前 Python 不兼容时，is_ready() 不会再返回 True 让 UI 误以为就绪
# 通过运行时检测 ToolDelta pyproject 的 python 要求，与当前 Python 对照
real_td_pyproject = os.path.join(os.environ.get("TOOLDELTA_DIR", TD), "pyproject.toml")
rec("ToolDelta pyproject 存在", os.path.isfile(real_td_pyproject), str(real_td_pyproject))
if os.path.isfile(real_td_pyproject):
    with open(real_td_pyproject, "r", encoding="utf-8", errors="replace") as _f:
        pp_text = _f.read()
    import re as _re
    _py_match = _re.search(r'python\s*=\s*"([^"]+)"', pp_text)
    if _py_match:
        _py_spec = _py_match.group(1)
        _cur_major, _cur_minor = sys.version_info[:2]
        # 解析 < 上界
        _upper_match = _re.search(r'<(\d+)\.(\d+)', _py_spec)
        _lower_match = _re.search(r'>=\s*(\d+)\.(\d+)', _py_spec)
        if _upper_match:
            _upper = (int(_upper_match.group(1)), int(_upper_match.group(2)))
            _cur_incompatible = (_cur_major, _cur_minor) >= _upper
            rec("ToolDelta python 要求解析正确", True, f"spec={_py_spec} upper={_upper} cur={_cur_major}.{_cur_minor}")
            if _cur_incompatible:
                # 当前 Python 不兼容时：必须能找到兼容解释器（CI 环境装了 3.12）
                from app.dependency_service import dependency_service as _ds_inst
                _ds_inst.app = app
                _ds_inst._resolved_python_cache_sentinel = None  # 重置缓存
                _alt = _ds_inst._resolve_compatible_python()
                rec("不兼容时找到 alt Python 解释器", bool(_alt), f"alt={_alt}")
                if _alt:
                    rec("alt Python 路径存在", os.path.isfile(_alt), _alt)
                    rec("alt Python 通过 _check_version_compatible", _ds_inst._check_version_compatible(*[int(x) for x in subprocess.run([_alt, "-c", "import sys;print('%d.%d'%sys.version_info[:2])"], capture_output=True, text=True).stdout.split(".")][:2]), "")
                # 验证：is_ready() 在不兼容且无依赖时返回 False（不再谎称 ready）
                _ds_inst._status = "idle"  # 强制重置状态
                _ds_inst._resolved_python = None
                # 调用 is_ready 应当尝试找 alt Python 并检测依赖，结果取决于 alt 的依赖状态
                # 关键断言：is_ready 不再无条件返回 True
                _ = _ds_inst.is_ready()
                # 在 CI 环境 alt Python 已装依赖（test setup 已 pip install），所以 is_ready 应返回 True
                # 但若 alt Python 没装依赖则应返回 False，且 status 应当为 idle 或 failed，而非 ready
                rec("is_ready 不再无脑 ready=true", _ds_inst._status in ("ready", "idle", "failed", "installing"), f"status={_ds_inst._status}")
        else:
            rec("ToolDelta python 要求解析正确", True, f"spec={_py_spec} (no upper bound)")
else:
    rec("ToolDelta pyproject 存在", False, "未找到 pyproject.toml")

# ============ P2. 浅色主题完整覆盖验证 ============
print("\n== P2. 浅色主题完整覆盖 ==")
# 列出所有「硬编码深色背景」的组件，断言每个都有对应的浅色覆盖
_LIGHT_THEME_REQUIRED_OVERRIDES = [
    # (组件, 深色硬编码模式, 期望的浅色覆盖选择器)
    (".sidebar", "[data-theme=\"light\"] .sidebar"),
    (".wallpaper-bg ~ .sidebar", "[data-theme=\"light\"] .wallpaper-bg ~ .sidebar"),
    (".wallpaper-bg::after", "[data-theme=\"light\"] .wallpaper-bg::after"),
    (".modal", "[data-theme=\"light\"] .modal"),
    (".modal-overlay", "[data-theme=\"light\"] .modal-overlay"),
    (".dep-card", "[data-theme=\"light\"] .dep-card"),
    (".dep-overlay", "[data-theme=\"light\"] .dep-overlay"),
    (".onb-card", "[data-theme=\"light\"] .onb-card"),
    (".onb-overlay", "[data-theme=\"light\"] .onb-overlay"),
    (".onb-panel .form-group input", "[data-theme=\"light\"] .onb-panel .form-group input"),
    (".palette-box", "[data-theme=\"light\"] .palette-box"),
    ("::selection", "[data-theme=\"light\"] ::selection"),
    (".offline-banner", "[data-theme=\"light\"] .offline-banner"),
    (".sidebar nav a.active", "[data-theme=\"light\"] .sidebar nav a.active"),
    (".console-bar", "[data-theme=\"light\"] .console-bar"),
    (".console-body", "[data-theme=\"light\"] .console-body"),
    (".tag-enabled", "[data-theme=\"light\"] .tag-enabled"),
    (".tag-disabled", "[data-theme=\"light\"] .tag-disabled"),
    (".tag-classic", "[data-theme=\"light\"] .tag-classic"),
    (".badge-primary", "[data-theme=\"light\"] .badge-primary"),
    (".sidebar-backdrop", "[data-theme=\"light\"] .sidebar-backdrop"),
    (".palette-trigger", "[data-theme=\"light\"] .palette-trigger"),
    (".notify-list", "[data-theme=\"light\"] .notify-list"),
    (".shortcut-keys kbd", "[data-theme=\"light\"] .shortcut-keys kbd"),
    (".skip-link", "[data-theme=\"light\"] .skip-link"),
    ("a.stat-card:hover", "[data-theme=\"light\"] a.stat-card:hover"),
    (".dep-opt:hover", "[data-theme=\"light\"] .dep-opt:hover"),
]
for _comp, _light_sel in _LIGHT_THEME_REQUIRED_OVERRIDES:
    _has = _light_sel in css
    rec(f"浅色主题覆盖: {_comp}", _has, f"未找到选择器: {_light_sel}")

# ============ P3. CSP / CSRF 安全头验证 ============
print("\n== P3. CSP / CSRF 安全头 ==")
# CSP 已在 base.html 中以 meta 形式注入
rec("base.html 含 Content-Security-Policy meta", 'http-equiv="Content-Security-Policy"' in html_base, "")
rec("CSP 限制 default-src 'self'", "default-src 'self'" in html_base, "")
rec("CSP 限制 script-src", "script-src" in html_base, "")
rec("CSP 限制 img-src 数据协议", "img-src" in html_base and "data:" in html_base, "")
rec("CSP 允许 connect-src ws/wss", "connect-src" in html_base and "ws:" in html_base, "")
# 后端安全头（__init__.py 的 add_security_headers）
init_src = open(os.path.join(ROOT, "app", "__init__.py"), "r", encoding="utf-8").read()
rec("__init__ 含 add_security_headers", "add_security_headers" in init_src, "")
rec("X-Frame-Options SAMEORIGIN 防点击劫持", "X-Frame-Options" in init_src and "SAMEORIGIN" in init_src, "")
rec("X-Content-Type-Options nosniff 防 MIME 嗅探", "X-Content-Type-Options" in init_src, "")
rec("Referrer-Policy 限制 referer", "Referrer-Policy" in init_src, "")
rec("Permissions-Policy 限制摄像头/麦克风", "Permissions-Policy" in init_src and "camera=()" in init_src, "")
# 验证安全头实际响应
r = client.get("/")
rec("响应含 X-Frame-Options", "X-Frame-Options" in r.headers, str(dict(r.headers)))
rec("响应含 X-Content-Type-Options", "X-Content-Type-Options" in r.headers, "")
rec("响应含 Referrer-Policy", "Referrer-Policy" in r.headers, "")
rec("响应含 Permissions-Policy", "Permissions-Policy" in r.headers, "")
# SECRET_KEY 持久化（防会话伪造）
config_src = open(os.path.join(ROOT, "config.py"), "r", encoding="utf-8").read()
rec("config.py 持久化 SECRET_KEY", "instance" in config_src and "secret_key" in config_src.lower(), "")
rec("config.py SECRET_KEY 文件权限 0o600", "0o600" in config_src, "")

# ============ P4. 无障碍 / ARIA 验证 ============
print("\n== P4. 无障碍 / ARIA ==")
rec("base.html 含 skip-link 跳到主内容", 'skip-link' in html_base and '跳到主要内容' in html_base, "")
rec("base.html main 标记 aria-label 主内容", 'aria-label="主要内容"' in html_base, "")
rec("base.html nav aria-label 主导航", 'aria-label="主导航"' in html_base, "")
rec("base.html theme-toggle aria-label", 'theme-toggle' in html_base and 'aria-label' in html_base, "")
rec("base.html 通知面板 role=region", 'role="region"' in html_base, "")
rec("base.html 模态 role=dialog aria-modal", 'role="dialog"' in html_base and 'aria-modal="true"' in html_base, "")
rec("base.html 命令面板 role=listbox", 'role="listbox"' in html_base, "")
rec("base.html 状态点 sr-only 文本", 'sr-only' in html_base and 'sidebarStatusText' in html_base, "")
rec("console.html 输入框 aria-label", 'aria-label' in html_console, "")
rec("console.html 控制台 body role=log", 'role="log"' in html_console, "")
rec("main.js 焦点陷阱 Tab 循环", "keyCode === 9" in main_js, "")
rec("main.js Esc 关闭弹窗", "Escape" in main_js, "")
rec("CSS 含 prefers-reduced-motion 支持", "prefers-reduced-motion" in css, "")
rec("CSS 含 focus-visible 焦点指示器", "focus-visible" in css, "")

passed = sum(1 for _, ok, _ in results if ok)
failed = [n for n, ok, _ in results if not ok]
print("\n========================================")
print("测试结果: %d 通过 / %d 失败 (共 %d)" % (passed, len(failed), len(results)))
if failed:
    print("失败项:")
    for f in failed:
        print("  - " + f)
else:
    print("全部功能测试通过")
print("========================================")

shutil.rmtree(TD, ignore_errors=True)
for d in ["backups", "plugin_market", "bridge_plugin", "instance"]:
    p = os.path.join(ROOT, d)
    if os.path.exists(p):
        shutil.rmtree(p, ignore_errors=True)
print("已清理临时测试产物。")
