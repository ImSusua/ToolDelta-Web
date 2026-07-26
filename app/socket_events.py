import time
import threading
from flask import request, session
from flask_socketio import emit, disconnect
from app.tooldelta_manager import tooldelta_manager, ansi_to_html, escape_html
from app import auth_service


def _session_valid(require_admin=True):
    """校验当前 socket 会话是否有效。

    补齐 HTTP 层 before_request 的 session_version 校验:被删除/改密的管理员
    旧 cookie 仍带 authenticated=True/role=10,但 session_version 已过期。
    HTTP 请求每次都走 before_request 重新校验,WebSocket 长连接却只在 connect
    时校验一次,后续事件只读 cookie,会使被吊销的管理员在 8h 会话期内仍能
    通过既有连接操控面板。这里在每个事件入口集中校验,与 HTTP 层对齐。

    返回 (ok, error_emit_or_None):
      - ok=True: 通过,可继续执行业务
      - ok=False: 拒绝,且已调用 disconnect()/emit() 反馈,调用方应直接 return
    """
    if not session.get("authenticated"):
        disconnect()
        return False, None
    if require_admin and session.get("role") != 10:
        return False, None
    # session_version 校验:用户被删/改密/重置密码后 session_version 递增,
    # cookie 中的旧版本号失效,强制断开该长连接
    ses_user = session.get("username", "")
    ses_ver = session.get("session_version")
    if ses_user and ses_ver is not None:
        cur_ver = auth_service.get_user_session_version(ses_user)
        if cur_ver is None or cur_ver != ses_ver:
            session.clear()
            disconnect()
            return False, None
    return True, None

# 命令发送频率限制：同一客户端 1 秒内最多 10 条，防止刷屏/暴力输入（P2-7）
# 用 sid（Socket.IO 连接 id）作为 key 而非 remote_addr：反代/容器部署下所有请求
# remote_addr 都是同一个 IP（如 127.0.0.1），以 IP 为 key 会导致全员共享额度，
# 一人刷屏就触发限速拖累所有人。sid 与单个浏览器连接绑定，准确反映单用户行为。
_CMD_RATE_LIMIT = 10
_CMD_RATE_WINDOW = 1.0
_cmd_rate_map: dict[str, list[float]] = {}
_cmd_rate_lock = threading.Lock()


def _cleanup_cmd_rate(now):
    """清理过期的命令速率记录，防止长期运行后内存无限增长（P2-8）。"""
    expired = [k for k, window in _cmd_rate_map.items() if not window or now - window[-1] >= _CMD_RATE_WINDOW * 2]
    for k in expired:
        _cmd_rate_map.pop(k, None)


def _check_cmd_rate(key):
    with _cmd_rate_lock:
        now = time.time()
        # 每 100 次检查做一次轻量清理，平衡内存与性能
        if len(_cmd_rate_map) >= 1000 and hash(key) % 100 == 0:
            _cleanup_cmd_rate(now)
        window = _cmd_rate_map.get(key, [])
        window = [t for t in window if now - t < _CMD_RATE_WINDOW]
        if len(window) >= _CMD_RATE_LIMIT:
            _cmd_rate_map[key] = window
            return False
        window.append(now)
        _cmd_rate_map[key] = window
        return True


