import os
import sys
import threading
import traceback
from datetime import timedelta
from urllib.parse import urlparse
from flask import Flask, session, redirect, request
from flask_socketio import SocketIO
from config import Config

socketio = SocketIO()


def _validate_sio_origin(o):
    """校验单个 Socket.IO CORS origin 字符串是否合法。

    返回 (normalized_origin, error_reason):
      - 合法: (origin, None)
      - 非法: (None, reason)

    关键安全约束:
      1) 拒绝 '*' 通配: Socket.IO 携带会话 cookie,允许任意源 = 任意网站可
         借受害者 cookie 发起已认证 WebSocket 连接(可读终端输出/触发高危操作)。
         本面板为高权限管理工具,即便运维误配 '*' 也不能放行,作为安全护栏。
      2) 仅允许 http/https scheme: 拒绝 file://、ftp://、data: 等无意义协议。
      3) 必须包含 host: 拒绝裸 scheme、纯路径等畸形值。
      4) 不允许 path/query/fragment: CORS origin 仅含 scheme://host[:port],
         带 path 的值在浏览器层面不会匹配任何实际请求 origin,属于配置错误。
      5) 标准化: scheme/host 小写、去除末尾斜杠,避免大小写/尾斜杠差异导致
         白名单条目失效(Flask-SocketIO 内部用字符串精确匹配,不做归一化)。
    """
    if o == "*":
        return None, "通配符 * 被禁止(Socket.IO 携带会话 cookie,不允许跨站通配)"
    if not o:
        return None, "空字符串"
    try:
        parsed = urlparse(o)
    except Exception as e:
        return None, f"URL 解析失败: {e}"
    if parsed.scheme not in ("http", "https"):
        return None, f"协议必须为 http/https,实际为 {parsed.scheme or '空'}"
    if not parsed.netloc:
        return None, "缺少 host"
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None, "origin 仅允许 scheme://host[:port],不应包含 path/query/fragment"
    return f"{parsed.scheme}://{parsed.netloc.rstrip('/')}", None


def _thread_excepthook(args):
    """daemon 线程未捕获异常兜底：避免线程静默消亡而无任何日志（P1-5）"""
    try:
        from app.log_service import log_service
        log_service.error(
            f"未捕获异常 thread={args.thread.name}: {args.exc_value}",
            source="SYSTEM"
        )
    except Exception:
        pass


def _sys_excepthook(exc_type, exc_value, exc_tb):
    """主线程未捕获异常兜底：写入日志后再交给默认 hook 输出到 stderr（P1-5）"""
    try:
        if not issubclass(exc_type, KeyboardInterrupt):
            from app.log_service import log_service
            log_service.error(
                "未捕获异常: " + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
                source="SYSTEM"
            )
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)

