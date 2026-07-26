from flask import Blueprint, render_template, request, jsonify, session

from app import connection_service as conn_svc

bp = Blueprint("connections", __name__)


def _ok(data=None):
    r = {"success": True}
    if data is not None:
        r["data"] = data
    return jsonify(r)


def _fail(msg):
    return jsonify({"success": False, "error": msg})


def _admin_required():
    """校验当前会话是否为管理员，非管理员返回错误响应。"""
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限，仅管理员可操作"}), 403
    return None


@bp.route("/connections")
def connections_page():
    return render_template("connections.html")


@bp.route("/api/connections")
def api_list():
    # 仅管理员可读,避免泄露 token 凭据给普通用户
    err = _admin_required()
    if err:
        return err
    return jsonify(conn_svc.list_connections())


@bp.route("/api/connections/add", methods=["POST"])
def api_add():
    err = _admin_required()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    host = (data.get("host") or "").strip()
    port = data.get("port")
    if not name:
        return _fail("名称不能为空")
    if not host:
        return _fail("地址不能为空")
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        return _fail("端口必须为数字")
    conn, err = conn_svc.add_connection({
        "name": name,
        "host": host,
        "port": port,
        "protocol": data.get("protocol"),
        "token": data.get("token"),
        "note": data.get("note"),
    })
    if not conn:
        return _fail(err)
    return jsonify({"success": True, "conn": conn})


@bp.route("/api/connections/update", methods=["POST"])
def api_update():
    err = _admin_required()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    conn_id = data.get("id")
    if not conn_id:
        return _fail("缺少 id")
    if "port" in data and data["port"] not in (None, ""):
        try:
            int(data["port"])
        except (TypeError, ValueError):
            return _fail("端口必须为数字")
    ok = conn_svc.update_connection(conn_id, data)
    if not ok:
        return _fail("连接不存在")
    return _ok()


@bp.route("/api/connections/delete", methods=["POST"])
def api_delete():
    err = _admin_required()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    conn_id = data.get("id")
    if not conn_id:
        return _fail("缺少 id")
    ok = conn_svc.delete_connection(conn_id)
    if not ok:
        return _fail("连接不存在")
    return _ok()


@bp.route("/api/connections/default", methods=["POST"])
def api_default():
    err = _admin_required()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    conn_id = data.get("id")
    if not conn_id:
        return _fail("缺少 id")
    ok = conn_svc.set_default(conn_id)
    if not ok:
        return _fail("连接不存在")
    return _ok()


@bp.route("/api/connections/test", methods=["POST"])
def api_test():
    """测试 MC 服务器网络连通性。setup 引导第 3 步与 connections 页都用此端点。
    仅做 TCP 套接字探测（connect 5s 超时），不发送任何协议握手数据。"""
    err = _admin_required()
    if err:
        return err
    import socket
    import ipaddress
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    port = data.get("port")
    if not host:
        return _fail("地址不能为空")
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        return _fail("端口必须为数字")
    if not (1 <= port <= 65535):
        return _fail("端口必须在 1-65535 范围内")
    # 限制 hostname 长度，防止超长输入
    if len(host) > 255:
        return _fail("地址过长")
    # SSRF 防护：与 api.py:_is_safe_url / plugin_service.install_network_plugin 同源拦截。
    # 旧实现仅校验端口与长度，未拦截内网地址，管理员（或被劫持的管理员 session）
    # 可探测 127.0.0.1:6379（Redis）、169.254.169.254:80（云元数据）、10.0.0.x 等内网服务。
    # 即便只做 TCP 三次握手不发协议数据，仍可探测端口存活与内网拓扑。
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return _fail("地址解析失败，请检查服务器地址")
    for info in addrinfo:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        # is_private 仅覆盖 10/172.16-31/192.168，必须额外拦截：
        # - is_loopback: 127.0.0.0/8（可探测本机服务）
        # - is_link_local: 169.254.0.0/16（云元数据 169.254.169.254）
        # - is_reserved: 0.0.0.0/8、240.0.0.0/4 等
        # - is_multicast: 224.0.0.0/4
        # - is_unspecified: 0.0.0.0 / :: （IPv6 未指定地址）
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return _fail("不允许连接内网或本地地址")
    try:
        # getaddrinfo 会解析 IPv4/IPv6/主机名；超时 5s
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        return _ok({"latency_ms": 0, "reachable": True})
    except socket.timeout:
        return _fail("连接超时（5s），请检查服务器地址与端口")
    except ConnectionRefusedError:
        return _fail("连接被拒绝（服务器未启动或端口错误）")
    except socket.gaierror:
        return _fail("地址解析失败，请检查服务器地址")
    except OSError as e:
        # 不回显 e.strerror（可能泄露内部网络环境细节如 DNS 错误形态），仅记日志
        try:
            from app.log_service import log_service
            log_service.warn(f"连接测试失败 {host}:{port}: {e}", "CONN")
        except Exception:
            pass
        return _fail("连接失败，请稍后重试")
