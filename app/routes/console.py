from flask import Blueprint, render_template, session, abort

bp = Blueprint("console", __name__)

@bp.route("/console")
def console():
    # 控制台输出可能含敏感信息（token/路径/堆栈），仅管理员可访问页面
    # socket_events.handle_connect 已对非管理员断开 socket，但路由层提前拦截
    # 给出更明确的反馈，避免普通用户进入页面后才发现"未连接"而困惑
    if session.get("role") != 10:
        abort(403)
    return render_template("console.html")
