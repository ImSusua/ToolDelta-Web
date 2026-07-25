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
    try:
        # getaddrinfo 会解析 IPv4/IPv6/主机名；超时 5s
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        return _ok({"latency_ms": 0, "reachable": True})
    except socket.timeout:
        return _fail(f"连接超时（5s）: {host}:{port}")
    except ConnectionRefusedError:
        return _fail(f"连接被拒绝: {host}:{port}（服务器未启动或端口错误）")
    except socket.gaierror as e:
        return _fail(f"地址解析失败: {host}（{e.strerror or 'unknown host'}）")
    except OSError as e:
        return _fail(f"连接失败: {host}:{port}（{e.strerror or str(e)}）")
