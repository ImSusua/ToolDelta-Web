import os
import json
import socket
from urllib.parse import urlparse
from ipaddress import ip_address
from flask import Blueprint, request, jsonify, current_app, session
from app.tooldelta_manager import tooldelta_manager
from app.plugin_service import plugin_service
from app.market_service import market_service
from app.backup_service import backup_service
from app.cmd_scanner import cmd_scanner
from app.log_service import log_service

bp = Blueprint("api", __name__, url_prefix="/api")

# 插件上传大小上限：防止超大 zip 拖垮服务（P2-2）
MAX_PLUGIN_UPLOAD_SIZE = 50 * 1024 * 1024


def _is_safe_url(url):
    """基础 SSRF 校验：仅允许 http/https，禁止内网/回环/链路本地/保留/组播地址与过长 URL。
    拦截范围与 plugin_service.install_network_plugin 保持一致，避免两处校验逻辑分叉导致
    market_connect 能探测 127.0.0.1 / 169.254.169.254 等地址而 install_network 不能。"""
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        if not host:
            return False
        addrinfo = socket.getaddrinfo(host, None)
        for info in addrinfo:
            ip = ip_address(info[4][0])
            # is_private 仅覆盖 10/172.16-31/192.168，必须额外拦截：
            # - is_loopback: 127.0.0.0/8（可探测本机服务）
            # - is_link_local: 169.254.0.0/16（云元数据 169.254.169.254）
            # - is_reserved: 0.0.0.0/8、240.0.0.0/4 等
            # - is_multicast: 224.0.0.0/4
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                return False
    except Exception:
        return False
    return True

def audit(action, detail=""):
    # 用户名/详情统一过滤控制字符,防止 plugin name / filename / plugin_id 等
    # 含 \n 伪造审计日志行(日志注入)。复用 log_service.sanitize_for_log
    # 与 routes/auth.py:_sanitize_for_log 行为一致,统一从 log_service 导出避免分叉。
    user = log_service.sanitize_for_log(session.get("username", "?"))
    ip = _client_ip()
    log_service.info(f"[{user}@{ip}] {action} {log_service.sanitize_for_log(detail)}", "AUDIT")

def _client_ip():
    """获取客户端真实 IP:反代场景下取 X-Forwarded-For 最左值,
    与 routes/auth.py:_client_ip 行为一致,避免所有审计日志记录为反代 IP。"""
    if current_app.config.get("BEHIND_PROXY"):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or "?"

def _internal_error(e, action="操作"):
    """统一处理内部异常：记录详情到日志，只返回通用错误给客户端，避免 str(e) 泄露内部路径/堆栈（P1-5）"""
    try:
        log_service.error(action + "失败: " + str(e), "API")
    except Exception:
        pass
    return jsonify({"success": False, "error": action + "失败，请查看日志"})

# ─── ToolDelta 进程管理 ────────────────────────

@bp.route("/status")
def status():
    s = tooldelta_manager.get_status()
    return jsonify(s)

@bp.route("/tool/start", methods=["POST"])
def tool_start():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    ok = tooldelta_manager.start()
    if ok:
        audit("启动 ToolDelta")
    return jsonify({"success": ok})

@bp.route("/tool/stop", methods=["POST"])
def tool_stop():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    ok = tooldelta_manager.stop()
    if ok:
        audit("停止 ToolDelta")
    return jsonify({"success": ok})

@bp.route("/tool/restart", methods=["POST"])
def tool_restart():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    ok = tooldelta_manager.restart()
    if ok:
        audit("重启 ToolDelta")
    return jsonify({"success": ok})

@bp.route("/tool/command", methods=["POST"])
def tool_command():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    cmd = data.get("cmd", "")
    if not isinstance(cmd, str):
        return jsonify({"success": False, "error": "命令格式不合法"})
    if not cmd:
        return jsonify({"success": False, "error": "命令不能为空"})
    ok = tooldelta_manager.send_command(cmd)
    return jsonify({"success": ok})

@bp.route("/tool/output")
def tool_output():
    # 进程输出可能含敏感信息(token/路径),仅管理员可读
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    tail = request.args.get("tail", 200, type=int)
    # 上界：防止 tail=10000000 让 get_output 对整段 buffer 跑 ansi_to_html 造成 CPU 飙升
    if tail > 500:
        tail = 500
    as_html = request.args.get("html", "0") == "1"
    return jsonify({"lines": tooldelta_manager.get_output(tail, as_html=as_html)})

