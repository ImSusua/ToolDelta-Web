import os
import json
from flask import current_app

class MarketService:
    # 单个 datas.json 读取上限，防止异常大文件拖垮扫描（P2-2）
    MAX_DATAS_JSON_SIZE = 2 * 1024 * 1024

    def __init__(self):
        self._plugins = None
        self._packages = None
        self._id_map = None
        self._scan_mtime = None  # 上次 scan 时的 market 目录 mtime

    def get_market_dir(self):
        return current_app.config["PLUGIN_MARKET_DIR"]

    def scan(self):
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
            os.makedirs(mdir, exist_ok=True)
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
                if os.path.getsize(datapath) > self.MAX_DATAS_JSON_SIZE:
                    continue
                with open(datapath, "r", encoding="utf-8") as f:
                    data = json.load(f)
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
                has_readme = os.path.isfile(os.path.join(full, "readme.md")) or os.path.isfile(os.path.join(full, "readme.txt"))
                self._plugins.append({
                    "id": pid,
                    "name": d,
                    "author": data.get("author", "?"),
                    "version": data.get("version", "0.0.0"),
                    "description": data.get("description", ""),
                    "plugin_type": data.get("plugin-type", "classic"),
                    "pre_plugins": data.get("pre-plugins", {}),
                    "has_readme": has_readme,
                    "dir": full,
                })
                self._id_map[pid] = d
        self._scan_mtime = cur_mtime

    def get_plugins(self, refresh=False):
        if refresh:
            self._scan_mtime = None  # 强制重新扫描
        if self._plugins is None:
            self.scan()
        return self._plugins or []

    def get_packages(self, refresh=False):
        if refresh:
            self._scan_mtime = None  # 强制重新扫描
        if self._packages is None:
            self.scan()
        return self._packages or []

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
