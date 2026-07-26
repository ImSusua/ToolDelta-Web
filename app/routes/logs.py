from flask import Blueprint, render_template, request, jsonify, Response, session, abort

from app.log_service import log_service

bp = Blueprint("logs", __name__)


def _admin_required():
    """校验当前会话是否为管理员，非管理员返回 403。"""
    if session.get("role") != 10:
        return jsonify({"success": False, "error": "无权限"}), 403
    return None

@bp.route("/logs")
def logs_page():
    # 与其它管理员页(console/backup/commands/market/plugins/watchdog/scheduler)对齐:
    # 普通用户(role=1)不应访问 /logs 页面,虽 API 层会返回 403,
    # 但页面骨架会暴露功能存在性与 API 路径,便于攻击者侦察。
    if session.get("role") != 10:
        abort(403)
    return render_template("logs.html")

def _validate_log_date(date):
    """校验日志日期参数，仅允许 YYYY-MM-DD 格式，防止路径遍历。

    与 log_service.get_log_file 的 fullmatch 规则保持一致,
    避免路由层校验比服务层宽松(纵深防御一致性)。
    """
    if date is None:
        return None
    date = date.strip()
    if not date:
        return None
    # 严格 fullmatch:旧实现仅校验 len==10 + replace("-","").isdigit(),
    # 形如 "20240123--" / "2024-01-0-" 等可绕过(虽下游 log_service 会再次拒绝)
    import re
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
        return None
    return date


# ─── 日志增强 API ──────────────────────────────

@bp.route("/api/logs/query")
def api_logs_query():
    err = _admin_required()
    if err:
        return err
    date = _validate_log_date(request.args.get("date"))
    # 参数长度限制:超长 keyword 会让 log_service.query 的 O(行数×keyword 长度) 子串
    # 匹配消耗数秒 CPU(单次请求可拖垮响应),level/source 同理限制避免异常输入。
    level = (request.args.get("level") or "")[:32] or None
    source = (request.args.get("source") or "")[:64] or None
    keyword = (request.args.get("keyword") or "")[:256] or None
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
    # 与 query 一致的参数长度限制,防止超长 keyword 拖垮 CPU
    level = (request.args.get("level") or "")[:32] or None
    source = (request.args.get("source") or "")[:64] or None
    keyword = (request.args.get("keyword") or "")[:256] or None
    text = log_service.export_text(date=date, level=level, source=source, keyword=keyword)
    return Response(
        text,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=logs_export.txt"},
    )
