from flask import Blueprint, render_template, request, jsonify, session

import time as _time

from app.scheduler_service import scheduler_service

bp = Blueprint("scheduler", __name__)

# run_now 频率限制:维护每个任务最近一次手动触发的 timestamp,
# 强制最小间隔 RUN_MIN_INTERVAL 秒,防止管理员高频触发灌爆 ToolDelta stdin 缓冲。
# 仅进程内限制(与 auth_service 限流一致),单进程部署已足够。
_RUN_NOW_LAST_TS: dict[str, float] = {}
RUN_MIN_INTERVAL = 5.0  # 秒


def _admin_required():
    """校验当前会话是否为管理员，非管理员返回错误响应。"""
    if session.get("role") != 10:
        return jsonify({"success": False, "message": "无权限，仅管理员可操作"}), 403
    return None


def _validate_job_id(payload):
    """校验 job_id:必须是非空字符串。
    防止 dict/list 等非字符串类型传入 scheduler_service 导致语义模糊。"""
    job_id = payload.get("id")
    if not isinstance(job_id, str) or not job_id:
        return None, jsonify({"success": False, "message": "缺少任务 id"})
    return job_id, None


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
    job_id, verr = _validate_job_id(payload)
    if verr is not None:
        return verr
    try:
        # update_job 返回 (ok, message)：
        # - 任务不存在 → "任务不存在"
        # - 参数非法（interval 负数、type 不合法等）→ 透传具体校验错误
        # 原 update_job 把 ValueError 吞掉返回 False，路由统一显示"任务不存在"，
        # 误导管理员以为任务被删，其实是参数不合法。现改为透传 message。
        ok, msg = scheduler_service.update_job(job_id, payload)
    except Exception:
        return jsonify({"success": False, "message": "更新任务失败"})
    if not ok:
        return jsonify({"success": False, "message": msg or "更新失败"})
    return jsonify({"success": True})


@bp.route("/api/scheduler/delete", methods=["POST"])
def api_delete():
    err = _admin_required()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    job_id, verr = _validate_job_id(payload)
    if verr is not None:
        return verr
    ok = scheduler_service.delete_job(job_id)
    if not ok:
        return jsonify({"success": False, "message": "任务不存在"})
    # 清理频率限制状态,避免任务被重建后误以为刚触发过
    _RUN_NOW_LAST_TS.pop(job_id, None)
    return jsonify({"success": True})


@bp.route("/api/scheduler/run", methods=["POST"])
def api_run():
    err = _admin_required()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    job_id, verr = _validate_job_id(payload)
    if verr is not None:
        return verr
    # 频率限制:每个任务手动触发间隔至少 RUN_MIN_INTERVAL 秒,
    # 防止管理员(或被劫持的 session)高频触发灌爆 ToolDelta stdin 缓冲。
    now = _time.time()
    last = _RUN_NOW_LAST_TS.get(job_id, 0.0)
    if now - last < RUN_MIN_INTERVAL:
        wait = RUN_MIN_INTERVAL - (now - last)
        return jsonify({"success": False, "message": f"任务刚触发过,请 {wait:.0f} 秒后再试"})
    ok = scheduler_service.run_now(job_id)
    if not ok:
        return jsonify({"success": False, "message": "任务不存在"})
    _RUN_NOW_LAST_TS[job_id] = now
    return jsonify({"success": True})