# ─── ToolDelta 运行依赖自管 ─────────────

@bp.route("/dependencies")
def dependencies_status():
    from app.dependency_service import dependency_service
    return jsonify(dependency_service.get_status())

@bp.route("/dependencies/install", methods=["POST"])
def dependencies_install():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    from app.dependency_service import dependency_service
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    if mode not in ("local", "online"):
        mode = None
    return jsonify(dependency_service.start_install(mode))

# ─── 插件管理 ────────────────────────

@bp.route("/plugins")
def list_plugins():
    return jsonify(plugin_service.list_plugins())

@bp.route("/plugins/toggle", methods=["POST"])
def toggle_plugin():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    name, enable = data.get("name"), data.get("enable")
    if not name:
        return jsonify({"success": False, "error": "缺少插件名"})
    ok = plugin_service.toggle_plugin(name, enable)
    if ok:
        audit("切换插件", f"{name} -> {'启用' if enable else '禁用'}")
    return jsonify({"success": ok})

@bp.route("/plugins/delete", methods=["POST"])
def delete_plugin():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"success": False, "error": "缺少插件名"})
    ok = plugin_service.delete_plugin(name)
    if ok:
        audit("删除插件", f"插件={name}")
    return jsonify({"success": ok})

@bp.route("/plugins/upload", methods=["POST"])
def upload_plugin():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    if "file" not in request.files:
        return jsonify({"success": False, "error": "未上传文件"})
    f = request.files["file"]
    if not f.filename or not f.filename.endswith(".zip"):
        return jsonify({"success": False, "error": "仅支持 .zip 文件"})
    # 大小校验：content_length 可能不可靠，保存后再兜底
    if f.content_length and f.content_length > MAX_PLUGIN_UPLOAD_SIZE:
        return jsonify({"success": False, "error": "插件包超过 50MB 上限"})
    try:
        # upload_plugin 在「插件名不合法」「压缩包结构错误」「同名插件已存在」等场景
        # 会返回 (False, msg) 元组而非抛异常。旧实现丢弃返回值恒返回 success=True,
        # 导致客户端误以为上传成功但插件实际未落盘。这里检查返回值并反馈。
        ret = plugin_service.upload_plugin(f)
        if isinstance(ret, tuple) and ret[0] is False:
            return jsonify({"success": False, "error": ret[1] or "上传失败"})
        audit("上传插件", f"文件={f.filename}")
        return jsonify({"success": True})
    except Exception as e:
        return _internal_error(e, "上传插件")

@bp.route("/plugins/readme")
def plugin_readme():
    name = request.args.get("name")
    if not name:
        return jsonify({"error": "缺少插件名"})
    return jsonify(plugin_service.get_plugin_readme(name) or {"error": "未找到文档"})

@bp.route("/plugins/config")
def plugin_config():
    name = request.args.get("name")
    if not name:
        return jsonify({"error": "缺少插件名"})
    # 仅管理员可读插件配置(配置文件可能含敏感信息)
    if session.get("role") != 10:
        return jsonify({"error": "无权限"})
    return jsonify(plugin_service.get_plugin_config(name) or {"error": "无配置文件"})

@bp.route("/plugins/config", methods=["POST"])
def save_plugin_config():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    name, config = data.get("name"), data.get("config")
    if not name or not config:
        return jsonify({"success": False, "error": "缺少参数"})
    # 类型校验：config 必须是 dict，避免任意类型（list/str/巨型 JSON）撑爆磁盘
    if not isinstance(config, dict):
        return jsonify({"success": False, "error": "config 必须是 JSON 对象"})
    # 大小校验：序列化后不超过 1MB，防止巨型 JSON 撑爆 cfg/{name}.json
    try:
        if len(json.dumps(config, ensure_ascii=False)) > 1024 * 1024:
            return jsonify({"success": False, "error": "配置内容过大（超过 1MB）"})
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "config 序列化失败"})
    plugin_service.save_plugin_config(name, config)
    return jsonify({"success": True})

