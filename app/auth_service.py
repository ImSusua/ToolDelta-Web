import os
import json
import time
import re
import tempfile
import threading
from werkzeug.security import generate_password_hash, check_password_hash

USER_FILE = None
LOGIN_FAIL_MAP: dict[str, list[float]] = {}
BAN_MAP: dict[str, float] = {}
_lock = threading.Lock()

ROLES = {"admin": 10, "user": 1, "guest": 0}

# 密码哈希算法：scrypt 比 werkzeug 默认的 pbkdf2:sha256 更抗 GPU/ASIC 暴力破解。
# check_password_hash 会从哈希字符串前缀解析算法,故旧 pbkdf2 哈希仍可验证;
# 仅新生成/改密时切换为 scrypt,迁移对老用户透明。
# werkzeug 2.3+ 原生支持 scrypt(项目使用 flask>=3.0,传递依赖 werkzeug>=3.0)。
HASH_METHOD = "scrypt"

def init_app(app):
    global USER_FILE
    USER_FILE = os.path.join(app.instance_path, "user.json")
    os.makedirs(os.path.dirname(USER_FILE), exist_ok=True)
    # 既有 user.json 权限收敛:同主机其他用户不应读取密码哈希
    if os.path.isfile(USER_FILE):
        try:
            os.chmod(USER_FILE, 0o600)
        except OSError:
            pass

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
    # 用 tempfile.mkstemp 生成唯一临时文件(而非固定名 user.json.tmp),
    # 防止未来引入多进程时两个进程同时写 tmp 互相覆盖。
    # 同时补充 flush/fsync 保证数据真正落盘,与 scheduler/watchdog/connection/wallpaper 一致。
    if not USER_FILE:
        return
    fd, tmp = tempfile.mkstemp(prefix="user.", suffix=".tmp", dir=os.path.dirname(USER_FILE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, USER_FILE)
        tmp = None  # 标记已成功 replace，finally 不再删除
        # 收敛权限:含 password_hash,同主机其他用户不可读
        try:
            os.chmod(USER_FILE, 0o600)
        except OSError:
            pass
    finally:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

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
    """初始化面板时创建首个管理员账号。

    关键：必须在 _lock 内检查 users 是否已非空，仅当 users 为空时才允许创建。
    防止抢注攻击：路由层 /api/setup 虽先调 is_configured() 拦截，但两个并发请求
    可能同时通过 is_configured() 检查（TOCTOU），随后都进入 setup_user 写入，
    导致首个管理员账号被抢注或产生两个管理员。锁内二次校验杜绝该竞态。
    """
    valid, msg = validate_username(username)
    if not valid:
        return False, msg
    valid, msg = validate_password(password)
    if not valid:
        return False, msg
    with _lock:
        users = _read_locked()
        # 锁内二次校验：若已存在任何用户（说明面板已被初始化过），
        # 即使路由层 is_configured() 因 TOCTOU 未拦住，这里也必须拒绝创建。
        # 否则攻击者可在合法管理员 setup 过程中抢先创建管理员账号劫持面板。
        if users:
            return False, "面板已初始化"
        # 检查用户是否已存在（理论上 users 为空时永远 False，但保持防御）
        if any(u.get("username") == username for u in users):
            return False, "用户名已存在"
        users.append({
            "username": username,
            "password_hash": generate_password_hash(password, method=HASH_METHOD),
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
            "password_hash": generate_password_hash(password, method=HASH_METHOD),
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
        target["password_hash"] = generate_password_hash(new_password, method=HASH_METHOD)
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
                u["password_hash"] = generate_password_hash(new_password, method=HASH_METHOD)
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
