import os
import json
import inspect
from tooldelta import Plugin, plugin_entry

class WebPanelBridge(Plugin):
    name = "WebPanelBridge"
    author = "WebPanel"
    version = (1, 0, 0)

    def __init__(self, frame):
        super().__init__(frame)
        self._command_registry = {}
        self._original_register = frame.cmd_manager.add_console_cmd_trigger
        frame.cmd_manager.add_console_cmd_trigger = self._tracked_register
        self._bridge_data_path = os.path.join(self.data_path, "commands_registry.json")
        self._load_registry()
        self.ListenFrameExit(lambda _: self._save_registry())

    def _tracked_register(self, triggers, arg_hint, usage, func):
        plugin_name = self._find_caller_plugin()
        for trigger in triggers:
            self._command_registry[trigger] = {
                "plugin": plugin_name,
                "usage": usage,
                "hint": arg_hint,
            }
        self._save_registry()
        self._original_register(triggers, arg_hint, usage, func)

    def _find_caller_plugin(self):
        for frame_info in inspect.stack():
            locals_dict = frame_info.frame.f_locals
            if "self" in locals_dict:
                obj = locals_dict["self"]
                if hasattr(obj, "name") and hasattr(obj, "frame"):
                    return obj.name
        return "未知"

    def _load_registry(self):
        if os.path.isfile(self._bridge_data_path):
            with open(self._bridge_data_path, "r", encoding="utf-8") as f:
                self._command_registry = json.load(f)

    def _save_registry(self):
        # 原子写 + 权限收敛(与 auth_service / connection_service / scheduler_service 一致):
        # 旧实现 open(path, "w") 默认 mode 0o644,且非原子写 — 进程崩溃时
        # commands_registry.json 可能被截断/损坏,导致下次启动 _load_registry 的
        # json.load 抛 JSONDecodeError 阻断插件加载。改用 tempfile.mkstemp(默认 0o600)
        # + os.replace 原子替换,与全局 makedirs(0o700) 策略对齐。
        import tempfile
        d = os.path.dirname(self._bridge_data_path)
        os.makedirs(d, exist_ok=True, mode=0o700)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".registry_", dir=d)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._command_registry, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._bridge_data_path)
            tmp = None
        finally:
            if tmp is not None and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

entry = plugin_entry(WebPanelBridge)
