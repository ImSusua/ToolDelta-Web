from flask import Blueprint, render_template, request, jsonify, session

from app.watchdog_service import watchdog_service

bp = Blueprint("watchdog", __name__)


def _admin_required():
    if session.get("role") != 10:
        return jsonify({"success": False, "message": "无权限，仅管理员可操作"}), 403
    return None


@bp.route("/watchdog")
def watchdog_page():
    return render_template("watchdog.html")


@bp.route("/api/watchdog/config", methods=["GET"])
def watchdog_config():
    err = _admin_required()
    if err:
        return err
    return jsonify(watchdog_service.get_config())


@bp.route("/api/watchdog/set", methods=["POST"])
def watchdog_set():
    err = _admin_required()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    ok = watchdog_service.set_config(payload)
    return jsonify({"success": ok})


@bp.route("/api/watchdog/status", methods=["GET"])
def watchdog_status():
    err = _admin_required()
    if err:
        return err
    return jsonify(watchdog_service.get_runtime())


@bp.route("/api/watchdog/enable", methods=["POST"])
def watchdog_enable():
    err = _admin_required()
    if err:
        return err
    watchdog_service.enable()
    return jsonify({"success": True})


@bp.route("/api/watchdog/disable", methods=["POST"])
def watchdog_disable():
    err = _admin_required()
    if err:
        return err
    watchdog_service.disable()
    return jsonify({"success": True})
