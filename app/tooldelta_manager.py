import os
import sys
import re
import atexit
import signal
# Windows 没有 pty 模块：pty 仅用于类 Unix 平台给子进程套伪终端以输出 ANSI 真彩，
# 故仅在非 Windows 平台导入；Windows 走 PIPE 回退（彩色由主程序自身兜底）。
# 注意：import 必须条件化，否则 Windows 上模块加载即因 ImportError 整体崩溃、无法启动。
if os.name != "nt":
    import pty
import subprocess
import threading
import time
import locale
import shutil
import zipfile
from app.log_service import log_service

# 匹配所有 Minecraft 颜色/格式控制序列(§ + 其后任意字符)。
# 主程序部分输出(如 rich logging 路径未转换的 §S 删除线、扩展色 §g~§v 等)
# 会以裸 §X 形式进入 Web 终端, 若不清理会残留成乱码。
# 注意: 主程序通过 colormode_replace / rich 已把大部分 § 转成 ANSI,
# 此处仅作兜底, 清除任何残留的 § 控制序列。
_MC_COLOR_RE = re.compile(r"§.")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_ANSI_SEQ_RE = re.compile(r"\x1b\[([0-9;]*)m")
# 匹配除 SGR 颜色序列(\x1b[...m)之外的所有 ANSI 控制序列(清屏/清行/光标移动等)，
# 避免在控制台中渲染出乱码控制字符。排除 m/M 结尾以保留颜色序列供后续转换。
_ANSI_NON_COLOR_RE = re.compile(r"\x1b\[[0-9;?]*[a-ln-zA-Z]")
# 剥离"非 CSI"的终端控制序列：OSC(\x1b]...title...\x07/ST)、字符集/私有模式
# (\x1b(0 等行绘制字符集)、以及任何孤立 ESC。这些序列在 Web 终端里无法渲染，
# 若不清理会以裸控制字符(乱码)形式残留，表现为"终端字符转义失效"。
# 负向先行 (?![\[]) 保证不误伤 CSI(\x1b[...，含颜色 SGR) 序列。
_ANSI_NON_CSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*?(?:\x07|\x1b\\)"   # OSC: \x1b]...BEL/ST
    r"|\x1b[\(\)\*\+\-\.\/][^\x1b]?"        # 字符集/私有模式: \x1b(0 等
    r"|\x1b(?![\[])"                         # 兜底: 孤立 ESC(排除 CSI)
)

_ANSI_COLORS = {
    "0;30": "#000", "0;31": "#e74c3c", "0;32": "#2ecc71", "0;33": "#f1c40f",
    "0;34": "#3498db", "0;35": "#9b59b6", "0;36": "#1abc9c", "0;37": "#ecf0f1",
    "1;30": "#555", "1;31": "#ff6b6b", "1;32": "#55efc4", "1;33": "#ffeaa7",
    "1;34": "#74b9ff", "1;35": "#a29bfe", "1;36": "#00cec9", "1;37": "#fff",
    "0;90": "#666", "0;91": "#e17055", "0;92": "#00b894", "0;93": "#fdcb6e",
    "0;94": "#6c5ce7", "0;95": "#e056fd", "0;96": "#00cec9", "0;97": "#dfe6e9",
    "1;90": "#999", "1;91": "#fab1a0", "1;92": "#55efc4", "1;93": "#ffeaa7",
    "1;94": "#a29bfe", "1;95": "#fd79a8", "1;96": "#81ecec", "1;97": "#fff",
}

# Minecraft 格式码 -> 标准 ANSI 16 色(近似映射, 用于把残留 § 序列也上色)
_MC_TO_ANSI = {
    "0": "30", "1": "34", "2": "32", "3": "36", "4": "31", "5": "35", "6": "33",
    "7": "37", "8": "90", "9": "94", "a": "92", "b": "96", "c": "91", "d": "95",
    "e": "93", "f": "97",
}

