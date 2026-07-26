import os
import json
import shutil
import zipfile
import tempfile
import threading
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
    # 恢复时单个 zip 内条目数上限,防止百万级条目 zip 拖垮 CPU
    MAX_RESTORE_ENTRIES = 100000

    # 需要备份的文件夹与配置文件（集中一处，避免 create_backup / restore 多处硬编码）
    _BACKUP_FOLDERS = ["插件文件", "插件配置文件", "插件数据文件"]
    _BACKUP_CFG = "ToolDelta基本配置.json"
    # 恢复白名单:仅允许这些顶层条目落地 td_dir,防止恶意备份包覆盖 main.py 等任意文件
    # 实现 RCE(详见 S1:旧实现直接遍历 zip 顶层条目复制,缺乏白名单校验)
    _RESTORE_WHITELIST = set(_BACKUP_FOLDERS) | {_BACKUP_CFG}

    def __init__(self):
        # 高危操作互斥锁:restore_backup / reset_to_factory / create_backup 都会
        # 操作 td_dir 下文件,两个管理员(或一个管理员 + 一个被钓鱼的 session)并发触发
        # 会出现一个 rmtree 一个 copytree 的混乱状态,导致 main.py 半写损坏。
        # 锁串行化这些操作,保证原子性。
        self._op_lock = threading.Lock()

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
        abs_td_dir = os.path.abspath(td_dir)
        for folder in self._BACKUP_FOLDERS:
            src = os.path.join(td_dir, folder)
            if not os.path.isdir(src):
                continue
            abs_src = os.path.abspath(src)
            for root, dirs, files in os.walk(src):
                # os.walk 默认 followlinks=False 不递归进入符号链接子目录，
                # 但 os.path.getsize / z.write 会跟随文件符号链接。
                # 若 td_dir 下存在指向 /etc/shadow 的符号链接，备份包会包含其内容。
                # 校验每个 fp 的 realpath 仍在 src 内，跳过越权符号链接。
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        real_fp = os.path.realpath(fp)
                        if real_fp != abs_src and not real_fp.startswith(abs_src + os.sep):
                            # 符号链接逃逸出 src 目录，跳过避免备份外部文件
                            continue
                    except OSError:
                        continue
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
        # 与 restore/reset 共享 _op_lock:备份期间不能有 restore/reset 改 td_dir 下文件,
        # 否则备份的快照可能含半写状态(restore 正在 copytree 某个目录时,create_backup
        # 的 _collect_backup_items walk 进去会读到不一致快照)
        with self._op_lock:
            td_dir = current_app.config["TOOLDELTA_DIR"]
            backup_dir = self.get_backup_dir()
            os.makedirs(backup_dir, exist_ok=True)
            # 备份目录权限收敛:备份 zip 含插件配置/数据文件,可能含插件凭据
            # (与 user.json / server_conn.json 收敛策略一致)
            try:
                os.chmod(backup_dir, 0o700)
            except OSError:
                pass
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
            # 备份产物权限收敛:含插件凭据,同主机其他用户不应读取
            try:
                os.chmod(zip_path, 0o600)
                os.chmod(metapath, 0o600)
            except OSError:
                pass
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
        # 与 create/reset 共享 _op_lock:两个并发 restore 会出现一个 rmtree 一个 copytree
        # 的混乱状态,导致 main.py 半写损坏
        with self._op_lock:
            return self._restore_backup_impl(zip_name)

    def _restore_backup_impl(self, zip_name):
        backup_dir = self.get_backup_dir()
        # 内部纵深防御:即便路由层已 _sanitize_label 校验,service 层也独立校验,
        # 防止未来新增调用方(调度任务/内部脚本)忘记校验导致 zip_name="../../../etc/x.zip"
        # 路径遍历。与 delete_backup 内部校验保持一致。
        safe = self._sanitize_label(zip_name.replace(".zip", ""))
        if not safe or safe + ".zip" != zip_name:
            return False, "备份文件名不合法"
        # 先停止 ToolDelta 进程，避免运行期覆盖文件导致主程序损坏（P1-3）
        # stop 失败不能继续覆盖文件，否则可能损坏运行中的主程序（P1-5）
        try:
            from app.tooldelta_manager import tooldelta_manager
            tooldelta_manager.stop()
        except Exception as e:
            log_service.error("停止 ToolDelta 失败，中止恢复: " + str(e), "BACKUP")
            return False, "停止运行中的 ToolDelta 失败，请先手动停止后重试"
        td_dir = current_app.config["TOOLDELTA_DIR"]
        abs_backup_dir = os.path.abspath(backup_dir)
        zip_path = os.path.join(backup_dir, zip_name)
        # 二次校验 zip_path 落在 backup_dir 内,防止 _sanitize_label 漏过边缘 case
        if not os.path.abspath(zip_path).startswith(abs_backup_dir + os.sep):
            return False, "备份文件路径越权"
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
            # _make_snapshot 失败时 snapshot_path 可能是部分写入的 zip 残留磁盘，
            # 必须在此显式清理，否则多次失败累积多个 __pre_restore_*.zip 文件
            try:
                if os.path.isfile(snapshot_path):
                    os.remove(snapshot_path)
            except OSError:
                pass
            return False, f"当前数据过大，无法创建恢复前快照: {e}"

        # 用 tempfile.mkdtemp 替代固定目录名 __restore_temp__：
        # 固定名在两个恢复操作并发时会互相覆盖——A 还在解压时 B 的 rmtree 会删掉
        # A 的中间产物。mkdtemp 每次生成唯一目录名，并发互不干扰。
        import tempfile
        temp = tempfile.mkdtemp(prefix="__restore_", dir=backup_dir)
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                # 条目数上限:防止百万级条目 zip 拖垮 CPU
                if len(z.infolist()) > self.MAX_RESTORE_ENTRIES:
                    return False, "备份包条目数过多"
                # 防御 zip slip：拒绝绝对路径、..、以及解压后超出 temp 的条目
                for info in z.infolist():
                    fn = info.filename
                    if os.path.isabs(fn) or ".." in fn.split("/"):
                        raise ValueError("备份包包含非法路径")
                    dest = os.path.normpath(os.path.join(temp, fn))
                    if dest != temp and not dest.startswith(temp + os.sep):
                        raise ValueError("备份包路径越权")
                z.extractall(temp)
            # 白名单校验(S1 关键修复):仅允许 _BACKUP_FOLDERS + _BACKUP_CFG 中的顶层条目
            # 落地 td_dir,防止恶意备份包覆盖 main.py 等任意文件实现 RCE。
            # 旧实现直接遍历 zip 顶层条目复制,缺乏白名单校验:构造一个顶层放 main.py
            # 的 zip 即可在恢复后让 /api/tool/start 启动攻击者代码以面板权限执行。
            # 与 delete_backup 内部白名单校验逻辑一致,纵深防御。
            for item in os.listdir(temp):
                if item not in self._RESTORE_WHITELIST:
                    log_service.error(
                        f"恢复中止:备份包含未授权顶层条目 {log_service.sanitize_for_log(item)}", "BACKUP"
                    )
                    return False, "备份包含未授权条目,可能为恶意备份包"
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
            # 异常详情记日志,对客户端只返回通用消息,避免 str(e) 泄露服务器绝对路径/
            # 临时文件路径/权限错误形态(与 routes/api.py:_internal_error 一致策略)
            log_service.error("恢复失败: " + str(e), "BACKUP")
            # 恢复失败，用快照回滚到恢复前状态
            # 回滚本身也可能失败：必须明确告知调用方数据可能损坏，不能宣称"已回滚"（P1-5）
            rollback_ok = False
            try:
                abs_td_dir = os.path.abspath(td_dir)
                # 回滚前必须先清空 _BACKUP_FOLDERS 中对应的目录：
                # 否则恢复过程中已部分写入的新文件/目录会与快照中的旧文件混合，
                # 形成"旧快照 + 部分新文件"的脏数据，ToolDelta 启动后行为不可预期
                for folder in self._BACKUP_FOLDERS:
                    dst = os.path.join(abs_td_dir, folder)
                    if os.path.isdir(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                with zipfile.ZipFile(snapshot_path, "r") as z:
                    # 回滚解压同样需要 zip slip 防护，避免快照被篡改后越权写文件
                    for member in z.namelist():
                        member_path = os.path.normpath(os.path.join(abs_td_dir, member))
                        if not member_path.startswith(abs_td_dir + os.sep) and member_path != abs_td_dir:
                            raise ValueError("zip slip detected: " + member)
                    z.extractall(abs_td_dir)
                rollback_ok = True
            except Exception as rollback_ex:
                log_service.error("恢复回滚失败: " + str(rollback_ex), "BACKUP")
            if rollback_ok:
                return False, "恢复失败,已回滚至恢复前状态,请查看日志"
            return False, "恢复失败且回滚也失败,数据可能损坏,请检查文件系统"
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
        # 与 restore/create 共享 _op_lock:避免并发清空与解压的混乱状态
        with self._op_lock:
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
                # 异常详情记日志,对客户端只返回通用消息,避免 str(e) 泄露 zip_path 绝对路径
                log_service.error("出厂程序包读取失败: " + str(e), "BACKUP")
                return False, "出厂程序包读取失败,请查看日志"

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
            abs_td_dir = os.path.abspath(td_dir)
            try:
                with zipfile.ZipFile(zip_path) as z:
                    for info in z.infolist():
                        rel = info.filename[len(top):] if top and info.filename.startswith(top) else info.filename
                        if not rel:
                            continue
                        # zip slip 防护：与 restore_backup 回滚分支、plugin_service.upload_plugin 对齐。
                        # 出厂包虽来自可信配置，但 defense-in-depth：若出厂包被替换/篡改，
                        # 无此校验可向任意路径写文件实现 RCE。
                        if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
                            log_service.error("出厂包包含非法路径: " + info.filename, "BACKUP")
                            return False, "出厂包包含非法路径,请查看日志"
                        dest = os.path.normpath(os.path.join(td_dir, rel))
                        if dest != abs_td_dir and not dest.startswith(abs_td_dir + os.sep):
                            log_service.error("出厂包路径越权: " + info.filename, "BACKUP")
                            return False, "出厂包路径越权,请查看日志"
                        if info.filename.endswith("/"):
                            os.makedirs(dest, exist_ok=True)
                        else:
                            parent = os.path.dirname(dest)
                            if parent:
                                os.makedirs(parent, exist_ok=True)
                            with z.open(info) as src, open(dest, "wb") as dst:
                                shutil.copyfileobj(src, dst)
            except Exception as e:
                log_service.error("出厂包解压失败: " + str(e), "BACKUP")
                return False, "出厂包解压失败,请查看日志"

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
