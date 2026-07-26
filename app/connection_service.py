"""服务器连接配置服务（模块函数 + init_app 风格）。

持久化：<instance_path>/server_conn.json，结构为对象数组。
并发安全：使用 threading.Lock + 原子写（临时文件 + os.replace）。
"""
import os
import json
import tempfile
import threading
import uuid
from datetime import datetime

# 全局文件句柄（由 init_app 设置），未初始化时为 None
_FILE = None
_LOCK = threading.Lock()

# 允许的协议
PROTOCOLS = ("tcp", "ws")


def init_app(app):
    """根据 app.instance_path 设置持久化文件路径并创建目录。"""
    global _FILE
    _FILE = os.path.join(app.instance_path, "server_conn.json")
    # 目录权限收敛:同 instance/secret_key、logs/ 一致,仅本用户可访问
    # (server_conn.json 含 Minecraft 服务器连接 token 明文,同主机其他用户不应读取)
    # makedirs 时直接指定 mode=0o700,消除"先创建 0o755 再 chmod"的 TOCTOU 窗口
    # (与 config.py / market_service.py / log_service.py 一致)
    os.makedirs(os.path.dirname(_FILE), exist_ok=True, mode=0o700)
    try:
        os.chmod(os.path.dirname(_FILE), 0o700)
    except OSError:
        pass
    # 既有 server_conn.json 权限收敛:旧文件可能仍是默认 0o644
    if os.path.isfile(_FILE):
        try:
            os.chmod(_FILE, 0o600)
        except OSError:
            pass


def _read_all():
    """读取全部连接；文件不存在或损坏时返回空列表。"""
    if not _FILE or not os.path.isfile(_FILE):
        return []
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return data
    except Exception:
        return []


def _write_all(conns):
    """原子写：先写临时文件，再 os.replace 覆盖。"""
    if not _FILE:
        return
    d = os.path.dirname(_FILE)
    os.makedirs(d, exist_ok=True)
    # 关键修复(TOCTOU):旧实现 open(tmp, "w") 创建文件默认 mode 0o644,
    # os.replace 后 _FILE 也是 0o644,直到后续 os.chmod 才收敛到 0o600。
    # 在 replace 与 chmod 之间存在 TOCTOU 窗口,同主机其他用户可在此期间
    # open fd 读取 server_conn.json(含 Minecraft 连接 token),即便后续 chmod
    # 也无法关闭已打开的 fd → token 泄露。
    # 修复:用 tempfile.mkstemp 创建临时文件(默认 mode 0o600),写入后
    # os.replace 保留 0o600 权限,无需事后 chmod。
    fd, tmp = tempfile.mkstemp(prefix=".server_conn.tmp.", dir=d)
    # try/finally 确保异常路径下清理 tmp：json.dump/flush/fsync 任一抛异常时
    # os.replace 不会执行，tmp 会残留在磁盘上，多次失败累积多个 .server_conn.tmp.* 文件
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(conns, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _FILE)
        tmp = None  # 标记已成功 replace，finally 不再删除
        # mkstemp 创建的文件已是 0o600,os.replace 保留权限,无需再 chmod
    finally:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def list_connections():
    """返回全部连接（深拷贝，避免外部修改缓存）。"""
    with _LOCK:
        return [dict(c) for c in _read_all()]


def get_connection(conn_id):
    """按 id 获取单个连接，不存在返回 None。"""
    with _LOCK:
        for c in _read_all():
            if c.get("id") == conn_id:
                return dict(c)
    return None


# 字段长度上限，防止持久化过大或展示异常（P2-2）
MAX_CONN_NAME_LEN = 64
MAX_CONN_HOST_LEN = 256
MAX_CONN_TOKEN_LEN = 512
MAX_CONN_NOTE_LEN = 256


def _sanitize(text):
    """去除首尾空白与控制字符，避免持久化或展示时出问题。"""
    if not isinstance(text, str):
        return ""
    return text.strip().replace("\x00", "")


def _validate(payload):
    """校验连接字段，返回 (ok, error_msg)。"""
    name = _sanitize(payload.get("name", ""))
    host = _sanitize(payload.get("host", ""))
    port = payload.get("port")
    if not name:
        return False, "连接名称不能为空"
    if len(name) > MAX_CONN_NAME_LEN:
        return False, "连接名称过长"
    if not host:
        return False, "主机地址不能为空"
    if len(host) > MAX_CONN_HOST_LEN:
        return False, "主机地址过长"
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False, "端口必须是数字"
    if not (1 <= port <= 65535):
        return False, "端口必须在 1-65535 之间"
    return True, ""


def add_connection(payload):
    """新增一个连接，返回 (conn, error_msg)。

    payload 至少应含 name/host/port（由路由层校验）。
    其余字段（protocol/token/note）可选，is_default 默认 False。
    """
    if payload is None:
        payload = {}
    ok, err = _validate(payload)
    if not ok:
        return None, err
    name = _sanitize(payload.get("name", ""))
    host = _sanitize(payload.get("host", ""))
    port = int(payload.get("port") or 0)
    protocol = payload.get("protocol") or "tcp"
    if protocol not in PROTOCOLS:
        protocol = "tcp"
    token = _sanitize(payload.get("token", "") or "")[:MAX_CONN_TOKEN_LEN]
    note = _sanitize(payload.get("note", "") or "")[:MAX_CONN_NOTE_LEN]
    conn = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "host": host,
        "port": port,
        "protocol": protocol,
        "token": token,
        "note": note,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_default": False,
    }
    with _LOCK:
        conns = _read_all()
        conns.append(conn)
        _write_all(conns)
    return conn, ""


def update_connection(conn_id, payload):
    """更新指定 id 的连接，成功返回 True。"""
    if payload is None:
        payload = {}
    with _LOCK:
        conns = _read_all()
        for c in conns:
            if c.get("id") == conn_id:
                for key in ("name", "host", "port", "protocol", "token", "note"):
                    if key in payload:
                        val = payload[key]
                        if key == "port":
                            try:
                                val = int(val)
                            except (TypeError, ValueError):
                                continue
                            if not (1 <= val <= 65535):
                                continue
                        if key in ("name", "host", "token", "note") and isinstance(val, str):
                            val = _sanitize(val)
                            if key == "name":
                                val = val[:MAX_CONN_NAME_LEN]
                            elif key == "host":
                                val = val[:MAX_CONN_HOST_LEN]
                            elif key == "token":
                                val = val[:MAX_CONN_TOKEN_LEN]
                            elif key == "note":
                                val = val[:MAX_CONN_NOTE_LEN]
                        if key == "protocol" and val not in PROTOCOLS:
                            continue
                        c[key] = val if val is not None else ""
                _write_all(conns)
                return True
    return False


def delete_connection(conn_id):
    """删除指定 id 的连接，成功返回 True。"""
    with _LOCK:
        conns = _read_all()
        new = [c for c in conns if c.get("id") != conn_id]
        if len(new) == len(conns):
            return False
        _write_all(new)
        return True


def set_default(conn_id):
    """将指定 id 设为默认（is_default=True），其余置为 False。成功返回 True。"""
    with _LOCK:
        conns = _read_all()
        found = False
        for c in conns:
            c["is_default"] = (c.get("id") == conn_id)
            if c["is_default"]:
                found = True
        if not found:
            return False
        _write_all(conns)
        return True
