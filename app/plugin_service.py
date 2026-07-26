import os
import json
import shutil
import zipfile
import socket
import tempfile
import threading
from urllib.parse import urlparse, urlsplit, urlunsplit
from werkzeug.utils import secure_filename
from flask import current_app
from app.market_service import market_service

# SSRF DNS rebinding 防护:在 getaddrinfo 校验后到 requests 实际连接之间,
# DNS 记录可能被攻击者切换到 169.254.169.254 等内网地址。
# 通过自定义 HTTPAdapter 把请求 URL 中的 host 改写为已校验的 IP,
# 让 urllib3 直接连接到 IP,跳过其内部的 DNS 解析,彻底阻断 rebinding 窗口。
# 对 HTTPS 关闭证书校验(cert 是域名,IP 直连时不匹配),已校验 IP 非内网故可接受。
import requests
from requests.adapters import HTTPAdapter

from app.log_service import log_service

try:
    # 抑制关闭 verify 后的 InsecureRequestWarning
    from urllib3.exceptions import InsecureRequestWarning
    import warnings as _warnings
    _warnings.simplefilter('ignore', InsecureRequestWarning)
except Exception:
    pass


class _SSRFSafeAdapter(HTTPAdapter):
    """把对指定 host 的请求改写为直连已校验的 IP,并保留原 host 的 Host 头。
    仅对 self._host 匹配的请求生效,其他 host 保持原样(不应用 pinning)。"""

    def __init__(self, host, ip, *args, **kwargs):
        self._host = host
        self._ip = ip
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        parts = urlsplit(request.url)
        if parts.hostname == self._host:
            netloc = self._ip
            if parts.port:
                netloc += f":{parts.port}"
            new_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
            request.url = new_url
            # 保留原 host 用于虚拟主机(同一 IP 多站点场景)
            request.headers["Host"] = self._host
            # HTTPS 证书是域名签发,IP 直连时 SNI=IP 不匹配会校验失败
            # 此时已校验 IP 非内网,降级关闭 verify 可接受
            if parts.scheme == "https":
                kwargs["verify"] = False
        return super().send(request, **kwargs)


def _build_ssrf_safe_session(host, ip):
    """构造一个把指定 host 的请求固定连接到 ip 的 requests.Session。
    用于 install_network_plugin:DNS 一次性解析校验后,所有后续请求复用同一 IP,
    避免每次 requests.get 都重新走 DNS,缩小 rebinding 窗口。"""
    s = requests.Session()
    adapter = _SSRFSafeAdapter(host, ip)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def resolve_safe_session(url):
    """统一 SSRF 防护入口:解析 URL、校验协议、解析 DNS、拒绝内网/回环地址,
    并构造把 host 固定连接到已校验 IP 的 requests.Session。

    返回 (session, None) 成功;或 (None, error_msg) 失败。
    供 market_connect(api.py)与 install_network_plugin(plugin_service)共用,
    避免两处校验逻辑分叉导致一方漏掉 IP pinning 触发 DNS rebinding。
    """
    import ipaddress
    if not isinstance(url, str) or len(url) > 2048:
        return None, "URL 不合法"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None, "仅支持 http/https 协议"
    host = parsed.hostname or ""
    if not host:
        return None, "URL 主机名不合法"
    try:
        ips = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None, "无法解析域名"
    safe_ip = None
    for family, _, _, _, sockaddr in ips:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        # is_private 仅覆盖 10/172.16-31/192.168,必须额外拦截:
        # - is_loopback: 127.0.0.0/8
        # - is_link_local: 169.254.0.0/16(云元数据 169.254.169.254)
        # - is_reserved: 0.0.0.0/8、240.0.0.0/4
        # - is_multicast: 224.0.0.0/4
        # - is_unspecified: 0.0.0.0
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return None, "不允许访问内网或本地地址"
        # 取第一个有效公网 IP 用于 IP 直连
        if safe_ip is None:
            # IPv6 需要用 [] 包裹
            safe_ip = f"[{ip_str}]" if ip.version == 6 else ip_str
    if safe_ip is None:
        return None, "无法解析到有效 IP"
    return _build_ssrf_safe_session(host, safe_ip), None


