from flask import Blueprint, render_template, session, abort

bp = Blueprint("plugins", __name__)

@bp.route("/plugins")
def plugins():
    # 插件管理页面入口要求管理员:与 console.py 一致,
    # 普通用户进入后所有切换/删除/上传 API 返回 403,提前拦截避免空白页
    if session.get("role") != 10:
        abort(403)
    return render_template("plugins.html")
