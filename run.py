# ruff: noqa: E402
import os
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app, socketio
from config import Config

if __name__ == "__main__":
    try:
        app = create_app()
    except Exception as e:
        # create_app 失败会向上抛裸 traceback(可能含路径/配置信息),
        # 这里包一层打印友好错误信息后退出
        import traceback
        print("[ToolDelta Web] 应用初始化失败: " + str(e), file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
    # 反代场景（如 nginx）下修正 remote_addr，使登录限流按真实客户端 IP 生效（P2-8）
    # 仅在显式声明部署在反向代理后时启用 ProxyFix，否则可被伪造 X-Forwarded-For 绕过限流
    if os.environ.get("BEHIND_PROXY", "false").lower() in ("1", "true"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
        print("[ToolDelta Web] 已启用 ProxyFix（BEHIND_PROXY=1）")
    else:
        print("[ToolDelta Web] 未启用 ProxyFix；如部署在反向代理后请设置 BEHIND_PROXY=1")
    print("[ToolDelta Web] 管理面板启动于 http://%s:%s" % (Config.HOST, Config.PORT))
    # 不再向 stdout 打印 TOOLDELTA_DIR / PLUGIN_MARKET_DIR 绝对路径:
    # 容器日志/共享主机上可能被其他用户或日志聚合系统读取,泄露文件系统布局。
    # 详细路径信息可通过 DEBUG 级别日志查看。
    if Config.DEBUG:
        print("[ToolDelta Web] 工作目录: %s" % Config.TOOLDELTA_DIR)
        print("[ToolDelta Web] 插件市场: %s" % Config.PLUGIN_MARKET_DIR)
    # allow_unsafe_werkzeug 默认开启(项目当前用 Werkzeug dev server 作生产部署),
    # 但允许通过环境变量关闭。生产建议改用 gunicorn + eventlet/gevent,
    # 设置 ALLOW_UNSAFE_WERKZEUG=0 强制关闭 dev server 启动以便发现配置漂移。
    _allow_unsafe = os.environ.get("ALLOW_UNSAFE_WERKZEUG", "true").lower() in ("1", "true", "yes")
    if _allow_unsafe and os.environ.get("FLASK_ENV", "production").lower() != "development":
        print("[ToolDelta Web] [WARN] 当前使用 Werkzeug dev server,生产环境建议改用 "
              "gunicorn + eventlet;设置 ALLOW_UNSAFE_WERKZEUG=0 可禁用", file=sys.stderr)
    try:
        socketio.run(app, host=Config.HOST, port=Config.PORT, debug=False,
                     allow_unsafe_werkzeug=_allow_unsafe)
    except Exception as e:
        # 端口占用等启动失败,打印友好错误而非裸 traceback
        print("[ToolDelta Web] 启动失败: " + str(e), file=sys.stderr)
        sys.exit(1)
