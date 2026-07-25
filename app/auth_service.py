import os
import json
import time
import re
import threading
from werkzeug.security import generate_password_hash, check_password_hash

USER_FILE = None
LOGIN_FAIL_MAP: dict[str, list[float]] = {}
BAN_MAP: dict[str, float] = {}
_lock = threading.Lock()

ROLES = {"admin": 10, "user": 1, "guest": 0}

def init_app(app):
    global USER_FILE
    USER_FILE = os.path.join(app.instance_path, "user.json")
    os.makedirs(os.path.dirname(USER_FILE), exist_ok=True)

def _read():
    with _lock:
        return _read_locked()

def _read_locked():
    if not USER_FILE or not os.path.isfile(USER_FILE):
        return []
    try:
        with open(USER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return [data]
        return data
    except (json.JSONDecodeError, FileNotFoundError, IOError, OSError):
        return []

def _write(users):
    with _lock:
        _write_locked(users)

def _write_locked(users):
    # 原子写：先写临时文件再替换，避免写一半崩溃导致用户数据丢失
    if not USER_FILE:
        return
    tmp = USER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
    os.replace(tmp, USER_FILE)

# 用户名规则：字母/数字/下划线/连字符/中文，长度 1-32，避免控制字符或路径遍历（P1-2）
# 支持中文（CJK统一表意文字范围），不允许全为空白或纯标点
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff\u3400-\u4dbf]{1,32}$")

def validate_username(username):
    if not isinstance(username, str):
        return False, "用户名必须是字符串"
    if not _USERNAME_RE.match(username):
        return False, "用户名需为1-32位字母、数字、下划线、连字符或中文"
    return True, ""

def validate_password(password):
    """校验密码格式：长度 8-64，且至少同时包含字母和数字。"""
    if not isinstance(password, str) or len(password) < 8 or len(password) > 64:
        return False, "密码长度需8-64位"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return False, "密码需同时包含字母和数字"
    return True, ""

def check_password_strength(password):
    """检查密码强度，返回 (level, tips)。
    level: 'strong' | 'medium' | 'weak'
    tips: 改进建议列表
    弱密码仅提示，不阻止创建账号。
    """
    tips = []
    if not isinstance(password, str) or not password:
        return "weak", ["密码不能为空"]
    if len(password) < 8:
        tips.append("建议密码长度至少8位")
    has_letter = bool(re.search(r"[A-Za-z]", password))
    has_digit = bool(re.search(r"\d", password))
    has_special = bool(re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>/?]", password))
    if not has_letter:
        tips.append("建议包含字母")
    if not has_digit:
        tips.append("建议包含数字")
    if not has_special:
        tips.append("建议包含特殊字符可增强安全性")
    if password.lower() in ("password123", "12345678", "qwerty123", "admin123", "11111111"):
        tips.append("该密码过于常见，极易被破解")
    if len(password) >= 12 and has_letter and has_digit and has_special:
        return "strong", tips if tips else []
    if len(password) >= 8 and has_letter and has_digit:
        return "medium", tips
    return "weak", tips

def is_configured():
    users = _read()
    return len(users) > 0

def get_users():
    return _read()

def get_user(username):
    for u in _read():
        if u.get("username") == username:
            return u
    return None

def verify_username_password(username, password):
    u = get_user(username)
    if u and check_password_hash(u.get("password_hash", ""), password):
        return u
    return None

def verify_password(password):
    users = _read()
    for u in users:
        if u.get("role") == 10 and check_password_hash(u.get("password_hash", ""), password):
            return u
    return None

def setup_user(username, password, role=10):
    valid, msg = validate_username(username)
    if not valid:
        return False, msg
    valid, msg = validate_password(password)
    if not valid:
        return False, msg
    with _lock:
        users = _read_locked()
        # 检查用户是否已存在，避免重复创建或覆盖现有管理员
        if any(u.get("username") == username for u in users):
            return False, "用户名已存在"
        users.append({
            "username": username,
            "password_hash": generate_password_hash(password),
            "role": role,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "login_at": "",
            "session_version": 1   # 用于失效既有 session：登录时存入 session，before_request 校验
        })
        _write_locked(users)
    return True, ""

