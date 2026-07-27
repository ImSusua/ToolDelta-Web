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
        # 目录权限收敛:与 user.json / server_conn.json / connection_service 一致
        # scheduler.json 含定时命令(可能含敏感参数),同主机其他用户不应读取
        # makedirs 时直接指定 mode=0o700,消除"先创建 0o755 再 chmod"的 TOCTOU 窗口
        # (与 config.py / market_service.py / log_service.py 一致)
        os.makedirs(os.path.dirname(self._data_path), exist_ok=True, mode=0o700)
        try:
            os.chmod(os.path.dirname(self._data_path), 0o700)
        except OSError:
            pass
        # 既有文件权限收敛:旧文件可能仍是默认 0o644
        if os.path.isfile(self._data_path):
            try:
                os.chmod(self._data_path, 0o600)
            except OSError:
                pass
        self._load_jobs()

        # 兼容：若主应用未注册本蓝图，则在此兜底注册（幂等，避免重复注册）
        try:
            from app.routes.scheduler import bp as scheduler_bp
            if "scheduler" not in getattr(app, "blueprints", {}):
                app.register_blueprint(scheduler_bp)
        except Exception:
            pass

        # 启动后台调度线程（daemon）
        # 关键:is_alive() 检查与 start() 必须在 _lock 内,否则并发调用 init_app
        # (测试/热重载场景)时两个调用方可能同时通过 is_alive() 检查并各自创建线程,
        # 导致两个调度循环并行运行,同一 job 被双重执行(run_count 重复递增、
        # send_command 重复发送 ToolDelta 控制台命令)。
        # 注意:_loop 内的 _tick 也用 _lock,但此处持锁时间极短(仅 start 调用),
        # 不会与 _tick 形成长时间死锁。
        with self._lock:
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
        # 用 tempfile.mkstemp 替换固定 .tmp 名:多进程/多线程场景下固定名会互相覆盖
        # (虽然当前持锁,但与 auth_service/connection_service 的 mkstemp 模式一致更安全)
        # 同时补充 flush/fsync 保证数据真正落盘，与 watchdog/connection/wallpaper 一致。
        if not self._data_path:
            return
        import tempfile as _tf
        fd, tmp = _tf.mkstemp(prefix=_DATA_FILE + ".", suffix=".tmp",
                              dir=os.path.dirname(self._data_path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._jobs, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._data_path)
            tmp = None  # 标记已成功 replace，finally 不再删除
            # 收敛权限:含定时命令(可能含敏感参数 op xxx / login token=xxx),
            # 同主机其他用户不应读取,与 user.json / server_conn.json 一致
            try:
                os.chmod(self._data_path, 0o600)
            except OSError:
                pass
        finally:
            if tmp is not None and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

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
            "cron": job.get("cron"),
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
        # 长度上限：防止超大字符串撑爆 scheduler.json 落盘 + list_jobs 全量回传前端
        if len(str(name)) > 64:
            raise ValueError("任务名称不能超过 64 字符")
        if not command or not str(command).strip():
            raise ValueError("命令不能为空")
        # 与 tooldelta_manager.MAX_COMMAND_LEN=8192 对齐，防止超大命令撑爆持久化文件
        if len(str(command)) > 8192:
            raise ValueError("命令长度不能超过 8192 字符")
        if type_ not in ("interval", "daily", "cron"):
            raise ValueError("任务类型不合法（应为 interval / daily / cron）")

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
            if interval > 86400 * 365:
                raise ValueError("间隔秒数不能超过 1 年（31536000 秒）")
            job["interval"] = interval
            job["hour"] = None
            job["minute"] = None
            job["cron"] = None
        elif type_ == "daily":
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
            job["cron"] = None
        else:  # cron
            cron_expr = payload.get("cron") if base is None else payload.get("cron", base.get("cron"))
            if not cron_expr or not str(cron_expr).strip():
                raise ValueError("cron 表达式不能为空")
            cron_str = str(cron_expr).strip()
            SchedulerService._validate_cron(cron_str)
            job["cron"] = cron_str
            job["interval"] = None
            job["hour"] = None
            job["minute"] = None

        if "enabled" in payload:
            job["enabled"] = bool(payload["enabled"])
        elif base is None:
            # 默认任务 disabled，避免无真实进程时产生副作用
            job["enabled"] = False

        return job

    @staticmethod
    def _parse_cron_field(field, min_val, max_val):
        """解析单个 cron 字段，返回允许值的 set。支持 * , - /"""
        result = set()
        for part in field.split(","):
            part = part.strip()
            if not part:
                raise ValueError("cron 字段格式错误")
            step = 1
            if "/" in part:
                part, step_str = part.split("/", 1)
                try:
                    step = int(step_str)
                except (TypeError, ValueError):
                    raise ValueError("cron 步长必须是整数")
                if step < 1:
                    raise ValueError("cron 步长必须 >= 1")
            if part == "*":
                start, end = min_val, max_val
            elif "-" in part:
                try:
                    start_str, end_str = part.split("-", 1)
                    start = int(start_str)
                    end = int(end_str)
                except (TypeError, ValueError):
                    raise ValueError("cron 范围格式错误")
            else:
                try:
                    start = int(part)
                    end = start
                except (TypeError, ValueError):
                    raise ValueError("cron 值必须是整数")
            if start < min_val or end > max_val or start > end:
                raise ValueError(f"cron 值超出范围 ({min_val}-{max_val})")
            for v in range(start, end + 1, step):
                result.add(v)
        return result

    @staticmethod
    def _validate_cron(expr):
        """验证 5 字段 cron 表达式：分 时 日 月 周"""
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError("cron 表达式必须包含 5 个字段（分 时 日 月 周）")
        try:
            SchedulerService._parse_cron_field(parts[0], 0, 59)
            SchedulerService._parse_cron_field(parts[1], 0, 23)
            SchedulerService._parse_cron_field(parts[2], 1, 31)
            SchedulerService._parse_cron_field(parts[3], 1, 12)
            SchedulerService._parse_cron_field(parts[4], 0, 6)
        except ValueError as e:
            raise ValueError(f"cron 表达式错误: {e}")

    @staticmethod
    def _cron_matches(dt, minute_set, hour_set, dom_set, month_set, dow_set,
                       dom_is_star=True, dow_is_star=True):
        """检查 datetime 是否匹配 cron 字段集合。
        标准cron语义：当 day-of-month 和 day-of-week 都非*时取并集(OR)。
        """
        if dom_is_star and dow_is_star:
            day_match = True
        elif dom_is_star:
            day_match = (dt.weekday() + 1) % 7 in dow_set
        elif dow_is_star:
            day_match = dt.day in dom_set
        else:
            day_match = (dt.day in dom_set) or ((dt.weekday() + 1) % 7 in dow_set)
        return (dt.minute in minute_set and
                dt.hour in hour_set and
                day_match and
                dt.month in month_set)

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
            if job.get("type") == "cron":
                cron_str = job.get("cron", "")
                parts = cron_str.split()
                if len(parts) != 5:
                    return None
                minute_set = SchedulerService._parse_cron_field(parts[0], 0, 59)
                hour_set = SchedulerService._parse_cron_field(parts[1], 0, 23)
                dom_set = SchedulerService._parse_cron_field(parts[2], 1, 31)
                month_set = SchedulerService._parse_cron_field(parts[3], 1, 12)
                dow_set = SchedulerService._parse_cron_field(parts[4], 0, 6)
                _dom_star = parts[2].strip() == "*"
                _dow_star = parts[4].strip() == "*"
                cand = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
                for _ in range(366 * 24 * 60):
                    try:
                        if SchedulerService._cron_matches(cand, minute_set, hour_set, dom_set, month_set, dow_set,
                                                          _dom_star, _dow_star):
                            return cand.strftime(_FMT)
                    except Exception:
                        pass
                    cand = cand + timedelta(minutes=1)
                return None
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
        # time.sleep 包在 try 内:与 watchdog_service._loop 风格一致,
        # 极端情况下(如解释器关闭抛 SystemExit)sleep 抛异常会被捕获并记录,
        # 避免线程静默消亡导致所有定时任务停摆且无日志
        while True:
            try:
                time.sleep(5)
                self._tick()
            except Exception as e:
                try:
                    log_service.error("定时任务循环异常: " + str(e), "SCHEDULER")
                except Exception:
                    pass

    def _tick(self):
        now = datetime.now()
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
                                 or parse(job.get("last_run")).date() != now.date()
                                 or (now - parse(job.get("last_run"))).total_seconds() >= 60)):
                        should = True
                elif job.get("type") == "cron":
                    cron_str = job.get("cron", "")
                    parts = cron_str.split()
                    if len(parts) == 5:
                        minute_set = self._parse_cron_field(parts[0], 0, 59)
                        hour_set = self._parse_cron_field(parts[1], 0, 23)
                        dom_set = self._parse_cron_field(parts[2], 1, 31)
                        month_set = self._parse_cron_field(parts[3], 1, 12)
                        dow_set = self._parse_cron_field(parts[4], 0, 6)
                        _dom_star = parts[2].strip() == "*"
                        _dow_star = parts[4].strip() == "*"
                        if self._cron_matches(now.replace(second=0, microsecond=0),
                                              minute_set, hour_set, dom_set, month_set, dow_set,
                                              _dom_star, _dow_star):
                            lst = job.get("last_run")
                            if lst is None or (now - parse(lst)).total_seconds() >= 60:
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
                        # job['name'] 用户可控,走 sanitize_for_log 防日志注入
                        # (与 routes/auth.py:audit、routes/watchdog.py:_audit 一致)
                        log_service.warn(
                            f"定时任务未执行({err_reason}): {log_service.sanitize_for_log(job['name'])}",
                            "SCHEDULER"
                        )
                    except Exception:
                        pass
                    return
                job["last_run"] = now.strftime(_FMT)
                job["run_count"] = job.get("run_count", 0) + 1
                try:
                    # 关键修复(凭据泄露 + 日志注入):
                    #   1) job['name'] 用户可控,走 sanitize_for_log 防日志注入
                    #   2) 旧实现记录完整 cmd,可能含 `op 用户名 密码` / `login token=xxx`
                    #      等敏感参数。log_service._redact 的正则覆盖有限(如 `op` 后跟
                    #      裸密码不含字段名时无法匹配),仅记录命令长度避免凭据落盘。
                    log_service.info(
                        f"定时任务执行: {log_service.sanitize_for_log(job['name'])} "
                        f"(命令长度 {len(cmd)})",
                        "SCHEDULER"
                    )
                except Exception:
                    pass
            except Exception as e:
                job["last_error"] = now.strftime(_FMT) + " " + str(e)
                job["last_run"] = now.strftime(_FMT)
                try:
                    log_service.error(
                        f"定时任务失败: {log_service.sanitize_for_log(job['name'])}: {e}",
                        "SCHEDULER"
                    )
                except Exception:
                    pass
            finally:
                # 持久化 last_run / run_count
                self._write_locked()


scheduler_service = SchedulerService()
