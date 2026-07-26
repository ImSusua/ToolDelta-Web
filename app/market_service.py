import os
import json
import threading
from flask import current_app

class MarketService:
    # 单个 datas.json 读取上限，防止异常大文件拖垮扫描（P2-2）
    MAX_DATAS_JSON_SIZE = 2 * 1024 * 1024

    def __init__(self):
        self._plugins = None
        self._packages = None
        self._id_map = None
        self._scan_mtime = None  # 上次 scan 时的 market 目录 mtime
        # 竞态防御:旧实现 scan/get_plugins/get_packages/get_plugin_data/search
        # 均无锁,两个并发请求同时进入 scan() 会:
        # ① 线程 A 执行 self._plugins = [] 后,线程 B 调 get_plugins() 看到 _plugins
        #   是空列表(非 None)直接返回 [],用户看到空市场;
        # ② 两个线程同时 append 造成列表内容混乱/重复/丢失;
        # ③ _id_map 与 _plugins 不一致,get_plugin_data 返回错误条目影响安装逻辑。
        # 用统一 _lock 保护所有读写 _plugins/_packages/_id_map/_scan_mtime 的方法。
        self._lock = threading.Lock()

    def get_market_dir(self):
        return current_app.config["PLUGIN_MARKET_DIR"]

    def scan(self):
        with self._lock:
            mdir = self.get_market_dir()
            # mtime 缓存：目录未变化则跳过扫描，避免每次请求全量扫描 + 解析 JSON
            try:
                cur_mtime = os.path.getmtime(mdir)
            except Exception:
                cur_mtime = 0
            if self._scan_mtime is not None and self._scan_mtime == cur_mtime and self._plugins is not None:
                return
            self._plugins = []
            self._packages = []
            self._id_map = {}
            if not os.path.isdir(mdir):
                # 显式指定 mode:与 scheduler_service(0o700)/auth_service(0o600) 风格一致,
                # 默认 makedirs mode=0o777(受 umask 通常 0o755)会让同主机其他用户可遍历。
                # market 目录含 datas.json(作者信息/插件描述),收敛为 0o750 限制访问。
                os.makedirs(mdir, exist_ok=True, mode=0o750)
                self._scan_mtime = cur_mtime
                return
            for d in sorted(os.listdir(mdir)):
                full = os.path.join(mdir, d)
                if not os.path.isdir(full):
                    continue
                datapath = os.path.join(full, "datas.json")
                if not os.path.isfile(datapath):
                    continue
                try:
                    # 读取时限制字节数消除 getsize+open 的 TOCTOU:
                    # 旧实现先 os.path.getsize(datapath) 校验大小,再 open+json.load。
                    # 两步之间若 market 目录被外部进程(同步进程/攻击者)用大文件替换
                    # datas.json,json.load 会读取超过 MAX_DATAS_JSON_SIZE 的数据。
                    # 改为一次 read(MAX+1),既校验大小又避免二次 open。
                    with open(datapath, "rb") as fb:
                        raw = fb.read(self.MAX_DATAS_JSON_SIZE + 1)
                    if len(raw) > self.MAX_DATAS_JSON_SIZE:
                        continue
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, OSError, IOError):
                    continue
                if "plugin-ids" in data:
                    self._packages.append({
                        "name": d,
                        "display_name": d.replace("[pkg]", ""),
                        "author": data.get("author", "?"),
                        "version": data.get("version", "0.0.0"),
                        "description": data.get("description", ""),
                        "plugin_ids": data.get("plugin-ids", []),
                        "is_package": True,
                    })
                elif data.get("plugin-id") or data.get("plugin-type"):
                    pid = data.get("plugin-id", d)
                    # 修复:旧实现可疑三元 `os.path.isfile(os.path.isfile(...) if False else ...)`
                    # 虽功能等价正确(if False 使内层 os.path.isfile 死代码),
                    # 但易误读且若误改为 True 会抛 TypeError。简化为清晰表达式。
                    has_readme = (os.path.isfile(os.path.join(full, "readme.md"))
                                  or os.path.isfile(os.path.join(full, "readme.txt")))
                    self._plugins.append({
                        "id": pid,
                        "name": d,
                        "author": data.get("author", "?"),
                        "version": data.get("version", "0.0.0"),
                        "description": data.get("description", ""),
                        "plugin_type": data.get("plugin-type", "classic"),
                        "pre_plugins": data.get("pre-plugins", {}),
                        "has_readme": has_readme,
                        # 不回传绝对路径(full):前端可见会泄露服务器目录结构,
                        # 有助攻击者侦察。仅回传目录名,
                        # install_preset_plugin 通过 market_dir + name 重建绝对路径。
                        "dir": d,
                    })
                    self._id_map[pid] = d
            self._scan_mtime = cur_mtime

    def get_plugins(self, refresh=False):
        with self._lock:
            if refresh:
                self._scan_mtime = None  # 强制重新扫描
            need_scan = self._plugins is None
        if need_scan or refresh:
            self.scan()
        with self._lock:
            return list(self._plugins or [])

    def get_packages(self, refresh=False):
        with self._lock:
            if refresh:
                self._scan_mtime = None  # 强制重新扫描
            need_scan = self._packages is None
        if need_scan or refresh:
            self.scan()
        with self._lock:
            return list(self._packages or [])

    def get_plugin_data(self, plugin_id, refresh=False):
        plugins = self.get_plugins(refresh)
        for p in plugins:
            if p["id"] == plugin_id:
                return p
        return None

    def search(self, keyword, by="name"):
        plugins = self.get_plugins()
        kw = keyword.lower()
        if by == "name":
            return [p for p in plugins if kw in p["name"].lower() or kw in p["id"].lower()]
        elif by == "author":
            return [p for p in plugins if kw in p["author"].lower()]
        return plugins

market_service = MarketService()
