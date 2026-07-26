import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_or_create_secret_key():
    """加载或生成持久化 SECRET_KEY（instance/secret_key），避免会话被伪造（P1-2）。"""
    key_file = os.path.join(BASE_DIR, "instance", "secret_key")
    key_dir = os.path.dirname(key_file)
    try:
        # 目录权限收敛:与 log_service.py 的 logs_dir、__init__.py 的 web_data_dir
        # 保持一致使用 0o700。instance/ 含 secret_key、logs/ 等敏感内容,
        # 不应被同主机其他用户列举/进入。makedirs(mode=0o700) 直接指定权限,
        # 0o700 不含 group/other 位,umask 无法进一步削弱(只能收紧不能放宽)。
        os.makedirs(key_dir, mode=0o700, exist_ok=True)
        # 兜底已存在目录(历史版本以默认 0o755 创建)的权限
        try:
            os.chmod(key_dir, 0o700)
        except OSError:
            pass

        if os.path.isfile(key_file):
            with open(key_file, "r", encoding="utf-8") as f:
                k = f.read().strip()
                if k:
                    # 历史文件权限归一化:旧版本可能以 0o644 创建(open 默认 mode),
                    # 启动时统一收紧到 0o600,避免 secret_key 被同主机其他用户读取。
                    try:
                        os.chmod(key_file, 0o600)
                    except OSError:
                        pass
                    return k
        k = secrets.token_hex(32)
        # 关键修复(TOCTOU):旧实现 open(key_file, "w") 以默认 mode 0o666 创建文件
        # (umask 削弱后通常为 0o644),写入 secret 后才 chmod 0o600。在 open 与 chmod
        # 之间存在时间窗口,同主机其他用户可在此期间 open fd 持续读取 secret_key,
        # 即便后续 chmod 也无法关闭已打开的 fd → SECRET_KEY 泄露 → 攻击者可伪造
        # 任意用户会话 cookie。
        # 修复:用 os.open + O_CREAT 指定 mode=0o600,文件创建即拥有正确权限,
        # 不存在 TOCTOU 窗口。与 log_service.py 的 _write 实现一致。
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(k)
        except Exception:
            # fdopen 失败需手动关闭 fd 避免泄漏
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        return k
    except Exception:
        # 极端场景（如只读文件系统）退化为内存随机值，绝不阻塞启动
        # 但必须明确告警:每次重启所有 session 都会失效(用户被踢下线但日志无异常)
        # 部署在只读容器(read-only rootfs)中会触发此分支
        import sys
        print("[WARN] SECRET_KEY 回退为内存随机值,重启将失效所有 session;"
              "请显式设置 SECRET_KEY 环境变量或确保 instance/ 可写", file=sys.stderr)
        return secrets.token_hex(32)


def _env_bool(name, default=False):
    return os.environ.get(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


class Config:
    # SECRET_KEY 安全加固（P1-2）：
    # 1) 优先读环境变量 SECRET_KEY（生产必须显式设置）；
    # 2) 否则读 instance/secret_key（首次运行自动生成并持久化），保证重启后密钥稳定；
    # 3) 都不存在则回退内存随机值（仅本进程有效，重启即失效）。
    SECRET_KEY = os.environ.get("SECRET_KEY") or _load_or_create_secret_key()

    # HOST 默认 127.0.0.1:仅监听本机回环,避免 VPS/云主机首次启动时公网攻击者
    # 比 /setup 抢注管理员账号(M4)。需对外提供服务时显式设置 HOST=0.0.0.0
    # 或通过反代(nginx + BEHIND_PROXY=1)暴露。
    HOST = os.environ.get("HOST", "127.0.0.1")
    PORT = int(os.environ.get("PORT", "5000"))
    # 统一 DEBUG：默认关闭，可通过环境变量开启；避免与 run.py 的 debug 参数相互矛盾
    DEBUG = _env_bool("DEBUG", False)

    # ToolDelta 主目录：优先使用环境变量 TOOLDELTA_DIR，
    # 未设置时回退到项目目录下的 ToolDelta 子目录（跨平台可用）。
    # 注意：Linux 环境下请通过环境变量 TOOLDELTA_DIR 指向真实的 ToolDelta 安装目录。
    TOOLDELTA_DIR = os.environ.get("TOOLDELTA_DIR") or os.path.join(BASE_DIR, "ToolDelta")
    TOOLDELTA_MAIN = os.path.join(TOOLDELTA_DIR, "main.py")

    # 出厂主程序包（重置功能使用）：放在 web 项目次级目录，
    # 避免把主程序直接解压到 web 根目录造成文件混合。
    TOOLDELTA_SOURCE_ZIP = os.path.join(BASE_DIR, "tooldelta_source", "ToolDelta-main.zip")

    PLUGIN_MARKET_DIR = os.path.join(BASE_DIR, "plugin_market")
    BACKUP_DIR = os.path.join(BASE_DIR, "backups")
    BRIDGE_PLUGIN_DIR = os.path.join(BASE_DIR, "bridge_plugin")

    # 插件市场预设源（列表，供前端下拉选择）
    MARKET_SOURCES = [
        {"name": "官方源", "url": "https://pm.tooldelta.top"},
        {"name": "镜像源 1", "url": "https://github.yuansi.xyz/https://raw.githubusercontent.com/ToolDelta-Basic/PluginMarket/main"},
        {"name": "镜像源 2", "url": "https://github.tooldelta.top/https://raw.githubusercontent.com/ToolDelta-Basic/PluginMarket/main"},
        {"name": "镜像源 3", "url": "https://github.ghfast.top/https://raw.githubusercontent.com/ToolDelta-Basic/PluginMarket/main"},
    ]

    TOOLDELTA_CLASSIC_PLUGIN_PATH = os.path.join(TOOLDELTA_DIR, "插件文件", "ToolDelta类式插件")
    TOOLDELTA_PLUGIN_CFG_DIR = os.path.join(TOOLDELTA_DIR, "插件配置文件")
    TOOLDELTA_PLUGIN_DATA_DIR = os.path.join(TOOLDELTA_DIR, "插件数据文件")

    # Web 面板自身数据目录（收藏等用户数据），独立于 ToolDelta 安装目录
    WEB_DATA_DIR = os.path.join(BASE_DIR, "data")

    # 全局请求体大小上限:Flask 在接收阶段就拒绝超限请求返回 413,
    # 防止攻击者通过省略 Content-Length 头(chunked transfer encoding)
    # 绕过路由层 f.content_length 预校验,持续推送 GB 级数据撑爆 /tmp
    # 或 TOOLDELTA_DIR 所在分区导致服务崩溃。
    # 设为 60MB:覆盖单文件 50MB 上限 + multipart 表单字段开销。
    MAX_CONTENT_LENGTH = 60 * 1024 * 1024