def mc_to_ansi(text):
    """把 Minecraft § 颜色/格式码转换为标准 ANSI SGR 序列,
    便于 ansi_to_html 统一还原为彩色 HTML。主程序经 rich/colormode_replace
    已把大部分 § 转成 ANSI, 但少数未被转换而残留的 § 序列(如 §S 删除线、
    部分扩展色)经此处理后也能正确着色, 避免 Web 控制台出现裸 § 乱码。"""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "§" and i + 1 < n:
            code = text[i + 1]
            if code in _MC_TO_ANSI:
                out.append("\x1b[" + _MC_TO_ANSI[code] + "m")
            elif code == "r":
                out.append("\x1b[0m")
            elif code == "l":
                out.append("\x1b[1m")
            elif code == "u":
                out.append("\x1b[4m")
            elif code == "o":
                out.append("\x1b[3m")
            elif code == "k":
                out.append("\x1b[8m")
            elif code == "S":
                out.append("\x1b[9m")
            # 其余 §X(如扩展色 §g~§v) 视作控制码丢弃
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)

def strip_ansi(text):
    text = _MC_COLOR_RE.sub("", text)
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _ANSI_NON_CSI_RE.sub("", text)
    return text

# SGR 代码 -> 16 色十六进制(基础前景 30-37 / 亮色前景 90-97;
# 背景 40-47、100-107 复用对应前景色)。
# 注意：必须在 _xterm256_to_hex 之前定义，避免函数运行时尚未初始化。
_BASE16 = {}
for _c in range(30, 38):
    _BASE16[_c] = _ANSI_COLORS.get("0;%d" % _c)
for _c in range(90, 98):
    _BASE16[_c] = _ANSI_COLORS.get("0;%d" % _c)


