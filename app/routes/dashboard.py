from flask import Blueprint, jsonify, session

from app.dashboard_service import dashboard_service

bp = Blueprint("dashboard", __name__)


@bp.route("/api/dashboard", methods=["GET"])
def dashboard():
    """聚合状态仪表盘数据，返回 JSON。

    dashboard 返回 CPU/内存/磁盘使用率、ToolDelta 运行状态、看门狗开关等服务端资源信息，
    普通用户获取这些信息有利于攻击者判断负载、规划拒绝服务或探测运行状态，仅管理员可读。
    """
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"}), 403
    return jsonify(dashboard_service.get_dashboard())


@bp.route("/api/version", methods=["GET"])
def version():
    """返回 Web 面板 / ToolDelta / 构建哈希 三版本信息（P2-5）。

    收紧为管理员可见:版本指纹(web_version/tooldelta_version/build_hash)结合公开 CVE
    库即可定位该版本是否含已知漏洞(如特定 ToolDelta 插件 RCE、特定 Flask 版本
    Werkzeug debugger 暴露等),显著降低攻击者侦察成本。同蓝图 /api/dashboard 已要求
    role==10,此处对齐。模板 inject_versions 仍可向管理员页面注入版本展示。
    """
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"}), 403
    return jsonify(dashboard_service.get_version_info())
