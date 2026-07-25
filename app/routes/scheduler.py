from flask import Blueprint, render_template, request, jsonify, session

from app.scheduler_service import scheduler_service

bp = Blueprint("scheduler", __name__)


def _admin_required():
    """校验当前会话是否为管理员，非管理员返回错误响应。"""
    if session.get("role") != 10:
        return jsonify({"success": False, "message": "无权限，仅管理员可操作"}), 403
    return None


@bp.route("/scheduler")
def scheduler_page():
    return render_template("scheduler.html")


@bp.route("/api/scheduler/jobs", methods=["GET"])
def api_jobs():
    err = _admin_required()
    if err:
        return err
    return jsonify(scheduler_service.list_jobs())


@bp.route("/api/scheduler/add", methods=["POST"])
def api_add():
    err = _admin_required()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    try:
        job = scheduler_service.add_job(payload)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)})
    except Exception:
        return jsonify({"success": False, "message": "添加任务失败"})
    return jsonify({"success": True, "job": job})


@bp.route("/api/scheduler/update", methods=["POST"])
def api_update():
    err = _admin_required()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    job_id = payload.get("id")
    if not job_id:
        return jsonify({"success": False, "message": "缺少任务 id"})
    try:
        ok = scheduler_service.update_job(job_id, payload)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)})
    except Exception:
        return jsonify({"success": False, "message": "更新任务失败"})
    if not ok:
        return jsonify({"success": False, "message": "任务不存在"})
    return jsonify({"success": True})


@bp.route("/api/scheduler/delete", methods=["POST"])
def api_delete():
    err = _admin_required()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    job_id = payload.get("id")
    if not job_id:
        return jsonify({"success": False, "message": "缺少任务 id"})
    ok = scheduler_service.delete_job(job_id)
    if not ok:
        return jsonify({"success": False, "message": "任务不存在"})
    return jsonify({"success": True})


@bp.route("/api/scheduler/run", methods=["POST"])
def api_run():
    err = _admin_required()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    job_id = payload.get("id")
    if not job_id:
        return jsonify({"success": False, "message": "缺少任务 id"})
    ok = scheduler_service.run_now(job_id)
    if not ok:
        return jsonify({"success": False, "message": "任务不存在"})
    return jsonify({"success": True})
