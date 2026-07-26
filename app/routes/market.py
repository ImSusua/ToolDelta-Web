from flask import Blueprint, render_template, session, abort

bp = Blueprint("market", __name__)

@bp.route("/market")
def market():
    # 插件市场页面入口要求管理员:与 console.py 一致,
    # 普通用户进入后 install-preset/install-network 等 API 返回 403,提前拦截避免空白页
    if session.get("role") != 10:
        abort(403)
    return render_template("market.html")
