from flask import Blueprint, render_template, request, jsonify, Response, session

from app.log_service import log_service

bp = Blueprint("logs", __name__)


def _admin_required():
    """校验当前会话是否为管理员，非管理员返回 403。"""
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"}), 403
    return None

@bp.route("/logs")
def logs_page():
    return render_template("logs.html")

def _validate_log_date(date):
    """校验日志日期参数，仅允许 YYYY-MM-DD 格式，防止路径遍历。"""
    if date is None:
        return None
    date = date.strip()
    if not date:
        return None
    if len(date) != 10 or not date.replace("-", "").isdigit():
        return None
    return date


# ─── 日志增强 API ──────────────────────────────

@bp.route("/api/logs/query")
def api_logs_query():
    err = _admin_required()
    if err:
        return err
    date = _validate_log_date(request.args.get("date"))
    level = request.args.get("level") or None
    source = request.args.get("source") or None
    keyword = request.args.get("keyword") or None
    limit = request.args.get("limit", 500, type=int)
    # 限制单次返回条数，防止超大日志查询拖垮响应（P2-2）
    if limit < 1:
        limit = 1
    if limit > 5000:
        limit = 5000
    lines = log_service.query(level=level, source=source, keyword=keyword, date=date, limit=limit)
    sources = log_service.list_sources(date)
    return jsonify({"lines": lines, "sources": sources})


@bp.route("/api/logs/sources")
def api_logs_sources():
    err = _admin_required()
    if err:
        return err
    date = _validate_log_date(request.args.get("date"))
    return jsonify(log_service.list_sources(date))


@bp.route("/api/logs/export")
def api_logs_export():
    err = _admin_required()
    if err:
        return err
    date = _validate_log_date(request.args.get("date"))
    level = request.args.get("level") or None
    source = request.args.get("source") or None
    keyword = request.args.get("keyword") or None
    text = log_service.export_text(date=date, level=level, source=source, keyword=keyword)
    return Response(
        text,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=logs_export.txt"},
    )
