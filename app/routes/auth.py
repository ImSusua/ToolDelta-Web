from flask import Blueprint, render_template, request, jsonify, session, redirect
from app import auth_service
from app.log_service import log_service
from app import wallpaper_service as wp_service
import time

bp = Blueprint("auth", __name__)

def ok(data=None):
    r = {"success": True}
    if data is not None:
        r["data"] = data
    return jsonify(r)

def fail(msg):
    return jsonify({"success": False, "error": msg})

def audit(action, detail=""):
    user = session.get("username", "?")
    ip = request.remote_addr or "?"
    log_service.info(f"[{user}@{ip}] {action} {detail}", "AUDIT")

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
    ip = request.remote_addr or "?"
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
    ip = request.remote_addr or "?"
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
        log_service.warn(f"[{ip}] 登录失败(非法用户名): {username}", "AUDIT")
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
        audit("修改密码", f"用户={username}")
        level, tips = auth_service.check_password_strength(new_pw)
        data = {}
        if level != "strong" and tips:
            data["password_warning"] = {"level": level, "tips": tips}
        return ok(data)
    return fail(err)

@bp.route("/api/reset-panel", methods=["POST"])
def reset_panel():
    if session.get("role") != 10:
        return fail("无权限")
    ip = request.remote_addr or "?"
    allowed, msg = auth_service.check_login_rate(ip)
    if not allowed:
        return fail(msg)
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if not auth_service.verify_password(password):
        auth_service.record_login_fail(ip)
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

@bp.route("/logout")
def logout():
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
