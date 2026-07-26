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
    """返回 Web 面板 / ToolDelta / 构建哈希 三版本信息（P2-5）。"""
    return jsonify(dashboard_service.get_version_info())
