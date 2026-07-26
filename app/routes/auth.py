import os
from flask import Blueprint, render_template, request, jsonify, session, redirect
from app import auth_service
from app.log_service import log_service
from app import wallpaper_service as wp_service
import time
import re

bp = Blueprint("auth", __name__)

def ok(data=None):
    r = {"success": True}
    if data is not None:
        r["data"] = data
    return jsonify(r)

def fail(msg):
    return jsonify({"success": False, "error": msg})

def _client_ip():
    """获取客户端真实 IP。
    关键：仅在显式声明部署在反代后（BEHIND_PROXY=1，对应 run.py 启用 ProxyFix）时
    才信任 request.access_route[0]（X-Forwarded-For 最左侧）。否则必须回退到
    request.remote_addr（TCP 对端，不可伪造）。

    若无条件信任 access_route：Werkzeug 在未启用 ProxyFix 时也会把 X-Forwarded-For
    头内容直接返回到 access_route，攻击者每次请求换一个伪造的 XFF 值，限流永远
    收不到 10 次失败，login/change-password/setup/reset-panel 的限流全部失效。
    """
    if os.environ.get("BEHIND_PROXY", "false").lower() in ("1", "true"):
        addrs = getattr(request, "access_route", None)
        if addrs:
            return addrs[0]
    return request.remote_addr or "?"

def audit(action, detail=""):
    user = session.get("username", "?")
    ip = _client_ip()
    log_service.info(f"[{user}@{ip}] {action} {detail}", "AUDIT")

def _sanitize_for_log(s):
    """日志注入防护：去除换行/制表等控制字符，防止伪造日志行。
    用户名等用户输入若含 \\n、\\r 等会被拼入日志 message，可伪造新的日志行
    干扰审计排查。校验未通过的 username 可包含任意字符，必须过滤。"""
    if not isinstance(s, str):
        return str(s)
    # 替换所有控制字符（含 \r\n\t）为可见占位
    return re.sub(r'[\x00-\x1f\x7f]', '?', s)

@bp.route("/login")
def login_page():
    if auth_service.is_configured() and session.get("authenticated"):
        return redirect("/")
    return render_template("login.html")

@bp.route("/setup")
def setup_page():
    if auth_service.is_configured():
        return redirect("/login")
    return render_template("setup.html")

@bp.route("/api/setup", methods=["POST"])
def setup():
    if auth_service.is_configured():
        return fail("已配置")
    ip = _client_ip()
    allowed, msg = auth_service.check_login_rate(ip)
    if not allowed:
        return fail(msg)
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        auth_service.record_login_fail(ip)
        return fail("用户名和密码不能为空")
    valid, msg = auth_service.validate_username(username)
    if not valid:
        auth_service.record_login_fail(ip)
        return fail(msg)
    valid, msg = auth_service.validate_password(password)
    if not valid:
        auth_service.record_login_fail(ip)
        return fail(msg)
    # 弱密码仅提示，不阻止创建
    level, tips = auth_service.check_password_strength(password)
    ok_, err = auth_service.setup_user(username, password)
    if not ok_:
        return fail(err)
    auth_service.clear_login_fails(ip)
    # 会话固定防护：登录前清空旧 session，强制服务端生成新 session id
    session.clear()
    session["authenticated"] = True
    session["username"] = username
    session["role"] = 10
    session["auth_at"] = time.time()
    session["session_version"] = 1   # 与 setup_user 写入的初始 session_version 一致
    session.permanent = True
    audit("初始化面板", f"用户={username}")
    data = {}
    if level != "strong" and tips:
        data["password_warning"] = {"level": level, "tips": tips}
    return ok(data)