class PluginService:
    def __init__(self):
        self._cache = None  # (mtime, plugins_list) for list_plugins
        self._cfg_lock = threading.Lock()

    def get_classic_plugin_path(self):
        return current_app.config["TOOLDELTA_CLASSIC_PLUGIN_PATH"]

    def get_cfg_path(self):
        return current_app.config["TOOLDELTA_PLUGIN_CFG_DIR"]

    def get_data_path(self):
        return current_app.config["TOOLDELTA_PLUGIN_DATA_DIR"]

    @staticmethod
    def _safe_name(name):
        """插件名白名单校验：禁止路径分隔符/.. 防止路径遍历。
        返回安全名或 None（不合法）。"""
        if not name or not isinstance(name, str):
            return None
        if "/" in name or "\\" in name or ".." in name or os.path.isabs(name):
            return None
        s = secure_filename(name)
        # secure_filename 对中文等会返回空，因此仅在它非空且与原名差异大时拒绝
        if not s:
            return None
        return s

    def list_plugins(self):
        plugins = []
        pdir = self.get_classic_plugin_path()
        # mtime 缓存：目录未变化则直接返回缓存，避免 dashboard 5 秒轮询全量扫描 + 解析 JSON
        try:
            cur_mtime = os.path.getmtime(pdir)
        except Exception:
            cur_mtime = 0
        if self._cache is not None and self._cache[0] == cur_mtime:
            return self._cache[1]
        if not os.path.isdir(pdir):
            os.makedirs(pdir, exist_ok=True)
            self._cache = (cur_mtime, plugins)
            return plugins
        for d in sorted(os.listdir(pdir)):
            full = os.path.join(pdir, d)
            if not os.path.isdir(full):
                continue
            is_disabled = d.endswith("+disabled")
            name = d.replace("+disabled", "")
            datas = {}
            datapath = os.path.join(full, "datas.json")
            if os.path.isfile(datapath):
                try:
                    with open(datapath, "r", encoding="utf-8") as f:
                        datas = json.load(f)
                except (json.JSONDecodeError, OSError, IOError):
                    datas = {}
            plugins.append({
                "name": name,
                "dir_name": d,
                "is_enabled": not is_disabled,
                "author": datas.get("author", "?"),
                "version": datas.get("version", "0.0.0"),
                "description": datas.get("description", ""),
                "plugin_id": datas.get("plugin-id", name),
                "plugin_type": datas.get("plugin-type", "classic"),
                "has_readme": os.path.isfile(os.path.join(full, "readme.md")) or os.path.isfile(os.path.join(full, "readme.txt")),
                "has_config": os.path.isfile(os.path.join(self.get_cfg_path(), f"{name}.json")),
            })
        self._cache = (cur_mtime, plugins)
        return plugins

    def toggle_plugin(self, name, enable):
        if self._safe_name(name) is None:
            return False
        pdir = self.get_classic_plugin_path()
        enabled_dir = os.path.join(pdir, name)
        disabled_dir = os.path.join(pdir, name + "+disabled")
        if enable:
            if os.path.isdir(disabled_dir):
                os.rename(disabled_dir, enabled_dir)
                return True
        else:
            if os.path.isdir(enabled_dir):
                os.rename(enabled_dir, disabled_dir)
                return True
        return False

    def delete_plugin(self, name):
        if self._safe_name(name) is None:
            return False
        pdir = self.get_classic_plugin_path()
        for d in [name, name + "+disabled"]:
            full = os.path.join(pdir, d)
            if os.path.isdir(full):
                shutil.rmtree(full)
                return True
        return False

    # 插件包解压上限：防止 zip 炸弹/超大包拖垮（P2-2）
    MAX_PLUGIN_ZIP_SIZE = 50 * 1024 * 1024
    MAX_PLUGIN_EXTRACT_FILES = 10000
    MAX_PLUGIN_EXTRACT_SIZE = 100 * 1024 * 1024

    def upload_plugin(self, zip_file, name=None):
        pdir = self.get_classic_plugin_path()
        os.makedirs(pdir, exist_ok=True)
        # 用 tempfile.mkdtemp 而非固定目录名 "__upload_temp__"：
        # 固定目录在并发上传时会互相覆盖——A 还在解压时 B 的 shutil.rmtree 会删掉
        # A 的中间产物，或 A 的 finally 把 B 刚写入的目录清空，导致插件损坏或上传失败。
        # mkdtemp 每次生成唯一目录名（随机后缀），并发互不干扰。
        # 放在 pdir 下而非系统 /tmp：保证与 target 同文件系统，shutil.move 走快速 rename。
        temp_dir = tempfile.mkdtemp(prefix="__upload_", dir=pdir)
        try:
            with zipfile.ZipFile(zip_file, "r") as z:
                # 先校验 zip 包内文件：禁止绝对路径、路径遍历、过大总大小与文件数
                total_size = 0
                file_count = 0
                for info in z.infolist():
                    fn = info.filename
                    if os.path.isabs(fn) or ".." in fn.split("/"):
                        raise ValueError("压缩包包含非法路径")
                    if info.file_size > self.MAX_PLUGIN_ZIP_SIZE:
                        raise ValueError("压缩包内单个文件过大")
                    total_size += info.file_size
                    file_count += 1
                    if file_count > self.MAX_PLUGIN_EXTRACT_FILES:
                        raise ValueError("压缩包内文件数过多")
                    if total_size > self.MAX_PLUGIN_EXTRACT_SIZE:
                        raise ValueError("压缩包解压后总大小过大")
                z.extractall(temp_dir)
            items = os.listdir(temp_dir)
            if not items:
                raise ValueError("压缩包为空")

            # 情况1：单个顶层目录（含 __init__.py）
            if len(items) == 1 and os.path.isdir(os.path.join(temp_dir, items[0])):
                plugin_root = os.path.join(temp_dir, items[0])
                if not os.path.isfile(os.path.join(plugin_root, "__init__.py")):
                    raise ValueError("压缩包中未找到有效的插件结构（缺少 __init__.py）")
                plugin_name = items[0]
            # 情况2：扁平结构（__init__.py 直接在压缩包根）
            elif os.path.isfile(os.path.join(temp_dir, "__init__.py")):
                datas = {}
                dpath = os.path.join(temp_dir, "datas.json")
                if os.path.isfile(dpath):
                    try:
                        with open(dpath, "r", encoding="utf-8") as f:
                            datas = json.load(f)
                    except Exception:
                        pass
                plugin_name = name or datas.get("plugin-id") or datas.get("name") or "plugin"
                plugin_root = temp_dir
                if self._safe_name(plugin_name) is None:
                    return False, "插件名不合法"
            else:
                raise ValueError("压缩包中未找到有效的插件结构（缺少 __init__.py）")

            # 清理可能同名的启用/禁用目录，避免两者共存导致状态混乱
            for suffix in ("", "+disabled"):
                existing = os.path.join(pdir, plugin_name + suffix)
                if os.path.exists(existing):
                    shutil.rmtree(existing)

            target = os.path.join(pdir, plugin_name)
            if not os.path.abspath(target).startswith(os.path.abspath(pdir) + os.sep):
                return False, "插件名不合法"
            if plugin_root == temp_dir:
                # 扁平结构：先建目录，再把内容移入
                os.makedirs(target, exist_ok=True)
                for item in os.listdir(temp_dir):
                    shutil.move(os.path.join(temp_dir, item), target)
            else:
                shutil.move(plugin_root, target)
            return True
        finally:
            # 无论成功失败都清理临时目录；mkdtemp 目录名唯一，不会误删其他并发上传的目录
            shutil.rmtree(temp_dir, ignore_errors=True)

    def get_plugin_readme(self, name):
        if self._safe_name(name) is None:
            return None
        pdir = self.get_classic_plugin_path()
        for d in [name, name + "+disabled"]:
            full = os.path.join(pdir, d)
            for fn in ["readme.md", "README.md", "readme.txt"]:
                fp = os.path.join(full, fn)
                if os.path.isfile(fp):
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        return {"content": f.read(), "format": "md" if fn.endswith(".md") else "txt"}
        return None

    def get_plugin_config(self, name):
        if self._safe_name(name) is None:
            return None
        cfg_path = os.path.join(self.get_cfg_path(), f"{name}.json")
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def save_plugin_config(self, name, data):
        if self._safe_name(name) is None:
            return False
        cfg_dir = self.get_cfg_path()
        os.makedirs(cfg_dir, exist_ok=True)
        cfg_path = os.path.join(cfg_dir, f"{name}.json")
        # 原子写：临时文件 + os.replace，避免并发写损坏（与 auth_service 一致）
        with self._cfg_lock:
            fd, tmp = tempfile.mkstemp(prefix=f"{name}.", suffix=".tmp", dir=cfg_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, cfg_path)
            except Exception:
                try: os.remove(tmp)
                except OSError: pass
                raise
        return True

    def get_plugin_data_files(self, name):
        if self._safe_name(name) is None:
            return []
        data_dir = os.path.join(self.get_data_path(), name)
        if not os.path.isdir(data_dir):
            return []
        # 兜底：确保 data_dir 在 get_data_path() 下，避免 .. 逃逸
        if not os.path.abspath(data_dir).startswith(os.path.abspath(self.get_data_path()) + os.sep):
            return []
        result = []
        for root, dirs, files in os.walk(data_dir):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), data_dir)
                result.append(rel)
        return result

    def upload_data_file(self, name, file):
        if self._safe_name(name) is None:
            return False
        data_dir = os.path.join(self.get_data_path(), name)
        os.makedirs(data_dir, exist_ok=True)
        fname = secure_filename(file.filename)
        if not fname:
            return False
        path = os.path.join(data_dir, fname)
        # 兜底校验落点在 data_dir 内，防止文件名遍历写越权（P0-2）
        if not os.path.abspath(path).startswith(os.path.abspath(data_dir) + os.sep):
            return False
        # 文件大小校验：与 routes/files.py upload_file 一致 50MB 上限，
        # content_length 可能不可靠，保存后再兜底校验
        if file.content_length and file.content_length > self.MAX_PLUGIN_ZIP_SIZE:
            return False
        file.save(path)
        if os.path.getsize(path) > self.MAX_PLUGIN_ZIP_SIZE:
            try:
                os.remove(path)
            except OSError:
                pass
            return False
        return True

    def delete_data_file(self, name, filename):
        if self._safe_name(name) is None:
            return False
        data_dir = os.path.join(self.get_data_path(), name)
        fname = secure_filename(filename)
        if not fname:
            return False
        path = os.path.join(data_dir, fname)
        # 净化 + 前缀校验，防止删除任意文件（P0-4）
        if not os.path.abspath(path).startswith(os.path.abspath(data_dir) + os.sep):
            return False
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False

    def upload_config_file(self, name, file):
        if self._safe_name(name) is None:
            return False
        cfg_dir = self.get_cfg_path()
        os.makedirs(cfg_dir, exist_ok=True)
        fname = secure_filename(file.filename)
        if not fname:
            return False
        path = os.path.join(cfg_dir, fname)
        # 兜底校验落点在 cfg_dir 内，防止文件名遍历写越权（P0-3）
        if not os.path.abspath(path).startswith(os.path.abspath(cfg_dir) + os.sep):
            return False
        # 文件大小校验：与 upload_data_file 一致
        if file.content_length and file.content_length > self.MAX_PLUGIN_ZIP_SIZE:
            return False
        file.save(path)
        if os.path.getsize(path) > self.MAX_PLUGIN_ZIP_SIZE:
            try:
                os.remove(path)
            except OSError:
                pass
            return False
        return True

    def install_preset_plugin(self, plugin_id):
        plugin_data = market_service.get_plugin_data(plugin_id, refresh=True)
        if not plugin_data:
            return False, "插件不存在"
        plugin_name = plugin_data.get("name")
        if self._safe_name(plugin_name) is None:
            return False, "插件名不合法"
        # 关键修复:src_dir 必须拼接到 market_dir 之下,而不是直接当相对路径用。
        # market_service.scan 故意只回传目录名(注释 market_service.py:70-72 说明
        # install_preset_plugin 会重建绝对路径),但旧实现直接用裸目录名,
        # os.path.isdir(src_dir) 与 shutil.copytree(src_dir, target) 都按 CWD 解析,
        # Flask 进程 CWD 通常是项目根而非 plugin_market:
        #   - 功能上几乎必然失败(os.path.isdir 返回 False)
        #   - 安全上若 CWD 恰好存在同名目录(如其他接口能向 CWD 写文件),
        #     install_preset 会把那个不可信目录当作预设插件复制为插件,
        #     随后 ToolDelta 启动加载其中 __init__.py 触发 RCE。
        src_dir_name = plugin_data.get("dir")
        if not src_dir_name:
            return False, "插件源目录不存在，请刷新市场后重试"
        market_dir = market_service.get_market_dir()
        src_dir = os.path.join(market_dir, src_dir_name)
        # 纵深防御:realpath 校验防止 market_dir 下存在符号链接逃逸到外部目录
        abs_market = os.path.realpath(market_dir)
        real_src = os.path.realpath(src_dir)
        if not (real_src == abs_market or real_src.startswith(abs_market + os.sep)):
            return False, "插件源目录越权"
        if not os.path.isdir(src_dir):
            return False, "插件源目录不存在，请刷新市场后重试"
        pdir = self.get_classic_plugin_path()
        target = os.path.join(pdir, plugin_name)
        if not os.path.abspath(target).startswith(os.path.abspath(pdir) + os.sep):
            return False, "插件名不合法"
        disabled_target = os.path.join(pdir, plugin_name + "+disabled")
        if os.path.exists(target) or os.path.exists(disabled_target):
            return False, f"插件已存在: {plugin_name}"
        try:
            shutil.copytree(src_dir, target)
        except Exception as e:
            # 异常详情记日志,对客户端只返回通用消息,避免 str(e) 泄露服务器绝对路径/
            # 权限错误形态等(与 routes/api.py:_internal_error 一致策略)
            try:
                log_service.error(f"安装预设插件失败 src={src_dir} target={target}: {e}", "PLUGIN")
            except Exception:
                pass
            return False, "复制插件失败,请查看日志"
        return True, plugin_name

    def install_preset_plugins_batch(self, plugin_ids):
        results = []
        for pid in plugin_ids:
            ok, msg = self.install_preset_plugin(pid)
            results.append({"id": pid, "success": ok, "message": msg})
        return results

    def install_network_plugin(self, market_url, plugin_id):
        # 临时暂存目录:所有文件先下载到 staging,全部成功后才 rename 到 target。
        # 旧实现直接 os.makedirs(target) 后逐文件下载,中途失败(超限/网络中断/
        # 市场源返回重定向)时 target 残留半写文件,list_plugins 会把它当作有效插件,
        # 攻击者可先下发恶意 __init__.py 再让第 2 个文件触发失败,残留的恶意
        # __init__.py 在下次 ToolDelta 启动时被执行实现 RCE。
        staging = None
        try:
            base = market_url.rstrip("/")
            # SSRF 防护:复用统一入口,避免与 market_connect 逻辑分叉
            session, err = resolve_safe_session(base)
            if session is None:
                return False, err or "市场源地址不合法"
            # allow_redirects=False：禁止自动跟随 3xx 跳转。
            # SSRF 校验只对原始 host 做了内网地址拦截，若放行跳转，恶意市场源可
            # 用 302 → http://169.254.169.254/... 把请求导向云元数据等内网资源，
            # 绕过上面的 is_private 拦截。遇到 3xx 直接判定为不安全（P1-5）
            resp_map = session.get(f"{base}/plugin_ids_map.json", timeout=10, allow_redirects=False)
            if resp_map.is_redirect or resp_map.is_permanent_redirect:
                return False, "市场源返回重定向，疑似不安全"
            pmap = resp_map.json()
            if plugin_id not in pmap:
                return False, "插件 ID 不在该市场源中"
            plugin_name = secure_filename(pmap[plugin_id])
            if not plugin_name:
                return False, "插件名不合法"
            resp_tree = session.get(f"{base}/directory_tree.json", timeout=10, allow_redirects=False)
            if resp_tree.is_redirect or resp_tree.is_permanent_redirect:
                return False, "市场源返回重定向，疑似不安全"
            tree = resp_tree.json()
            ftree = tree.get(plugin_name)
            if not ftree:
                return False, "无法获取插件文件列表"
            pdir = self.get_classic_plugin_path()
            target = os.path.join(pdir, plugin_name)
            disabled_target = os.path.join(pdir, plugin_name + "+disabled")
            if os.path.exists(target) or os.path.exists(disabled_target):
                return False, f"插件已存在: {plugin_name}"
            # 临时暂存目录:创建在 pdir 下(同文件系统,os.replace/shutil.move 才能原子)
            staging = tempfile.mkdtemp(prefix="__net_install_", dir=pdir)
            files_to_download = []
            self._unfold_dict(ftree, plugin_name, files_to_download)
            # 网络插件包整体上限：防止文件数/总大小异常拖垮磁盘（P2-2）
            MAX_FILE_SIZE = 10 * 1024 * 1024
            MAX_TOTAL_SIZE = 100 * 1024 * 1024
            MAX_NETWORK_FILES = 10000
            if len(files_to_download) > MAX_NETWORK_FILES:
                return False, f"插件文件数超过上限 ({MAX_NETWORK_FILES})"
            total_downloaded = 0
            for filepath in files_to_download:
                # 净化每个文件路径，禁止 "../" 等越权写（P1-5）
                rel = os.path.normpath(filepath).lstrip("/\\")
                if os.path.isabs(filepath) or ".." in rel.split(os.sep):
                    continue
                url = f"{base}/{plugin_name}/{filepath}"
                local = os.path.join(staging, rel)
                # 路径越权校验:相对 staging 目录
                if not os.path.abspath(local).startswith(os.path.abspath(staging) + os.sep):
                    continue
                os.makedirs(os.path.dirname(local), exist_ok=True)
                # 流式下载 + 分块写盘,避免大文件一次性载入内存（P2-2）
                # allow_redirects=False 同上：禁止 3xx 跳转绕过 SSRF 拦截（P1-5）
                with session.get(url, timeout=30, stream=True, allow_redirects=False) as resp:
                    if resp.is_redirect or resp.is_permanent_redirect:
                        return False, f"下载 {filepath} 时市场源返回重定向，疑似不安全"
                    resp.raise_for_status()
                    try:
                        cl = int(resp.headers.get("content-length", 0))
                    except (TypeError, ValueError):
                        cl = 0
                    if cl > MAX_FILE_SIZE:
                        return False, f"文件过大: {filepath}"
                    file_total = 0
                    with open(local, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            file_total += len(chunk)
                            total_downloaded += len(chunk)
                            if file_total > MAX_FILE_SIZE:
                                return False, f"文件过大: {filepath}"
                            if total_downloaded > MAX_TOTAL_SIZE:
                                return False, "插件包总大小超过上限"
                            f.write(chunk)
            # 全部下载成功:原子移动 staging → target
            # os.replace 对目录要求源/目标在同一文件系统(pdir 下,满足)
            os.replace(staging, target)
            staging = None
            return True, plugin_name
        except Exception as e:
            # 异常详情记日志,对客户端只返回通用消息,避免 str(e) 泄露内部网络环境/
            # DNS 错误形态/服务器路径(与 routes/api.py:_internal_error 一致策略)
            try:
                log_service.error(f"网络安装插件失败 url={market_url} pid={plugin_id}: {e}", "PLUGIN")
            except Exception:
                pass
            return False, "安装失败,请查看日志"
        finally:
            # 失败回滚:清理 staging 目录,避免半写插件残留触发 RCE
            if staging and os.path.exists(staging):
                try:
                    shutil.rmtree(staging, ignore_errors=True)
                except Exception:
                    pass

    def _unfold_dict(self, d, prefix, result):
        """把 directory_tree.json 的嵌套文件树展开为相对路径列表。

        注意：叶子节点必须返回完整相对路径（prefix + k），否则嵌套目录中的
        文件会被下载到插件根目录，造成文件结构错误。
        """
        for k, v in d.items():
            path = f"{prefix}/{k}"
            if isinstance(v, dict):
                self._unfold_dict(v, path, result)
            else:
                result.append(path)

plugin_service = PluginService()
