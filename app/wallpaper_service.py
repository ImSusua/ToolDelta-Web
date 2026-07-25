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
    try:
        addrinfo = socket.getaddrinfo(host, None)
        for info in addrinfo:
            if ip_address(info[4][0]).is_private:
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
        with urllib.request.urlopen(req, timeout=8) as resp:
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
    with open(WALLPAPER_FILE, "w", encoding="utf-8") as f:
        json.dump({"url": url, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)


def clear():
    if WALLPAPER_FILE and os.path.isfile(WALLPAPER_FILE):
        os.remove(WALLPAPER_FILE)
