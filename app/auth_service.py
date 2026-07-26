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

# 时序旁路防御:用户名不存在时也执行一次固定 scrypt 校验,拉平响应耗时。
# 旧实现 username 不存在时 get_user 返回 None,短路跳过 check_password_hash,
# 响应 ~1ms;存在时 scrypt 校验 ~50-200ms。攻击者可通过响应耗时枚举有效用户名。
# 注意:dummy hash 必须在模块加载时生成一次(scrypt 生成耗时较长),
# 不能每次调用动态生成,否则非存在路径会因生成耗时反而比存在路径慢。
_DUMMY_HASH = generate_password_hash("dummy-password-for-timing", method=HASH_METHOD)

# 公开视图字段白名单:get_users_public/get_user_public 仅返回这些字段,
# 强制剥离 password_hash/session_version 等敏感字段。
# 旧实现 get_users/get_user 直接返回 _read() 的完整字典(含 password_hash),
# 路由层一旦疏忽 jsonify(users) 就会泄露密码哈希给客户端,可离线暴力破解弱密码。
_PUBLIC_FIELDS = ("username", "role", "created_at", "login_at")

def _to_public(u):
    """剥离敏感字段,仅保留可对外暴露的字段。"""
    if not isinstance(u, dict):
        return {}
    return {k: u.get(k) for k in _PUBLIC_FIELDS}

def get_users():
    """返回内部完整用户列表(含 password_hash)。仅供服务层/路由层内部使用。
    面向客户端的接口必须改用 get_users_public。"""
    return _read()

def get_users_public():
    """返回剥离敏感字段后的用户列表,供路由层 jsonify。"""
    return [_to_public(u) for u in _read()]

def get_user(username):
    """返回内部完整用户对象(含 password_hash)。仅供服务层内部使用。"""
    for u in _read():
        if u.get("username") == username:
            return u
    return None

def get_user_public(username):
    """返回剥离敏感字段后的用户对象,供路由层 jsonify。"""
    u = get_user(username)
    return _to_public(u) if u else None

def verify_username_password(username, password):
    u = get_user(username)
    # 时序旁路防御:用户不存在时也执行一次 scrypt 校验(_DUMMY_HASH),
    # 使两条路径(存在/不存在)的响应耗时一致,防止通过耗时差异枚举用户名。
    stored = u.get("password_hash", "") if u else _DUMMY_HASH
    if check_password_hash(stored, password) and u:
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
    # role 白名单校验:仅允许 ROLES.values() 中的合法角色值。
    # 旧实现把 role 参数直接写入持久化文件,路由层若把请求体 role 字段透传
    # (如 role=10 创建管理员,或 role=999 制造超出预期的角色),会导致提权。
    if role not in ROLES.values():
        return False, "角色不合法"
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
    """删除用户。返回 (ok, msg)。
    - 最后一个管理员保护:删除后若无 role==10 用户,面板将永久失去管理能力
      (setup_user 仅在 users 为空时可用,无法补创建管理员,需手动编辑 user.json)。
      防止管理员误删自己或被社工诱导删光所有 admin。
    """
    with _lock:
        users = _read_locked()
        target = next((u for u in users if u.get("username") == username), None)
        if target and target.get("role") == 10:
            remaining_admins = [u for u in users
                                if u.get("role") == 10 and u.get("username") != username]
            if not remaining_admins:
                return False, "至少需保留一个管理员账号"
        users = [u for u in users if u.get("username") != username]
        _write_locked(users)
    return True, ""

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
    """重置面板:删除 user.json 并清理内存中的限流状态。
    关键:必须在 _lock 内执行删除,与 _write_locked 协调。
    旧实现未持锁,isfile 与 remove 之间存在 TOCTOU 窗口:
    若另一线程在此期间通过 _write_locked 重建了 USER_FILE(如 setup_user 抢先创建新管理员),
    remove 会误删新文件,导致刚设置的 admin 丢失。
    同时清理 LOGIN_FAIL_MAP/BAN_MAP:重置后已被封禁的 IP 仍处于封禁状态
    (逻辑不一致,虽然非安全问题但会造成用户困惑)。
    """
    with _lock:
        if USER_FILE and os.path.isfile(USER_FILE):
            try:
                os.remove(USER_FILE)
            except OSError:
                pass
        LOGIN_FAIL_MAP.clear()
        BAN_MAP.clear()

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
