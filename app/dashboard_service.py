"""状态仪表盘聚合数据服务（类单例 + init_app，风格B：无后台线程，纯聚合只读）。

职责：
- 聚合系统资源（CPU/内存/磁盘）、ToolDelta 运行状态、看门狗开关、连接数、插件数。
- 所有指标均用 try/except 包裹，异常时回退为 0 / None，绝不让 get_dashboard() 抛异常。
- 复用既有服务（tooldelta_manager / plugin_service / connection_service / watchdog_service），
  这些都是独立模块，不会与本项目产生循环 import。

get_dashboard() 既可被 /api/dashboard 路由调用，也可被测试直接调用（此时 app 可能为 None，
会用 os.getcwd() 作为磁盘采样基准目录）。
"""
import os
import time
import json
import shutil
import subprocess
import threading

from app.tooldelta_manager import tooldelta_manager
from app.plugin_service import plugin_service
from app.connection_service import list_connections
from app.watchdog_service import watchdog_service
from app.log_service import log_service


class DashboardService:
    def __init__(self):
        self.version = "1.0"
        self.app = None
        self._build_hash_cache = None
        # CPU 后台采样相关字段
        self._last_cpu_sample = 0.0
        self._cpu_sampler_running = False
        self._cpu_lock = threading.Lock()

    # ─── 初始化 ───────────────────────────────────────────────

    def init_app(self, app):
        """保存应用快照（基准目录 / 版本），并兜底注册仪表盘蓝图（幂等）。"""
        self.app = app
        # 快照版本号（风格B：纯只读聚合，不依赖后台线程）
        self.version = self._read_version()

        # 启动时预加载 build hash，避免请求期 subprocess 阻塞 10s
        try:
            self._build_hash_cache = self._compute_build_hash()
        except Exception:
            self._build_hash_cache = "unknown"

        # 启动后台 CPU 采样线程，避免请求期阻塞（之前每次采样会阻塞请求线程约 100ms）
        self._start_cpu_sampler()

        try:
            log_service.info("状态仪表盘服务已初始化", "DASHBOARD")
        except Exception:
            pass

        # 兼容：若主应用未注册本蓝图，则在此兜底注册（幂等，避免重复注册）
        try:
            from app.routes.dashboard import bp as dashboard_bp
            if "dashboard" not in getattr(app, "blueprints", {}):
                app.register_blueprint(dashboard_bp)
        except Exception:
            pass

    @staticmethod
    def _read_version():
        """从项目根目录 VERSION 文件读取 Web 面板版本号（P2-5）。"""
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            vf = os.path.join(os.path.dirname(here), "VERSION")
            if os.path.isfile(vf):
                with open(vf, "r", encoding="utf-8") as f:
                    v = f.read().strip()
                    if v:
                        return v
        except Exception:
            pass
        return "1.0"

    def get_version_info(self):
        """返回三版本信息，供 /api/version 与设置页展示（P2-5）。"""
        return {
            "web_version": self.version or "1.0",
            "build_hash": self._get_build_hash(),
            "tooldelta_version": self._get_tooldelta_version(),
        }

    def _get_build_hash(self):
        """构建哈希：直接返回启动时预加载的缓存值，绝不阻塞请求线程。

        缓存为 None 时返回 "unknown"（init_app 预加载失败的场景）。
        实际计算在 init_app 中通过 _compute_build_hash 一次性完成。
        """
        if self._build_hash_cache is None:
            return "unknown"
        return self._build_hash_cache

    def _compute_build_hash(self):
        """构建哈希实际计算逻辑：优先读 build_info.json，否则取 git 短哈希。

        只在 init_app 中调用一次，避免每次请求都触发 subprocess.run（最多阻塞 10s）。
        """
        result = "nogit"
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            root = os.path.dirname(here)
            bi = os.path.join(root, "build_info.json")
            if os.path.isfile(bi):
                try:
                    with open(bi, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("git"):
                        return d["git"]
                except Exception:
                    pass
            out = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                result = out.stdout.strip()
        except Exception:
            pass
        return result

    @staticmethod
    def _get_tooldelta_version():
        """尝试从 ToolDelta 安装目录读取版本（若存在 version.txt）。"""
        try:
            from flask import current_app
            td_dir = current_app.config.get("TOOLDELTA_DIR")
            if not td_dir:
                return "—"
            vf = os.path.join(td_dir, "version.txt")
            if os.path.isfile(vf):
                with open(vf, "r", encoding="utf-8") as f:
                    return f.read().strip() or "—"
        except Exception:
            pass
        return "—"

    # ─── CPU 近似采样（后台线程周期采样，请求直接读缓存） ────

    def _start_cpu_sampler(self):
        """启动后台 daemon 线程周期采样 CPU，避免请求线程阻塞。"""
        with self._cpu_lock:
            if self._cpu_sampler_running:
                return
            self._cpu_sampler_running = True
        t = threading.Thread(target=self._cpu_sampler_loop, daemon=True)
        t.start()

    def _cpu_sampler_loop(self):
        """后台循环：每 3 秒采样一次 CPU 写入 self._last_cpu_sample。

        采样采用 /proc/stat 两次读 + sleep(1) 的方式（在后台线程中阻塞安全）。
        非 Linux 或读不到时保持 0.0。
        """
        while True:
            try:
                self._last_cpu_sample = self._compute_cpu_sample()
            except Exception:
                pass
            # 总周期约 3 秒（采样间隔 1s + 等待 2s）
            time.sleep(2)

    @staticmethod
    def _compute_cpu_sample() -> float:
        """读 /proc/stat 首行，隔 1s 再读，按 (1 - delta_idle/total) 计算使用率。

        仅在后台线程调用；非 Linux 或读不到时返回 0.0。
        """
        try:
            if not os.path.isfile("/proc/stat"):
                return 0.0

            def _read():
                with open("/proc/stat", "r") as f:
                    line = f.readline()
                parts = [int(x) for x in line.split()[1:]]
                idle = parts[3]
                total = sum(parts)
                return idle, total

            idle1, total1 = _read()
            time.sleep(1)
            idle2, total2 = _read()

            total_delta = total2 - total1
            if total_delta <= 0:
                return 0.0
            idle_delta = idle2 - idle1
            cpu = (1.0 - idle_delta / total_delta) * 100.0
            return round(max(0.0, min(100.0, cpu)), 1)
        except Exception:
            return 0.0

    def _sample_cpu(self) -> float:
        """直接返回后台线程维护的 CPU 采样缓存，绝不阻塞请求线程。

        若后台线程尚未启动（如直接调用 get_dashboard 而未经 init_app）则惰性启动。
        """
        if not self._cpu_sampler_running:
            self._start_cpu_sampler()
        return self._last_cpu_sample

    # ─── 内存（/proc/meminfo） ───────────────────────────────

    @staticmethod
    def _mem_info():
        """返回 (mem_percent, mem_used_mb, mem_total_mb)。取不到给 (0.0, 0, 0)。"""
        try:
            if not os.path.isfile("/proc/meminfo"):
                return 0.0, 0, 0
            info = {}
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    kv = line.split(":")
                    if len(kv) != 2:
                        continue
                    key = kv[0].strip()
                    try:
                        val = int(kv[1].strip().split()[0])
                    except (ValueError, IndexError):
                        continue
                    info[key] = val
            total = info.get("MemTotal", 0)
            if total <= 0:
                return 0.0, 0, 0
            available = info.get("MemAvailable")
            if available is None:
                # 旧内核无 MemAvailable，用 MemFree + Buffers + Cached 近似
                available = (
                    info.get("MemFree", 0)
                    + info.get("Buffers", 0)
                    + info.get("Cached", 0)
                )
            used = total - available
            percent = used / total * 100.0
            return round(percent, 1), int(used / 1024), int(total / 1024)
        except Exception:
            return 0.0, 0, 0

    # ─── 磁盘（shutil.disk_usage） ───────────────────────────

    @staticmethod
    def _disk_info(base_dir: str):
        """返回 (disk_percent, disk_free_gb)。取不到给 (0.0, 0.0)。"""
        try:
            usage = shutil.disk_usage(base_dir)
            percent = usage.used / usage.total * 100.0
            free_gb = usage.free / (1024 ** 3)
            return round(percent, 1), round(free_gb, 2)
        except Exception:
            return 0.0, 0.0

    def _base_dir(self) -> str:
        try:
            if self.app is not None:
                return os.path.dirname(self.app.root_path)
        except Exception:
            pass
        return os.getcwd()

    # ─── 聚合入口 ─────────────────────────────────────────────

    def get_dashboard(self) -> dict:
        """聚合所有仪表盘指标；任何子项异常都回退为 0 / None / 空，绝不抛异常。"""
        # 系统资源
        try:
            cpu_percent = self._sample_cpu()
        except Exception:
            cpu_percent = 0.0

        try:
            mem_percent, mem_used_mb, mem_total_mb = self._mem_info()
        except Exception:
            mem_percent, mem_used_mb, mem_total_mb = 0.0, 0, 0

        try:
            disk_percent, disk_free_gb = self._disk_info(self._base_dir())
        except Exception:
            disk_percent, disk_free_gb = 0.0, 0.0

        # ToolDelta 运行状态（直接复用既有接口）
        try:
            tooldelta_status = tooldelta_manager.get_status() or {}
        except Exception:
            tooldelta_status = {}

        # 看门狗开关
        try:
            watchdog_enabled = bool(watchdog_service.get_config().get("enabled", False))
        except Exception:
            watchdog_enabled = False

        # 连接数
        try:
            connections = list_connections()
            connections_count = len(connections) if connections is not None else 0
        except Exception:
            connections_count = 0

        # 插件数
        try:
            plugins = plugin_service.list_plugins()
            plugins_count = len(plugins) if plugins else 0
        except Exception:
            plugins_count = 0

        # 定时任务数
        try:
            from app.scheduler_service import scheduler_service
            scheduler_jobs = scheduler_service.list_jobs() or []
            scheduler_jobs_count = len(scheduler_jobs)
        except Exception:
            scheduler_jobs_count = 0

        return {
            "system": {
                "cpu_percent": cpu_percent,
                "mem_percent": mem_percent,
                "mem_used_mb": mem_used_mb,
                "mem_total_mb": mem_total_mb,
                "disk_percent": disk_percent,
                "disk_free_gb": disk_free_gb,
            },
            "tooldelta": tooldelta_status,
            "panel": {
                "version": self.version,
                "watchdog_enabled": watchdog_enabled,
                "connections_count": connections_count,
                "plugins_count": plugins_count,
                "scheduler_jobs_count": scheduler_jobs_count,
            },
        }


# 类单例
dashboard_service = DashboardService()