@bp.route("/api/login", methods=["POST"])
def login():
    ip = _client_ip()
    allowed, msg = auth_service.check_login_rate(ip)
    if not allowed:
        return fail(msg)
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    # 用户名格式校验：即使不存在也走统一失败提示，避免用户名枚举（P1-2）
    valid, _ = auth_service.validate_username(username)
    if not valid:
        auth_service.record_login_fail(ip)
        # 日志注入防护：username 未通过校验说明含非法字符（可能含 \n 等控制字符），
        # 直接拼入日志会伪造新的日志行干扰审计排查，必须过滤控制字符
        log_service.warn(f"[{ip}] 登录失败(非法用户名): {_sanitize_for_log(username)}", "AUDIT")
        return fail("用户名或密码错误")
    user = auth_service.verify_username_password(username, password)
    if user:
        auth_service.clear_login_fails(ip)
        auth_service.update_login_time(username)
        # 会话固定防护：登录前清空旧 session，强制服务端生成新 session id
        session.clear()
        session["authenticated"] = True
        session["username"] = username
        session["role"] = user.get("role", 1)
        session["auth_at"] = time.time()
        # 记录当前 session_version：管理员后续改密 / 删用户会递增该值，
        # before_request 校验发现不一致即清空 session 强制重登，避免旧 session 长期可用
        session["session_version"] = user.get("session_version", 1)
        session.permanent = True
        audit("登录", f"用户={username}")
        return ok()
    auth_service.record_login_fail(ip)
    log_service.warn(f"[{ip}] 登录失败: {username}", "AUDIT")
    return fail("用户名或密码错误")

@bp.route("/api/change-password", methods=["POST"])
def change_password():
    # 限流 + 失败审计：session 被劫持（XSS 偷 cookie / 公共电脑未注销）后，攻击者可
    # 无限次暴力枚举 old_password，猜中后改成自己的密码锁定真实用户。
    # 这里复用 check_login_rate（按 IP 限流）+ record_login_fail，
    # 并在失败时写审计日志便于追责。成功路径同样记审计。
    ip = _client_ip()
    allowed, msg = auth_service.check_login_rate(ip)
    if not allowed:
        return fail(msg)
    data = request.get_json(silent=True) or {}
    old_pw = data.get("old_password") or ""
    new_pw = data.get("new_password") or ""
    if not old_pw or not new_pw:
        return fail("参数不完整")
    valid, msg = auth_service.validate_password(new_pw)
    if not valid:
        return fail(msg)
    username = session.get("username", "")
    ok_, err = auth_service.change_password(username, old_pw, new_pw)
    if ok_:
        auth_service.clear_login_fails(ip)
        audit("修改密码", f"用户={username}")
        # 关键：同步 session 中的 session_version 到新值。
        # auth_service.change_password 成功后递增了 user.session_version，
        # 若不同步 session 中的旧值，下一次请求时 before_request 会发现版本不一致
        # 立即 session.clear() 跳转 /login——用户刚改完密码就被踢下线，体验割裂。
        new_ver = auth_service.get_user_session_version(username)
        if new_ver is not None:
            session["session_version"] = new_ver
        level, tips = auth_service.check_password_strength(new_pw)
        data = {}
        if level != "strong" and tips:
            data["password_warning"] = {"level": level, "tips": tips}
        return ok(data)
    # 旧密码错误：计入失败次数 + 审计日志，便于检测爆破
    auth_service.record_login_fail(ip)
    log_service.warn(f"[{username}@{ip}] 修改密码失败: {err}", "AUDIT")
    return fail(err)

@bp.route("/api/reset-panel", methods=["POST"])
def reset_panel():
    if session.get("role") != 10:
        return fail("无权限")
    ip = _client_ip()
    allowed, msg = auth_service.check_login_rate(ip)
    if not allowed:
        return fail(msg)
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if not auth_service.verify_password(password):
        auth_service.record_login_fail(ip)
        log_service.warn(f"[{session.get('username','?')}@{ip}] 重置面板失败(密码错误)", "AUDIT")
        return fail("密码错误")
    auth_service.clear_login_fails(ip)
    audit("重置面板", f"操作者={session.get('username','?')}")
    auth_service.reset_panel()
    session.clear()
    return ok()

@bp.route("/api/auth/status")
def auth_status():
    return ok({
        "isConfigured": auth_service.is_configured(),
        "authenticated": session.get("authenticated", False),
        "username": session.get("username", ""),
        "role": session.get("role", 0),
    })