def create_user(username, password, role=1):
    valid, msg = validate_username(username)
    if not valid:
        return False, msg
    valid, msg = validate_password(password)
    if not valid:
        return False, msg
    with _lock:
        users = _read_locked()
        if any(u.get("username") == username for u in users):
            return False, "用户名已存在"
        users.append({
            "username": username,
            "password_hash": generate_password_hash(password),
            "role": role,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "login_at": "",
            "session_version": 1
        })
        _write_locked(users)
    return True, ""

def delete_user(username):
    with _lock:
        users = _read_locked()
        users = [u for u in users if u.get("username") != username]
        _write_locked(users)
    return True

def change_password(username, old_password, new_password):
    valid, msg = validate_password(new_password)
    if not valid:
        return False, msg
    with _lock:
        users = _read_locked()
        target = next((u for u in users if u.get("username") == username), None)
        if target is None:
            return False, "用户不存在"
        if old_password is not None:
            if not check_password_hash(target.get("password_hash", ""), old_password):
                return False, "旧密码错误"
        target["password_hash"] = generate_password_hash(new_password)
        # 递增 session_version：旧 session 中存的版本号失效，before_request 强制踢出
        # 让被改密用户的既有会话立刻失效，避免攻击者窃取的旧 session 在 30 天有效期内继续可用
        target["session_version"] = target.get("session_version", 1) + 1
        _write_locked(users)
    return True, ""

def admin_reset_password(username, new_password):
    valid, msg = validate_password(new_password)
    if not valid:
        return False, msg
    with _lock:
        users = _read_locked()
        found = False
        for u in users:
            if u.get("username") == username:
                u["password_hash"] = generate_password_hash(new_password)
                # 递增 session_version：管理员重置密码后该用户的所有既有 session 立即失效
                u["session_version"] = u.get("session_version", 1) + 1
                found = True
        if found:
            _write_locked(users)
        return found, "" if found else "用户不存在"

def get_user_session_version(username):
    """读取用户当前的 session_version。用户不存在或字段缺失时返回 None。
    before_request 用此函数校验 session 中的版本号是否过期。"""
    if not username:
        return None
    users = _read()
    for u in users:
        if u.get("username") == username:
            return u.get("session_version", 1)
    return None

def reset_panel():
    if USER_FILE and os.path.isfile(USER_FILE):
        os.remove(USER_FILE)

def update_login_time(username):
    with _lock:
        users = _read_locked()
        for u in users:
            if u.get("username") == username:
                u["login_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_locked(users)

def get_admin_username():
    for u in _read():
        if u.get("role") == 10:
            return u.get("username", "")
    return ""

# ─── Login rate limiting ───

def _cleanup_old_fails(now):
    """清理超过30分钟的旧失败记录与过期封禁，防止内存无限增长。"""
    for ip in list(LOGIN_FAIL_MAP.keys()):
        fails = [t for t in LOGIN_FAIL_MAP[ip] if now - t < 300]
        if fails:
            LOGIN_FAIL_MAP[ip] = fails
        else:
            LOGIN_FAIL_MAP.pop(ip, None)
    for ip in list(BAN_MAP.keys()):
        if now - BAN_MAP[ip] >= 600:
            BAN_MAP.pop(ip, None)

def check_login_rate(ip):
    with _lock:
        now = time.time()
        _cleanup_old_fails(now)
        if ip in BAN_MAP:
            if now - BAN_MAP[ip] < 600:
                return False, "IP已被临时封禁，请10分钟后重试"
            del BAN_MAP[ip]
        fails = LOGIN_FAIL_MAP.get(ip, [])
        fails = [t for t in fails if now - t < 300]
        if len(fails) >= 10:
            BAN_MAP[ip] = now
            LOGIN_FAIL_MAP[ip] = []
            return False, "登录失败次数过多，IP已被封禁10分钟"
        LOGIN_FAIL_MAP[ip] = fails
        return True, ""

def record_login_fail(ip):
    with _lock:
        now = time.time()
        fails = LOGIN_FAIL_MAP.get(ip, [])
        fails.append(now)
        LOGIN_FAIL_MAP[ip] = fails
        _cleanup_old_fails(now)

def clear_login_fails(ip):
    with _lock:
        LOGIN_FAIL_MAP.pop(ip, None)
