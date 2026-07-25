import os
import re
import shutil
from flask import Blueprint, request, jsonify, send_file, session, after_this_request
from werkzeug.utils import secure_filename
from app.log_service import log_service
from config import Config

bp = Blueprint("files", __name__, url_prefix="/api/files")

ALLOWED_ROOT = os.path.normpath(Config.TOOLDELTA_DIR)

# 资源防护上限：防止目录打包/搜索耗尽内存或磁盘（P2-2）
MAX_SEARCH_DEPTH = 10           # 文件搜索最大递归深度
MAX_DOWNLOAD_DIR_SIZE = 500 * 1024 * 1024  # 目录打包上限 500 MB
MAX_UPLOAD_SIZE = 50 * 1024 * 1024         # 单次上传上限 50 MB
MAX_DOWNLOAD_FILES = 10000                 # 目录打包最大文件数，防止 inode 耗尽

# 危险文件名/路径字符：禁止控制字符、路径分隔符、空名等
_INVALID_NAME_RE = re.compile(r'[\x00-\x1f\\/:*?"<>|]')

# 文件列表返回上限：防止超大目录一次性返回过多条目造成前端/带宽压力（P2-2）
MAX_LIST_ENTRIES = 5000

# 目录打包最大递归深度：与搜索保持一致，防止极深目录树阻塞（P2-2）
MAX_DOWNLOAD_DIR_DEPTH = 10

# 禁止上传的高危可执行扩展名：避免通过文件管理器直接写入二进制/脚本木马。
# 注意：.py/.sh 等是 ToolDelta 插件合法文件，不在拦截范围内。
_UPLOAD_BLOCKED_EXTS = {
    ".exe", ".bat", ".cmd", ".com", ".msi", ".scr",
    ".dll", ".sys", ".vbs", ".js", ".wsf", ".ps1",
    ".php", ".jsp", ".asp", ".aspx", ".jar", ".class",
}

def audit(action, detail=""):
    user = session.get("username", "?")
    ip = request.remote_addr or "?"
    log_service.info(f"[{user}@{ip}] {action} {detail}", "AUDIT")

def ok(data=None):
    r = {"success": True}
    if data is not None:
        r["data"] = data
    return jsonify(r)

def fail(msg):
    return jsonify({"success": False, "error": msg})

def _admin_required():
    """文件管理所有写操作均要求管理员，避免普通用户(role=1)任意写删 ToolDelta 目录文件。"""
    if session.get("role") != 10:
        return fail("无权限")
    return None

def safe_path(path_str):
    if not path_str:
        return ALLOWED_ROOT
    # 先归一化，再校验「必须等于根目录或以根目录 + 路径分隔符开头」，
    # 避免 ../../ToolDeltaX 这类同级兄弟目录越权访问（P1-1）。
    full = os.path.normpath(os.path.join(ALLOWED_ROOT, path_str.lstrip("/\\")))
    if full != ALLOWED_ROOT and not full.startswith(ALLOWED_ROOT + os.sep):
        return None
    return full


def _safe_name(name):
    """净化用户输入的单级文件名/目录名，返回 None 表示不合法。"""
    if not name or not isinstance(name, str):
        return None
    name = name.strip()
    if name in (".", "..") or _INVALID_NAME_RE.search(name):
        return None
    # 去除首尾空格与点，避免 Windows 隐藏文件或尾部空格问题
    name = name.strip(". ")
    if not name:
        return None
    return name


def _is_real_path(path):
    """检查 path 不是指向根目录外的符号链接（防止目录遍历/越权读取）。"""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return real == ALLOWED_ROOT or real.startswith(ALLOWED_ROOT + os.sep)

