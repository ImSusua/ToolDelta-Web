"""ToolDelta 运行依赖的网站内自管模块。

为什么要做这件事：
ToolDelta 本身是 poetry 工程(pyproject.toml)，自带 colorama/pyspeedtest/aiohttp/
numpy/grpcio/protobuf/nbtlib/rich 等大量第三方依赖；而 Web 面板自身只装了 flask/
flask-socketio/requests。全新 Linux 环境若缺这些依赖，main.py 一启动就
ModuleNotFoundError 退出，表现为“点击启动起不来”。Windows 上往往因为之前全局装过
依赖而正常，于是问题只在 Linux 暴露。

本模块把“依赖安装”做成网站内功能：
1. 应用初始化时自动检测，缺失则在后台安装（不阻塞启动请求）。
2. 对官方源 + 多个国内镜像源做测速，选最快的作为主源（-i），官方源兜底
   （--extra-index-url）。
3. 后台线程跑 `pip install -e .`，实时解析进度并广播给前端。
4. 前端用高斯模糊遮罩 + 动画进度条 + 实时日志呈现，依赖就绪后自动消失。
"""

import os
import re
import sys
import time
import ssl
import shutil
import threading
import subprocess
import urllib.request
import importlib.util

# 镜像源：官方 + 国内主流。测速后按延迟升序，最快者作为 -i。
MIRRORS = [
    ("官方 PyPI", "https://pypi.org/simple"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple"),
    ("清华大学", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("腾讯云", "https://mirrors.tencent.com/pypi/simple"),
    ("华为云", "https://repo.huaweicloud.com/repository/pypi/simple"),
    ("中科大", "https://pypi.mirrors.ustc.edu.cn/simple"),
]

# 轻量依赖校验（导入探测），全在则视为已就绪，可跳过安装。
_LIGHTWEIGHT_CHECK = [
    "pyspeedtest", "colorama", "aiohttp", "numpy", "grpc",
    "nbtlib", "rich", "websockets", "yaml", "brotli",
]

# pip 输出里解析“正在安装/已安装”的包名列表，用于推进进度条。
_RE_INSTALLING = re.compile(r"Installing collected packages:\s*(.+)", re.IGNORECASE)
_RE_INSTALLED = re.compile(r"Successfully installed\s*(.+)", re.IGNORECASE)
# 纯进度条行（只含百分比/进度条字符），无实际信息，过滤掉避免刷屏。
_RE_PROGRESS_NOISE = re.compile(r"^\s*[^A-Za-z\u4e00-\u9fff]*\d+%[^A-Za-z\u4e00-\u9fff]*$")
_RE_ERROR = re.compile(r"^(ERROR|error:)", re.IGNORECASE)

LOG_CAP = 500          # 内存中保留的日志行上限
BROADCAST_THROTTLE = 0.2  # 进度广播最小间隔（秒）


class DependencyService:
    def __init__(self):
        self.app = None
        self._lock = threading.Lock()
        self._listeners = []
        self._done_event = threading.Event()
        self._thread = None
        self._status = "idle"   # idle | installing | ready | failed
        self._progress = 0       # 0-100
        self._stage = "未初始化"
        self._mirror = ""
        self._mirror_name = ""
        self._log = []
        self._error = ""
        self._install_total = 0
        self._install_done = 0
        self._last_broadcast = 0.0
        # 已解析的兼容 Python 解释器（当主进程 Python 不满足 ToolDelta 要求时使用）。
        # None=未探测；""=探测过但没找到；其他字符串=找到的绝对路径。
        self._resolved_python = None
        # 内部缓存哨兵：避免重复子进程探测。None=未探测；""=没找到；其他=路径。
        self._resolved_python_cache_sentinel = None

    # ─── 初始化 ────────────────────────────────

    def init_app(self, app):
        self.app = app

    # ─── 监听器（供 socket 层转发前端） ──────────

    def add_listener(self, fn):
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def clear_listeners(self):
        with self._lock:
            self._listeners.clear()

    def _broadcast(self, event_type, data=None):
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(event_type, data)
            except Exception:
                pass

    def _payload(self):
        return {
            "status": self._status,
            "progress": self._progress,
            "stage": self._stage,
            "mirror": self._mirror_name or self._mirror,
            "mirror_url": self._mirror,
            "install_total": self._install_total,
            "install_done": self._install_done,
            "ready": self._status == "ready",
            "error": self._error,
            "log_tail": self._log[-40:],
            "ts": time.time(),
        }

    def _emit_progress(self, force=False):
        now = time.time()
        if not force and now - self._last_broadcast < BROADCAST_THROTTLE:
            return
        self._last_broadcast = now
        self._broadcast("dependency_progress", self._payload())

    # ─── 状态查询 ───────────────────────────────

    def is_ready(self):
        if self._status == "ready":
            return True
        # 若当前 Python 不满足 ToolDelta 要求，尝试找一个兼容的解释器（3.10/3.11/3.12）
        # 并用它来检测/安装依赖；找不到才真正失败。
        compat, spec = self._python_compatible()
        if not compat:
            alt = self._resolve_compatible_python()
            if not alt:
                # 既无兼容 Python，依赖又不可能装上：明确标 failed，不再“谎称 ready”
                # 让 UI 透出明确错误，避免用户点“启动”后看到“未连接”而无所适从。
                with self._lock:
                    self._status = "failed"
                    self._progress = 100
                    self._stage = (
                        f"当前 Python {sys.version_info.major}.{sys.version_info.minor} "
                        f"不满足 ToolDelta 要求 ({spec})，且未找到 3.10/3.11/3.12 解释器。"
                        f"请安装兼容版本后再启动。"
                    )
                    self._error = self._stage
                self._emit_progress(force=True)
                return False
            # 找到兼容解释器：用它的环境做依赖检测，避免主进程 Python 与子进程 Python
            # 各自独立 site-packages 导致误判。
            if self._deps_present(python_bin=alt):
                with self._lock:
                    self._status = "ready"
                    self._progress = 100
                    self._stage = (
                        f"依赖已就绪（使用 {alt} 运行 ToolDelta）"
                    )
                    self._resolved_python = alt
                self._emit_progress(force=True)
                return True
            # 兼容 Python 找到了但依赖没装：不直接返回 True（这会让 start() 误以为已就绪），
            # 而是保持 idle 让 _ensure_dependencies/start_install 用 alt 去装依赖。
            with self._lock:
                if self._status not in ("installing", "ready"):
                    self._status = "idle"
                    self._stage = (
                        f"已找到兼容解释器 {alt}，请选择依赖安装方式"
                    )
                self._resolved_python = alt
            self._emit_progress(force=True)
            return False
        # 离线安装后依赖已装但 status 可能仍为 idle（maybe_auto_install 改为只检测不触发后），
        # 用 import 探测做实检，就绪则自动更新状态为 ready，避免 _ensure_dependencies 误判。
        if self._deps_present():
            with self._lock:
                self._status = "ready"
                self._progress = 100
                self._stage = "依赖已就绪"
            self._emit_progress(force=True)
            return True
        return False

    def get_status(self):
        return self._payload()

    def _td_dir(self):
        if not self.app:
            return ""
        return self.app.config["TOOLDELTA_DIR"]

    def _deps_present(self, python_bin=None):
        """检测 ToolDelta 运行依赖是否已安装。

        python_bin=None：使用当前进程 Python 的 importlib 探测。
        python_bin=<path>：用指定解释器子进程探测其 site-packages。
          这是为了应对 Web 面板 Python 与 ToolDelta 子进程 Python 不一致的场景
          （如面板跑在 3.14，ToolDelta 需要 3.10/3.11/3.12）。
        """
        if python_bin is None or python_bin == sys.executable:
            try:
                missing = [m for m in _LIGHTWEIGHT_CHECK
                           if importlib.util.find_spec(m) is None]
                return not missing
            except Exception:
                return False
        # 跨解释器检测：用子进程 import 探测目标 site-packages 是否齐备
        try:
            code = (
                "import importlib.util, sys\n"
                "missing=[m for m in %r if importlib.util.find_spec(m) is None]\n"
                "sys.exit(0 if not missing else 1)\n"
            ) % (_LIGHTWEIGHT_CHECK,)
            proc = subprocess.run(
                [python_bin, "-c", code],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return proc.returncode == 0
        except Exception:
            return False

    # 兼容 Python 解释器候选：与 pyproject `python = ">=3.10,<3.13"` 对齐。
    # 顺序按 ToolDelta 官方推荐度排序：3.12 > 3.11 > 3.10。
    _COMPAT_PYTHON_CANDIDATES = (
        "python3.12", "python3.11", "python3.10",
        "python3.12.exe", "python3.11.exe", "python3.10.exe",
    )

    def _resolve_compatible_python(self):
        """当前 Python 不满足 ToolDelta 要求时，搜索系统中可用的兼容解释器。

        优先级：
        1. 环境变量 TD_PYTHON（用户显式指定，最高优先级）
        2. pyenv 版本目录（/root/.pyenv/versions/3.12.x/bin/python3.12 等）
        3. PATH 上的 python3.10 / python3.11 / python3.12
        找到后缓存，避免每次启动都重复探测。返回绝对路径字符串或 None。
        """
        if getattr(self, "_resolved_python_cache_sentinel", None) is not None:
            return self._resolved_python_cache_sentinel

        def _check_compat(python_bin):
            """快速验证解释器存在且满足 pyproject python 要求。"""
            if not python_bin or not os.path.isfile(python_bin):
                return False
            try:
                # 直接复用 _python_compatible 的解析逻辑，但用目标解释器版本号
                proc = subprocess.run(
                    [python_bin, "-c",
                     "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=8,
                )
                if proc.returncode != 0:
                    return False
                ver_str = (proc.stdout or b"").decode(errors="replace").strip()
                parts = ver_str.split(".")
                if len(parts) < 2:
                    return False
                major, minor = int(parts[0]), int(parts[1])
                # 与 pyproject >=3.10,<3.13 硬编码对齐（pyproject 解析已在
                # _python_compatible 中实现，此处用子进程版本走相同逻辑）。
                compat, _ = self._check_version_compatible(major, minor)
                return compat
            except Exception:
                return False

        candidates = []

        # 1. 环境变量优先
        env_py = os.environ.get("TD_PYTHON")
        if env_py:
            candidates.append(env_py)

        # 2. pyenv 版本目录
        try:
            pyenv_root = os.environ.get("PYENV_ROOT") or os.path.expanduser("~/.pyenv")
            versions_dir = os.path.join(pyenv_root, "versions")
            if os.path.isdir(versions_dir):
                for v in sorted(os.listdir(versions_dir), reverse=True):
                    # 仅取 3.10 / 3.11 / 3.12 前缀的版本目录
                    if v.startswith(("3.10", "3.11", "3.12")):
                        for bn in ("python3", "python", "python3.12", "python3.11", "python3.10"):
                            p = os.path.join(versions_dir, v, "bin", bn)
                            if os.path.isfile(p):
                                candidates.append(p)
                                break
        except Exception:
            pass

        # 3. PATH 上的候选
        for name in self._COMPAT_PYTHON_CANDIDATES:
            try:
                p = shutil.which(name)
                if p:
                    candidates.append(p)
            except Exception:
                pass

        for c in candidates:
            if _check_compat(c):
                self._resolved_python_cache_sentinel = c
                return c
        self._resolved_python_cache_sentinel = ""
        return ""

    def _check_version_compatible(self, major, minor):
        """与 _python_compatible 同语义，但接受外部传入的版本号。
        复用同一份 pyproject 解析，避免逻辑分叉。"""
        pyproject = os.path.join(self._td_dir(), "pyproject.toml")
        if not os.path.isfile(pyproject):
            return True, ""
        try:
            with open(pyproject, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            m = re.search(r'python\s*=\s*"([^"]+)"', text)
            if not m:
                return True, ""
            spec = m.group(1)
            for cond in spec.split(","):
                cond = cond.strip()
                if not cond:
                    continue
                if cond.startswith("~="):
                    ver_str = cond[2:]
                    parts = [x for x in ver_str.split(".") if x.isdigit()]
                    if len(parts) >= 2:
                        lower = tuple(int(x) for x in parts[:2])
                        upper_minor = lower[1] + 1
                        if (major, minor) < lower or (major, minor) >= (lower[0], upper_minor):
                            return False, spec
                    continue
                if cond.startswith(">="):
                    req = tuple(int(x) for x in cond[2:].split(".") if x.isdigit())
                    if (major, minor) < req[:2]:
                        return False, spec
                    continue
                if cond.startswith("<"):
                    req = tuple(int(x) for x in cond[1:].split(".") if x.isdigit())
                    if (major, minor) >= req[:2]:
                        return False, spec
                    continue
            return True, spec
        except Exception:
            return True, ""

    def _python_compatible(self):
        """检查当前 Python 版本是否满足 ToolDelta pyproject.toml 中的 python 要求。

        支持 >=, >, <=, <, ==, ~= 等常见 poetry 版本约束。
        返回 (compatible, spec_string)。无法解析或不存在 pyproject 时默认视为兼容，
        避免偶发的文件读取问题阻塞启动。
        """
        pyproject = os.path.join(self._td_dir(), "pyproject.toml")
        if not os.path.isfile(pyproject):
            return True, ""
        try:
            with open(pyproject, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            m = re.search(r'python\s*=\s*"([^"]+)"', text)
            if not m:
                return True, ""
            spec = m.group(1)
            major, minor = sys.version_info[:2]
            for cond in spec.split(","):
                cond = cond.strip()
                if not cond:
                    continue
                # ~=3.12 -> >=3.12,<3.13
                if cond.startswith("~="):
                    ver_str = cond[2:]
                    parts = [x for x in ver_str.split(".") if x.isdigit()]
                    if len(parts) >= 2:
                        lower = tuple(int(x) for x in parts[:2])
                        upper_minor = lower[1] + 1
                        if (major, minor) < lower or (major, minor) >= (lower[0], upper_minor):
                            return False, spec
                    continue
                if cond.startswith("=="):
                    req = tuple(int(x) for x in cond[2:].split(".") if x.isdigit())
                    if (major, minor) != req[:2]:
                        return False, spec
                    continue
                if cond.startswith(">="):
                    req = tuple(int(x) for x in cond[2:].split(".") if x.isdigit())
                    if (major, minor) < req[:2]:
                        return False, spec
                    continue
                if cond.startswith(">"):
                    req = tuple(int(x) for x in cond[1:].split(".") if x.isdigit())
                    if (major, minor) <= req[:2]:
                        return False, spec
                    continue
                if cond.startswith("<="):
                    req = tuple(int(x) for x in cond[2:].split(".") if x.isdigit())
                    if (major, minor) > req[:2]:
                        return False, spec
                    continue
                if cond.startswith("<"):
                    req = tuple(int(x) for x in cond[1:].split(".") if x.isdigit())
                    if (major, minor) >= req[:2]:
                        return False, spec
                    continue
            return True, spec
        except Exception:
            return True, ""

    # ─── 依赖清单解析 ───────────────────────────

    def parse_td_dependencies(self):
        """解析 ToolDelta 的 pyproject.toml 中的 [tool.poetry.dependencies]。
        返回依赖名列表（排除 python 与 source 项）。"""
        pyproject = os.path.join(self._td_dir(), "pyproject.toml")
        if not os.path.isfile(pyproject):
            return []
        try:
            with open(pyproject, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            return []
        # 定位 [tool.poetry.dependencies] 段
        m = re.search(r"\[tool\.poetry\.dependencies\](.*?)(\n\[|\Z)", text, re.DOTALL)
        if not m:
            return []
        deps = []
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("python") or line.startswith("source"):
                continue
            name = re.split(r"\s*=\s*", line, 1)[0].strip()
            if name and re.match(r"^[A-Za-z0-9_.\-]+$", name):
                deps.append(name)
        return deps

    # ─── 镜像源测速 ─────────────────────────────

    def _speed_test_mirrors(self):
        """并行测速各镜像源，返回 (url, name) 最快者；全失败时回退官方源。"""
        results = {}
        results_lock = threading.Lock()

        def test(name, url):
            base = url.rstrip("/") + "/"
            target = base + "certifi/"
            t0 = time.time()
            try:
                try:
                    ctx = ssl.create_default_context()
                except Exception:
                    ctx = None
                req = urllib.request.Request(target, headers={"User-Agent": "pip/24"})
                if ctx is not None:
                    with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
                        r.read(1024)
                else:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        r.read(1024)
                with results_lock:
                    results[name] = (time.time() - t0, url)
            except Exception:
                with results_lock:
                    results[name] = (None, url)

        threads = []
        for name, url in MIRRORS:
            t = threading.Thread(target=test, args=(name, url), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=6)

        ranked = sorted(
            ((v[0], k, v[1]) for k, v in results.items() if v[0] is not None),
            key=lambda x: x[0],
        )
        if ranked:
            return ranked[0][2], ranked[0][1]
        # 全失败：回退官方源
        return MIRRORS[0][1], MIRRORS[0][0]

    # ─── 安装流程 ───────────────────────────────

    def maybe_auto_install(self):
        """应用初始化时调用：仅做依赖检测，不自动触发安装。

        依赖缺失时保持 idle 状态，由前端弹出「选择安装方式」卡片，
        让用户决定本地（离线）或网络（在线）安装；若用户未选择而直接
        点击启动，tooldelta_manager._ensure_dependencies 会作为兜底自动后台安装。
        """
        if not self.app:
            return
        pyproject = os.path.join(self._td_dir(), "pyproject.toml")
        if not os.path.isfile(pyproject):
            with self._lock:
                self._status = "ready"
                self._progress = 100
                self._stage = "未检测到 pyproject，跳过依赖安装"
                self._mirror_name = "—"
            self._emit_progress(force=True)
            return
        # Python 版本不兼容时：尝试找一个兼容解释器（3.10/3.11/3.12），
        # 用它来检测/安装依赖；找不到才标 failed 让 UI 给出明确错误。
        # 修复点：原逻辑直接置 ready=true 让 start() 放行，但子进程依赖确实没装，
        # 表现为「点启动→未连接」，与用户期望严重背离。
        compat, spec = self._python_compatible()
        if not compat:
            alt = self._resolve_compatible_python()
            if not alt:
                with self._lock:
                    self._status = "failed"
                    self._progress = 100
                    self._stage = (
                        f"当前 Python {sys.version_info.major}.{sys.version_info.minor} "
                        f"不满足 ToolDelta 要求 ({spec})，且未找到 3.10/3.11/3.12 解释器。"
                        f"请安装兼容版本后再启动。"
                    )
                    self._error = self._stage
                    self._mirror_name = "—"
                self._emit_progress(force=True)
                return
            # 找到兼容解释器：用它做依赖检测，并把 _resolved_python 暴露给
            # tooldelta_manager._spawn() 用于真正拉起子进程。
            self._resolved_python = alt
            if self._deps_present(python_bin=alt):
                with self._lock:
                    self._status = "ready"
                    self._progress = 100
                    self._stage = (
                        f"依赖已就绪（使用 {alt} 运行 ToolDelta）"
                    )
                    self._mirror_name = "—"
                self._emit_progress(force=True)
                return
            # 兼容解释器存在但依赖缺失：保持 idle，等用户在前端选择安装方式
            with self._lock:
                if self._status not in ("ready", "installing"):
                    self._status = "idle"
                    self._stage = (
                        f"当前 Python 不兼容，已找到 {alt}。请选择依赖安装方式（本地 / 网络）"
                    )
                self._mirror_name = "—"
            self._emit_progress(force=True)
            return
        if self._deps_present():
            with self._lock:
                self._status = "ready"
                self._progress = 100
                self._stage = "依赖已就绪"
                self._mirror_name = "—"
            self._emit_progress(force=True)
            return
        # 依赖缺失：保持 idle，等待前端选择安装方式（不再自动触发）。
        with self._lock:
            if self._status in ("ready", "installing"):
                return
            self._status = "idle"
            self._stage = "请选择依赖安装方式（本地离线 / 网络在线）"
            self._mirror_name = "—"
        self._emit_progress(force=True)

    def start_install(self, mode=None):
        """非阻塞触发安装（幂等）。

        mode:
          - 'local'  : 仅本地离线安装（随附 wheels/），不回退联网，适合共享服务器/无外网
          - 'online' : 仅联网安装（测速选最快镜像源后 pip install -e .），获取最新版本
          - None/其他: 自动模式——离线优先，离线失败再回退联网兜底
        """
        if mode not in ("local", "online"):
            mode = None
        with self._lock:
            if self._status == "installing":
                return self._payload()
            if self._status == "ready":
                return self._payload()
            self._status = "installing"
            self._progress = 4
            self._stage = "正在检测运行环境依赖…"
            self._mirror = ""
            self._mirror_name = ""
            self._log = []
            self._error = ""
            self._install_total = 0
            self._install_done = 0
            self._install_mode = mode or "auto"
            self._done_event.clear()
        self._thread = threading.Thread(
            target=self._run_install_wrapper, args=(mode,), daemon=True
        )
        self._thread.start()
        return self._payload()

    def _run_install_wrapper(self, mode=None):
        """_run_install 的安全包装：任何未捕获异常都导致失败回调，
        避免 daemon 线程静默消亡后状态永远卡在 installing。"""
        try:
            self._run_install(mode)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self._append_log("✘ 安装进程异常: " + str(e))
            self._append_log(tb)
            self._fail_install("安装异常: " + str(e))

    def ensure_installed_blocking(self, timeout=30):
        """供 start() 在拉起主程序前调用：若已就绪立即返回 True；
        否则触发安装并最多等待 timeout 秒。返回 (ok, msg)。"""
        if self.is_ready():
            return True, "ToolDelta 依赖已就绪"
        self.start_install()
        waited = self._done_event.wait(timeout=timeout)
        if self.is_ready():
            return True, "ToolDelta 依赖安装完成"
        if not waited and self._status == "installing":
            return False, "依赖仍在后台初始化中（约 %d%%），请稍候片刻后重试启动" % self._progress
        return False, "ToolDelta 依赖安装失败：" + (self._error or "未知错误")

    def _append_log(self, line):
        self._log.append(line)
        if len(self._log) > LOG_CAP:
            self._log = self._log[-LOG_CAP:]

    # ─── 本地离线安装（随项目分发的 wheels/） ──────────

    def _wheels_dir(self):
        """随项目分发的离线依赖目录（仓库根/wheels）。"""
        here = os.path.dirname(os.path.abspath(__file__))  # .../app
        return os.path.join(os.path.dirname(here), "wheels")

    def _get_pip_python(self):
        """返回应执行 pip 的 Python 解释器。

        - 当前 Python 兼容 ToolDelta：返回 sys.executable
        - 不兼容但已找到 3.10/3.11/3.12：返回该解释器路径
          （必须用它装 pip 才能让依赖落到子进程 site-packages，避免装错环境）
        """
        if self._resolved_python:
            return self._resolved_python
        compat, _ = self._python_compatible()
        if not compat:
            alt = self._resolve_compatible_python()
            if alt:
                self._resolved_python = alt
                return alt
        return sys.executable

    def _run_pip(self, cmd, cwd):
        """执行 pip 子进程，实时收集日志与进度，返回进程退出码。不设置终态。"""
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except FileNotFoundError:
            self._append_log("✘ 未找到 pip，请先安装 Python 包管理工具 pip")
            return 127
        except Exception as e:
            self._append_log("✘ 启动安装进程失败：" + str(e))
            return 1
        try:
            stdout = proc.stdout
            if stdout is None:
                return proc.wait()
            for raw in stdout:
                line = raw.replace("\r", "").strip()
                if not line:
                    continue
                if _RE_PROGRESS_NOISE.match(line):
                    continue
                self._append_log(line)
                self._parse_progress(line)
                if _RE_ERROR.match(line):
                    self._error = line
                self._emit_progress()
            return proc.wait()
        except Exception as e:
            self._append_log("✘ 读取安装输出异常：" + str(e))
            return 1
        finally:
            # 异常路径下子进程未回收会变僵尸进程：_append_log/_parse_progress/
            # _emit_progress 任一抛异常时，上面 for 循环中断但 pip 子进程可能
            # 仍在运行。多次触发会累积多个僵尸进程。finally 确保 terminate + wait。
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
            except Exception:
                pass

    def _try_offline_install(self, td_dir):
        """优先尝试从本地 wheels/ 离线安装，避免联网（共享服务器限流/无外网）。成功返回 True。"""
        wheels = self._wheels_dir()
        req = os.path.join(wheels, "requirements.txt")
        if not os.path.isfile(req):
            self._append_log("ℹ 未发现本地离线依赖清单，跳过离线安装，转联网。")
            return False
        whl_files = [f for f in os.listdir(wheels) if f.endswith((".whl", ".tar.gz"))]
        if not whl_files:
            self._append_log("ℹ wheels 目录为空，跳过离线安装，转联网。")
            return False

        # 选定目标 Python：若当前不兼容则用 _resolved_python 装，确保依赖落到子进程环境
        pip_python = self._get_pip_python()
        if pip_python != sys.executable:
            self._append_log(f"ℹ 当前 Python {sys.version_info.major}.{sys.version_info.minor} "
                             f"不兼容，改用 {pip_python} 安装依赖。")

        self._stage = "正在从本地离线包安装依赖…"
        self._progress = 6
        self._mirror_name = "本地离线包"
        self._mirror = wheels
        self._append_log("📦 使用本地离线依赖包（%d 个）安装，无需联网。" % len(whl_files))
        self._emit_progress(force=True)

        cmd = [
            pip_python, "-m", "pip", "install",
            "--no-index", "--find-links", wheels,
            "-r", req, "--upgrade", "--no-input", "--root-user-action", "ignore",
            "--break-system-packages",
        ]
        self._append_log("▶ 执行：" + " ".join(cmd))
        rc = self._run_pip(cmd, td_dir)
        # 校验依赖是否齐：用 pip_python 的环境做检测，而非当前进程
        if rc == 0 and self._deps_present(python_bin=pip_python):
            self._status = "ready"
            self._progress = 100
            self._stage = "依赖安装完成（本地离线），可启动 ToolDelta"
            self._append_log("✔ 依赖已从本地离线包安装完成。")
            self._resolved_python = pip_python
            self._emit_progress(force=True)
            self._done_event.set()
            return True
        if rc != 0:
            self._append_log("⚠ 本地离线安装返回码 %d，回退到联网安装。" % rc)
        else:
            self._append_log("⚠ 本地离线安装完成但依赖仍未全部就绪，回退联网补装。")
        return False

    def _online_install(self, td_dir):
        """联网兜底：测速选最快镜像源后 pip install -e .。"""
        pip_python = self._get_pip_python()
        if pip_python != sys.executable:
            self._append_log(f"ℹ 当前 Python {sys.version_info.major}.{sys.version_info.minor} "
                             f"不兼容，改用 {pip_python} 安装依赖。")
        self._stage = "正在测速选择最快镜像源…"
        self._progress = 8
        self._mirror = ""
        self._mirror_name = ""
        self._emit_progress(force=True)
        best_url, best_name = self._speed_test_mirrors()
        self._mirror = best_url
        self._mirror_name = best_name
        self._append_log("⚡ 已选择最快镜像源：%s (%s)" % (best_name, best_url))
        self._stage = "正在使用「%s」安装依赖…" % best_name
        self._progress = 12
        self._emit_progress(force=True)

        cmd = [
            pip_python, "-m", "pip", "install", "-e", ".", "--upgrade",
            "--no-input", "--root-user-action", "ignore",
            "--break-system-packages",
            "-i", best_url, "--extra-index-url", "https://pypi.org/simple",
        ]
        self._append_log("▶ 执行：" + " ".join(cmd))
        rc = self._run_pip(cmd, td_dir)
        if rc == 0:
            self._status = "ready"
            self._progress = 100
            self._stage = "依赖安装完成，可启动 ToolDelta"
            self._append_log("✔ 依赖安装完成。")
            self._resolved_python = pip_python
            self._emit_progress(force=True)
            self._done_event.set()
        else:
            last = "\n".join(self._log[-12:])
            self._fail_install(last)

    def _run_install(self, mode=None):
        td_dir = self._td_dir()
        # Python 版本不兼容：若找到兼容解释器则继续，否则明确失败。
        # 修复点：原逻辑直接 fail，但忽略了系统可能装了 3.10/3.11/3.12 的情况，
        # 导致用户即使有兼容解释器也无法启动。
        compat, spec = self._python_compatible()
        if not compat:
            alt = self._resolve_compatible_python()
            if not alt:
                self._fail_install(
                    f"当前 Python {sys.version_info.major}.{sys.version_info.minor} 不满足 "
                    f"ToolDelta 要求 ({spec})，且未找到 3.10/3.11/3.12 解释器。"
                    f"请安装兼容版本后再启动。"
                )
                return
            self._resolved_python = alt
            self._append_log(
                f"ℹ 当前 Python {sys.version_info.major}.{sys.version_info.minor} 不兼容 ({spec})，"
                f"改用 {alt} 安装依赖。"
            )
        self._append_log("▶ 开始检测并安装 ToolDelta 运行依赖…")
        if mode == "local":
            self._append_log("▶ 安装方式：本地离线（随附 wheels/，可能非最新版本）")
        elif mode == "online":
            self._append_log("▶ 安装方式：网络在线（从镜像源获取最新版本）")
        else:
            self._append_log("▶ 安装方式：自动（离线优先，失败回退联网）")
        self._emit_progress(force=True)

        deps = self.parse_td_dependencies()
        if deps:
            self._append_log("检测到 %d 个依赖：%s" % (len(deps), ", ".join(deps)))
        else:
            self._append_log("⚠ 未解析到 pyproject 依赖清单，将安装当前目录包。")

        if mode == "local":
            # 仅本地离线安装，不回退联网；失败则标记失败，由前端提示改用网络安装
            if self._try_offline_install(td_dir):
                return
            self._fail_install(
                "本地离线安装未完成（离线包可能不适用当前平台 / Python 版本），请改用「网络安装」重试。"
            )
            return
        if mode == "online":
            # 仅联网安装，获取最新版本
            self._online_install(td_dir)
            return
        # 自动：离线优先，失败回退联网兜底
        if self._try_offline_install(td_dir):
            return
        self._online_install(td_dir)

    def _parse_progress(self, line):
        m = _RE_INSTALLING.search(line)
        if m:
            pkgs = [p for p in re.split(r",\s*", m.group(1).strip()) if p]
            self._install_total = len(pkgs)
            return
        m = _RE_INSTALLED.search(line)
        if m:
            pkgs = [p for p in m.group(1).split() if p]
            self._install_done = len(pkgs)
            if self._install_total:
                pct = int(round(self._install_done / self._install_total * 100))
                self._progress = min(99, max(self._progress, pct))
            else:
                self._progress = min(99, self._progress + 2)
            return
        # 下载 / 收集阶段：缓慢推进，避免进度条卡死
        if "Collecting" in line or "Downloading" in line:
            self._progress = min(11, self._progress)

    def _fail_install(self, msg):
        self._status = "failed"
        self._error = msg
        self._stage = "依赖安装失败"
        self._append_log("✘ " + msg)
        self._emit_progress(force=True)
        self._done_event.set()


dependency_service = DependencyService()
