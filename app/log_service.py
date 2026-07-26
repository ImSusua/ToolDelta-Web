import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta

# 日志脱敏正则:拦截常见敏感字段(密码/token/secret/cookie/key/auth 等)的赋值与 JSON 字段,
# 替换为 ***REDACTED*** 防止 ToolDelta stdout、命令参数、配置打印等把凭据落盘。
# 同时拦截 Bearer Token 与长 hex/base64 token(典型 32+ 字符)避免泄露 API 凭据。
#
# 字段名集合覆盖(关键修复):
#   - 增加 `session`(裸 cookie 字段名,如 `Cookie: session=xxx`)与 `set[_-]?cookie`
#     (HTTP 头名 `Set-Cookie`,同时匹配下划线/连字符两种写法)
#   - 保留 `session[_-]?id` 以兼容旧格式
# 值匹配组(group 3)改进:
#   - 引号包裹的值匹配到配对引号(`"..."`/`'...'`),允许内部含空格
#   - 裸值仍按 `[^\s"\',}]+` 截断到分隔符,避免误吞整行
_SENSITIVE_PATTERN = re.compile(
    r'(?i)\b(password|passwd|pwd|token|secret|cookie|set[_-]?cookie|api[_-]?key|'
    r'access[_-]?token|refresh[_-]?token|authorization|session|session[_-]?id|client[_-]?secret)'
    r'(["\']?\s*[:=]\s*)'
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s"\',}]+)'
)
_BEARER_PATTERN = re.compile(r'(?i)\b(Bearer\s+)([A-Za-z0-9._\-/+=]+)')
_BASIC_PATTERN = re.compile(r'(?i)\b(Basic\s+)([A-Za-z0-9._\-/+=]+)')
# 数据库连接串中的凭据:protocol://user:password@host
# 拦截 SQLAlchemy/redis/mongodb 等异常信息中泄露的连接串
_DB_URL_PATTERN = re.compile(
    r'(?i)\b([a-z][a-z0-9+\-.]*://)([^:@/\s]+):([^@/\s]+)@'
)


def _redact(message):
    """对日志 message 做脱敏:替换敏感凭据字段为 ***REDACTED***。
    仅在落盘前调用,避免 password/token/cookie 等被持久化到 instance/logs/*.log。

    顺序很关键:必须先脱敏 Bearer/Basic,再走通用字段正则。
    否则 `Authorization: Bearer xxx` 中的 `Bearer` 会被通用正则当作
    `authorization` 字段的"值"先吃掉,导致 Bearer 正则匹配不到,
    真实 JWT token 原样落盘。"""
    if not isinstance(message, str):
        message = str(message)
    # 先脱敏 Bearer / Basic:它们的值是 token,不能被通用正则吞掉前缀
    message = _BEARER_PATTERN.sub(
        lambda m: m.group(1) + "***REDACTED***", message
    )
    message = _BASIC_PATTERN.sub(
        lambda m: m.group(1) + "***REDACTED***", message
    )
    # 再脱敏数据库连接串中的 password 字段
    message = _DB_URL_PATTERN.sub(
        lambda m: m.group(1) + "***REDACTED***:***REDACTED***@", message
    )
    # 最后脱敏通用敏感字段(此时 Bearer/Basic 已替换,不会误吞)
    message = _SENSITIVE_PATTERN.sub(
        lambda m: m.group(1) + m.group(2) + "***REDACTED***", message
    )
    return message


# 日志注入防护:替换换行/制表等控制字符为 ?
# 用户控制的字符串(plugin name / filename / host / detail 等)拼入 audit 日志前
# 必须先经此函数,防止 \n 伪造日志行干扰审计追责、嫁祸他人或隐藏真实操作。
# 与 routes/auth.py:_sanitize_for_log 保持一致,统一在此模块导出供所有 blueprint 复用。
# 覆盖 ASCII 控制字符 + Unicode 行/段分隔符(NEL/U+2028/U+2029),
# 防止外部 SIEM/tail -f 把这些字符解释为换行造成行注入
_CTRL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f\u0085\u2028\u2029]')


def sanitize_for_log(s):
    """日志注入防护:把控制字符替换为 ?,防止伪造审计日志行。

    所有 blueprint 的 audit() 函数在拼入用户可控字段(name/filename/host/path/detail)
    前应调用此函数,与 routes/auth.py 既有的 _sanitize_for_log 行为一致。"""
    if not isinstance(s, str):
        if s is None:
            return ""
        s = str(s)
    return _CTRL_CHAR_RE.sub('?', s)