@bp.route("/plugins/data-files")
def plugin_data_files():
    name = request.args.get("name")
    if not name:
        return jsonify({"error": "缺少插件名"})
    # 与 /api/plugins/config 对齐：插件数据文件清单可能含敏感文件名（fbtoken 缓存、凭据文件），
    # 普通用户枚举后可辅助后续定向攻击，仅管理员可读
    if session.get("role") != 10:
        return jsonify({"error": "无权限"})
    return jsonify(plugin_service.get_plugin_data_files(name))

@bp.route("/plugins/data-upload", methods=["POST"])
def upload_data_file():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    name = request.form.get("name")
    if not name:
        return jsonify({"success": False, "error": "缺少插件名"})
    if "file" not in request.files:
        return jsonify({"success": False, "error": "未上传文件"})
    f = request.files["file"]
    # upload_data_file 在「插件名不合法」「文件名不合法」「落点越权」「文件过大」时返回 False,
    # 旧实现丢弃返回值恒返回 success=True。这里检查并反馈,避免客户端误以为上传成功
    if not plugin_service.upload_data_file(name, f):
        return jsonify({"success": False, "error": "上传失败:插件名或文件名不合法,或文件超过 50MB 上限"})
    return jsonify({"success": True})

@bp.route("/plugins/data-delete", methods=["POST"])
def delete_data_file():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    filename = data.get("file")
    if not name or not filename:
        return jsonify({"success": False, "error": "缺少参数"})
    ok = plugin_service.delete_data_file(name, filename)
    return jsonify({"success": ok})

@bp.route("/plugins/config-upload", methods=["POST"])
def upload_config_file():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    name = request.form.get("name")
    if not name:
        return jsonify({"success": False, "error": "缺少插件名"})
    if "file" not in request.files:
        return jsonify({"success": False, "error": "未上传文件"})
    f = request.files["file"]
    # upload_config_file 在「插件名不合法」「文件名不合法」「落点越权」时返回 False,
    # 旧实现丢弃返回值恒返回 success=True。这里检查并反馈
    if not plugin_service.upload_config_file(name, f):
        return jsonify({"success": False, "error": "上传失败:插件名或文件名不合法"})
    return jsonify({"success": True})

# ─── 预设/网络 安装 ────────────────────────

@bp.route("/plugins/presets")
def preset_plugins():
    return jsonify(market_service.get_plugins(refresh=True))

@bp.route("/plugins/install-preset", methods=["POST"])
def install_preset():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    plugin_id = data.get("plugin_id")
    if not plugin_id:
        return jsonify({"success": False, "error": "缺少插件ID"})
    ok, msg = plugin_service.install_preset_plugin(plugin_id)
    if ok:
        audit("安装预设插件", f"插件ID={plugin_id}")
    return jsonify({"success": ok, "message": msg})

@bp.route("/plugins/install-preset-batch", methods=["POST"])
def install_preset_batch():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    ids = data.get("plugin_ids", [])
    # 类型与长度校验：ids 必须是数组且不超过 200 项，
    # 防止 None/字符串触发 TypeError 或极大列表（10w 项）触发全量市场扫描造成 CPU/IO DoS
    if not isinstance(ids, list):
        return jsonify({"success": False, "error": "plugin_ids 必须是数组"})
    if len(ids) > 200:
        return jsonify({"success": False, "error": "plugin_ids 不能超过 200 项"})
    # 每个元素必须是字符串且长度合理
    for pid in ids:
        if not isinstance(pid, str) or not pid or len(pid) > 128:
            return jsonify({"success": False, "error": "插件 ID 不合法"})
    results = plugin_service.install_preset_plugins_batch(ids)
    return jsonify({"results": results})

@bp.route("/plugins/install-network", methods=["POST"])
def install_network():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").rstrip("/")
    plugin_id = data.get("plugin_id")
    if not url or not plugin_id:
        return jsonify({"success": False, "error": "缺少 market_url 或 plugin_id"})
    if not _is_safe_url(url):
        return jsonify({"success": False, "error": "URL 不合法或不允许访问该地址"})
    ok, msg = plugin_service.install_network_plugin(url, plugin_id)
    return jsonify({"success": ok, "message": msg})

@bp.route("/market/sources")
def market_sources():
    from flask import current_app
    return jsonify(current_app.config.get("MARKET_SOURCES", []))

