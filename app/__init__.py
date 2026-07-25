import os
import sys
import threading
import traceback
from datetime import timedelta
from flask import Flask, session, redirect, request
from flask_socketio import SocketIO
from config import Config

socketio = SocketIO()


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
        os.makedirs(web_data_dir, exist_ok=True)
    _flask_env = os.environ.get("FLASK_ENV", "production").lower()
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0 if _flask_env == "development" else 31536000
    app.config["SESSION_PERMANENT"] = True
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    # 安全 cookie：默认 False 保持本地/HTTP 开发可用，生产环境可通过环境变量强制 HTTPS
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

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
    _sio_origins_env = os.environ.get("SOCKETIO_CORS_ALLOWED_ORIGINS", "").strip()
    if _sio_origins_env:
        _sio_origins = [o.strip() for o in _sio_origins_env.split(",") if o.strip()]
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
        allowed_prefixes = ["/login", "/setup", "/api/login", "/api/setup", "/api/reset-panel", "/logout", "/static/"]
        if any(path == p or path.startswith(p) for p in allowed_prefixes):
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
        # 允许 self + inline(因模板大量内联 script/style) + ws/wss(Socket.IO) + data:(图片)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self' ws: wss:; "
            "font-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "frame-ancestors 'self'"
        )
        # HSTS:仅在生产环境 HTTPS 部署时启用(通过环境变量控制)
        if os.environ.get("ENABLE_HSTS", "false").lower() in ("1", "true", "yes"):
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

    return app
