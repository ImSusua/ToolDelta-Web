import os
import json
import time
import uuid
import threading
from datetime import datetime, timedelta

from app.log_service import log_service
from app.tooldelta_manager import tooldelta_manager

_FMT = "%Y-%m-%d %H:%M:%S"
_DATA_FILE = "scheduler.json"


def parse(s):
    """解析持久化的时间字符串 -> datetime。"""
    return datetime.strptime(s, _FMT)


class SchedulerService:
    """定时任务服务：按计划（间隔 / 每日定点）向 ToolDelta 控制台发送命令（如重启、备份）。

    持久化：app.instance_path + Lock + 原子写（临时文件 + os.replace）。
    后台线程：daemon=True，线程内不触碰 current_app；init_app 时快照路径/数据。
    线程循环整体用 try/except 包裹，单个循环异常只记录日志不退出。
    默认任务 enabled=False，未启用的任务不会触发任何命令，无副作用。
    """

    def __init__(self):
        self._data_path = None
        self._jobs = []
        self._lock = threading.Lock()
        self._thread = None

    # ─── 初始化 ───────────────────────────────────────────────

    def init_app(self, app):
        # 快照路径（仅此处依赖 app，之后线程内不再使用 current_app）
        self._data_path = os.path.join(app.instance_path, _DATA_FILE)
        os.makedirs(os.path.dirname(self._data_path), exist_ok=True)
        self._load_jobs()

        # 兼容：若主应用未注册本蓝图，则在此兜底注册（幂等，避免重复注册）
        try:
            from app.routes.scheduler import bp as scheduler_bp
            if "scheduler" not in getattr(app, "blueprints", {}):
                app.register_blueprint(scheduler_bp)
        except Exception:
            pass

        # 启动后台调度线程（daemon）
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="scheduler-loop"
            )
            self._thread.start()

    # ─── 持久化 ───────────────────────────────────────────────

    def _load_jobs(self):
        data = None
        if self._data_path and os.path.isfile(self._data_path):
            try:
                with open(self._data_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = None
        if isinstance(data, list):
            self._jobs = [self._normalize_job(j) for j in data]
        else:
            self._jobs = []
        self._write_locked()

    def _write(self):
        with self._lock:
            self._write_locked()

    def _write_locked(self):
        # 原子写：先写临时文件再替换，避免写一半崩溃导致数据丢失
        if not self._data_path:
            return
        tmp = self._data_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._jobs, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self._data_path)

    @staticmethod
    def _normalize_job(job):
        """补全所有字段，保证结构完整（缺失字段用默认值）。"""
        if not isinstance(job, dict):
            job = {}
        return {
            "id": job.get("id"),
            "name": job.get("name", ""),
            "type": job.get("type", "interval"),
            "interval": job.get("interval"),
            "hour": job.get("hour"),
            "minute": job.get("minute"),
            "command": job.get("command", ""),
            "enabled": bool(job.get("enabled", False)),
            "last_run": job.get("last_run"),
            "next_run": job.get("next_run"),
            "run_count": int(job.get("run_count", 0) or 0),
            "last_error": job.get("last_error"),
        }

    # ─── 校验与构造 ───────────────────────────────────────────

    @staticmethod
    def _validate_and_build(payload, base=None):
        """校验 payload 并生成/合并 job 字典；非法时抛出 ValueError。

        base 为已有 job（更新时传入），用于保留 id / run_count / last_run。
        """
        if not isinstance(payload, dict):
            raise ValueError("请求数据格式不合法")

        name = payload.get("name") if base is None else payload.get("name", base.get("name"))
        command = payload.get("command") if base is None else payload.get("command", base.get("command"))
        type_ = payload.get("type") if base is None else payload.get("type", base.get("type"))

        if not name or not str(name).strip():
            raise ValueError("任务名称不能为空")
        if not command or not str(command).strip():
            raise ValueError("命令不能为空")
        if type_ not in ("interval", "daily"):
            raise ValueError("任务类型不合法（应为 interval 或 daily）")

        job = dict(base) if base else {}
        job["name"] = str(name).strip()
        job["command"] = str(command)
        job["type"] = type_

        if type_ == "interval":
            interval = payload.get("interval") if base is None else payload.get("interval", base.get("interval"))
            try:
                interval = int(interval or 0)
            except (TypeError, ValueError):
                raise ValueError("间隔秒数（interval）必须是整数")
            if interval < 1:
                raise ValueError("间隔秒数（interval）必须 >= 1")
            job["interval"] = interval
            job["hour"] = None
            job["minute"] = None
        else:  # daily
            hour = payload.get("hour") if base is None else payload.get("hour", base.get("hour"))
            minute = payload.get("minute") if base is None else payload.get("minute", base.get("minute"))
            if hour is None or minute is None:
                raise ValueError("小时/分钟不能为空")
            try:
                hour = int(hour)
                minute = int(minute)
            except (TypeError, ValueError):
                raise ValueError("小时/分钟必须是整数")
            if hour < 0 or hour > 23:
                raise ValueError("小时（hour）范围为 0-23")
            if minute < 0 or minute > 59:
                raise ValueError("分钟（minute）范围为 0-59")
            job["hour"] = hour
            job["minute"] = minute
            job["interval"] = None

        if "enabled" in payload:
            job["enabled"] = bool(payload["enabled"])
        elif base is None:
            # 默认任务 disabled，避免无真实进程时产生副作用
            job["enabled"] = False

        return job

    @staticmethod
    def _compute_next_run(job, now):
        try:
            if job.get("type") == "interval":
                interval = int(job.get("interval") or 0)
                lst = job.get("last_run")
                base = parse(lst) if lst else now
                return (base + timedelta(seconds=interval)).strftime(_FMT)
            if job.get("type") == "daily":
                hour = int(job.get("hour") or 0)
                minute = int(job.get("minute") or 0)
                cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if cand <= now:
                    cand = cand + timedelta(days=1)
                return cand.strftime(_FMT)
        except Exception:
            return None
        return None

    # ─── 公开方法 ─────────────────────────────────────────────

    def list_jobs(self):
        with self._lock:
            jobs = list(self._jobs)
        now = datetime.now()
        out = []
        for job in jobs:
            item = dict(job)
            item["next_run"] = self._compute_next_run(job, now)
            out.append(item)
        return out

    def add_job(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("请求数据格式不合法")
        job = self._validate_and_build(payload)
        job["id"] = uuid.uuid4().hex
        job["last_run"] = None
        job["next_run"] = None
        job["run_count"] = 0
        with self._lock:
            self._jobs.append(job)
            self._write_locked()
        return dict(job)

    def update_job(self, job_id, payload):
        """更新任务。返回 (ok, message)：
        - (True, "")：更新成功
        - (False, "任务不存在")：job_id 未找到
        - (False, "<校验错误详情>")：参数非法（原代码返回 False 让路由显示"任务不存在"，
          误导用户以为任务被删，其实是 interval/type/command 等字段不合法）
        保留 tuple 返回值，路由层据此返回明确错误信息。"""
        if not job_id:
            return False, "缺少任务 id"
        if not isinstance(payload, dict):
            return False, "参数格式错误"
        with self._lock:
            base = None
            for j in self._jobs:
                if j.get("id") == job_id:
                    base = j
                    break
            if base is None:
                return False, "任务不存在"
            try:
                updated = self._validate_and_build(payload, base)
            except ValueError as e:
                # 关键修正：原代码吞掉 ValueError 返回 False，路由层误报"任务不存在"，
                # 但任务其实存在只是参数非法。这里把具体校验错误透传给调用方
                return False, str(e)
            updated["id"] = job_id
            # 保留运行时计数（仅当 payload 未显式覆盖时）
            if "run_count" not in payload:
                updated["run_count"] = base.get("run_count", 0)
            if "last_run" not in payload:
                updated["last_run"] = base.get("last_run")
            if "next_run" not in payload:
                updated["next_run"] = base.get("next_run")
            # 原地替换，保持列表引用稳定
            idx = self._jobs.index(base)
            self._jobs[idx] = updated
            self._write_locked()
        return True, ""

    def delete_job(self, job_id):
        if not job_id:
            return False
        with self._lock:
            before = len(self._jobs)
            self._jobs = [j for j in self._jobs if j.get("id") != job_id]
            if len(self._jobs) == before:
                return False
            self._write_locked()
        return True

    def run_now(self, job_id):
        if not job_id:
            return False
        with self._lock:
            target = None
            for j in self._jobs:
                if j.get("id") == job_id:
                    target = j
                    break
        if target is None:
            return False
        self._run_job(target, datetime.now())
        return True

    # ─── 后台线程 ─────────────────────────────────────────────

    def _loop(self):
        while True:
            time.sleep(5)
            try:
                self._tick()
            except Exception as e:
                try:
                    log_service.error("定时任务循环异常: " + str(e), "SCHEDULER")
                except Exception:
                    pass

    def _tick(self):
        now = datetime.now()
        # 拷贝一份快照迭代，避免迭代期间被修改
        with self._lock:
            jobs = list(self._jobs)
        for job in jobs:
            if not job.get("enabled"):
                continue
            should = False
            try:
                if job.get("type") == "interval":
                    lst = job.get("last_run")
                    if lst is None or (now - parse(lst)).total_seconds() >= job.get("interval", 1):
                        should = True
                elif job.get("type") == "daily":
                    if (now.hour == int(job.get("hour", -1))
                            and now.minute == int(job.get("minute", -1))
                            and (job.get("last_run") is None
                                 or parse(job.get("last_run")).date() != now.date())):
                        should = True
            except Exception:
                should = False
            if should:
                self._run_job(job, now)

    def _run_job(self, job, now):
        # send_command 内部会写子进程 stdin，ToolDelta 阻塞读 stdin 时管道写满会阻塞 write()；
        # 不能在 self._lock 内调用，否则会卡住 list_jobs/add_job/delete_job 以及 dashboard 轮询
        # 导致整个面板无响应。改为：锁外调用 send_command，成功后再持锁更新 last_run/run_count
        cmd = job.get("command", "")
        ok = False
        send_err = None
        try:
            ok = tooldelta_manager.send_command(cmd)
        except Exception as e:
            send_err = e
        with self._lock:
            try:
                if not ok:
                    err_reason = "进程未运行或命令发送失败"
                    if send_err is not None:
                        err_reason = str(send_err)
                    job["last_error"] = now.strftime(_FMT) + " " + err_reason
                    try:
                        log_service.warn(
                            f"定时任务未执行({err_reason}): {job['name']}", "SCHEDULER"
                        )
                    except Exception:
                        pass
                    return
                job["last_run"] = now.strftime(_FMT)
                job["run_count"] = job.get("run_count", 0) + 1
                try:
                    log_service.info(
                        f"定时任务执行: {job['name']} -> {cmd}", "SCHEDULER"
                    )
                except Exception:
                    pass
            except Exception as e:
                job["last_error"] = now.strftime(_FMT) + " " + str(e)
                job["last_run"] = now.strftime(_FMT)
                try:
                    log_service.error(
                        f"定时任务失败: {job['name']}: {e}", "SCHEDULER"
                    )
                except Exception:
                    pass
            finally:
                # 持久化 last_run / run_count
                self._write_locked()


scheduler_service = SchedulerService()