@bp.route("/list")
def list_files():
    raw = request.args.get("path", "")
    full = safe_path(raw)
    if not full:
        return fail("路径不允许")
    if not os.path.exists(full):
        return fail("路径不存在")
    if os.path.isfile(full):
        return fail("路径不是目录")
    try:
        entries = []
        names = sorted(os.listdir(full), key=lambda x: (not os.path.isdir(os.path.join(full, x)), x.lower()))
        truncated = len(names) > MAX_LIST_ENTRIES
        for name in names[:MAX_LIST_ENTRIES]:
            path = os.path.join(full, name)
            rel = os.path.relpath(path, ALLOWED_ROOT)
            stat = os.stat(path)
            is_dir = os.path.isdir(path)
            entries.append({
                "name": name,
                "path": rel,
                "is_dir": is_dir,
                "size": 0 if is_dir else stat.st_size,
                "mtime": stat.st_mtime,
                "ext": os.path.splitext(name)[1].lower() if not is_dir else "",
            })
        return ok({
            "entries": entries,
            "current": os.path.relpath(full, ALLOWED_ROOT),
            "root": ALLOWED_ROOT,
            "truncated": truncated,
            "total": len(names),
        })
    except Exception as e:
        return fail(str(e))

@bp.route("/read")
def read_file():
    raw = request.args.get("path", "")
    full = safe_path(raw)
    if not full:
        return fail("路径不允许")
    if not os.path.isfile(full):
        return fail("不是文件")
    if not _is_real_path(full):
        return fail("符号链接目标不在允许范围内")
    try:
        size = os.path.getsize(full)
        if size > 5 * 1024 * 1024:
            return fail("文件超过5MB，无法在线查看")
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        audit("读取文件", f"路径={raw}")
        return ok({"content": content, "path": os.path.relpath(full, ALLOWED_ROOT), "size": size})
    except Exception as e:
        return fail(str(e))

@bp.route("/save", methods=["POST"])
def save_file():
    err = _admin_required()
    if err: return err
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip()
    content = data.get("content") or ""
    full = safe_path(raw)
    if not full:
        return fail("路径不允许")
    if full != ALLOWED_ROOT and not full.startswith(ALLOWED_ROOT + os.sep):
        return fail("路径不允许")
    # 限制单文件保存大小，防止内存/磁盘被拖垮（P2-2）
    if len(content) > 10 * 1024 * 1024:
        return fail("文件内容超过 10MB 上限")
    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        audit("保存文件", f"路径={raw}")
        return ok()
    except Exception as e:
        return fail(str(e))

@bp.route("/upload", methods=["POST"])
def upload_file():
    err = _admin_required()
    if err: return err
    raw = request.form.get("path", "")
    full = safe_path(raw)
    if not full:
        return fail("路径不允许")
    if not _is_real_path(full):
        return fail("目标目录包含越权符号链接")
    if "file" not in request.files:
        return fail("未上传文件")
    f = request.files["file"]
    if not f.filename:
        return fail("文件名为空")
    # 文件大小校验：避免超大文件拖垮服务（P2-2）
    if f.content_length and f.content_length > MAX_UPLOAD_SIZE:
        return fail("文件超过 50MB 上限")
    # 净化文件名：去除路径分隔符与 ".."，防止上传路径遍历任意写（P0-1）
    fname = secure_filename(f.filename)
    if not fname:
        return fail("文件名不合法")
    # 拦截高危可执行扩展名上传
    ext = os.path.splitext(fname)[1].lower()
    if ext in _UPLOAD_BLOCKED_EXTS:
        return fail("不允许上传该类型文件")
    dest = os.path.join(full, fname)
    # 兜底：确认最终落点仍在上传目录内
    if os.path.abspath(dest) != os.path.abspath(full) and not os.path.abspath(dest).startswith(os.path.abspath(full) + os.sep):
        return fail("路径不允许")
    try:
        f.save(dest)
        # 保存后再做一次大小校验（某些客户端 content_length 不可靠）
        if os.path.getsize(dest) > MAX_UPLOAD_SIZE:
            os.remove(dest)
            return fail("文件超过 50MB 上限")
        audit("上传文件", f"路径={os.path.join(raw, f.filename)}")
        return ok()
    except Exception as e:
        return fail(str(e))

