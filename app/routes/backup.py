from flask import Blueprint, render_template, session, abort

bp = Blueprint("backup", __name__)

@bp.route("/backup")
def backup():
    # 备份/恢复为管理员专属功能,与 console.py 保持一致:页面层即拦截非管理员,
    # 避免普通用户进入页面后试图调用 api 触发 403 的体验割裂。
    if session.get("role") != 10:
        abort(403)
    return render_template("backup.html")