def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_object(Config)
    web_data_dir = app.config.get("WEB_DATA_DIR")
    if web_data_dir:
        # 权限收敛: 与 log_service.py 的 logs_dir 保持一致使用 0o700。
        # WEB_DATA_DIR 存放用户偏好/收藏等数据,部分字段可能含用户标识/路径等
        # 敏感信息,不应被同主机其他用户读取。直接 makedirs(mode=0o700) 避免
        # open 创建子文件与事后 chmod 之间的 TOCTOU 窗口被同主机用户抢 fd。
        # makedirs 的 mode 会被进程 umask 削弱,但 0o700 不含 group/other 位,
        # umask 无法进一步削弱它(umask 只能"收紧"不能"放宽")。
        os.makedirs(web_data_dir, mode=0o700, exist_ok=True)
        # 兜底已存在目录(历史版本以 0o755 创建)的权限,避免遗留目录过宽
        try:
            os.chmod(web_data_dir, 0o700)
        except OSError:
            pass
    _flask_env = os.environ.get("FLASK_ENV", "production").lower()
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0 if _flask_env == "development" else 31536000
    app.config["SESSION_PERMANENT"] = True
    # 会话有效期 8 小时:这是控制 Minecraft bot 的高权限管理面板,
    # 30 天有效期使被窃取的 session 长期可用,即使有 session_version 失效机制
    # 也仅覆盖改密/删除场景。8 小时滚动续期兼顾安全与可用性。
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
    # 滚动续期:每次请求自动刷新 session 过期时间,只要用户活跃就不会被踢
    app.config["SESSION_REFRESH_EACH_REQUEST"] = True
    # 安全 cookie：生产环境（FLASK_ENV 非 development）默认强制 Secure，
    # 防止会话 cookie 在 HTTP 上明文传输被中间人嗅探劫持。
    # 开发环境（FLASK_ENV=development）默认关闭以便本地 HTTP 调试，
    # 仍可通过显式设置 SESSION_COOKIE_SECURE=1 强制开启。
    _is_prod = _flask_env != "development"
    _secure_default = "true" if _is_prod else "false"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", _secure_default).lower() in ("1", "true", "yes")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    # SameSite=Strict:Lax 允许顶级 GET 导航跨站携带 cookie,Strict 完全禁止。
    # 管理面板不需要被外部链接跳入时保持登录态,Strict 更安全
    # (阻断跨站 GET 触发的 CSRF 残余面,如 <a href=/api/...> 跳转)。
    # 可通过环境变量显式回退为 Lax(如需支持 OAuth 回调导航)。
    app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get("SESSION_COOKIE_SAMESITE", "Strict")

    from app.routes import main, console, plugins, market, backup, commands, api, logs, auth, files, connections, watchdog, scheduler, dashboard
    app.register_blueprint(main.bp)
    app.register_blueprint(console.bp)
    app.register_blueprint(plugins.bp)
    app.register_blueprint(market.bp)
    app.register_blueprint(backup.bp)
    app.register_blueprint(commands.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(logs.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(files.bp)
    app.register_blueprint(connections.bp)
    app.register_blueprint(watchdog.bp)
    app.register_blueprint(scheduler.bp)
    app.register_blueprint(dashboard.bp)

    # Socket.IO CORS:默认仅允许同源(携带会话 cookie 时禁止任意跨域连接),
    # 生产可通过环境变量 SOCKETIO_CORS_ALLOWED_ORIGINS 配置逗号分隔白名单(如 https://panel.example.com)
    # 关键安全约束(见 _validate_sio_origin):
    #   - 拒绝 '*' 通配: Socket.IO 携带会话 cookie,允许任意源 = CSWSH 风险,
    #     即便运维误配 '*' 也不能放行(作为安全护栏)。
    #   - 仅允许 http/https scheme + host[:port],拒绝裸域名/file:// 等。
    #   - 非法条目告警并跳过,而非静默接受;全部非法则回退到同源 None,
    #     避免 Flask-SocketIO 把空 list 误解为"允许任意 origin"。
    # 防御纵深: socket_events.py 的 _session_valid 在每个事件 handler 入口
    # 额外校验 Origin 头,即使此处配置被绕过/框架版本差异也能拦截 CSWSH。
    _sio_origins_env = os.environ.get("SOCKETIO_CORS_ALLOWED_ORIGINS", "").strip()
    if _sio_origins_env:
        _sio_origins = []
        for _o in _sio_origins_env.split(","):
            _o = _o.strip()
            if not _o:
                continue
            _norm, _err = _validate_sio_origin(_o)
            if _norm:
                # 去重(标准化后可能出现重复,如 https://a.com 与 https://a.com/)
                if _norm not in _sio_origins:
                    _sio_origins.append(_norm)
            else:
                # 非法 origin 告警 + 跳过:绝不静默忽略
                # (避免管理员误配 * 自以为开放但实际被拒,或误填裸域名导致 CORS 失败)
                try:
                    from app.log_service import log_service as _sio_log
                    _sio_log.warn(
                        f"Socket.IO CORS origin 被拒绝: {_o!r} ({_err})",
                        source="SYSTEM"
                    )
                except Exception:
                    pass
        if not _sio_origins:
            # 全部条目都非法时回退到同源 None:
            # Flask-SocketIO 收到空 list 行为未定义(可能被解释为允许任意 origin,
            # 与安全意图相反),统一回退 None(仅同源)更安全。
            _sio_origins = None
    else:
        # 同源:Flask-SocketIO 中 None 表示仅允许同源连接
        _sio_origins = None
    socketio.init_app(app, cors_allowed_origins=_sio_origins, async_mode="threading")

    from app.tooldelta_manager import tooldelta_manager
    tooldelta_manager.init_app(app)

    # 依赖自管模块：初始化上下文，供 socket 事件与 start() 使用
    from app.dependency_service import dependency_service
    dependency_service.init_app(app)

    from app.log_service import log_service
    log_service.init_app(app)

    from . import auth_service as auth_svc
    auth_svc.init_app(app)

    from . import wallpaper_service as wp_svc
    wp_svc.init_app(app)

    from app.socket_events import init_socketio
    init_socketio(socketio)

    # 初始化即解压出厂主程序（让 pyproject.toml 存在），再检测并后台安装运行依赖，
    # 避免全新 Linux 环境“点启动才装、30s 超时装不完、起不来”的问题
    try:
        tooldelta_manager._ensure_main_program()
    except Exception as e:
        log_service.error("主程序初始化失败: " + str(e), "SYSTEM")
    dependency_service.maybe_auto_install()

    # 新增模块（P1/P2 增强）
    from app import connection_service as conn_svc
    conn_svc.init_app(app)
    from app.watchdog_service import watchdog_service
    watchdog_service.init_app(app)
    from app.scheduler_service import scheduler_service
    scheduler_service.init_app(app)
    from app.dashboard_service import dashboard_service
    dashboard_service.init_app(app)

    @app.before_request
    def check_auth():
        if request.method == "OPTIONS":
            return
        path = request.path
        # 已配置后不应再访问 setup 页面，避免误操作重新初始化
        if auth_svc.is_configured() and (path == "/setup" or path.startswith("/api/setup")):
            return redirect("/")
        # 注意：/api/reset-panel 不在白名单中。
        # reset-panel 是高危重置操作（清空所有账号配置），必须在全局登录态校验通过后
        # 再由路由层做 role==10 + 密码二次确认。若放入白名单，路由层校验一旦被改松或误删，
        # 全局层不会兜底，攻击者可未登录触发重置。纵深防御：先过全局认证，再做高危校验。
        allowed_prefixes = ["/login", "/setup", "/api/login", "/api/setup", "/logout", "/static/"]
        # 精确匹配或目录前缀(/static/ 末尾已带 /):
        # 旧实现 path.startswith(p) 会让 /login-foo /login_xxx 等被误放行,
        # 虽然当前没有这类端点,但若未来新增 /login-callback 等接口会被白名单误盖。
        # 这里改为 p + "/" 前缀匹配,确保仅匹配目录或精确路径。
        if any(path == p or (p.endswith("/") and path.startswith(p)) or path.startswith(p + "/") for p in allowed_prefixes):
            return
        if not auth_svc.is_configured():
            if path != "/setup":
                return redirect("/setup")
            return
        if not session.get("authenticated"):
            return redirect("/login")
        # session_version 校验：用户被删除 / 改密 / 管理员重置密码后，其 user.session_version
        # 已递增；session 中存的旧版本号失效，强制清空 session 重新登录，避免攻击者窃取的
        # 旧 session 在 30 天 PERMANENT_SESSION_LIFETIME 内继续可用
        ses_user = session.get("username", "")
        ses_ver = session.get("session_version")
        if ses_user and ses_ver is not None:
            cur_ver = auth_svc.get_user_session_version(ses_user)
            # 用户被删除返回 None，或版本号不一致 → 立即失效
            if cur_ver is None or cur_ver != ses_ver:
                session.clear()
                return redirect("/login")
        else:
            # 旧 session（迁移期）无 session_version，补一次校验
            cur_ver = auth_svc.get_user_session_version(ses_user) if ses_user else None
            if ses_user and cur_ver is None:
                session.clear()
                return redirect("/login")

    @app.context_processor
    def inject_wallpaper():
        return {"wallpaper_url": wp_svc.get_wallpaper()}

    @app.context_processor
    def inject_versions():
        # 注入三版本信息供设置页等模板展示（P2-5）
        try:
            from app.dashboard_service import dashboard_service
            return {"versions": dashboard_service.get_version_info()}
        except Exception:
            return {"versions": {"web_version": "1.0", "build_hash": "nogit", "tooldelta_version": "—"}}

    @app.after_request
    def add_security_headers(response):
        # 基础安全响应头：防点击劫持、MIME 嗅探、XSS 过滤
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # 限制前端 JS 能力，防止 XSS 后滥用敏感 API（摄像头/地理位置等）
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # CSP:限制脚本/样式/连接来源,防止 XSS 后加载外部资源
        # 旧版 img-src 'self' data: https: 中 https: 允许任意 HTTPS 图片源,XSS 后
        # 可用 <img src="https://attacker/?leak=..."> 外泄数据;改为 'self' data:
        # 仅允许本站与 data: 内联图(壁纸 url() 通过 backgroundImage 设置不受 img-src 限制,
        # 由 style-src 控制但 style 不发外网请求,实际壁纸 URL 已在 save() 校验内只允许
        # http/https,故不影响壁纸功能)
        # connect-src 旧版 ws: wss: 无主机限制,XSS 后可向任意 WebSocket 服务器推送数据;
        # Socket.IO 默认同源(参见 socketio.init_app 配置),这里收紧为 'self' wss: ws:
        # 仍保留 ws/wss 是因为开发环境用 http+ws,但若部署在生产 https+wss 且无跨域需求,
        # 可通过环境变量 CSP_CONNECT_SRC 显式收紧为 'self' wss://<host>
        # 新增 form-action 'self':XSS 可构造表单向攻击者服务器提交数据,form-action 限制
        # 表单提交目标
        # img-src 关键修复:旧 HTTP 头为 'self' data: 但 base.html meta 为
        # 'self' data: blob: https:,浏览器取并集使 https: 生效,XSS 后可外泄数据
        # 到任意 HTTPS 服务器。现统一为 'self' data: cdn.8845.top(壁纸 CDN 白名单),
        # 仅允许该可信 CDN 的图片加载,而非任意 https: 站点。
        # 管理员手动设置的壁纸 URL 也需在 cdn.8845.top 白名单内,否则无法显示
        # (建议引导用户使用 fetch_new 随机壁纸而非手动 URL)。
        _csp_connect = os.environ.get("CSP_CONNECT_SRC", "'self' ws: wss:")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: cdn.8845.top; "
            "connect-src " + _csp_connect + "; "
            "font-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'self'"
        )
        # HSTS:生产环境(FLASK_ENV != development)默认开启,
        # 浏览器在有效期内强制 HTTPS,防止首次 HTTP 请求被中间人降级嗅探。
        # HSTS 仅在浏览器实际通过 HTTPS 访问时生效(反代场景下浏览器看到的是 HTTPS),
        # 即便后端是 HTTP 也无副作用。开发环境默认关闭,可用 ENABLE_HSTS=1 显式开启。
        _hsts_default = "true" if _is_prod else "false"
        if os.environ.get("ENABLE_HSTS", _hsts_default).lower() in ("1", "true", "yes"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    # 注册全局异常钩子：daemon 线程与主线程未捕获异常均记录到日志，
    # 避免线程静默消亡后无任何线索可查（P1-5）
    threading.excepthook = _thread_excepthook
    sys.excepthook = _sys_excepthook

    # ─── 全局错误处理：API 路径返回 JSON 而非 HTML ───
    # 之前路由抛异常会冒到 Flask 默认 500 HTML 页面，前端 fetch 的 r.json() 解析失败
    # 走 catch 分支后丢失服务器错误信息；这里对 /api/ 路径统一返回 JSON 结构
    from flask import jsonify as _jsonify, request as _req
    from app.log_service import log_service as _log

    @app.errorhandler(Exception)
    def _handle_exception(e):
        try:
            _log.error(f"未捕获异常: {type(e).__name__}: {e}", "APP")
        except Exception:
            pass
        if _req.path.startswith("/api/"):
            return _jsonify({"success": False, "error": "服务器内部错误，请查看日志"}), 500
        # 非 API 路径走 Flask 默认 HTML 错误页（让浏览器用户看到友好页面）
        from flask import abort as _abort
        _abort(500)

    @app.errorhandler(404)
    def _handle_404(e):
        if _req.path.startswith("/api/"):
            return _jsonify({"success": False, "error": "接口不存在"}), 404
        return _abort(404)

    @app.errorhandler(403)
    def _handle_403(e):
        if _req.path.startswith("/api/"):
            return _jsonify({"success": False, "error": "无权限"}), 403
        return _abort(403)

    @app.errorhandler(413)
    def _handle_413(e):
        # MAX_CONTENT_LENGTH 触发:返回 JSON 而非默认 HTML,便于前端 fetch 处理
        if _req.path.startswith("/api/"):
            return _jsonify({"success": False, "error": "请求体过大(超过 60MB 上限)"}), 413
        return _abort(413)

    return app
