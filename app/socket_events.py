import time
import threading
from flask import request
from flask_socketio import emit
from app.tooldelta_manager import tooldelta_manager, ansi_to_html, escape_html

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
        from flask import session
        from flask_socketio import disconnect
        if not session.get("authenticated") or session.get("role") != 10:
            disconnect()

    @socketio.on("console_command")
    def handle_command(data):
        from flask import session
        from flask_socketio import disconnect
        # per-event 鉴权：长连接期间 session 可能过期
        if not session.get("authenticated"):
            disconnect()
            return
        # 命令发送需管理员权限，与 /api/tool/command HTTP 端点保持一致
        if session.get("role") != 10:
            emit("console_output", {"type": "system", "data": "无权限发送命令（需管理员）", "data_html": "无权限发送命令（需管理员）"})
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
        from flask import session
        from flask_socketio import disconnect
        if not session.get("authenticated"):
            disconnect()
            return
        # 依赖安装需管理员权限，与 /api/dependencies/install HTTP 端点保持一致
        if session.get("role") != 10:
            emit("console_output", {"type": "system", "data": "无权限安装依赖（需管理员）", "data_html": "无权限安装依赖（需管理员）"})
            return
        return dependency_service.start_install()

    @socketio.on("get_dependency_status")
    def handle_get_dependency_status():
        from flask import session
        from flask_socketio import disconnect
        if not session.get("authenticated"):
            disconnect()
            return
        return dependency_service.get_status()
