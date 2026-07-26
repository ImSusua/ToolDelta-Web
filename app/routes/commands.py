from flask import Blueprint, render_template, session, abort

bp = Blueprint("commands", __name__)

@bp.route("/commands")
def commands():
    # 命令预设/管理为管理员专属功能,与 console.py 保持一致:页面层即拦截非管理员。
    # 命令最终通过 /api/commands/* 执行,API 层也会再做 role==10 校验形成纵深防御。
    if session.get("role") != 10:
        abort(403)
    return render_template("commands.html")