def init_socketio(socketio):
    # 注册前清空监听器，避免重复初始化时同一事件被多次广播
    tooldelta_manager.clear_listeners()

    @socketio.on("connect")
    def handle_connect():
        # 鉴权：未登录或非管理员的 WebSocket 连接直接断开，防止未授权访问控制台
        # 控制台输出可能含 token/路径/堆栈等敏感信息，仅管理员可访问（与 /api/tool/output 一致）
        # fail-closed：会话校验异常（含未登录）一律拒绝连接，避免误放行
        # return False 是 Flask-SocketIO 拒绝连接的官方方式（仅 disconnect() 在
        # 函数体末尾会依赖框架隐式行为，未来若追加逻辑可能在已拒绝的连接上误执行）
        if not session.get("authenticated") or session.get("role") != 10:
            return False
        # session_version 校验:cookie 可能是改密/删用户前的旧值,校验后才允许建连
        ses_user = session.get("username", "")
        ses_ver = session.get("session_version")
        if ses_user and ses_ver is not None:
            cur_ver = auth_service.get_user_session_version(ses_user)
            if cur_ver is None or cur_ver != ses_ver:
                session.clear()
                return False

    @socketio.on("console_command")
    def handle_command(data):
        # per-event 鉴权 + session_version 校验:长连接期间 session 可能过期或被吊销
        ok, _ = _session_valid(require_admin=True)
        if not ok:
            return
        # 兼容字符串与 {"cmd": "..."} / {"command": "..."} 两种前端格式
        if isinstance(data, dict):
            cmd = data.get("cmd") or data.get("command") or ""
        elif isinstance(data, str):
            cmd = data
        else:
            cmd = ""
        # 确保命令是字符串，防止非字符串类型导致 .strip() 崩溃
        if not isinstance(cmd, str):
            cmd = str(cmd) if cmd else ""
        cmd = cmd.strip()
        if not cmd:
            return
        # 长度校验与前端保持一致，防止超长命令冲击子进程
        if len(cmd) > tooldelta_manager.MAX_COMMAND_LEN:
            emit("console_output", {"type": "system", "data": "命令过长，已被忽略", "data_html": "命令过长，已被忽略"})
            return
        # 速率限制：用 sid 而非 remote_addr（反代下所有用户 IP 相同，会共享额度）
        rate_key = request.sid if hasattr(request, "sid") else "anon"
        if not _check_cmd_rate(rate_key):
            emit("console_output", {"type": "system", "data": "发送过于频繁，请稍候", "data_html": "发送过于频繁，请稍候"})
            return
        # 检查 send_command 返回值：进程未运行时 send_command 静默返回 False，
        # 旧逻辑不反馈，前端会以为命令已发送。这里明确 emit 提示，避免静默丢失
        ok = tooldelta_manager.send_command(cmd)
        if not ok:
            emit("console_output", {"type": "system", "data": "命令未发送：ToolDelta 进程未运行", "data_html": "命令未发送：ToolDelta 进程未运行"})

    def broadcast_listener(type_, data):
        try:
            html = ansi_to_html(data) if data else data
        except Exception:
            # 转换异常时降级为纯文本，避免单行异常导致整条广播失败、控制台丢行
            try:
                html = escape_html(data) if data else ""
            except Exception:
                html = data
        try:
            if type_ == "output":
                socketio.emit("console_output", {
                    "type": "output",
                    "data": data,
                    "data_html": html,
                })
            elif type_ == "system":
                socketio.emit("console_output", {
                    "type": "system",
                    "data": data,
                    "data_html": html,
                })
        except Exception:
            # emit 异常不应中断输出线程
            pass

    tooldelta_manager.add_listener(broadcast_listener)

    # ─── ToolDelta 运行依赖管理（网站内自管） ──
    from app.dependency_service import dependency_service
    dependency_service.clear_listeners()  # 避免重复初始化时重复广播

    def dependency_listener(type_, data):
        if type_ == "dependency_progress":
            socketio.emit("dependency_progress", data)

    dependency_service.add_listener(dependency_listener)

    @socketio.on("install_dependencies")
    def handle_install_dependencies():
        # 依赖安装需管理员权限 + session_version 校验,与 /api/dependencies/install 一致
        ok, _ = _session_valid(require_admin=True)
        if not ok:
            return
        return dependency_service.start_install()

    @socketio.on("get_dependency_status")
    def handle_get_dependency_status():
        # 与 install_dependencies 鉴权保持一致:get_status 返回 log_tail/mirror_url
        # 等可能含内部路径的字段,仅管理员可读(原仅校验 authenticated,这里补齐 role)
        ok, _ = _session_valid(require_admin=True)
        if not ok:
            return
        return dependency_service.get_status()
