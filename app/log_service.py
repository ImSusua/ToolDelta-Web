import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta

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
        os.makedirs(self._logs_dir, exist_ok=True)
        self._rotate()
        # 启动时清理超过 MAX_HISTORY_DAYS 天的旧日志，避免长期运行磁盘塞满
        try:
            self._cleanup_old_logs()
        except Exception:
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
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
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
                        sys.stderr.write("[LOG_FALLBACK] " + str(e) + " | " + line + "\n")
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
        path = os.path.join(self._logs_dir, date_str + ".log")
        if os.path.isfile(path):
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
            lines = self._today_lines
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
            lines = self._today_lines
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
