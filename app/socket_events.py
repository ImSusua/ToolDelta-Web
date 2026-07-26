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
    # cookie 中的旧版本号失效,强制断开该长连接。
    # 关键 fail-closed 修复:旧实现仅在 `if ses_user and ses_ver is not None`
    # 时才校验版本,当 username 缺失或 session_version 缺失时直接跳过校验。
    # 这意味着伪造的 cookie(authenticated=True, role=10 但无 username/session_version)
    # 可绕过版本校验,在长连接上持续操控面板。合法会话在登录时必定同时写入
    # username 与 session_version,缺失任一即视为非法/篡改会话,fail-closed 拒绝。
    ses_user = session.get("username", "")
    ses_ver = session.get("session_version")
    if require_admin:
        # 管理员会话必须同时具备 username + session_version,缺一即拒绝
        if not ses_user or ses_ver is None:
            session.clear()
            disconnect()
            return False, None
        cur_ver = auth_service.get_user_session_version(ses_user)
        if cur_ver is None or cur_ver != ses_ver:
            session.clear()
            disconnect()
            return False, None
    else:
        # 非管理员:有 username 则校验版本,无则放行(兼容早期普通用户会话)
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
        # session_version 校验:cookie 可能是改密/删用户前的旧值,校验后才允许建连。
        # fail-closed:管理员会话必须同时具备 username + session_version,缺一即拒绝,
        # 防止伪造的 cookie(authenticated=True, role=10 但无 username/session_version)
        # 绕过版本校验建连(与 _session_valid 修复同源)。
        ses_user = session.get("username", "")
        ses_ver = session.get("session_version")
        if not ses_user or ses_ver is None:
            session.clear()
            return False
        cur_ver = auth_service.get_user_session_version(ses_user)
        if cur_ver is None or cur_ver != ses_ver:
            session.clear()
            return False
        # CSWSH(Cross-Site WebSocket Hijacking)防御:浏览器发起跨站 WebSocket
        # 时会自动携带受害者 cookie,恶意页面可借受害者身份建立连接操控面板。
        # Flask-SocketIO 的 cors_allowed_origins 已在握手层拦截跨域,但此处
        # 额外校验 Origin 头作为 defense-in-depth:防止 SOCKETIO_CORS_ALLOWED_ORIGINS
        # 被误配为 "*" 通配,或框架版本差异导致握手层校验失效。
        # 浏览器必定发送 Origin 头;无 Origin 视为非浏览器客户端(curl 等),
        # 会话校验已覆盖鉴权,放行。
        origin = request.headers.get("Origin")
        if origin:
            try:
                from urllib.parse import urlparse
                o = urlparse(origin)
                # 比较 Origin 的 host:port 与当前请求 Host 头(反代下已透传真实 host)
                # 仅 host+port 比较,scheme 不校验(ws/wss 混用属配置问题,非 Origin 越权)
                # 关键修复:旧实现仅比较 hostname 未比较 port,同主机不同端口的恶意页面
                # 可建立 WebSocket 连接(若 SOCKETIO_CORS_ALLOWED_ORIGINS 被误配为通配,
                # 框架层 CORS 兜底失效时此处成为最后防线)。
                if o.hostname:
                    req_host, _, req_port = request.host.partition(":")
                    if o.hostname != req_host:
                        return False
                    # 若 Origin 含端口,则端口也须匹配(防御同主机不同端口的跨站页面)
                    if o.port is not None and req_port and str(o.port) != req_port:
                        return False
            except Exception:
                # 解析异常时 fail-closed 拒绝,避免畸形 Origin 绕过校验
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
        # 频率限制:get_status 会解析依赖文件/拼接 log_tail,有 IO+CPU 开销。
        # 无限流时恶意客户端可每秒发数百次事件造成 CPU 飙升。复用 _check_cmd_rate
        # 但用独立 key 前缀 "dep_status:",与命令发送速率桶隔离,互不影响。
        rate_key = "dep_status:" + (request.sid if hasattr(request, "sid") else "anon")
        if not _check_cmd_rate(rate_key):
            return None
        return dependency_service.get_status()
