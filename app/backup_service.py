import os
import json
import shutil
import zipfile
from datetime import datetime
from flask import current_app
from app.log_service import log_service


def _parse_time(s):
    try:
        return datetime.strptime(s, "%Y%m%d_%H%M%S")
    except Exception:
        return datetime.min


class BackupService:
    # 备份资源上限：防止打包超大目录拖垮服务（P2-2）
    MAX_BACKUP_SIZE = 1024 * 1024 * 1024  # 1 GB
    MAX_BACKUP_FILES = 50000

    # 需要备份的文件夹与配置文件（集中一处，避免 create_backup / restore 多处硬编码）
    _BACKUP_FOLDERS = ["插件文件", "插件配置文件", "插件数据文件"]
    _BACKUP_CFG = "ToolDelta基本配置.json"

    def __init__(self):
        pass

    def get_backup_dir(self):
        return current_app.config["BACKUP_DIR"]

    @staticmethod
    def _sanitize_label(label):
        """备份名只允许字母、数字、下划线、连字符和点，防止路径遍历或非法文件名。"""
        if not label:
            return None
        import re
        label = label.strip().replace(" ", "_")
        label = re.sub(r"[^A-Za-z0-9_\-\.]", "", label)
        label = label.strip(".")
        if not label or label in (".", ".."):
            return None
        return label[:80]

    def _collect_backup_items(self, td_dir):
        """收集需要备份的文件列表，同时校验总大小与文件数上限。

        返回 (file_list, total_size)，其中 file_list 元素为 (abs_path, arcname)。
        由 create_backup 与 restore 前的快照共用，避免逻辑分叉（P2-8）。
        """
        total_size = 0
        file_count = 0
        items = []
        for folder in self._BACKUP_FOLDERS:
            src = os.path.join(td_dir, folder)
            if not os.path.isdir(src):
                continue
            for root, dirs, files in os.walk(src):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        total_size += os.path.getsize(fp)
                    except OSError:
                        pass
                    file_count += 1
                    if total_size > self.MAX_BACKUP_SIZE:
                        raise ValueError("待备份数据超过 1GB 上限")
                    if file_count > self.MAX_BACKUP_FILES:
                        raise ValueError("待备份文件数超过上限")
                    items.append((fp, os.path.relpath(fp, td_dir)))
        cfg_file = os.path.join(td_dir, self._BACKUP_CFG)
        if os.path.isfile(cfg_file):
            try:
                total_size += os.path.getsize(cfg_file)
            except OSError:
                pass
            file_count += 1
            if total_size > self.MAX_BACKUP_SIZE:
                raise ValueError("待备份数据超过 1GB 上限")
            if file_count > self.MAX_BACKUP_FILES:
                raise ValueError("待备份文件数超过上限")
            items.append((cfg_file, self._BACKUP_CFG))
        return items, total_size

    def create_backup(self, name=None):
        td_dir = current_app.config["TOOLDELTA_DIR"]
        backup_dir = self.get_backup_dir()
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        label = self._sanitize_label(name) or f"backup_{ts}"
        zip_name = f"{label}.zip"
        zip_path = os.path.join(backup_dir, zip_name)

        items, _ = self._collect_backup_items(td_dir)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for fp, arcname in items:
                z.write(fp, arcname)
        meta = {
            "name": label,
            "time": ts,
            "zip": zip_name,
            "size": os.path.getsize(zip_path),
        }
        metapath = os.path.join(backup_dir, f"{label}.meta.json")
        with open(metapath, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        return meta

    def list_backups(self):
        backup_dir = self.get_backup_dir()
        if not os.path.isdir(backup_dir):
            return []
        backups = []
        seen = set()
        for fn in os.listdir(backup_dir):
            if fn.startswith("__"):
                continue
            if fn.endswith(".meta.json"):
                metapath = os.path.join(backup_dir, fn)
                try:
                    with open(metapath, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if isinstance(meta, dict) and meta.get("zip"):
                        backups.append(meta)
                        seen.add(meta["zip"])
                except (json.JSONDecodeError, OSError, IOError):
                    continue
            elif fn.endswith(".zip") and fn not in seen:
                zip_path = os.path.join(backup_dir, fn)
                try:
                    size = os.path.getsize(zip_path)
                except OSError:
                    size = 0
                backups.append({
                    "name": fn.replace(".zip", ""),
                    "time": fn.replace("backup_", "").replace(".zip", ""),
                    "zip": fn,
                    "size": size,
                })
        backups.sort(key=lambda x: _parse_time(x.get("time", "")), reverse=True)
        return backups

    def restore_backup(self, zip_name):
        backup_dir = self.get_backup_dir()
        # 先停止 ToolDelta 进程，避免运行期覆盖文件导致主程序损坏（P1-3）
        # stop 失败不能继续覆盖文件，否则可能损坏运行中的主程序（P1-5）
        try:
            from app.tooldelta_manager import tooldelta_manager
            tooldelta_manager.stop()
        except Exception as e:
            log_service.error("停止 ToolDelta 失败，中止恢复: " + str(e), "BACKUP")
            return False, "停止运行中的 ToolDelta 失败，请先手动停止后重试"
        td_dir = current_app.config["TOOLDELTA_DIR"]
        zip_path = os.path.join(backup_dir, zip_name)
        if not os.path.isfile(zip_path):
            return False, "备份文件不存在"

        # 恢复前先对当前状态做快照，便于失败回滚
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_path = os.path.join(backup_dir, f"__pre_restore_{ts}.zip")

        def _make_snapshot():
            items, _ = self._collect_backup_items(td_dir)
            with zipfile.ZipFile(snapshot_path, "w", zipfile.ZIP_DEFLATED) as z:
                for fp, arcname in items:
                    z.write(fp, arcname)

        try:
            _make_snapshot()
        except ValueError as e:
            return False, f"当前数据过大，无法创建恢复前快照: {e}"

        temp = os.path.join(backup_dir, "__restore_temp__")
        if os.path.isdir(temp):
            shutil.rmtree(temp)
        os.makedirs(temp, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                # 防御 zip slip：拒绝绝对路径、..、以及解压后超出 temp 的条目
                for info in z.infolist():
                    fn = info.filename
                    if os.path.isabs(fn) or ".." in fn.split("/"):
                        raise ValueError("备份包包含非法路径")
                    dest = os.path.normpath(os.path.join(temp, fn))
                    if dest != temp and not dest.startswith(temp + os.sep):
                        raise ValueError("备份包路径越权")
                z.extractall(temp)
            for item in os.listdir(temp):
                src = os.path.join(temp, item)
                dst = os.path.join(td_dir, item)
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                elif os.path.isfile(dst):
                    os.remove(dst)
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            return True, "恢复成功"
        except Exception as e:
            # 恢复失败，用快照回滚到恢复前状态
            # 回滚本身也可能失败：必须明确告知调用方数据可能损坏，不能宣称"已回滚"（P1-5）
            rollback_ok = False
            rollback_err = None
            try:
                abs_td_dir = os.path.abspath(td_dir)
                with zipfile.ZipFile(snapshot_path, "r") as z:
                    # 回滚解压同样需要 zip slip 防护，避免快照被篡改后越权写文件
                    for member in z.namelist():
                        member_path = os.path.normpath(os.path.join(abs_td_dir, member))
                        if not member_path.startswith(abs_td_dir + os.sep) and member_path != abs_td_dir:
                            raise ValueError("zip slip detected: " + member)
                    z.extractall(abs_td_dir)
                rollback_ok = True
            except Exception as rollback_ex:
                rollback_err = rollback_ex
                log_service.error("恢复回滚失败: " + str(rollback_ex), "BACKUP")
            if rollback_ok:
                return False, f"恢复失败，已回滚至恢复前状态: {e}"
            return False, "恢复失败且回滚也失败，数据可能损坏，请检查文件系统（原始错误: " + str(e) + "）"
        finally:
            if os.path.isdir(temp):
                shutil.rmtree(temp)
            if os.path.isfile(snapshot_path):
                os.remove(snapshot_path)

    def reset_to_factory(self):
        """重置 ToolDelta 到出厂状态：主程序与用户数据一并重置。

        流程：先清空整个 TOOLDELTA_DIR（删除原有主程序及用户插件/配置/数据），
        再解压出厂包（ToolDelta-main.zip）恢复主程序。
        - 出厂包顶层若有统一目录（如 ToolDelta-main/），解压时自动剥离，
          确保 main.py 落在 TOOLDELTA_DIR 下，而不会出现 TOOLDELTA_DIR/ToolDelta-main/ 的嵌套。
        """
        td_dir = current_app.config["TOOLDELTA_DIR"]
        # 先停止 ToolDelta 进程，避免运行期删文件破坏数据（P1-3）
        # stop 失败不能继续清空目录，否则可能损坏运行中的主程序（P1-5）
        try:
            from app.tooldelta_manager import tooldelta_manager
            tooldelta_manager.stop()
        except Exception as e:
            log_service.error("停止 ToolDelta 失败，中止重置: " + str(e), "BACKUP")
            return False, "停止运行中的 ToolDelta 失败，请先手动停止后重试"
        zip_path = current_app.config.get("TOOLDELTA_SOURCE_ZIP")
        if not zip_path or not os.path.isfile(zip_path):
            return False, "出厂程序包不存在，无法进行重置"

        # 读取 zip 条目，确定顶层目录前缀（如 "ToolDelta-main/"）
        try:
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
        except Exception as e:
            return False, f"出厂程序包读取失败: {e}"

        top = ""
        if names and "/" in names[0]:
            top = names[0].split("/", 1)[0] + "/"

        # 1) 清空整个 TOOLDELTA_DIR（主程序与用户数据一并重置为出厂状态）
        if os.path.isdir(td_dir):
            for entry in os.listdir(td_dir):
                p = os.path.join(td_dir, entry)
                try:
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                except OSError:
                    pass
        os.makedirs(td_dir, exist_ok=True)

        # 2) 解压出厂包到 TOOLDELTA_DIR（去除顶层目录前缀）
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                rel = info.filename[len(top):] if top and info.filename.startswith(top) else info.filename
                if not rel:
                    continue
                dest = os.path.join(td_dir, rel)
                if info.filename.endswith("/"):
                    os.makedirs(dest, exist_ok=True)
                else:
                    parent = os.path.dirname(dest)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with z.open(info) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)

        main_py = os.path.join(td_dir, "main.py")
        if not os.path.isfile(main_py):
            return False, "重置完成但 main.py 未生成，请检查出厂包"
        return True, "已恢复出厂（主程序与用户数据已重置）"

    def delete_backup(self, zip_name):
        backup_dir = self.get_backup_dir()
        # 校验文件名格式，防止通过 .. 等删除任意文件（P1-1）
        safe = self._sanitize_label(zip_name.replace(".zip", ""))
        if not safe or safe + ".zip" != zip_name:
            return False
        zip_path = os.path.join(backup_dir, zip_name)
        meta_path = os.path.join(backup_dir, zip_name.replace(".zip", ".meta.json"))
        for p in [zip_path, meta_path]:
            if os.path.isfile(p) and os.path.abspath(p).startswith(os.path.abspath(backup_dir) + os.sep):
                os.remove(p)
        return True

backup_service = BackupService()