def _xterm256_to_hex(n):
    """xterm 256 色调色板: 0-15 基础色, 16-231 6x6x6 彩色立方体, 232-255 灰度。"""
    if n < 0:
        n = 0
    elif n > 255:
        n = 255
    if n < 8:
        return _BASE16.get(30 + n, "#000000")
    if n < 16:
        return _BASE16.get(90 + (n - 8), "#ffffff")
    if n < 232:
        n -= 16
        levels = (0, 95, 135, 175, 215, 255)
        r = levels[n // 36]
        g = levels[(n // 6) % 6]
        b = levels[n % 6]
        return "#%02x%02x%02x" % (r, g, b)
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def ansi_to_html(text):
    text = mc_to_ansi(text)
    # 先剥离 OSC/字符集/孤立 ESC(非 CSI 控制序列)，避免裸控制字符残留成乱码；
    # 再剥离 CSI 非颜色序列，保留 SGR(\x1b[...m) 供下方颜色转换。
    text = _ANSI_NON_CSI_RE.sub("", text)
    text = _ANSI_NON_COLOR_RE.sub("", text)
    parts = _ANSI_SEQ_RE.split(text)
    html = ""
    fg = None
    bg = None
    bold = False
    italic = False
    underline = False
    strike = False
    for i, part in enumerate(parts):
        if i % 2 == 0:
            # 文本段：用当前样式(前景/背景/粗体/斜体/下划线/删除线)包裹
            if part:
                style = ""
                if bold:
                    style += "font-weight:bold;"
                if italic:
                    style += "font-style:italic;"
                if underline or strike:
                    deco = []
                    if underline:
                        deco.append("underline")
                    if strike:
                        deco.append("line-through")
                    style += "text-decoration:" + " ".join(deco) + ";"
                if fg:
                    style += "color:" + fg + ";"
                if bg:
                    style += "background-color:" + bg + ";"
                if style:
                    html += '<span style="' + style + '">' + escape_html(part) + "</span>"
                else:
                    html += escape_html(part)
        else:
            # SGR 控制序列：解析颜色/格式码。
            # 关键修复：rich 在支持真彩的终端下输出 38;2;r;g;b(而非 16 色)，
            # 旧逻辑 _ANSI_COLORS 只认 16 色导致绝大部分彩色日志丢失颜色。
            # 此处完整支持 真彩(38;2)、256 色(38;5)、背景(48;...)、
            # 下划线(4)/斜体(3)/删除线(9) 等格式码。
            nums = part.split(";")
            j = 0
            while j < len(nums):
                code = nums[j]
                if not code:
                    j += 1
                    continue
                try:
                    ci = int(code)
                except ValueError:
                    j += 1
                    continue
                if ci == 0:
                    fg = bg = None
                    bold = italic = underline = strike = False
                elif ci == 1:
                    bold = True
                elif ci in (2, 21, 22):
                    bold = False
                elif ci == 3:
                    italic = True
                elif ci == 23:
                    italic = False
                elif ci == 4:
                    underline = True
                elif ci == 24:
                    underline = False
                elif ci == 9:
                    strike = True
                elif ci == 29:
                    strike = False
                elif ci == 39:
                    fg = None
                elif ci == 49:
                    bg = None
                elif ci == 38 and j + 1 < len(nums):
                    mode = nums[j + 1]
                    if mode == "2" and j + 4 < len(nums):
                        try:
                            fg = "#%02x%02x%02x" % (int(nums[j + 2]), int(nums[j + 3]), int(nums[j + 4]))
                        except ValueError:
                            pass
                        j += 4
                    elif mode == "5" and j + 2 < len(nums):
                        try:
                            fg = _xterm256_to_hex(int(nums[j + 2]))
                        except ValueError:
                            pass
                        j += 2
                elif ci == 48 and j + 1 < len(nums):
                    mode = nums[j + 1]
                    if mode == "2" and j + 4 < len(nums):
                        try:
                            bg = "#%02x%02x%02x" % (int(nums[j + 2]), int(nums[j + 3]), int(nums[j + 4]))
                        except ValueError:
                            pass
                        j += 4
                    elif mode == "5" and j + 2 < len(nums):
                        try:
                            bg = _xterm256_to_hex(int(nums[j + 2]))
                        except ValueError:
                            pass
                        j += 2
                elif 30 <= ci <= 37:
                    fg = _BASE16.get(ci)
                elif 90 <= ci <= 97:
                    fg = _BASE16.get(ci)
                elif 40 <= ci <= 47:
                    bg = _BASE16.get(ci - 10)
                elif 100 <= ci <= 107:
                    bg = _BASE16.get(ci - 10)
                j += 1
    return html

def escape_html(text):
    """HTML 转义:补全引号转义以兼容属性上下文。
    旧实现仅转义 & < >,在 <span>文本</span> 上下文足够,
    但若未来被用于属性值(data-x="..."、title="..."),未转义的 " ' ` 会触发属性逃逸。
    另剥离 null 字节:某些 HTML 解析器(尤其旧版 IE/嵌入式 webview)会把 \x00 当作
    字符串终止符,导致转义后的内容被截断、后续注入绕过过滤。
    """
    return (text.replace("\x00", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))

def detect_encoding(raw_bytes):
    # 去重，避免 locale 编码与 utf-8 重复探测（P2-8）
    encodings = ["utf-8"]
    pref = locale.getpreferredencoding()
    if pref and pref.lower() not in encodings:
        encodings.append(pref)
    for enc in ("gbk", "gb2312"):
        if enc not in encodings:
            encodings.append(enc)
    for enc in encodings:
        try:
            raw_bytes.decode(enc, errors="strict")
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"

class ToolDeltaManager:
    def __init__(self):
        self.process = None
        self.output_thread = None
        self.running = False
        self.listeners = []
        self._lock = threading.RLock()
        self.output_buffer = []
        self.output_raw_buffer = []
        self.MAX_BUFFER = 500
        self.pty_master = None
        self._encoding = "utf-8"
        self._enc_detected = False
        # 优雅退出保护标志：signal handler 触发后置 True，避免 SIGTERM/SIGINT
        # 重复递交或与 atexit 叠加导致 stop() 被多次调用。
        self._shutting_down = False

    def init_app(self, app):
        self.app = app
        # 注册优雅退出钩子：Flask 主进程被 kill 或正常退出时，确保 ToolDelta
        # 子进程被 stop()（避免成为孤儿进程），并关闭 pty_master 文件描述符
        # （避免 fd 永久泄漏）。init_app 由 create_app 在主线程调用，故此处的
        # signal.signal 注册满足“仅主线程可注册”的约束。
        atexit.register(self.stop)
        # 用 try/except 包裹 signal 注册：部分环境（如 Windows 对 SIGTERM 的
        # 限制、或非主线程调用）可能抛 ValueError/AttributeError/OSError。
        try:
            signal.signal(signal.SIGTERM, self._signal_shutdown)
        except (ValueError, AttributeError, OSError):
            pass
        try:
            signal.signal(signal.SIGINT, self._signal_shutdown)
        except (ValueError, AttributeError, OSError):
            pass

    def _signal_shutdown(self, *_args):
        """SIGTERM/SIGINT 信号处理：触发优雅退出。
        用 _shutting_down 标志保护，避免信号重复递交导致多次 stop()。

        关键:stop() 后必须显式退出主进程。Python 自定义信号处理器返回后会恢复
        被中断的执行点,Werkzeug dev server 的 serve_forever 循环不会因信号而退出,
        导致 kill/Ctrl+C 后子进程被终止但 Web 面板仍在服务,用户误以为已停止;
        容器化部署时需等待 orchestrator 的 SIGKILL 超时(通常 10-30s)才真正退出,
        拖慢滚动更新。故 stop() 后调用 sys.exit(0) 强制退出。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            self.stop()
        except Exception:
            pass
        # 强制退出主进程:仅 stop() 子进程不会让 Flask/Werkzeug 退出
        # 用 os._exit 避免 atexit 钩子再次触发 stop()(已在 _shutting_down 标志保护下,
        # 但 atexit 钩子可能因其他注册顺序问题导致死锁,os._exit 直接终止进程更稳)
        import os as _os
        _os._exit(0)

    def _is_valid_main(self, main_py):
        """检查 main.py 是否真正可用：存在、非空、且语法可编译（仅编译不执行，避免副作用）。

        这样即使 main.py 被意外清空/损坏（仅 isfile 为 True），也能被识别为无效，
        从而触发自动重新解压出厂包恢复，而不是带着坏文件去启动导致控制台起不来。
        """
        if not os.path.isfile(main_py):
            return False
        try:
            if os.path.getsize(main_py) == 0:
                return False
            import ast
            with open(main_py, "r", encoding="utf-8", errors="replace") as f:
                ast.parse(f.read())
            return True
        except Exception:
            return False

    def _ensure_main_program(self):
        """确保主程序存在：首次启动且 TOOLDELTA_DIR 为空（没有 main.py）时，
        自动从出厂包(TOOLDELTA_SOURCE_ZIP)解压初始主程序到 TOOLDELTA_DIR。
        返回 (ok, msg)。
        """
        app = self.app
        td_dir = app.config["TOOLDELTA_DIR"]
        main_py = app.config["TOOLDELTA_MAIN"]
        if self._is_valid_main(main_py):
            return True, "主程序已存在"
        zip_path = app.config.get("TOOLDELTA_SOURCE_ZIP")
        if not zip_path or not os.path.isfile(zip_path):
            return False, "出厂程序包不存在: " + (zip_path or "未配置")
        try:
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
            # 出厂包通常带统一顶层目录(如 ToolDelta-main/)，解压时剥离，
            # 确保 main.py 落在 TOOLDELTA_DIR 下，避免出现嵌套目录。
            top = ""
            if names and "/" in names[0]:
                top = names[0].split("/", 1)[0] + "/"
            os.makedirs(td_dir, exist_ok=True)
            abs_td_dir = os.path.abspath(td_dir)
            with zipfile.ZipFile(zip_path) as z:
                for info in z.infolist():
                    rel = info.filename[len(top):] if (top and info.filename.startswith(top)) else info.filename
                    if not rel:
                        continue
                    # zip slip 防护：拒绝绝对路径与 .. 遍历，校验解压落点在 td_dir 内。
                    # 虽然出厂包来自可信配置 TOOLDELTA_SOURCE_ZIP，但若出厂包被替换/篡改
                    # （如通过 backup_service.reset_to_factory 用伪造 zip 重置），可向任意
                    # 路径写文件实现 RCE。与 plugin_service.upload_plugin / backup_service.restore_backup 对齐。
                    if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
                        raise ValueError("出厂包包含非法路径: " + info.filename)
                    dest = os.path.normpath(os.path.join(td_dir, rel))
                    if dest != abs_td_dir and not dest.startswith(abs_td_dir + os.sep):
                        raise ValueError("出厂包路径越权: " + info.filename)
                    if info.filename.endswith("/"):
                        os.makedirs(dest, exist_ok=True)
                    else:
                        parent = os.path.dirname(dest)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                        with z.open(info) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
            if not self._is_valid_main(main_py):
                return False, "解压完成但 main.py 未生成或无效，请检查出厂包"
            return True, "已从出厂包解压主程序"
        except Exception as e:
            return False, f"解压出厂包失败: {e}"

    def _ensure_dependencies(self):
        """判断 ToolDelta 主程序依赖是否已就绪（就绪判定 + 必要的安装触发）。

        ToolDelta 是 poetry 工程(pyproject.toml)，自带 colorama/pyspeedtest/aiohttp/
        numpy/grpcio/protobuf/nbtlib/rich 等大量第三方依赖；而 Web 面板自身只安装了
        flask/flask-socketio/requests。若这些依赖缺失，main.py 一启动就 ModuleNotFoundError
        退出（表现为“点击启动起不来”，Linux 全新环境尤其常见）。

        这里只做“就绪判定 + 必要时触发后台安装”：已就绪返回 (True, msg)；
        未就绪则返回 (False, msg) 并由 start() 决定异步等待安装完成后再拉起，
        避免在请求线程里被安装耗时（全新环境装 grpcio 等可能数分钟）卡死导致启动被拒。
        """
        try:
            from app.dependency_service import dependency_service
            if dependency_service.is_ready():
                return True, "ToolDelta 依赖已就绪"
            dependency_service.start_install()  # 幂等：已在装/已装则无副作用
            return False, "检测到缺失运行依赖，正在后台安装，完成后将自动启动"
        except Exception as e:
            return False, "依赖检查异常: " + str(e)

    def start(self):
        with self._lock:
            if self.running:
                return True
            # _shutting_down 检查:SIGTERM/SIGINT 已触发后,API 层若再调 start() 会
            # 重新拉起子进程,与"优雅退出"意图相悖。这里拦截避免重启幽灵进程。
            if self._shutting_down:
                self._broadcast("system", "正在关闭，无法启动")
                return False
            if not getattr(self, "app", None):
                self._broadcast("system", "应用上下文未初始化，无法启动")
                return False
            main_py = self.app.config["TOOLDELTA_MAIN"]
            if not os.path.isfile(main_py):
                # 首次启动/初始化时 TOOLDELTA_DIR 可能为空，自动解压出厂包让主程序就绪
                ok, msg = self._ensure_main_program()
                if not ok:
                    # 不向广播拼接 main_py 绝对路径:泄露服务器目录结构
                    # 详细 msg 已在 _ensure_main_program 内部按需记录日志
                    self._broadcast("system", "找不到主程序,请查看日志")
                    return False
                self._broadcast("system", msg)
            # 启动前检查 ToolDelta 自身依赖是否已就绪
            dep_ok, dep_msg = self._ensure_dependencies()
            if dep_msg:
                self._broadcast("system", dep_msg)
            if dep_ok:
                return self._spawn()
            # 依赖未就绪：后台线程等待安装完成后自动拉起，避免请求线程被长耗时安装阻塞
            # （全新 Linux 环境装 17 个包含 grpcio 可能耗时数十秒~数分钟）
            self._broadcast("system", "依赖安装进行中，请稍候，完成后将自动启动 ToolDelta…")
            threading.Thread(target=self._start_after_deps, daemon=True).start()
            return True

    def _start_after_deps(self):
        """依赖未就绪时，在后台等待安装完成后再拉起主程序。"""
        # 整体 try/except：daemon 线程未捕获异常会静默退出，前端将持续显示
        # "依赖安装进行中，请稍候" 但永远不会触发启动，用户无从知晓失败原因。
        # 兜底捕获后广播失败原因，让用户能感知并手动重试。
        try:
            from app.dependency_service import dependency_service
            ok, msg = dependency_service.ensure_installed_blocking(timeout=600)
            if not ok:
                self._broadcast("system", "依赖安装失败，无法启动：" + msg)
                return
            with self._lock:
                if self.running:
                    return
                self._spawn()
        except Exception as e:
            try:
                from app.log_service import log_service
                log_service.error("_start_after_deps 异常: " + str(e), "TOOLDELTA")
            except Exception:
                pass
            try:
                self._broadcast("system", "后台启动失败: " + str(e))
            except Exception:
                pass

    def _spawn(self):
        """真正拉起 ToolDelta 子进程（依赖已就绪的前提下）。
        注意：不带任何额外参数启动，让 ToolDelta 保持原生交互模式，
        Web 控制台即作为终端模拟器，用户可完整操作所有菜单和提示。"""
        main_py = self.app.config["TOOLDELTA_MAIN"]
        td_dir = self.app.config["TOOLDELTA_DIR"]
        # 选择 Python 解释器：若 dependency_service 已找到兼容解释器（如 3.12）则用之，
        # 否则用当前进程 sys.executable。修复「面板 Python 3.14 不兼容 ToolDelta 时
        # 子进程立即 ModuleNotFoundError 崩溃、控制台显示未连接」的问题。
        python_bin = sys.executable
        try:
            from app.dependency_service import dependency_service as _ds
            if _ds and getattr(_ds, "_resolved_python", None):
                python_bin = _ds._resolved_python
        except Exception:
            pass
        try:
            startupinfo = None
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            # 真彩环境：确保子进程(rich/colorama)尽量输出 24-bit ANSI 真彩色（P2-6）
            env["COLORTERM"] = "truecolor"
            env["TERM"] = "xterm-256color"
            # 跨平台强制彩色：Web 侧子进程的 stdout 是管道/伪终端而非真实控制台，
            # 多数库(rich/colorama/click)据此(非 TTY)会关闭彩色。用 FORCE_COLOR /
            # CLICOLOR_FORCE 强制其输出 ANSI 转义，再由 Web 端 ansi_to_html 还原彩色 HTML。
            env["FORCE_COLOR"] = "1"
            env["CLICOLOR_FORCE"] = "1"
            # Windows 管道下 Python stdout 默认块缓冲，会导致控制台输出严重延迟/不刷新，
            # 强制无缓冲以保证实时性（Unix 走 pty 已是行缓冲，此项无害）。
            env["PYTHONUNBUFFERED"] = "1"
            # 每次启动重置解码探测状态与 pty 句柄
            self._encoding = "utf-8"
            self._enc_detected = False
            self.pty_master = None
            if os.name != "nt":
                # Linux/macOS: 用伪终端(pty)作为子进程的 stdout/stderr。
                # 这样主程序里 rich/colorama 检测到自己连着终端(stdout.isatty()==True),
                # 就会输出 ANSI 彩色转义码, 从而修复 Web 控制台"彩色字体丢失"的问题。
                # stdin 仍走 PIPE(不接 pty), 避免终端回显把输入又打回控制台。
                master, slave = pty.openpty()
                self.pty_master = master
                try:
                    self.process = subprocess.Popen(
                        [python_bin, main_py],
                        cwd=td_dir,
                        stdin=subprocess.PIPE,
                        stdout=slave,
                        stderr=slave,
                        startupinfo=startupinfo,
                        bufsize=0,
                        env=env,
                    )
                except Exception:
                    # Popen 失败时清理已打开的 pty fd,避免累积 fd 泄漏
                    os.close(slave)
                    os.close(master)
                    self.pty_master = None
                    raise
                os.close(slave)  # 父进程关闭 slave 副本, 子进程已 dup2
            else:
                # Windows 无 pty 模块, 回退 PIPE。自适配策略：Unix 用真实伪终端让子进程
                # 检测到 TTY 而输出 ANSI；Windows 无 pty，改为用 FORCE_COLOR/CLICOLOR_FORCE
                # 环境强制子进程在管道下仍输出 ANSI 真彩转义，Web 端再转成彩色 HTML。
                self.process = subprocess.Popen(
                    [python_bin, main_py],
                    cwd=td_dir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    startupinfo=startupinfo,
                    bufsize=0,
                    env=env,
                )
            self.running = True
            self._broadcast("system", "ToolDelta 进程已启动")
            self.output_thread = threading.Thread(target=self._read_output, daemon=True)
            self.output_thread.start()
            return True
        except Exception as e:
            # 异常详情记日志,广播用脱敏短消息,避免 str(e) 泄露绝对路径/权限错误形态
            # (FileNotFoundError 等异常消息含解释器/脚本绝对路径)
            try:
                from app.log_service import log_service as _ls
                _ls.error("ToolDelta 启动失败: " + str(e), "TOOLDELTA")
            except Exception:
                pass
            self._broadcast("system", "启动失败,请查看日志")
            return False

    def stop(self):
        proc = None
        pty = None
        with self._lock:
            if not self.running or not self.process:
                return True
            proc = self.process
            self.running = False
            self.process = None
            pty = self.pty_master
        # 在锁外等待进程退出，避免持锁阻塞其他调用（如 send_command）最长 5 秒（P2-4）
        try:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    # 显式 wait 回收僵尸进程,避免 PID/资源不释放
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                except Exception:
                    pass
            # 进程退出后再关闭 stdin，避免子进程因 stdin 提前关闭收到 SIGPIPE 崩溃
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        finally:
            if pty is not None:
                try:
                    os.close(pty)
                except Exception:
                    pass
            # 在锁内清空 pty_master,避免锁外赋 None 覆盖并发 start() 已设置的新 master
            with self._lock:
                # 仅当当前 pty_master 仍是本次 stop 持有的旧 master 时才清空
                if self.pty_master is pty:
                    self.pty_master = None
        self._broadcast("system", "ToolDelta 进程已停止")
        return True

    def restart(self):
        with self._lock:
            proc = self.process
        self.stop()
        # 等待旧进程完全退出（最多轮询 5 秒），避免旧进程占用文件/端口（P2-4）
        if proc:
            for _ in range(50):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            else:
                # 5 秒仍未退出，强制 kill 并短暂等待，确保释放资源（P2-8）
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
        return self.start()

    # 控制台命令长度上限：防止超大 payload 拖垮或阻塞 stdin（P2-2）
    MAX_COMMAND_LEN = 8192

    def send_command(self, cmd):
        if not isinstance(cmd, str):
            return False
        if len(cmd) > self.MAX_COMMAND_LEN:
            self._broadcast("system", "命令过长，已被忽略")
            return False
        # 关键：必须在锁外执行 stdin.write/flush，否则子进程不读 stdin 时管道缓冲区
        # 写满会无限阻塞 write()，此时锁被持有，stop()/get_status()/restart()/
        # 其他 send_command 全部卡死，整个面板无响应（与 scheduler_service._run_job
        # 之前的死锁问题同源）。改为：锁内只快照 process 与 running，锁外执行写。
        with self._lock:
            running = self.running
            proc = self.process
        if not running or not proc or not proc.stdin:
            return False
        try:
            proc.stdin.write((cmd + "\n").encode("utf-8", errors="replace"))
            proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            # 子进程已退出/管道关闭：标记 running=False 让前端感知
            with self._lock:
                if self.process is proc:
                    self.running = False
            return False

    def _read_output(self):
        # 在锁内一次性快照读取源与进程对象，避免 stop() 并发修改导致 TOCTOU 竞态
        with self._lock:
            pty_master = self.pty_master
            stdout = self.process.stdout if self.process else None
            proc = self.process
        if pty_master is None and stdout is None:
            self.running = False
            self._broadcast("system", "ToolDelta 进程已退出")
            return
        # 使用 select + 超时机制：当子进程输出不含换行的片段（如 "请选择: "）
        # 时，旧逻辑会阻塞在 os.read 等待更多数据，导致用户必须再输入一次才能看到
        # 残留输出。此处改为：有数据就读、按行 emit；150ms 内无新数据但有残留
        # 缓冲时，立即把不完整行 flush 出去，让 Web 端实时看到提示符/进度。
        import select as _select
        buf = b""
        fd = pty_master if pty_master is not None else (stdout.fileno() if stdout and hasattr(stdout, "fileno") else None)
        # Windows 的 select.select 仅支持 socket fd，对管道/pty fd 会抛
        # OSError(WinError 10038)，被 except 捕获后 break 会让 output 线程立即退出，
        # 导致子进程仍在运行但前端无任何输出且 running=False 让 send_command 失效。
        # Windows 下回退为 reader 线程 + queue 轮询：把阻塞读转移到独立线程，
        # 主循环按 150ms 超时 poll queue，无数据时 flush 残留缓冲，保证提示符实时显示。
        use_select = fd is not None and os.name != "nt"
        # Windows 路径专用：reader 线程把 stdout 阻塞读结果投递到 queue
        read_q = None
        reader_thread = None
        if not use_select and stdout is not None:
            import queue as _queue
            read_q = _queue.Queue()

            def _win_reader():
                # reader 线程：阻塞读 stdout，每读到一段就 put 到 queue。
                # 关键：用 read1 而非 read(n)——BufferedReader.read(n) 会阻塞直到读满 n 字节，
                # 而 read1 只做一次底层 raw read，有多少返回多少（仍会阻塞直到至少 1 字节可用）。
                # 这样 "请选择: " 这种无换行的片段也能立即被读到并投递到 queue。
                try:
                    while True:
                        try:
                            if hasattr(stdout, "read1"):
                                chunk = stdout.read1(4096)
                            else:
                                chunk = stdout.read(4096)
                        except (OSError, ValueError):
                            break
                        if not chunk:
                            break
                        read_q.put(chunk)
                except Exception:
                    pass
                finally:
                    # sentinel：通知主循环 stdout 已 EOF（子进程退出/关闭管道）
                    read_q.put(None)

            reader_thread = threading.Thread(target=_win_reader, daemon=True)
            reader_thread.start()
        while self.running and proc and proc.poll() is None:
            try:
                if use_select:
                    # 最多等待 150ms：有数据就立即读，超时则 flush 残留缓冲
                    ready, _, _ = _select.select([fd], [], [], 0.15)
                    if not ready:
                        # 超时且有残留不完整行：立即 flush，避免提示符卡住不显示
                        if buf:
                            self._emit_line(self._decode_line(buf))
                            buf = b""
                        continue
                    if pty_master is not None:
                        chunk = os.read(pty_master, 4096)
                    elif stdout is not None:
                        chunk = stdout.read(4096)
                    else:
                        break
                elif read_q is not None:
                    # Windows 路径：从 queue 拉取 reader 线程投递的 chunk，最多等 150ms
                    try:
                        item = read_q.get(timeout=0.15)
                    except _queue.Empty:
                        # 超时且有残留不完整行：立即 flush，避免提示符卡住不显示
                        if buf:
                            self._emit_line(self._decode_line(buf))
                            buf = b""
                        continue
                    if item is None:
                        # reader 线程退出 sentinel（stdout EOF）
                        break
                    chunk = item
                else:
                    # 无 fd 也无 stdout（不应发生）：直接退出避免空转
                    break
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                self._emit_line(self._decode_line(raw_line))
        if buf:
            self._emit_line(self._decode_line(buf))
        with self._lock:
            # 关键校验：self.process 必须仍是本线程当初监控的 proc 才能置 running=False。
            # 否则在 restart 场景下：stop() 把 self.process 置 None，旧 _read_output 线程
            # 在 emit 残留 buf（_broadcast 调 socketio.emit 可能耗时数十 ms）期间，
            # restart 的 start()/_spawn() 已设置 self.process=new_proc 并启动新线程。
            # 旧线程随后持锁把 running 置回 False 并广播"进程已退出"，但新进程其实还在运行，
            # 导致 send_command 等后续操作全部失效（与 #2 send_command 死锁同源问题模式）。
            if self.process is proc:
                self.running = False
                self._broadcast("system", "ToolDelta 进程已退出")

    def _decode_line(self, raw):
        # 先尝试 utf-8 实时显示；遇到非 utf-8 字节再检测编码，
        # 兼顾实时性与中文(gbk等)正确解码，避免控制台开头乱码
        if not self._enc_detected:
            try:
                return raw.decode("utf-8").rstrip("\r")
            except UnicodeDecodeError:
                self._encoding = detect_encoding(raw)
                self._enc_detected = True
                return raw.decode(self._encoding, errors="replace").rstrip("\r")
        return raw.decode(self._encoding, errors="replace").rstrip("\r")
    def _emit_line(self, line):
        cleaned = strip_ansi(line)
        self.output_raw_buffer.append(line)
        self.output_buffer.append(cleaned)
        if len(self.output_buffer) > self.MAX_BUFFER:
            self.output_buffer = self.output_buffer[-self.MAX_BUFFER:]
            self.output_raw_buffer = self.output_raw_buffer[-self.MAX_BUFFER:]
        # 控制台输出不再写日志文件:ToolDelta stdout 可能包含 fbtoken、登录密码、
        # 连接 token 等敏感字段,即使脱敏正则也无法覆盖所有格式。output_buffer 已缓存
        # 最近 MAX_BUFFER 行供 /api/tool/output 查询,无需再持久化到磁盘。
        self._broadcast("output", line)

    def get_status(self):
        with self._lock:
            alive = False
            if self.process:
                alive = self.process.poll() is None
            return {
                "running": self.running and alive,
                "pid": self.process.pid if self.process else None,
                "buffer_size": len(self.output_buffer),
            }

    def get_output(self, tail=200, as_html=False):
        if as_html:
            return [ansi_to_html(line) for line in self.output_raw_buffer[-tail:]]
        return self.output_raw_buffer[-tail:]

    def clear_listeners(self):
        with self._lock:
            self.listeners = []

    def add_listener(self, cb):
        with self._lock:
            if cb not in self.listeners:
                self.listeners.append(cb)

    def _broadcast(self, type_, data):
        with self._lock:
            listeners = list(self.listeners)
        for cb in listeners:
            try:
                cb(type_, data)
            except Exception as e:
                # 监听器异常不能静默吞掉，否则下游故障无从排查（P1-5）
                log_service.warn("output listener 抛出异常: " + str(e), "TOOLDELTA")

tooldelta_manager = ToolDeltaManager()
