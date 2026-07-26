import os
import json
import time
import threading
from datetime import datetime

from app.log_service import log_service
from app.tooldelta_manager import tooldelta_manager

_FMT = "%Y-%m-%d %H:%M:%S"

_DEFAULT_CONFIG = {
    "enabled": False,          # 默认关闭，避免无真实进程时产生副作用
    "check_interval": 10,      # 检查间隔（秒）
    "auto_restart": True,      # 进程停止时自动重启
    "max_restarts": 5,         # 最大重启次数
    "restart_cooldown": 30,    # 两次重启之间的最小冷却时间（秒）
}


class WatchdogService:
    """看门狗服务：监控 ToolDelta 主进程，停止/崩溃时按配置自动重启。

    持久化：app.instance_path + Lock + 原子写（临时文件 + os.replace）。
    后台线程：daemon=True，线程内不触碰 current_app；init_app 时快照路径/开关。
    线程循环整体用 try/except 包裹，单个循环异常只记录日志不退出。
    """

    def __init__(self):
        self._config_path = None
        self._config = dict(_DEFAULT_CONFIG)
        self._lock = threading.Lock()
        self._thread = None
        # 仅内存的运行时状态（非持久化）
        self._runtime = {
            "monitor_running": False,
            "healthy": True,
            "last_check": None,
            "restarts_count": 0,
            "last_restart": None,
            "last_event": None,
        }

    # ─── 初始化 ───────────────────────────────────────────────

    def init_app(self, app):
        # 快照路径（仅此处依赖 app，之后线程内不再使用 current_app）
        self._config_path = os.path.join(app.instance_path, "watchdog.json")
        os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
        self._load_config()

        # 兼容：若主应用未注册本蓝图，则在此兜底注册（幂等，避免重复注册）
        try:
            from app.routes.watchdog import bp as watchdog_bp
            if "watchdog" not in getattr(app, "blueprints", {}):
                app.register_blueprint(watchdog_bp)
        except Exception:
            pass

        # 启动后台监控线程（daemon）
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="watchdog-monitor"
            )
            self._thread.start()

    # ─── 配置持久化 ───────────────────────────────────────────

    def _coerce_config(self, data):
        cfg = dict(_DEFAULT_CONFIG)
        if isinstance(data, dict):
            for k in _DEFAULT_CONFIG:
                if k in data:
                    cfg[k] = data[k]
        # 类型校验与归一化
        cfg["enabled"] = bool(cfg.get("enabled", False))
        try:
            cfg["check_interval"] = int(cfg.get("check_interval", 10))
        except (TypeError, ValueError):
            cfg["check_interval"] = _DEFAULT_CONFIG["check_interval"]
        if cfg["check_interval"] < 1:
            cfg["check_interval"] = 1
        cfg["auto_restart"] = bool(cfg.get("auto_restart", True))
        try:
            cfg["max_restarts"] = int(cfg.get("max_restarts", 5))
        except (TypeError, ValueError):
            cfg["max_restarts"] = _DEFAULT_CONFIG["max_restarts"]
        if cfg["max_restarts"] < 0:
            cfg["max_restarts"] = 0
        try:
            cfg["restart_cooldown"] = int(cfg.get("restart_cooldown", 30))
        except (TypeError, ValueError):
            cfg["restart_cooldown"] = _DEFAULT_CONFIG["restart_cooldown"]
        if cfg["restart_cooldown"] < 0:
            cfg["restart_cooldown"] = 0
        return cfg

    def _load_config(self):
        data = None
        if self._config_path and os.path.isfile(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = None
        # 文件不存在则用默认值；无论是否存在均写入归一化后的配置
        self._config = self._coerce_config(data)
        self._write_config()

    def _write_config(self):
        with self._lock:
            self._write_config_locked()

    def _write_config_locked(self):
        # 原子写：先写临时文件再替换，避免写一半崩溃导致配置丢失
        if not self._config_path:
            return
        tmp = self._config_path + ".tmp"
        # try/finally 确保异常路径下清理 tmp 文件：json.dump/open 抛异常时
        # os.replace 不会执行，tmp 残留为 watchdog.json.tmp（固定名，虽会被下次覆盖
        # 但仍属未清理的临时文件）。同时补充 flush/fsync 保证数据真正落盘。
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._config_path)
            tmp = None  # 标记已成功 replace，finally 不再删除
        finally:
            if tmp is not None and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ─── 公开方法 ─────────────────────────────────────────────

    def get_config(self):
        with self._lock:
            return dict(self._config)

    def set_config(self, payload):
        """合并校验并更新配置，返回是否成功。

        关键：READ+MODIFY+WRITE 必须在同一个 self._lock 临界区内完成。
        旧实现把锁拆成两段（READ 持锁→释放→MODIFY 不持锁→WRITE 重新持锁），
        在 MODIFY 窗口内其他线程（enable/disable/另一个 set_config）的写入会被
        本线程的旧 cfg 副本整体覆盖，造成「丢失更新」。
        校验逻辑不涉及 I/O 或长耗时，无需让出锁，统一在锁内完成即可。
        """
        if not isinstance(payload, dict):
            return False
        with self._lock:
            cfg = dict(self._config)
            # 合并校验：仅校验 payload 中出现的字段，失败时返回 False 且不落盘
            if "enabled" in payload:
                if not isinstance(payload["enabled"], bool):
                    return False
                cfg["enabled"] = payload["enabled"]
            if "check_interval" in payload:
                try:
                    v = int(payload["check_interval"])
                except (TypeError, ValueError):
                    return False
                if v < 1:
                    return False
                cfg["check_interval"] = v
            if "auto_restart" in payload:
                if not isinstance(payload["auto_restart"], bool):
                    return False
                cfg["auto_restart"] = payload["auto_restart"]
            if "max_restarts" in payload:
                try:
                    v = int(payload["max_restarts"])
                except (TypeError, ValueError):
                    return False
                if v < 0:
                    return False
                cfg["max_restarts"] = v
            if "restart_cooldown" in payload:
                try:
                    v = int(payload["restart_cooldown"])
                except (TypeError, ValueError):
                    return False
                if v < 0:
                    return False
                cfg["restart_cooldown"] = v
            self._config = cfg
            self._write_config_locked()
            return True

    def enable(self):
        with self._lock:
            self._config["enabled"] = True
            self._write_config_locked()

    def disable(self):
        with self._lock:
            self._config["enabled"] = False
            self._write_config_locked()

    def get_runtime(self):
        # 合并 config.enabled 与运行时字段；monitor_running 动态计算
        with self._lock:
            return {
                "enabled": self._config.get("enabled", False),
                "monitor_running": bool(self._thread and self._thread.is_alive()),
                "healthy": self._runtime["healthy"],
                "last_check": self._runtime["last_check"],
                "restarts_count": self._runtime["restarts_count"],
                "last_restart": self._runtime["last_restart"],
                "last_event": self._runtime["last_event"],
            }

    # ─── 后台线程 ─────────────────────────────────────────────

    def _loop(self):
        while True:
            try:
                with self._lock:
                    interval = self._config.get("check_interval", 10)
                time.sleep(interval)
                self._check()
            except Exception as e:
                try:
                    log_service.error("看门狗循环异常: " + str(e), "WATCHDOG")
                except Exception:
                    pass

    @staticmethod
    def _seconds_diff(start_str, end_str):
        try:
            s = datetime.strptime(start_str, _FMT)
            e = datetime.strptime(end_str, _FMT)
            return (e - s).total_seconds()
        except Exception:
            return float("inf")

    def _check(self):
        now = datetime.now().strftime(_FMT)
        with self._lock:
            self._runtime["last_check"] = now
            if not self._config.get("enabled"):
                # 未启用：视为健康，不做任何操作（无副作用）
                self._runtime["healthy"] = True
                return

        status = tooldelta_manager.get_status()
        if status.get("running"):
            with self._lock:
                self._runtime["healthy"] = True
                # 已稳定运行一段时间则清零重启计数，使 max_restarts 成为
                # 「滑动窗口内」的上限而非终身上限（P2-1）
                last = self._runtime.get("last_restart")
                if self._runtime["restarts_count"] > 0 and (
                    last is None or self._seconds_diff(last, now) > 600
                ):
                    self._runtime["restarts_count"] = 0
            return

        # 进程未运行
        with self._lock:
            auto_restart = self._config.get("auto_restart", True)
            max_restarts = self._config.get("max_restarts", 5)
            restart_cooldown = self._config.get("restart_cooldown", 30)
            restarts_count = self._runtime["restarts_count"]
            last_restart = self._runtime["last_restart"]

        cooldown_ok = (
            last_restart is None
            or self._seconds_diff(last_restart, now) > restart_cooldown
        )
        can_restart = (
            auto_restart
            and restarts_count < max_restarts
            and cooldown_ok
        )

        if can_restart:
            start_ok = tooldelta_manager.start()
            # 仅在 start() 返回成功时才递增 restarts_count,避免依赖缺失/文件损坏时
            # 反复尝试失败也耗尽 max_restarts 导致永久放弃守护
            if start_ok:
                with self._lock:
                    self._runtime["restarts_count"] += 1
                    self._runtime["last_restart"] = now
                    self._runtime["healthy"] = False
                    self._runtime["last_event"] = "于 %s 自动重启 ToolDelta" % now
                try:
                    log_service.warn(self._runtime["last_event"], "WATCHDOG")
                except Exception:
                    pass
        else:
            with self._lock:
                self._runtime["healthy"] = False


watchdog_service = WatchdogService()