@bp.route("/market/connect", methods=["POST"])
def market_connect():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").rstrip("/")
    if len(url) > 2048:
        return jsonify({"success": False, "error": "URL 过长"})
    # 复用统一 SSRF 校验，避免与 _is_safe_url 逻辑分叉（P2-8）
    if not _is_safe_url(url):
        return jsonify({"success": False, "error": "URL 不合法或不允许访问该地址"})
    try:
        import requests
        # allow_redirects=False：禁止自动跟随 3xx 跳转。
        # _is_safe_url 只校验了原始 host，若放行跳转，恶意市场源可用
        # 302 → http://169.254.169.254/... 把请求导向云元数据等内网资源，绕过 SSRF 拦截（P1-5）
        r = requests.get(f"{url}/market_tree.json", timeout=10, allow_redirects=False)
        if r.is_redirect or r.is_permanent_redirect:
            return jsonify({"success": False, "error": "市场源返回重定向，疑似不安全"})
        r.raise_for_status()
        tree = r.json()
        plugins_list = []
        for pid, info in tree.get("MarketPlugins", {}).items():
            plugins_list.append({
                "id": pid,
                "name": info.get("name", pid),
                "version": info.get("version", "?"),
                "author": info.get("author", "?"),
                "plugin_type": info.get("plugin-type", "classic"),
            })
        return jsonify({"success": True, "source_name": tree.get("SourceName", url), "plugins": plugins_list})
    except Exception as e:
        return _internal_error(e, "连接插件市场")

# ─── 插件市场 ────────────────────────

@bp.route("/market/plugins")
def market_plugins():
    market_service.scan()
    by = request.args.get("by", "name")
    # 长度上限:防止超长字符串触发 market_service.search 的 .lower() 全串内存分配
    # 与同文件 /commands 的 [:128] 一致
    kw = request.args.get("q", "")[:128]
    if kw:
        return jsonify(market_service.search(kw, by))
    return jsonify(market_service.get_plugins())

@bp.route("/market/packages")
def market_packages():
    market_service.scan()
    return jsonify(market_service.get_packages())

@bp.route("/market/plugin")
def market_plugin_detail():
    pid = request.args.get("id")
    if not pid:
        return jsonify({"error": "缺少插件ID"})
    # 长度上限：防止超长字符串触发逐项比较浪费 CPU（与 kw/plugin_filter 的 [:128] 一致）
    pid = pid[:128]
    return jsonify(market_service.get_plugin_data(pid, refresh=True) or {"error": "未找到"})

# ─── 命令扫描 ────────────────────────

@bp.route("/commands")
def list_commands():
    results = cmd_scanner.scan_all_plugins()
    kw = request.args.get("q", "")[:128].lower()
    plugin_filter = request.args.get("plugin", "")[:64].lower()
    if kw:
        filtered = []
        for p in results:
            matched_cmds = [c for c in p["commands"] if kw in " ".join(c["triggers"]).lower() or kw in c.get("usage", "").lower()]
            if matched_cmds:
                filtered.append({**p, "commands": matched_cmds, "count": len(matched_cmds)})
        results = filtered
    if plugin_filter:
        results = [p for p in results if plugin_filter in p["plugin"].lower()]
    return jsonify(results)

@bp.route("/commands/plugin")
def commands_by_plugin():
    name = request.args.get("name")
    if not name:
        return jsonify({"error": "缺少插件名"})
    # 长度上限:防止超长字符串触发 cmd_scanner.scan_by_plugin 内部 basename+realpath 浪费 CPU
    # 与同文件 /commands /market/plugin 的 [:128] 一致
    name = name[:128]
    return jsonify(cmd_scanner.scan_by_plugin(name))

@bp.route("/commands/stats")
def commands_stats():
    results = cmd_scanner.scan_all_plugins()
    total_cmds = sum(p["count"] for p in results)
    total_plugins = len(results)
    return jsonify({"total_commands": total_cmds, "total_plugins": total_plugins, "plugins": results})

