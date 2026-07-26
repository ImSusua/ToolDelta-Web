import os
import json
import time
import re
import socket
import urllib.request
import urllib.error
from urllib.parse import urlparse
from ipaddress import ip_address

WALLPAPER_FILE = None
# 壁纸 URL 白名单：仅允许常见图片协议，避免 data:/javascript: 等注入
_ALLOWED_SCHEMES = ("http", "https")
_MAX_URL_LEN = 2048


def init_app(app):
    global WALLPAPER_FILE
    WALLPAPER_FILE = os.path.join(app.instance_path, "wallpaper.json")
    os.makedirs(os.path.dirname(WALLPAPER_FILE), exist_ok=True)


def _is_safe_url(url):
    """校验壁纸 URL：限定协议、排除危险字符、限制长度、禁止内网/元地址（SSRF 防护）。"""
    if not url or len(url) > _MAX_URL_LEN:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # 解析 IP 并阻止内网地址，比硬编码规则更全面（覆盖 10/172/127/169.254 等）
    # 与 app/routes/api.py:_is_safe_url 保持一致的判定范围：
    # is_private 仅覆盖 10/172.16-31/192.168，IPv6 场景下 ::（unspecified）在某些
    # Python 版本 is_private 返回 False，必须额外用 is_unspecified 拦截，否则可绕过。
    try:
        addrinfo = socket.getaddrinfo(host, None)
        for info in addrinfo:
            ip = ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
    except Exception:
        return False
    # 阻止引号/尖括号/反斜杠等可造成 CSS/HTML 逃逸的字符
    if re.search(r'[<"\'\\\x00-\x08\x0b\x0c\x0e-\x1f]', url):
        return False
    return True


def get_wallpaper():
    if not WALLPAPER_FILE or not os.path.isfile(WALLPAPER_FILE):
        return ""
    try:
        with open(WALLPAPER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        url = data.get("url", "")
        # 与 save 一致的危险字符校验：拦截 ) < > " ' ` \ 防 CSS/HTML 上下文注入
        if not isinstance(url, str) or re.search(r'[<>"\'`\\)]', url):
            return ""
        return url if _is_safe_url(url) else ""
    except Exception:
        return ""


def fetch_new():
    url = "https://cdn.8845.top/api/limo?orientation=pc"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        # 禁止自动跟随重定向：urllib 默认会跟随 3xx 跳转。
        # _is_safe_url 只校验最终 URL，若 CDN 被劫持返回 302 → http://169.254.169.254/，
        # urllib 会先打到内网再返回 final_url，存在 TOCTOU：实际请求已发往内网地址。
        # 自定义 opener 拦截所有 HTTPRedirectHandler，遇到 3xx 直接抛异常终止。
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None  # 返回 None 阻止重定向，urllib 会抛 HTTPError
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=8) as resp:
            final_url = resp.geturl()
        if final_url and _is_safe_url(final_url):
            save(final_url)
            return final_url
    except Exception:
        pass
    return ""


def save(url):
    if not WALLPAPER_FILE:
        return
    # 显式校验 URL 格式：必须以 http:// 或 https:// 开头，且不含可造成 CSS/HTML 逃逸的危险字符
    if not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
        return
    if re.search(r'[<>"\'\\)]', url):
        return
    if not _is_safe_url(url):
        return
    # 原子写：先用 tmp 文件写入再 os.replace 覆盖目标。
    # 旧实现直接 open(WALLPAPER_FILE, "w") 截断写，若 json.dump 中途异常（磁盘满/权限错误）
    # 目标文件已被截断，原壁纸配置丢失且不可恢复。改为 tmp 中转保证原子性。
    d = os.path.dirname(WALLPAPER_FILE)
    os.makedirs(d, exist_ok=True)
    tmp = WALLPAPER_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"url": url, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, WALLPAPER_FILE)
        tmp = None  # 标记已成功 replace，finally 不再删除
    finally:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def clear():
    if WALLPAPER_FILE and os.path.isfile(WALLPAPER_FILE):
        os.remove(WALLPAPER_FILE)