class LogService:
    # 内存中当日日志行数上限，超出滚动截断，防止长期运行内存泄漏（P1-4）
    MAX_LOG_LINES = 5000
    # 写文件失败时的内存兜底队列上限：保留最近 N 条便于事后排查（P1-5）
    MAX_FALLBACK_QUEUE = 200
    # 单日志文件大小上限：超过则轮转到 .1 / .2 / .3 后缀，避免单文件 GB 级塞满磁盘
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
    # 历史日志保留天数：启动时清理超过该天数的 .log 文件，避免无限累积
    MAX_HISTORY_DAYS = 30
    # 单文件轮转时保留的旧档数量：当日志写入超限后保留最近 N 份滚动副本
    MAX_ROTATED_FILES = 5

    def __init__(self):
        self._logs_dir = None
        self._today = None
        self._today_lines = []
        self._lock = threading.RLock()
        # 写文件失败时的内存兜底队列：避免日志完全丢失（P1-5）
        self._fallback_queue = []
        # 当日当前写入文件大小缓存（避免每次 _write 都 os.path.getsize 触发 stat）
        self._today_size = 0

    def init_app(self, app):
        self._logs_dir = os.path.join(app.instance_path, "logs")
        # 目录创建直接指定 mode=0o700:不依赖事后 chmod,避免 open 创建日志文件
        # 与 chmod 之间的 TOCTOU 窗口被同主机用户抢 fd 读敏感日志。
        # makedirs 的 mode 会被进程 umask 削弱,但 0o700 不含 group/other 位,
        # umask 无法进一步削弱它(umask 只能"收紧"不能"放宽")。
        os.makedirs(self._logs_dir, mode=0o700, exist_ok=True)
        # 兜底已存在目录(可能是历史版本以 0o755 创建)的权限
        try:
            os.chmod(self._logs_dir, 0o700)
        except OSError:
            pass
        # 既有日志文件权限归一化:旧版本可能以 0o644 创建,启动时统一收敛到 0o600
        # 避免历史日志(含凭据/IP/路径)被同主机其他用户读取
        self._normalize_log_file_perms()
        self._rotate()
        # 启动时清理超过 MAX_HISTORY_DAYS 天的旧日志，避免长期运行磁盘塞满
        try:
            self._cleanup_old_logs()
        except Exception:
            pass

    def _normalize_log_file_perms(self):
        """启动时把 logs_dir 下所有 .log/.log.N 文件权限收敛到 0o600。
        旧版本创建的日志文件可能仍是默认 0o644,需统一收紧避免历史凭据泄露。"""
        if not self._logs_dir or not os.path.isdir(self._logs_dir):
            return
        try:
            for name in os.listdir(self._logs_dir):
                if not (name.endswith(".log") or
                        re.match(r'^\d{4}-\d{2}-\d{2}\.log\.\d+$', name)):
                    continue
                path = os.path.join(self._logs_dir, name)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
        except OSError:
            pass

    def _rotate(self):
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if today != self._today:
                self._today = today
                self._today_lines = []
                self._load_today()

    def _load_today(self):
        path = self._today_path()
        if not path:
            return
        if os.path.isfile(path):
            # 同步当日文件大小缓存：用于 _write 中按大小轮转判断，避免每次 os.path.getsize
            try:
                self._today_size = os.path.getsize(path)
            except OSError:
                self._today_size = 0
            with open(path, "r", encoding="utf-8") as f:
                self._today_lines = [line.rstrip("\r\n") for line in f.readlines()]
            if len(self._today_lines) > self.MAX_LOG_LINES:
                self._today_lines = self._today_lines[-self.MAX_LOG_LINES:]
        else:
            self._today_size = 0

    def _maybe_rotate_by_size(self):
        """单文件大小轮转：当日志文件超过 MAX_FILE_SIZE 时滚动到 .1/.2/... 后缀。
        避免高频日志（如 ToolDelta stdout 全量 debug）单文件无限增长塞满磁盘。
        调用方须持有 self._lock。"""
        path = self._today_path()
        if not path or not os.path.isfile(path):
            return
        if self._today_size < self.MAX_FILE_SIZE:
            return
        # 滚动：.4 → .5（删除），.3 → .4，...，.log → .1
        # 先删除最旧一份（超出保留数量）
        oldest = path + "." + str(self.MAX_ROTATED_FILES)
        if os.path.isfile(oldest):
            try:
                os.remove(oldest)
            except OSError:
                pass
        # 从次新到旧依次重命名（i 从 MAX_ROTATED_FILES-1 到 1）
        for i in range(self.MAX_ROTATED_FILES - 1, 0, -1):
            src = path + "." + str(i)
            dst = path + "." + str(i + 1)
            if os.path.isfile(src):
                try:
                    os.rename(src, dst)
                except OSError:
                    pass
        # 当前文件 → .1
        try:
            os.rename(path, path + ".1")
        except OSError:
            pass
        # 重置内存大小缓存，下一行写入会创建新文件
        self._today_size = 0

    def _cleanup_old_logs(self):
        """启动时清理超过 MAX_HISTORY_DAYS 天的 .log* 文件，避免长期运行累积塞满磁盘。
        以文件 mtime 而非文件名日期为基准，避免时区差异误删当日日志。"""
        if not self._logs_dir or not os.path.isdir(self._logs_dir):
            return
        cutoff = time.time() - self.MAX_HISTORY_DAYS * 86400
        for name in os.listdir(self._logs_dir):
            # 只清理 .log 和 .log.N 滚动文件
            if not name.endswith(".log") and not re.match(r'^\d{4}-\d{2}-\d{2}\.log\.\d+$', name):
                continue
            path = os.path.join(self._logs_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass

    def _today_path(self):
        if not self._logs_dir or not self._today:
            return None
        return os.path.join(self._logs_dir, self._today + ".log")

    def _write(self, level, source, message):
        self._rotate()
        # 落盘前脱敏:拦截 password/token/cookie/Authorization 等敏感字段,
        # 避免 ToolDelta stdout、命令参数、配置打印等把凭据持久化到 .log 文件
        message = _redact(message)
        # source 兜底过滤:替换控制字符(防 ]/换行破坏日志解析),截断到 32 字符
        # 调用方虽多传常量,但作为纵深防御避免未来新增调用方拼入用户可控 source
        source = sanitize_for_log(source).replace("]", "")[:32]
        # message 中的换行做转义:虽然 _redact 已脱敏,但 message 仍可能含
        # 调用方未 sanitize 的用户输入(如 routes/auth.py:141 的 username)。
        # 这里把 \n/\r 替换为可见转义,阻断行注入但保留可读性。
        # 注意:必须在 _redact 之后做,避免影响脱敏正则的匹配
        message = message.replace("\n", "\\n").replace("\r", "\\r")
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}][{level}][{source}] {message}"
        with self._lock:
            self._today_lines.append(line)
            if len(self._today_lines) > self.MAX_LOG_LINES:
                self._today_lines = self._today_lines[-self.MAX_LOG_LINES:]
            path = self._today_path()
            if path:
                # 单文件大小轮转：写之前先检查是否超限，超限则滚动到 .1/.2/...
                # 避免高频日志（如 ToolDelta stdout 全量 debug）单文件无限增长塞满磁盘
                self._maybe_rotate_by_size()
                try:
                    # 用 os.open + O_CREAT 指定 mode=0o600,避免 open(path, "a")
                    # 默认 mode 0o666 与事后 chmod 之间的 TOCTOU 窗口被同主机用户
                    # 抢 fd 持续读取敏感日志。O_APPEND 保证多线程写不会互相覆盖。
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    try:
                        with os.fdopen(fd, "a", encoding="utf-8") as f:
                            f.write(line + "\n")
                    except Exception:
                        # fdopen 失败需手动关闭 fd 避免泄漏
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        raise
                    # 维护内存中的文件大小缓存：line 长度 + 1（换行符）字节数
                    # 用 len(line.encode('utf-8')) 而非 len(line) 以正确处理中文等多字节字符
                    self._today_size += len(line.encode("utf-8")) + 1
                except Exception as e:
                    # 静默 fallback 到 stderr + 内存兜底队列，绝不向调用方抛出（P1-5）
                    # 避免日志写入失败拖垮主流程；同时保留线索便于事后排查
                    self._fallback_queue.append(line)
                    if len(self._fallback_queue) > self.MAX_FALLBACK_QUEUE:
                        self._fallback_queue = self._fallback_queue[-self.MAX_FALLBACK_QUEUE:]
                    try:
                        # 异常信息也需脱敏,避免 str(e) 含路径/连接串泄露到 stderr
                        sys.stderr.write("[LOG_FALLBACK] " + _redact(str(e)) + " | " + line + "\n")
                    except Exception:
                        pass

    def debug(self, message, source="SYSTEM"):
        self._write("DEBUG", source, message)

    def info(self, message, source="SYSTEM"):
        self._write("INFO", source, message)

    def warn(self, message, source="SYSTEM"):
        self._write("WARN", source, message)

    def error(self, message, source="SYSTEM"):
        self._write("ERROR", source, message)

    def get_fallback_queue(self):
        """返回写文件失败的兜底队列快照，供诊断接口查看。"""
        with self._lock:
            return list(self._fallback_queue)

    def get_today_logs(self, tail=500):
        self._rotate()
        with self._lock:
            return self._today_lines[-tail:]

    def list_log_files(self):
        self._rotate()
        if not self._logs_dir or not os.path.isdir(self._logs_dir):
            return []
        files = []
        for f in sorted(os.listdir(self._logs_dir), reverse=True):
            if f.endswith(".log"):
                path = os.path.join(self._logs_dir, f)
                size = os.path.getsize(path)
                files.append({
                    "name": f.replace(".log", ""),
                    "date": f.replace(".log", ""),
                    "size": size,
                })
        return files

    # 单日志文件读取上限：避免历史日志过大时一次性读入内存（P2-2）
    MAX_LOG_FILE_BYTES = 10 * 1024 * 1024

    def get_log_file(self, date_str):
        if not self._logs_dir or not date_str:
            return []
        # 纵深防御:强制校验 date_str 格式为 YYYY-MM-DD,避免 ../ 逃逸日志目录。
        # 当前所有调用方(routes/api.py、routes/logs.py)已做 isdigit 校验,
        # 但作为公共方法不能依赖调用方校验,未来新增调用方可能传未校验的值。
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_str):
            return []
        path = os.path.normpath(os.path.join(self._logs_dir, date_str + ".log"))
        # 双重校验:词法 normpath 后必须仍位于 logs_dir 之下
        abs_logs = os.path.abspath(self._logs_dir)
        if not (path == abs_logs or path.startswith(abs_logs + os.sep)):
            return []
        if os.path.isfile(path):
            # 读操作持锁,避免与 _maybe_rotate_by_size 的 rename 序列竞态
            # 导致读到一半文件被 rename 走抛 FileNotFoundError
            with self._lock:
                if not os.path.isfile(path):
                    return []
                size = os.path.getsize(path)
                if size > self.MAX_LOG_FILE_BYTES:
                    # 超大日志只读取最后 10 MB，避免阻塞
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(-self.MAX_LOG_FILE_BYTES, os.SEEK_END)
                        f.readline()  # 跳过可能被截断的首行
                        return [line.rstrip("\r\n") for line in f.readlines()]
                with open(path, "r", encoding="utf-8") as f:
                    return [line.rstrip("\r\n") for line in f.readlines()]
        return []

    # ─── 日志增强：分级 / 搜索 / 过滤 / 导出 ─────────────

    LEVELS = ["DEBUG", "INFO", "WARN", "ERROR"]

    @staticmethod
    def _parse_line(line):
        """解析单行日志，返回 dict 或 None。"""
        m = re.match(r"^\[(\d{2}:\d{2}:\d{2})\]\[(\w+)\]\[([^\]]+)\]\s*(.*)$", line)
        if not m:
            return None
        return {
            "time": m.group(1),
            "level": m.group(2),
            "source": m.group(3),
            "message": m.group(4),
        }

    def query(self, level=None, source=None, keyword=None, date=None, limit=500):
        """按级别 / 来源 / 关键字 / 日期过滤日志，返回最后 limit 条（保持原顺序）。"""
        if date is None or date == self._today:
            # 取快照后释放锁再迭代:避免迭代过程中 _write 触发
            # self._today_lines = self._today_lines[-N:] 替换 list 抛 RuntimeError
            with self._lock:
                lines = list(self._today_lines)
        else:
            lines = self.get_log_file(date)
        results = []
        for raw in lines:
            parsed = self._parse_line(raw)
            if not parsed:
                continue
            if level and parsed["level"].lower() != level.lower():
                continue
            if source and parsed["source"] != source:
                continue
            if keyword and keyword.lower() not in parsed["message"].lower():
                continue
            results.append(parsed)
        if limit is not None:
            results = results[-limit:]
        return results

    def list_sources(self, date=None):
        """返回某日日志中出现过的全部来源（去重并排序）。"""
        if date is None or date == self._today:
            with self._lock:
                lines = list(self._today_lines)
        else:
            lines = self.get_log_file(date)
        sources = set()
        for raw in lines:
            parsed = self._parse_line(raw)
            if parsed:
                sources.add(parsed["source"])
        return sorted(sources)

    def export_text(self, date=None, level=None, source=None, keyword=None):
        """将过滤后的日志拼成纯文本，每行 '时间 [LEVEL][SOURCE] message'。"""
        results = self.query(level=level, source=source, keyword=keyword, date=date)
        return "\n".join(
            f"{r['time']} [{r['level']}][{r['source']}] {r['message']}" for r in results
        )


log_service = LogService()