@bp.route("/logout", methods=["POST"])
def logout():
    # 改为 POST：避免 Logout CSRF。
    # 原来是 GET，攻击者可在恶意页面嵌入 <a href="/logout">点击领奖</a>，
    # 受害者点击即被登出。SameSite=Lax 允许顶层导航 GET 携带 cookie，无法防住。
    # POST 要求携带 CSRF token 或同源 fetch，攻击者跨站无法伪造。
    audit("退出登录", f"用户={session.get('username','?')}")
    session.clear()
    return redirect("/login")

@bp.route("/settings")
def settings_page():
    return render_template("settings.html", active_page="settings")

# ─── 用户管理 ───

@bp.route("/api/users")
def list_users():
    if session.get("role") != 10:
        return fail("无权限")
    users = auth_service.get_users()
    safe = [{"username": u["username"], "role": u["role"],
             "created_at": u.get("created_at", ""),
             "login_at": u.get("login_at", "")}
            for u in users]
    return ok(safe)

@bp.route("/api/users/create", methods=["POST"])
def create_user():
    if session.get("role") != 10:
        return fail("无权限")
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role", 1)
    # 角色范围校验:只允许 1(普通用户)或 10(管理员),防止 role=99 等污染
    if role not in (1, 10):
        return fail("角色不合法")
    if not username or not password:
        return fail("参数不完整")
    valid, msg = auth_service.validate_username(username)
    if not valid:
        return fail(msg)
    valid, msg = auth_service.validate_password(password)
    if not valid:
        return fail(msg)
    ok_, err = auth_service.create_user(username, password, role)
    if ok_:
        audit("创建用户", f"用户名={username} 角色={role}")
        # 弱密码仅提示，不阻止创建
        level, tips = auth_service.check_password_strength(password)
        data = {}
        if level != "strong" and tips:
            data["password_warning"] = {"level": level, "tips": tips}
        return ok(data)
    return fail(err)

@bp.route("/api/users/delete", methods=["POST"])
def delete_user():
    if session.get("role") != 10:
        return fail("无权限")
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    if not username:
        return fail("参数不完整")
    if username == session.get("username"):
        return fail("不能删除自己")
    auth_service.delete_user(username)
    audit("删除用户", f"用户名={username}")
    return ok()

@bp.route("/api/users/reset-password", methods=["POST"])
def reset_user_password():
    if session.get("role") != 10:
        return fail("无权限")
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    new_pw = data.get("password", "")
    if not username or not new_pw:
        return fail("参数不完整")
    valid, msg = auth_service.validate_password(new_pw)
    if not valid:
        return fail(msg)
    ok_, err = auth_service.admin_reset_password(username, new_pw)
    if ok_:
        audit("重置用户密码", f"用户名={username}")
        return ok()
    return fail(err)

# ─── 壁纸设置 ───

@bp.route("/api/settings/wallpaper")
def get_wallpaper():
    url = wp_service.get_wallpaper()
    return ok({"url": url})

@bp.route("/api/settings/wallpaper/fetch", methods=["POST"])
def fetch_wallpaper():
    if session.get("role") != 10:
        return fail("无权限")
    data = {}
    try:
        data = request.get_json(silent=True) or {}
    except Exception:
        pass
    manual_url = (data.get("url") or "").strip()
    if manual_url:
        # SSRF + CSS 注入防护：仅允许 HTTPS URL
        from urllib.parse import urlparse
        parsed = urlparse(manual_url)
        if parsed.scheme != "https":
            return fail("仅支持 HTTPS 协议的图片链接")
        # 阻止引号注入（CSS context 逃逸）
        if '"' in manual_url or "'" in manual_url or '<' in manual_url:
            return fail("图片链接包含非法字符")
        wp_service.save(manual_url)
        audit("设置壁纸(手动)")
        return ok({"url": manual_url})
    url = wp_service.fetch_new()
    if url:
        audit("更换壁纸(随机)")
        return ok({"url": url})
    return fail("获取壁纸失败 - 服务器无法连接壁纸API，请手动输入图片URL")

@bp.route("/api/settings/wallpaper/clear", methods=["POST"])
def clear_wallpaper():
    if session.get("role") != 10:
        return fail("无权限")
    wp_service.clear()
    audit("清除壁纸")
    return ok()