# ─── 命令收藏（用户级，存于 Web 数据目录的 favorites.json） ────────────────
def _fav_path():
    base = current_app.config.get("WEB_DATA_DIR")
    if not base:
        base = os.path.join(current_app.root_path, "data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "favorites.json")

def _load_fav():
    try:
        with open(_fav_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_fav(data):
    with open(_fav_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _user_favs():
    user = session.get("username") or "default"
    return _load_fav().get(user, [])

def _set_user_favs(cmds):
    user = session.get("username") or "default"
    data = _load_fav()
    data[user] = cmds
    _save_fav(data)

@bp.route("/favorites", methods=["GET"])
def list_favorites():
    return jsonify({"commands": _user_favs()})

@bp.route("/favorites", methods=["POST"])
def add_favorite():
    data = request.get_json(silent=True) or {}
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return jsonify({"success": False, "error": "命令不能为空"})
    if len(cmd) > 256:
        return jsonify({"success": False, "error": "命令长度超过 256 字符限制"})
    if any(ord(ch) < 32 for ch in cmd):
        return jsonify({"success": False, "error": "命令包含非法控制字符"})
    cmds = _user_favs()
    if cmd not in cmds:
        cmds.append(cmd)
        _set_user_favs(cmds)
    return jsonify({"success": True, "commands": cmds})

@bp.route("/favorites", methods=["DELETE"])
def remove_favorite():
    data = request.get_json(silent=True) or {}
    cmd = (data.get("cmd") or "").strip()
    if not cmd:
        return jsonify({"success": False, "error": "命令不能为空"})
    cmds = [c for c in _user_favs() if c != cmd]
    _set_user_favs(cmds)
    return jsonify({"success": True, "commands": cmds})

# ─── 备份 ────────────────────────

@bp.route("/backups")
def list_backups():
    # 与 /api/backup/create|restore|delete 对齐：备份元数据（文件名/时间/备注）
    # 可辅助社工或定向破坏，仅管理员可读
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"}), 403
    return jsonify(backup_service.list_backups())

@bp.route("/backup/create", methods=["POST"])
def create_backup():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if name is not None and not isinstance(name, str):
        return jsonify({"success": False, "error": "名称必须为字符串"})
    try:
        meta = backup_service.create_backup(name)
    except (ValueError, AttributeError, TypeError) as e:
        return jsonify({"success": False, "error": str(e)})
    audit("创建备份", f"名称={meta.get('name','?')}")
    # 与 restore/delete 保持返回结构一致（前端期望 d.success），见 backup.html:100
    return jsonify({"success": True, "data": meta})

@bp.route("/backup/restore", methods=["POST"])
def restore_backup():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    zip_name = data.get("zip")
    if not zip_name or not isinstance(zip_name, str):
        return jsonify({"success": False, "error": "缺少备份文件名"})
    # 路径遍历防护:zip_name 必须是合法文件名,不含路径分隔符
    safe = backup_service._sanitize_label(zip_name.replace(".zip", ""))
    if not safe or safe + ".zip" != zip_name:
        return jsonify({"success": False, "error": "备份文件名不合法"})
    ok, msg = backup_service.restore_backup(zip_name)
    return jsonify({"success": ok, "message": msg})

@bp.route("/backup/delete", methods=["POST"])
def delete_backup():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    zip_name = data.get("zip")
    if not zip_name or not isinstance(zip_name, str):
        return jsonify({"success": False, "error": "缺少备份文件名"})
    safe = backup_service._sanitize_label(zip_name.replace(".zip", ""))
    if not safe or safe + ".zip" != zip_name:
        return jsonify({"success": False, "error": "备份文件名不合法"})
    ok = backup_service.delete_backup(zip_name)
    if not ok:
        return jsonify({"success": False, "error": "备份不存在或文件名不合法"})
    return jsonify({"success": True})

@bp.route("/reset", methods=["POST"])
def reset_to_factory():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    # 恢复出厂会清空所有插件/配置/数据，破坏力远大于"重置面板"(仅清账号)，
    # 后者已要求密码二次确认 + audit；这里对齐：必须验证管理员密码并记录审计
    # 防止管理员 session 被钓鱼/CSRF 拿到后一键清空所有数据
    from flask import request as _req
    from app import auth_service
    data = _req.get_json(silent=True) or {}
    password = data.get("password") or ""
    if not password:
        return jsonify({"success": False, "error": "请输入管理员密码以确认"})
    # 复用 /api/reset-panel 的限流逻辑（基于 IP），避免暴力尝试
    ip = _req.remote_addr or "?"
    allowed, msg = auth_service.check_login_rate(ip)
    if not allowed:
        return jsonify({"success": False, "error": msg})
    if not auth_service.verify_password(password):
        auth_service.record_login_fail(ip)
        return jsonify({"success": False, "error": "密码错误"})
    auth_service.clear_login_fails(ip)
    audit("恢复出厂设置", f"操作者={session.get('username','?')}")
    ok, msg = backup_service.reset_to_factory()
    if ok:
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg})

# ─── 日志 ────────────────────────

@bp.route("/logs")
def get_logs():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"}), 403
    tail = request.args.get("tail", 200, type=int)
    return jsonify({"lines": log_service.get_today_logs(tail)})

@bp.route("/logs/files")
def log_files():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"}), 403
    return jsonify(log_service.list_log_files())

@bp.route("/logs/file")
def log_file():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"}), 403
    date = request.args.get("date", "")
    # 日期只允许数字与连字符，防止路径遍历读取任意文件（P1-1）
    if not date or not date.replace("-", "").isdigit():
        return jsonify({"error": "日期参数不合法"})
    return jsonify({"lines": log_service.get_log_file(date), "date": date})

# ─── 系统信息 ────────────────────────

@bp.route("/system/info")
def system_info():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"}), 403
    import sys
    import platform
    td_dir = current_app.config["TOOLDELTA_DIR"]
    plugins = plugin_service.list_plugins()
    return jsonify({
        "python_version": sys.version,
        "platform": platform.platform(),
        "tooldelta_dir": td_dir,
        "tooldelta_exists": os.path.isfile(current_app.config["TOOLDELTA_MAIN"]),
        "plugin_count": len(plugins),
        "enabled_plugins": sum(1 for p in plugins if p["is_enabled"]),
    })

# ─── 配置页面 ────────────────────────

@bp.route("/launcher/config")
def launcher_config():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"}), 403
    td_dir = current_app.config["TOOLDELTA_DIR"]
    cfg_path = os.path.join(td_dir, "ToolDelta基本配置.json")
    if os.path.isfile(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({})

@bp.route("/launcher/config", methods=["POST"])
def save_launcher_config():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    # 白名单：只允许前端可安全写入的配置项
    ALLOWED_KEYS = {
        "全局GitHub镜像", "是否记录日志", "插件市场源",
        "FateArk接入点启动模式", "启动器启动模式(请不要手动更改此项, 改为0可重置)",
    }
    td_dir = current_app.config["TOOLDELTA_DIR"]
    cfg_path = os.path.join(td_dir, "ToolDelta基本配置.json")
    current = {}
    if os.path.isfile(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            current = json.load(f)
    current["全局GitHub镜像"] = current.get("全局GitHub镜像", "")
    for k, v in data.items():
        if k not in ALLOWED_KEYS:
            continue
        # 值类型与长度校验：防止非预期类型或超大写入（P1-2）
        if not isinstance(v, (str, int, bool)):
            return jsonify({"success": False, "error": f"配置项 {k} 值类型不合法"})
        if isinstance(v, str) and len(v) > 4096:
            return jsonify({"success": False, "error": f"配置项 {k} 值过长"})
        current[k] = v
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    return jsonify({"success": True})

@bp.route("/fbtoken")
def get_fbtoken():
    # fbtoken 是 ToolDelta 接入 MC 服务器的高危凭据,仅管理员可读
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    td_dir = current_app.config["TOOLDELTA_DIR"]
    token_path = os.path.join(td_dir, "fbtoken")
    token = ""
    if os.path.isfile(token_path):
        with open(token_path, "r", encoding="utf-8") as f:
            token = f.read().strip()
    return jsonify({"token": token})

@bp.route("/fbtoken", methods=["POST"])
def save_fbtoken():
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"})
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    # fbtoken 长度限制，防止无意义超大写入（P2-2）
    if len(token) > 4096:
        return jsonify({"success": False, "error": "token 过长"})
    td_dir = current_app.config["TOOLDELTA_DIR"]
    token_path = os.path.join(td_dir, "fbtoken")
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(token)
    # 文件权限加固：fbtoken 是 ToolDelta 接入 MC 服务器的高危凭据，
    # 默认 0o644 同主机其他普通用户可读取。设为 0o600 仅本用户可读写。
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    audit("更新 fbtoken")
    return jsonify({"success": True})
