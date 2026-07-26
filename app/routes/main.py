from flask import Blueprint, render_template, session, abort

bp = Blueprint("main", __name__)

@bp.route("/")
def index():
    return render_template("index.html")

@bp.route("/files")
def files_page():
    # 文件管理页面入口与 console.py 一致要求管理员:
    # 所有 /api/files/* 写操作均要求 role==10,普通用户进入页面后所有操作返回 403,
    # UI 显示空白且暴露前端骨架。提前拦截给出更明确反馈。
    if session.get("role") != 10:
        abort(403)
    return render_template("files.html", active_page="files")