@bp.route("/delete", methods=["POST"])
def delete_item():
    err = _admin_required()
    if err: return err
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip()
    full = safe_path(raw)
    if not full:
        return fail("路径不允许")
    if full == ALLOWED_ROOT:
        return fail("不能删除根目录")
    if not _is_real_path(full):
        return fail("路径包含越权符号链接")
    try:
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
        audit("删除", f"路径={raw}")
        return ok()
    except Exception as e:
        return fail(str(e))

@bp.route("/mkdir", methods=["POST"])
def make_dir():
    err = _admin_required()
    if err: return err
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip()
    full = safe_path(raw)
    if not full:
        return fail("路径不允许")
    if os.path.exists(full):
        return fail("路径已存在")
    parent = os.path.dirname(full)
    if parent and not _is_real_path(parent):
        return fail("父目录包含越权符号链接")
    try:
        os.makedirs(full)
        audit("创建目录", f"路径={raw}")
        return ok()
    except Exception as e:
        return fail(str(e))

@bp.route("/download")
def download_file():
    raw = request.args.get("path", "")
    full = safe_path(raw)
    if not full or not os.path.isfile(full):
        return fail("文件不存在")
    if not _is_real_path(full):
        return fail("符号链接目标不在允许范围内")
    audit("下载文件", f"路径={raw}")
    return send_file(full, as_attachment=True, download_name=os.path.basename(full))

@bp.route("/rename", methods=["POST"])
def rename_item():
    err = _admin_required()
    if err: return err
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip()
    new_name = _safe_name(data.get("new_name"))
    if not new_name:
        return fail("新名称不合法")
    full = safe_path(raw)
    if not full or not os.path.exists(full):
        return fail("路径不存在")
    parent = os.path.dirname(full)
    dest = os.path.join(parent, new_name)
    # 目标也必须落在允许根目录内
    if dest != ALLOWED_ROOT and not dest.startswith(ALLOWED_ROOT + os.sep):
        return fail("目标路径不允许")
    if os.path.exists(dest):
        return fail("目标路径已存在")
    try:
        os.rename(full, dest)
        audit("重命名", f"{raw} -> {new_name}")
        return ok()
    except Exception as e:
        return fail(str(e))

@bp.route("/move", methods=["POST"])
def move_item():
    err = _admin_required()
    if err: return err
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip()
    dest_raw = (data.get("destination") or "").strip()
    full = safe_path(raw)
    dest_full = safe_path(dest_raw)
    if not full or not os.path.exists(full):
        return fail("源路径不存在")
    if not dest_full:
        return fail("目标路径不允许")
    if os.path.abspath(full) == os.path.abspath(dest_full):
        return fail("源路径与目标路径相同")
    if os.path.exists(dest_full):
        return fail("目标已存在")
    if not _is_real_path(full) or not _is_real_path(dest_full):
        return fail("路径包含越权符号链接")
    try:
        os.rename(full, dest_full)
        audit("移动", f"{raw} -> {dest_raw}")
        return ok()
    except Exception as e:
        return fail(str(e))

@bp.route("/copy", methods=["POST"])
def copy_item():
    err = _admin_required()
    if err: return err
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip()
    dest_raw = (data.get("destination") or "").strip()
    full = safe_path(raw)
    dest_full = safe_path(dest_raw)
    if not full or not os.path.exists(full):
        return fail("源路径不存在")
    if not dest_full:
        return fail("目标路径不允许")
    if os.path.abspath(full) == os.path.abspath(dest_full):
        return fail("源路径与目标路径相同")
    if os.path.exists(dest_full):
        return fail("目标已存在")
    if not _is_real_path(full) or not _is_real_path(dest_full):
        return fail("路径包含越权符号链接")
    try:
        if os.path.isdir(full):
            shutil.copytree(full, dest_full)
        else:
            shutil.copy2(full, dest_full)
        audit("复制", f"{raw} -> {dest_raw}")
        return ok()
    except Exception as e:
        return fail(str(e))

