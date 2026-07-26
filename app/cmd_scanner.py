import os
import ast
import json
from flask import current_app

class CommandScanner:
    # __init__.py 文件大小上限:与 market_service.MAX_DATAS_JSON_SIZE 一致,
    # 防止恶意/损坏插件放置超大 __init__.py 拖垮 ast.parse 内存与 CPU
    MAX_INIT_PY_SIZE = 2 * 1024 * 1024

    def __init__(self):
        # mtime 缓存：(plugin_dir_mtime, bridge_registry_mtime, scan_result)
        self._scan_cache = None

    def scan_plugin(self, plugin_dir, plugin_name):
        init_py = os.path.join(plugin_dir, "__init__.py")
        if not os.path.isfile(init_py):
            return []
        # 文件大小校验:消除 getsize + open 的 TOCTOU 同时防资源耗尽
        # (与 market_service.scan 的 datas.json 上限策略一致)
        try:
            if os.path.getsize(init_py) > self.MAX_INIT_PY_SIZE:
                return []
        except OSError:
            return []
        with open(init_py, "r", encoding="utf-8", errors="replace") as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                return []
        commands = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_console_cmd_trigger"):
                continue
            args = node.args
            if len(args) < 3:
                continue
            try:
                triggers = self._safe_literal_eval(args[0])
                if not isinstance(triggers, list):
                    continue
                hint = self._safe_literal_eval(args[1]) if len(args) > 1 else None
                usage = self._safe_literal_eval(args[2]) if len(args) > 2 else ""
                commands.append({
                    "triggers": triggers,
                    "hint": hint if isinstance(hint, str) else None,
                    "usage": usage if isinstance(usage, str) else "",
                })
            except Exception:
                continue
        return commands

    def scan_all_plugins(self):
        pdir = current_app.config["TOOLDELTA_CLASSIC_PLUGIN_PATH"]
        # mtime 缓存：插件目录与 bridge registry 文件均未变化时直接返回缓存结果，
        # 避免每次请求都 AST 解析所有插件源码
        try:
            pdir_mtime = os.path.getmtime(pdir)
        except Exception:
            pdir_mtime = 0
        registry_path = self._bridge_registry_path()
        try:
            registry_mtime = os.path.getmtime(registry_path) if registry_path else 0
        except Exception:
            registry_mtime = 0
        cache_key = (pdir_mtime, registry_mtime)
        if self._scan_cache is not None and self._scan_cache[0] == cache_key:
            return self._scan_cache[1]

        result = []
        if os.path.isdir(pdir):
            for d in sorted(os.listdir(pdir)):
                full = os.path.join(pdir, d)
                if not os.path.isdir(full):
                    continue
                name = d.replace("+disabled", "")
                commands = self.scan_plugin(full, name)
                if commands:
                    result.append({
                        "plugin": name,
                        "is_enabled": not d.endswith("+disabled"),
                        "commands": commands,
                        "count": len(commands),
                    })
        # 合并 WebPanelBridge 运行时注册的命令：静态 AST 扫描无法覆盖
        # 运行时才 add_console_cmd_trigger 的动态命令，bridge 会在 ToolDelta
        # 端把它们记录到插件数据目录的 commands_registry.json，这里并入，
        # 使命令库 = 静态扫描 + 运行时注册，供命令页/控制台补全共用。
        registry = self._load_bridge_registry()
        if registry:
            by_plugin = {e["plugin"]: e for e in result}
            for trigger, info in registry.items():
                if not isinstance(info, dict):
                    continue
                pname = info.get("plugin") or "未知"
                entry = by_plugin.get(pname)
                if not entry:
                    entry = {"plugin": pname, "is_enabled": True, "commands": [], "count": 0}
                    result.append(entry)
                    by_plugin[pname] = entry
                if any(trigger in c["triggers"] for c in entry["commands"]):
                    continue
                entry["commands"].append({
                    "triggers": [trigger],
                    "hint": info.get("hint") if isinstance(info.get("hint"), str) else None,
                    "usage": info.get("usage", "") if isinstance(info.get("usage"), str) else "",
                })
            for e in result:
                e["count"] = len(e["commands"])
        self._scan_cache = (cache_key, result)
        return result

    def _bridge_registry_path(self):
        """返回 WebPanelBridge 命令注册表路径（不存在配置时返回 None，容错）。"""
        try:
            base = current_app.config.get("TOOLDELTA_PLUGIN_DATA_DIR")
            if not base:
                return None
            return os.path.join(base, "WebPanelBridge", "commands_registry.json")
        except Exception:
            return None

    def _load_bridge_registry(self):
        """读取 WebPanelBridge 运行时记录到插件数据目录的命令注册表(容错)。"""
        try:
            path = self._bridge_registry_path()
            if not path or not os.path.isfile(path):
                return {}
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data or {}
        except Exception:
            return {}

    def scan_by_plugin(self, plugin_name):
        # 路径遍历防护：plugin_name 直接来自 request.args（routes/api.py commands_by_plugin），
        # 若含 "../" 可逃逸出 TOOLDELTA_CLASSIC_PLUGIN_PATH，扫描到插件目录外的文件。
        # 这里取 basename 并校验规范化后的路径仍在 pdir 内，杜绝跨目录访问。
        if not isinstance(plugin_name, str) or not plugin_name:
            return None
        # 拒绝 NUL 与其他控制字符:os.path.basename/os.path.join 不会拒绝 NUL,
        # 但 os.path.realpath 在 CPython 3 中会对嵌入 NUL 抛 ValueError,
        # 该调用未包裹 try/except 会导致路由抛 500(轻微 DoS)。
        # 同时拦截其他控制字符(<0x20),合法插件名不应包含这些字符。
        if any(ord(c) < 32 for c in plugin_name):
            return None
        # 拒绝路径分隔符与危险字符：合法插件名只含字母/数字/下划线/连字符/中文
        safe_name = os.path.basename(plugin_name.replace("\\", "/"))
        if safe_name != plugin_name or "/" in plugin_name or "\\" in plugin_name:
            return None
        # 二次校验：规范化后路径必须仍在 pdir 内（防止符号链接/.. 等绕过 basename）
        pdir = os.path.realpath(current_app.config["TOOLDELTA_CLASSIC_PLUGIN_PATH"])
        for d in [safe_name, safe_name + "+disabled"]:
            full = os.path.realpath(os.path.join(pdir, d))
            # 前缀校验：full 必须以 pdir + os.sep 开头，确保未逃逸
            if not full.startswith(pdir + os.sep) and full != pdir:
                continue
            if os.path.isdir(full):
                commands = self.scan_plugin(full, safe_name)
                return {
                    "plugin": safe_name,
                    "is_enabled": not d.endswith("+disabled"),
                    "commands": commands,
                    "count": len(commands),
                }
        return None

    def _safe_literal_eval(self, node):
        try:
            return ast.literal_eval(node)
        except Exception:
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.List):
                return [self._safe_literal_eval(e) for e in node.elts]
            raise

cmd_scanner = CommandScanner()
