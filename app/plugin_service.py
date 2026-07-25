import os
import json
import shutil
import zipfile
import socket
import tempfile
import threading
from urllib.parse import urlparse
from werkzeug.utils import secure_filename
from flask import current_app
from app.market_service import market_service

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
        temp_dir = os.path.join(pdir, "__upload_temp__")
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)
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
            if os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir)

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
        file.save(path)
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
        file.save(path)
        return True

    def install_preset_plugin(self, plugin_id):
        plugin_data = market_service.get_plugin_data(plugin_id, refresh=True)
        if not plugin_data:
            return False, "插件不存在"
        src_dir = plugin_data.get("dir")
        plugin_name = plugin_data.get("name")
        if self._safe_name(plugin_name) is None:
            return False, "插件名不合法"
        if not src_dir or not os.path.isdir(src_dir):
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
            return False, f"复制插件失败: {e}"
        return True, plugin_name

    def install_preset_plugins_batch(self, plugin_ids):
        results = []
        for pid in plugin_ids:
            ok, msg = self.install_preset_plugin(pid)
            results.append({"id": pid, "success": ok, "message": msg})
        return results

    def install_network_plugin(self, market_url, plugin_id):
        try:
            import requests
            import ipaddress
            base = market_url.rstrip("/")
            # SSRF 防护：仅允许 http/https 协议的市场源（P1-5）
            parsed = urlparse(base)
            if parsed.scheme not in ("http", "https"):
                return False, "不支持的市场源协议"
            host = parsed.hostname or ""
            if not host:
                return False, "市场源地址不合法"
            # 拒绝内网/回环/链路本地/保留地址,防止 SSRF 探测内网或读取云元数据
            try:
                ips = socket.getaddrinfo(host, None)
            except socket.gaierror:
                return False, "无法解析市场源域名"
            for family, _, _, _, sockaddr in ips:
                ip_str = sockaddr[0]
                try:
                    ip = ipaddress.ip_address(ip_str)
                except ValueError:
                    continue
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                    return False, "不允许访问内网或本地地址"
            pmap = requests.get(f"{base}/plugin_ids_map.json", timeout=10).json()
            if plugin_id not in pmap:
                return False, "插件 ID 不在该市场源中"
            plugin_name = secure_filename(pmap[plugin_id])
            if not plugin_name:
                return False, "插件名不合法"
            tree = requests.get(f"{base}/directory_tree.json", timeout=10).json()
            ftree = tree.get(plugin_name)
            if not ftree:
                return False, "无法获取插件文件列表"
            pdir = self.get_classic_plugin_path()
            target = os.path.join(pdir, plugin_name)
            os.makedirs(target, exist_ok=True)
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
                local = os.path.join(target, rel)
                if not os.path.abspath(local).startswith(os.path.abspath(target) + os.sep):
                    continue
                os.makedirs(os.path.dirname(local), exist_ok=True)
                # 流式下载 + 分块校验，避免大文件一次性载入内存（P2-2）
                with requests.get(url, timeout=30, stream=True) as resp:
                    resp.raise_for_status()
                    try:
                        cl = int(resp.headers.get("content-length", 0))
                    except (TypeError, ValueError):
                        cl = 0
                    if cl > MAX_FILE_SIZE:
                        return False, f"文件过大: {filepath}"
                    file_total = 0
                    chunks = []
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        file_total += len(chunk)
                        total_downloaded += len(chunk)
                        if file_total > MAX_FILE_SIZE:
                            return False, f"文件过大: {filepath}"
                        if total_downloaded > MAX_TOTAL_SIZE:
                            return False, "插件包总大小超过上限"
                        chunks.append(chunk)
                    with open(local, "wb") as f:
                        for chunk in chunks:
                            f.write(chunk)
            return True, plugin_name
        except Exception as e:
            return False, str(e)

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
