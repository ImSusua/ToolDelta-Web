from flask import Blueprint, render_template, request, jsonify, session, abort

from app.watchdog_service import watchdog_service
from app.log_service import log_service

bp = Blueprint("watchdog", __name__)


def _admin_required():
    if session.get("role") != 10:
        return jsonify({"success": False, "message": "无权限，仅管理员可操作"}), 403
    return None


def _audit(action, detail):
    """审计日志:记录管理员对看门狗配置的变更。
    看门狗是保障 ToolDelta 进程可用性的关键控制面。攻击者劫持管理员 session 后可:
    ①enable=False 关闭看门狗,随后 DoS 让进程停摆不被自动拉起;
    ②max_restarts=0 让重启次数耗尽后永久放弃守护;③auto_restart=False 等同禁用。
    无审计日志则事后无法区分"合法管理员操作"与"攻击者破坏"。
    """
    # 关键修复(纵深防御):用户名也需 sanitize_for_log,与 routes/auth.py:audit
    # 和 routes/api.py:audit 保持一致。当前 username 受 _USERNAME_RE 限制不允许
    # 控制字符故不可利用,但若未来放宽正则(如允许 Unicode U+2028/U+2029 行分隔符)
    # 则此处会成为日志注入入口。统一调用 sanitize_for_log 防御未来变更。
    user = log_service.sanitize_for_log(session.get("username", "?"))
    try:
        log_service.info(
            f"[{user}] {action}: {log_service.sanitize_for_log(detail)}",
            "AUDIT"
        )
    except Exception:
        pass


@bp.route("/watchdog")
def watchdog_page():
    # 看门狗页面入口要求管理员:与 console.py 一致,
    # 普通用户进入后 enable/disable/set API 返回 403,提前拦截避免空白页
    if session.get("role") != 10:
        abort(403)
    return render_template("watchdog.html")


@bp.route("/api/watchdog/config", methods=["GET"])
def watchdog_config():
    err = _admin_required()
    if err:
        return err
    return jsonify(watchdog_service.get_config())


@bp.route("/api/watchdog/set", methods=["POST"])
def watchdog_set():
    err = _admin_required()
    if err:
        return err
    payload = request.get_json(silent=True) or {}
    ok = watchdog_service.set_config(payload)
    if ok:
        # 审计:记录变更的字段及其新值(数值型配置便于事后追溯)
        changed = {k: payload.get(k) for k in
                   ("enabled", "auto_restart", "max_restarts", "restart_cooldown", "check_interval")
                   if k in payload}
        _audit("看门狗配置变更", f"fields={changed}")
    return jsonify({"success": ok})


@bp.route("/api/watchdog/status", methods=["GET"])
def watchdog_status():
    err = _admin_required()
    if err:
        return err
    return jsonify(watchdog_service.get_runtime())


@bp.route("/api/watchdog/enable", methods=["POST"])
def watchdog_enable():
    err = _admin_required()
    if err:
        return err
    watchdog_service.enable()
    _audit("启用看门狗", "-")
    return jsonify({"success": True})


@bp.route("/api/watchdog/disable", methods=["POST"])
def watchdog_disable():
    err = _admin_required()
    if err:
        return err
    watchdog_service.disable()
    _audit("禁用看门狗", "-")
    return jsonify({"success": True})