@bp.route("/download-dir")
def download_dir():
    raw = request.args.get("path", "")
    full = safe_path(raw)
    if not full or not os.path.isdir(full):
        return fail("目录不存在")
    if not _is_real_path(full):
        return fail("目录包含越权符号链接")
    # 单次遍历完成统计 + 打包，避免大目录被扫描两次（P2-8）
    total = 0
    file_count = 0
    import tempfile
    import zipfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = tmp.name
    tmp.close()
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(full):
                depth = root[len(full):].count(os.sep)
                if depth >= MAX_DOWNLOAD_DIR_DEPTH:
                    del dirs[:]
                    continue
                for fn in files:
                    fp = os.path.join(root, fn)
                    if not _is_real_path(fp):
                        continue
                    file_count += 1
                    if file_count > MAX_DOWNLOAD_FILES:
                        raise ValueError("目录文件数超过打包上限")
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
                    if total > MAX_DOWNLOAD_DIR_SIZE:
                        raise ValueError("目录超过 500MB 打包上限")
                    arcname = os.path.relpath(fp, full)
                    zf.write(fp, arcname)
    except ValueError as e:
        if os.path.isfile(tmp_path):
            os.remove(tmp_path)
        return fail(str(e))
    except Exception as e:
        if os.path.isfile(tmp_path):
            os.remove(tmp_path)
        return fail(str(e))
    audit("下载目录", f"路径={raw}")

    @after_this_request
    def _cleanup(response):
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return response

    return send_file(tmp_path, as_attachment=True, download_name=os.path.basename(full) + ".zip")

@bp.route("/search")
def search_files():
    raw = request.args.get("path", "")
    query = request.args.get("q", "").strip().lower()
    if not query:
        return fail("请输入搜索关键词")
    full = safe_path(raw)
    if not full or not os.path.isdir(full):
        return fail("路径不存在")
    if not _is_real_path(full):
        return fail("目录包含越权符号链接")
    try:
        results = []
        for root, dirs, files in os.walk(full):
            # 限制搜索深度，防止在极深目录树中长时间阻塞（P2-2）
            depth = root[len(full):].count(os.sep)
            if depth >= MAX_SEARCH_DEPTH:
                del dirs[:]
                continue
            # 跳过越权符号链接子目录
            if not _is_real_path(root):
                del dirs[:]
                continue
            for name in files + dirs:
                if query in name.lower():
                    fp = os.path.join(root, name)
                    if not _is_real_path(fp):
                        continue
                    rel = os.path.relpath(fp, ALLOWED_ROOT)
                    stat = os.stat(fp)
                    is_dir = os.path.isdir(fp)
                    results.append({
                        "name": name,
                        "path": rel,
                        "is_dir": is_dir,
                        "size": 0 if is_dir else stat.st_size,
                        "mtime": stat.st_mtime,
                        "ext": os.path.splitext(name)[1].lower() if not is_dir else "",
                    })
        return ok({"entries": results, "current": os.path.relpath(full, ALLOWED_ROOT), "root": ALLOWED_ROOT})
    except Exception as e:
        return fail(str(e))

@bp.route("/batch-delete", methods=["POST"])
def batch_delete():
    err = _admin_required()
    if err: return err
    data = request.get_json(silent=True) or {}
    paths = data.get("paths") or []
    if not isinstance(paths, list):
        return fail("paths 必须为数组")
    if not paths:
        return fail("未选择文件")
    deleted = []
    errors = []
    for raw in paths:
        if not isinstance(raw, str):
            errors.append(f"{raw!r}: 路径必须为字符串")
            continue
        full = safe_path(raw.strip())
        if not full:
            errors.append(f"{raw}: 路径不允许")
            continue
        if full == ALLOWED_ROOT:
            errors.append(f"{raw}: 不能删除根目录")
            continue
        if not _is_real_path(full):
            errors.append(f"{raw}: 包含越权符号链接")
            continue
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            deleted.append(raw)
        except Exception as e:
            errors.append(f"{raw}: {e}")
    if deleted:
        audit("批量删除", str(deleted))
    return ok({"deleted": deleted, "errors": errors})
