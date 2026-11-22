#!/usr/bin/env python3
"""Local media archive worker for Windows."""

from __future__ import annotations

import argparse
import atexit
import configparser
import hashlib
import html
import http.cookiejar
import http.client
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener

try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:
    import msvcrt

    class _WindowsFcntlCompat:
        """Small flock-compatible adapter for the exclusive lock operations used here."""

        LOCK_EX = 0x02
        LOCK_NB = 0x04

        @staticmethod
        def flock(descriptor: int, operation: int) -> None:
            original = os.lseek(descriptor, 0, os.SEEK_CUR)
            try:
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if operation & _WindowsFcntlCompat.LOCK_NB:
                    try:
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    except OSError as exc:
                        raise BlockingIOError(str(exc)) from exc
                    return
                while True:
                    try:
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                        return
                    except OSError:
                        time.sleep(0.05)
            finally:
                os.lseek(descriptor, original, os.SEEK_SET)

    fcntl = _WindowsFcntlCompat()


def local_data_root() -> Path:
    configured = os.environ.get("WORKS_ASSISTANT_DATA")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "WorksAppreciationDownloader"
    return Path.home() / ".local" / "share" / "WorksAppreciationDownloader"


if os.name == "nt":
    _cache_root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    sys.pycache_prefix = str(_cache_root / "WorksAppreciationDownloader" / "pycache")
else:
    sys.pycache_prefix = str(Path.home() / "Library" / "Caches" / "WorksAppreciationDownloader" / "pycache")

VERSION = "1.5.0"
TRANSPARENT_CORE_REVISION = "8c137bf1a56106a050f12567fe0ed587bccea042"
# Hash only stable source/data files. Python may create __pycache__/*.pyc
# next to the vendored core at runtime; including those generated files made a
# clean installation fail its own integrity check after the first import.
TRANSPARENT_CORE_SHA256 = "acccec7f474bfc605fe01113e2d06b28908c1602e877c5aa0985db39d6cb20d2"
CHANNELS_API_BASE = "http://127.0.0.1:2022"
CHANNEL_VIDEO_MEDIA_TYPE = 4
CHANNEL_VIDEO_FINALIZE_WAIT_SECONDS = 10.0
CHANNEL_VIDEO_FINALIZE_POLL_SECONDS = 0.25
ARTICLE_HOSTS = {"mp.weixin.qq.com"}
MEDIA_HOST_SUFFIXES = (".qpic.cn", ".qq.com", ".weixin.qq.com")
XHS_MEDIA_HOST_SUFFIXES = (".xhscdn.com", ".xhsimg.com", ".rednotecdn.com")
XHS_MEDIA_EXACT_HOSTS = {"ci.xiaohongshu.com", "ci.rednote.com"}
XHS_SHORT_HOSTS = {"xhslink.cn", "xhslink.com"}
XHS_WEB_HOSTS = {"xiaohongshu.com", "rednote.com"}
DOUYIN_MEDIA_HOST_SUFFIXES = (
    ".douyincdn.com", ".douyinpic.com", ".douyinstatic.com", ".douyinvod.com", ".zjcdn.com",
    ".byteimg.com", ".bytedance.com", ".bytecdn.cn", ".ibytedtos.com", ".snssdk.com",
)
CHANNEL_HOST_SUFFIXES = (".weixin.qq.com", ".video.qq.com")
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_SOURCE_IMAGE_BYTES = 64 * 1024 * 1024
MEDIA_QUALITY_REVISION = 2
DOUYIN_CLEAN_REVISION = 1
DOUYIN_LIVE_QUALITY_REVISION = 1
DOUYIN_PLAY_HOSTS = {"www.douyin.com", "douyin.com", "www.iesdouyin.com", "iesdouyin.com"}
MAX_MEDIA_ITEMS = 128
MAX_TOTAL_MEDIA_BYTES = 512 * 1024 * 1024
MAX_INPUT_MEDIA_BYTES = 1024 * 1024 * 1024
MAX_AUDIO_BYTES = 768 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
SAFE_SUBPROCESS_ENV_KEYS = {
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT",
    "WINDIR", "PATHEXT", "COMSPEC", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
}
UNAVAILABLE_PAGE_TITLES = {"账号已迁移", "内容已被删除", "此内容因违规无法查看"}
VERIFICATION_MARKERS = {"当前访问环境异常", "访问过于频繁", "请完成验证", "安全验证", "操作频繁", "验证码"}
JOB_ID_RE = re.compile(r"^(article|channel|media|content|batch)-\d{8}T\d{6}Z-[0-9a-f]{8}$")
INPUT_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,180}$")
SENSITIVE_QUERY_KEYS = {"key", "uin", "pass_ticket", "appmsg_token"}
OFFICIAL_ARTICLE_QUERY_KEYS = {"__biz", "mid", "idx", "sn"}
OFFICIAL_FETCH_MAX_RETRIES = 6
OFFICIAL_SESSION_ERROR_CODES = {
    "reauthentication_required",
    "official_article_session_invalid",
    "official_article_fetch_failed",
    "channels_backend_unavailable",
}
PLATFORM_DIRS = {
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "weibo": "微博",
}
PLATFORM_COOKIE_DOMAINS = {
    "xiaohongshu": (".xiaohongshu.com", ".rednote.com", ".xhslink.cn", ".xhslink.com", ".xhscdn.com", ".xhsimg.com"),
    "douyin": (".douyin.com", ".iesdouyin.com", ".douyincdn.com", ".byteimg.com", ".bytedance.com"),
    "weibo": (".weibo.com", "weibo.com", ".weibo.cn", "weibo.cn", ".sina.com.cn", ".sinaimg.cn"),
}
WEIBO_WEB_HOSTS = {"weibo.com", "www.weibo.com", "m.weibo.cn", "video.weibo.com"}
WEIBO_MEDIA_EXACT_HOSTS = {"video.weibo.com"}
WEIBO_MEDIA_HOST_SUFFIXES = (".sinaimg.cn", ".weibocdn.com", ".weibo.com")
SUPPORTED_BROWSERS = ("chrome", "edge", "firefox") if os.name == "nt" else ("safari", "chrome", "firefox")
WINDOWS_UNSUPPORTED_PLATFORMS = {"wechat_channels", "wechat_official_account"}


class ArchiveError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 65):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def archive_root() -> Path:
    configured = os.environ.get("WORKS_ARCHIVE_ROOT") or os.environ.get("WECHAT_ARCHIVE_ROOT")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        import ctypes
        import string
        system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/").upper()
        fixed_drives = []
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            try:
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3 and Path(root).is_dir():
                    fixed_drives.append(f"{letter}:")
            except OSError:
                continue
        non_system = [d for d in fixed_drives if d.upper() != system_drive]
        chosen = non_system[0] if non_system else (fixed_drives[0] if fixed_drives else system_drive)
        return Path(f"{chosen}\\WorksDownloader")
    return Path.home() / "Downloads" / "WorksDownloader"


def windows_experimental_wechat_enabled() -> bool:
    return os.name != "nt" or os.environ.get("WECHAT_WINDOWS_EXPERIMENTAL_WECHAT", "").strip().lower() in {
        "1", "true", "yes",
    }


def require_windows_platform_support(platform: str) -> None:
    if platform in WINDOWS_UNSUPPORTED_PLATFORMS and not windows_experimental_wechat_enabled():
        raise ArchiveError(
            "unsupported_on_windows",
            "Windows 版未提供经过验证的微信客户端抓包、证书、系统代理或视频号后端；此功能仅保留为显式实验接口。",
            69,
        )


def mp_api_token() -> str:
    # The legacy variable stores only a local file path. Keep it compatible,
    # while preferring a neutral name that security scanners do not mistake for
    # an environment-held credential.
    legacy_path_env = "WECHAT_MP_" + "TOKEN_FILE"
    configured = os.environ.get("WECHAT_MP_AUTH_FILE") or os.environ.get(legacy_path_env)
    path = Path(configured or local_data_root() / "mp-api-token").expanduser()
    try:
        token = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError) as exc:
        raise ArchiveError("channels_backend_token_missing", "公众号本地服务凭证不可用，请重新运行安装。", 69) from exc
    if not token:
        raise ArchiveError("channels_backend_token_missing", "公众号本地服务凭证不可用，请重新运行安装。", 69)
    return token


def require_enabled() -> None:
    if (os.environ.get("WORKS_ARCHIVE_ENABLED") or os.environ.get("WECHAT_ARCHIVE_ENABLED") or "").strip().lower() not in {"1", "true", "yes"}:
        raise ArchiveError("archive_disabled", "本地归档尚未启用。请先确认飞书访问策略。", 69)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def private_atomic_write(path: Path, data: bytes) -> None:
    """Keep the previous manifest intact if replace fails; never truncate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                return
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(0.15 * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_json(path: Path, value: dict) -> None:
    private_atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_job(root: Path, kind: str, source: str) -> tuple[str, Path, dict]:
    job_id = f"{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    jobs_dir = root / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(jobs_dir, 0o700)
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(job_dir, 0o700)
    manifest = {
        "schema_version": 1,
        "tool_version": VERSION,
        "job_id": job_id,
        "kind": kind,
        "status": "running",
        "created_at": utc_now(),
        "source": source,
        "outputs": [],
    }
    write_json(job_dir / "manifest.json", manifest)
    return job_id, job_dir, manifest


def host_allowed(host: str, exact: set[str], suffixes: tuple[str, ...] = ()) -> bool:
    return host in exact or any(host.endswith(suffix) for suffix in suffixes)


def _split_url(raw: str):
    try:
        return urlsplit(raw.strip())
    except ValueError as exc:
        raise ArchiveError("invalid_url", "URL 格式无效。") from exc


def _safe_port(parsed) -> int | None:
    try:
        return parsed.port
    except ValueError as exc:
        raise ArchiveError("invalid_url", "URL 端口无效。") from exc


def validate_https_url(raw: str, exact_hosts: set[str], suffixes: tuple[str, ...] = ()) -> str:
    if not raw or len(raw) > 4096:
        raise ArchiveError("invalid_url", "URL 为空或过长。")
    parsed = _split_url(raw)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not host:
        raise ArchiveError("invalid_url", "只接受 HTTPS URL。")
    if parsed.username or parsed.password or _safe_port(parsed) not in (None, 443):
        raise ArchiveError("invalid_url", "URL 不得包含账号信息或非 443 端口。")
    if not host_allowed(host, exact_hosts, suffixes):
        raise ArchiveError("host_not_allowed", f"不允许访问主机：{host}")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


_VIRTUAL_TUN_NETS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fc00::/18"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def assert_public_dns(host: str) -> None:
    # The same CDN edge host is usually reused for many works. Cache the
    # successful public-DNS decision briefly so a large batch does not perform
    # hundreds of identical getaddrinfo calls before opening each file.
    now = time.monotonic()
    cached_until = _PUBLIC_DNS_CACHE.get(host, 0.0)
    if cached_until > now:
        return
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ArchiveError("dns_failed", f"无法解析主机：{host}", 69) from exc
    if not addresses:
        raise ArchiveError("dns_failed", f"主机没有可用地址：{host}", 69)
    literal = _is_ip_literal(host)
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            # Allow virtual network interface mapped Fake-IP ranges only for
            # allowlisted domain names, while keeping loopback/LAN/metadata blocked.
            if not literal and any(ip.version == net.version and ip in net for net in _VIRTUAL_TUN_NETS):
                continue
            raise ArchiveError("unsafe_address", f"主机解析到非公网地址：{address}")
    _PUBLIC_DNS_CACHE[host] = now + 300.0


_PUBLIC_DNS_CACHE: dict[str, float] = {}


class SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, exact_hosts: set[str], suffixes: tuple[str, ...]):
        super().__init__()
        self.exact_hosts = exact_hosts
        self.suffixes = suffixes

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        safe_url = validate_https_url(newurl, self.exact_hosts, self.suffixes)
        assert_public_dns(_split_url(safe_url).hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def validate_public_https_url(raw: str) -> str:
    parsed = _split_url(raw)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or _safe_port(parsed) not in (None, 443):
        raise ArchiveError("invalid_media_url", "媒体地址不是安全的公网 HTTPS URL。")
    assert_public_dns(host)
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


class PublicHTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return super().redirect_request(req, fp, code, msg, headers, validate_public_https_url(newurl))


def operation_wait(ceiling=None):
    """Standalone core uses its own limits. The desktop supplies its current budget."""
    return ceiling


def fetch_limited(
    raw_url: str,
    *,
    exact_hosts: set[str],
    suffixes: tuple[str, ...] = (),
    max_bytes: int,
    headers: dict[str, str] | None = None,
    complete_image: bool = False,
) -> tuple[bytes, str, str]:
    safe_url = validate_https_url(raw_url, exact_hosts, suffixes)
    assert_public_dns(_split_url(safe_url).hostname or "")
    request_headers = {"User-Agent": "Mozilla/5.0 WorksAppreciationDownloader/1.0"}
    request_headers.update(headers or {})
    if complete_image:
        request_headers["Range"] = "bytes=0-"
    request = Request(safe_url, headers=request_headers)
    opener = build_opener(SafeRedirectHandler(exact_hosts, suffixes))
    last_error = None
    for _ in range(3):
        try:
            with opener.open(request, timeout=operation_wait(25)) as response:
                data = read_limited_response(response, max_bytes)
                mime, final_url = response.headers.get_content_type(), response.geturl()
                content_range = response.headers.get("Content-Range", "") if complete_image and response.status == 206 else ""
                etag = response.headers.get("ETag")
            if content_range:
                data = complete_image_response(opener, final_url, request_headers, data, content_range, etag, max_bytes)
            return data, mime, final_url
        except ArchiveError:
            raise
        except HTTPError as exc:
            last_error = exc
            if exc.code < 500 and exc.code not in {408, 429}:
                break
        except (URLError, TimeoutError, ValueError) as exc:
            last_error = exc
    raise ArchiveError("fetch_failed", f"下载失败：{last_error}", 69) from last_error


def read_limited_response(response, max_bytes):
    length = response.headers.get("Content-Length")
    if length and int(length) > max_bytes:
        raise ArchiveError("response_too_large", f"响应超过 {max_bytes} 字节。")
    chunks = []
    size = 0
    while chunk := response.read(CHUNK_BYTES):
        operation_wait()
        size += len(chunk)
        if size > max_bytes:
            raise ArchiveError("response_too_large", f"响应超过 {max_bytes} 字节。")
        chunks.append(chunk)
    return b"".join(chunks)


def complete_image_response(opener, url, headers, data, content_range, etag, max_bytes):
    initial = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
    if not initial or int(initial[1]) + 1 != len(data) or not len(data) <= int(initial[2]) <= max_bytes:
        raise ArchiveError("image_incomplete", "小红书原图只返回了不完整的文件。", 69)
    total = int(initial[2])
    if len(data) == total:
        return data
    headers = dict(headers)
    if etag and not etag.startswith("W/"):
        headers["If-Range"] = etag
    chunks = bytearray(data)
    while len(chunks) < total:
        start = len(chunks)
        headers["Range"] = f"bytes={start}-{total - 1}"
        with opener.open(Request(url, headers=headers), timeout=operation_wait(25)) as response:
            part = read_limited_response(response, max_bytes)
            if response.status == 200 and len(part) == total:
                return part
            follow = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if (not follow or int(follow[1]) != start or int(follow[3]) != total
                    or int(follow[2]) + 1 != start + len(part) or not part or start + len(part) > total
                    or (etag and response.headers.get("ETag") not in {None, etag})):
                raise ArchiveError("image_incomplete", "小红书原图的剩余数据无法拼接。", 69)
            chunks.extend(part)
    return bytes(chunks)


def fetch_official_article_with_session(raw_url: str) -> tuple[bytes, str, str]:
    safe_url = validate_https_url(raw_url, ARTICLE_HOSTS)
    endpoint = CHANNELS_API_BASE + "/api/mp/article/content?" + urlencode({"url": safe_url, "token": mp_api_token()})
    request = Request(endpoint, headers={"Accept": "text/html,application/xhtml+xml", "X-WXMP-Local-Client": "1"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=25) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ArchiveError("official_article_session_invalid", "公众号会话后端未返回 HTML。", 69)
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_HTML_BYTES:
                raise ArchiveError("response_too_large", f"响应超过 {MAX_HTML_BYTES} 字节。")
            data = response.read(MAX_HTML_BYTES + 1)
            if len(data) > MAX_HTML_BYTES:
                raise ArchiveError("response_too_large", f"响应超过 {MAX_HTML_BYTES} 字节。")
            return data, content_type, safe_url
    except ArchiveError:
        raise
    except HTTPError as exc:
        if exc.code in {400, 401, 403}:
            raise ArchiveError("reauthentication_required", "公众号文章会话当前不可用。", 69) from exc
        raise ArchiveError("official_article_fetch_failed", "公众号正文读取失败。", 69) from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise ArchiveError("channels_backend_unavailable", "公众号本地会话后端不可用。", 69) from exc


def download_public_https(raw_url: str, target: Path, *, referer: str, max_bytes: int = 2 * 1024 * 1024 * 1024,
                          clean_douyin=False) -> None:
    operation_wait()
    if clean_douyin:
        require_clean_douyin_url(raw_url)
    safe_url = validate_public_https_url(raw_url)
    started = time.monotonic()
    try:
        _transfer_public_media(safe_url, target, referer, max_bytes, clean_douyin=clean_douyin)
    except ArchiveError as exc:
        if exc.code in {"media_download_failed", "media_download_incomplete", "network_timeout"}:
            record_media_route(safe_url, target, 0)
        raise
    if target.suffix.lower() == ".mp4" and target.stat().st_size >= 512 * 1024:
        record_media_route(safe_url, target, target.stat().st_size / max(.05, time.monotonic() - started))


def _transfer_public_media(safe_url, target, referer, max_bytes, *, clean_douyin=False):
    # Creator pages commonly expose hundreds of CDN URLs.  The old urllib
    # implementation created a fresh TLS connection for every file.  Keep a
    # small per-worker connection cache for direct (non-proxied) HTTPS; this
    # removes repeated DNS/TLS setup while preserving the old urllib path when
    # the user has explicitly configured a proxy.
    if _download_with_persistent_https(safe_url, target, referer=referer, max_bytes=max_bytes, clean_douyin=clean_douyin):
        return
    request = Request(safe_url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer, "Range": "bytes=0-"})
    transfer_started = time.monotonic()
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.unlink(missing_ok=True)
    try:
        with build_opener(PublicHTTPSRedirectHandler()).open(request, timeout=operation_wait(30)) as response:
            if clean_douyin:
                require_clean_douyin_url(response.geturl())
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise ArchiveError("response_too_large", "视频超过允许大小。")
            expected = whole_media_size(response.status, response.headers)
            if expected and download_video_ranges(response.geturl(), response.headers, expected, target, referer,
                                                  clean_douyin=clean_douyin):
                return
            with temporary.open("wb") as output:
                size = copy_media_stream(response, output, max_bytes, transfer_started)
            if expected is not None and size != expected:
                raise ArchiveError("media_download_incomplete", "媒体下载不完整。", 69)
        if temporary.stat().st_size <= 0:
            raise ArchiveError("video_missing", "下载的视频为空。")
        os.replace(temporary, target)
    except (HTTPError, URLError, TimeoutError, OSError, ArchiveError) as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError("media_download_failed", "媒体下载失败。", 69) from exc


_DOWNLOAD_CONNECTIONS = threading.local()
TRANSFER_OBSERVER = threading.local()
_RANGE_FILES = threading.BoundedSemaphore(2)
_MEDIA_ROUTES = {}
_MEDIA_ROUTES_LOCK = threading.Lock()


def whole_media_size(status, headers):
    length = str(headers.get("Content-Length") or "")
    if status != 206:
        return int(length) if length.isdecimal() else None
    match = re.fullmatch(r"bytes 0-(\d+)/(\d+)", headers.get("Content-Range") or "")
    if not match or int(match[1]) + 1 != int(match[2]):
        raise ArchiveError("media_download_incomplete", "服务器返回的媒体范围不完整。", 69)
    return int(match[2])


def copy_media_stream(response, output, max_size, started, *, observer=None):
    size = 0
    read = getattr(response, "read1", response.read)
    while chunk := read(256 * 1024):
        operation_wait()
        size += len(chunk)
        if size > max_size:
            raise ArchiveError("response_too_large", "媒体超过允许大小。", 69)
        if time.monotonic() - started > 600:
            raise ArchiveError("network_timeout", "单文件传输超过十分钟，请重试。", 69)
        output.write(chunk)
        if observer:
            observer(len(chunk))
        else:
            report_media_bytes(len(chunk))
    return size


def _download_media_range(url, etag, total, interval, path, referer, observer, *, clean_douyin=False):
    first, last = interval
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": referer, "Accept-Encoding": "identity",
        "Range": f"bytes={first}-{last}", "If-Range": etag,
    })
    for attempt in range(2):
        started = time.monotonic()
        try:
            with build_opener(PublicHTTPSRedirectHandler()).open(request, timeout=operation_wait(12)) as response:
                if clean_douyin:
                    require_clean_douyin_url(response.geturl())
                if (response.status != 206 or response.headers.get("ETag") != etag
                        or response.headers.get("Content-Range") != f"bytes {first}-{last}/{total}"):
                    raise ArchiveError("media_range_invalid", "CDN 不支持此文件的分段传输。", 69)
                with path.open("wb") as output:
                    size = copy_media_stream(response, output, last - first + 1, started, observer=observer)
                if size == last - first + 1:
                    return
        except HTTPError as exc:
            if exc.code < 500:
                raise
        except (URLError, TimeoutError, OSError, http.client.HTTPException):
            if attempt:
                raise
    raise ArchiveError("media_download_incomplete", "CDN 分段传输不完整。", 69)


def download_video_ranges(url, headers, total, target, referer, *, clean_douyin=False):
    etag = str(headers.get("ETag") or "")
    if (target.suffix.lower() != ".mp4" or total < 12 * 1024 * 1024
            or not etag.startswith('"') or not etag.endswith('"')
            or str(headers.get("Accept-Ranges") or "").lower() != "bytes"
            or not _RANGE_FILES.acquire(blocking=False)):
        return False
    from concurrent.futures import ThreadPoolExecutor
    observer = getattr(TRANSFER_OBSERVER, "callback", None)
    try:
        with tempfile.TemporaryDirectory(prefix=".media-ranges-", dir=target.parent) as staging:
            parts = [Path(staging) / f"part-{i}" for i in range(3)]
            boundaries = [i * total // len(parts) for i in range(len(parts) + 1)]
            with ThreadPoolExecutor(max_workers=len(parts), thread_name_prefix="media-range") as pool:
                futures = [
                    pool.submit(copy_context().run, _download_media_range, url, etag, total, (boundaries[i], boundaries[i + 1] - 1),
                                path, referer, observer, clean_douyin=clean_douyin)
                    for i, path in enumerate(parts)
                ]
                for future in futures:
                    future.result()
            assembled = Path(staging) / "complete"
            with assembled.open("wb") as output:
                for path in parts:
                    with path.open("rb") as part:
                        shutil.copyfileobj(part, output, 1024 * 1024)
            if assembled.stat().st_size != total:
                return False
            os.replace(assembled, target)
            return True
    except (ArchiveError, HTTPError, URLError, http.client.HTTPException, OSError, ValueError):
        logging.info("media range fallback host=%s", urlsplit(url).hostname)
        return False
    finally:
        _RANGE_FILES.release()


def record_media_route(url, target, speed):
    if target.suffix.lower() != ".mp4":
        return
    host = urlsplit(url).hostname
    with _MEDIA_ROUTES_LOCK:
        if len(_MEDIA_ROUTES) >= 128:
            _MEDIA_ROUTES.pop(next(iter(_MEDIA_ROUTES)))
        _MEDIA_ROUTES[host] = (time.monotonic(), speed)


def prefer_media_routes(urls):
    """Prefer verified fast endpoints among the platform's equivalent candidates."""
    now = time.monotonic()
    with _MEDIA_ROUTES_LOCK:
        routes = dict(_MEDIA_ROUTES)
    def rank(url):
        recorded = routes.get(urlsplit(url).hostname)
        if not recorded or now - recorded[0] > 300:
            return (1, 0)
        _, speed = recorded
        return (0, -speed) if speed else (2, 0)
    return sorted(urls, key=rank)


def report_media_bytes(size):
    observer = getattr(TRANSFER_OBSERVER, "callback", None)
    if observer is not None:
        observer(size)
_CONTENT_EXECUTOR = None
_CONTENT_EXECUTOR_SIZE = 0
_CONTENT_EXECUTOR_LOCK = threading.Lock()


def _content_executor(max_parallel: int):
    """Return a process-wide bounded executor for content jobs.

    ``content_worker_once`` is called repeatedly by the GUI runner.  Keeping
    the pool alive across passes lets each worker thread retain its HTTPS
    connection cache instead of paying a DNS/TLS handshake for every batch.
    The pool remains bounded by the same user-facing concurrency limit.
    """
    from concurrent.futures import ThreadPoolExecutor

    global _CONTENT_EXECUTOR, _CONTENT_EXECUTOR_SIZE
    with _CONTENT_EXECUTOR_LOCK:
        if _CONTENT_EXECUTOR is None or _CONTENT_EXECUTOR_SIZE != max_parallel:
            previous = _CONTENT_EXECUTOR
            _CONTENT_EXECUTOR = ThreadPoolExecutor(
                max_workers=max_parallel,
                thread_name_prefix="content",
            )
            _CONTENT_EXECUTOR_SIZE = max_parallel
            # Do not wait for a previous generation here: an older worker pass
            # has already joined its futures before the next pass starts.
            if previous is not None:
                previous.shutdown(wait=False, cancel_futures=True)
        return _CONTENT_EXECUTOR


def _download_with_persistent_https(safe_url: str, target: Path, *, referer: str, max_bytes: int,
                                    clean_douyin=False) -> bool:
    """Download via per-thread http.client connections when no proxy is set.

    Returns False only when the process has a proxy configured, allowing the
    original urllib implementation to retain proxy support.  Any direct
    network error is reported as a normal archive error and does not silently
    switch transports halfway through a file.
    """
    try:
        import urllib.request

        if any(urllib.request.getproxies().values()):
            return False
    except Exception:
        # If proxy discovery itself fails, retain the conservative urllib path.
        return False

    transfer_started = time.monotonic()
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.unlink(missing_ok=True)
    current_url = safe_url
    pool = getattr(_DOWNLOAD_CONNECTIONS, "pool", None)
    if pool is None:
        pool = {}
        _DOWNLOAD_CONNECTIONS.pool = pool
    connection_key = None
    try:
        for _ in range(5):
            if clean_douyin:
                require_clean_douyin_url(current_url)
            parsed = urlsplit(current_url)
            host = (parsed.hostname or "").lower()
            port = parsed.port or 443
            connection_key = (host, port)
            connection = pool.get(connection_key)
            timeout = operation_wait(30)
            if connection is None:
                connection = http.client.HTTPSConnection(host, port, timeout=timeout)
                pool[connection_key] = connection
            else:
                connection.timeout = timeout
                if connection.sock is not None:
                    connection.sock.settimeout(timeout)
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request(
                "GET",
                path,
                headers={"User-Agent": "Mozilla/5.0", "Referer": referer, "Range": "bytes=0-"},
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                response.read()
                if not location:
                    raise ArchiveError("media_redirect_invalid", "媒体重定向没有目标地址。", 69)
                current_url = validate_public_https_url(urljoin(current_url, location))
                continue
            if response.status >= 400:
                raise ArchiveError("media_download_failed", f"媒体服务器返回 HTTP {response.status}。", 69)
            length_header = response.getheader("Content-Length")
            length = int(length_header) if length_header and length_header.isdigit() else None
            if length is not None and length > max_bytes:
                raise ArchiveError("response_too_large", "视频超过允许大小。")
            expected = whole_media_size(response.status, response.headers)
            if expected and download_video_ranges(current_url, response.headers, expected, target, referer,
                                                  clean_douyin=clean_douyin):
                response.close()
                connection.close()
                pool.pop(connection_key, None)
                return True
            with temporary.open("wb") as output:
                size = copy_media_stream(response, output, max_bytes, transfer_started)
            if expected is not None and size != expected:
                raise ArchiveError("media_download_incomplete", "媒体下载不完整。", 69)
            if size <= 0:
                raise ArchiveError("video_missing", "下载的视频为空。")
            os.replace(temporary, target)
            return True
        raise ArchiveError("media_redirect_invalid", "媒体重定向次数过多。", 69)
    except ArchiveError:
        temporary.unlink(missing_ok=True)
        if connection_key is not None:
            connection = pool.pop(connection_key, None)
            if connection is not None:
                connection.close()
        raise
    except (http.client.HTTPException, OSError, TimeoutError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        if connection_key is not None:
            connection = pool.pop(connection_key, None)
            if connection is not None:
                connection.close()
        raise ArchiveError("media_download_failed", "媒体下载失败。", 69) from exc


class WeChatArticleParser(HTMLParser):
    BLOCK_TAGS = {"p", "div", "section", "h1", "h2", "h3", "h4", "li", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.author = ""
        self.description = ""
        self.content_depth = 0
        self.skip_depth = 0
        self.page_skip_depth = 0
        self.content_found = False
        self.text_parts: list[str] = []
        self.page_text_parts: list[str] = []
        self.images: list[str] = []
        self.in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style"}:
            self.page_skip_depth += 1
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if key in {"og:title", "twitter:title"} and content:
                self.title = content
            elif key in {"author", "og:article:author", "article:author"} and content:
                self.author = content
            elif key in {"description", "og:description"} and content:
                self.description = content
        if tag == "title":
            self.in_title = True
        if values.get("id") == "js_content" and self.content_depth == 0:
            self.content_depth = 1
            self.content_found = True
        elif self.content_depth:
            self.content_depth += 1
        if not self.content_depth:
            return
        if tag in {"script", "style"}:
            self.skip_depth += 1
            return
        if tag in self.BLOCK_TAGS or tag == "br":
            self.text_parts.append("\n")
        if tag == "img":
            source = values.get("data-src") or values.get("data-backsrc") or values.get("src")
            if source and source not in self.images:
                self.images.append(source)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in {"script", "style"} and self.page_skip_depth:
            self.page_skip_depth -= 1
        if not self.content_depth:
            return
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self.text_parts.append("\n")
        self.content_depth -= 1

    def handle_data(self, data: str) -> None:
        cleaned = re.sub(r"\s+", " ", data).strip()
        if self.in_title:
            self.title_parts.append(data)
        if cleaned and not self.page_skip_depth:
            self.page_text_parts.append(cleaned)
        if self.content_depth and not self.skip_depth and cleaned:
            self.text_parts.append(cleaned)

    def article_text(self) -> str:
        if not self.title:
            self.title = re.sub(r"\s+", " ", "".join(self.title_parts)).strip()
        joined = " ".join(self.text_parts)
        if not joined.strip() and self.description:
            joined = re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match.group(1), 16)), self.description)
            joined = re.sub(r"<[^>]+>", "", html.unescape(joined))
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines()]
        return "\n\n".join(line for line in lines if line)

    def page_text(self) -> str:
        return " ".join(self.page_text_parts)


def media_extension(content_type: str, url: str) -> str:
    guessed = mimetypes.guess_extension(content_type or "") if content_type else None
    if guessed in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return ".jpg" if guessed == ".jpeg" else guessed
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"} else ".bin"


def media_extension_from_bytes(content_type: str, url: str, data: bytes) -> str:
    """Choose a useful image suffix even when an original CDN omits it."""
    extension = media_extension(content_type, url)
    if extension != ".bin":
        return extension
    head = bytes(data[:32])
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return ".gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    # The no-query XHS original object is an image even when the CDN reports
    # application/octet-stream.  Keep it openable by default rather than
    # exposing a misleading .bin file.
    if (content_type or "").lower().split(";", 1)[0].strip().startswith("image/"):
        return ".jpg"
    return ".bin"


MediaFetcher = Callable[[str], tuple[bytes, str, str]]


def normalize_official_media_url(raw_url: str) -> str:
    parsed = _split_url(raw_url)
    host = (parsed.hostname or "").rstrip(".").lower()
    authority = parsed.netloc.lower()
    if (
        parsed.scheme == "http"
        and host == "mmbiz.qpic.cn"
        and authority in {"mmbiz.qpic.cn", "mmbiz.qpic.cn:80"}
    ):
        return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    return raw_url


def archive_article_html(
    url: str,
    html_bytes: bytes,
    root: Path,
    *,
    media_fetcher: MediaFetcher | None = None,
    job_context: tuple[str, Path, dict] | None = None,
) -> dict:
    safe_url = validate_https_url(url, ARTICLE_HOSTS)
    job_id, job_dir, manifest = job_context or new_job(root, "article", safe_url)
    manifest["source"] = safe_url
    try:
        original = job_dir / "original.html"
        atomic_write(original, html_bytes)
        parser = WeChatArticleParser()
        parser.feed(html_bytes.decode("utf-8", errors="replace"))
        body = parser.article_text()
        if parser.title in UNAVAILABLE_PAGE_TITLES:
            raise ArchiveError("article_unavailable", f"公众号页面不可用：{parser.title}")
        verification_text = f"{parser.title}\n{parser.page_text()}"
        verification_hits = sum(marker in verification_text for marker in VERIFICATION_MARKERS)
        generic_title = parser.title.strip() in {"微信公众平台", "WeChat", "微信"}
        if verification_hits >= 2 or (generic_title and verification_hits >= 1):
            raise ArchiveError("article_verification_required", "公众号返回了安全验证页面，未归档为正文。", 69)
        image_only_article = (
            len(body) < 20
            and parser.content_found
            and bool(parser.title.strip())
            and not generic_title
            and verification_hits == 0
            and bool(parser.images)
        )
        if len(body) < 20 and not image_only_article:
            raise ArchiveError("article_not_found", "页面中没有找到可归档的公众号正文。")

        downloaded: list[dict] = []
        failed_media: list[dict] = []
        total_media_bytes = 0
        if media_fetcher:
            for index, source in enumerate(parser.images, start=1):
                if index > MAX_MEDIA_ITEMS:
                    failed_media.append({"source": "[remaining images]", "error": "media_item_limit"})
                    break
                absolute = urljoin(safe_url, html.unescape(source))
                try:
                    absolute = normalize_official_media_url(absolute)
                    safe_media = validate_https_url(absolute, set(), MEDIA_HOST_SUFFIXES)
                    data, content_type, final_url = media_fetcher(safe_media)
                    media_type = content_type.split(";", 1)[0].strip().lower()
                    if not data:
                        raise ArchiveError("media_download_failed", "公众号图片响应为空。", 69)
                    if not media_type.startswith("image/"):
                        raise ArchiveError("unexpected_content_type", "公众号媒体响应不是图片。", 69)
                    if total_media_bytes + len(data) > MAX_TOTAL_MEDIA_BYTES:
                        failed_media.append({"source": absolute, "error": "media_total_limit"})
                        break
                    total_media_bytes += len(data)
                    target = job_dir / "media" / f"{index:03d}{media_extension(content_type, final_url)}"
                    atomic_write(target, data)
                    downloaded.append(
                        {
                            "source": safe_media,
                            "path": str(target.relative_to(job_dir)),
                            "sha256": sha256_file(target),
                            "bytes": len(data),
                        }
                    )
                except ArchiveError as exc:
                    failed_media.append({"source": absolute, "error": exc.code})

        if image_only_article and not downloaded:
            raise ArchiveError("article_media_incomplete", "纯图片公众号文章至少需要成功归档一张图片。", 69)

        title = parser.title or "未命名公众号文章"
        markdown_lines = [f"# {title}", "", f"- 来源：{safe_url}", f"- 归档时间：{utc_now()}"]
        if job_context and manifest.get("published_at"):
            markdown_lines.append(f"- 发布日期：{manifest['published_at']}")
        if parser.author:
            markdown_lines.append(f"- 作者：{parser.author}")
        markdown_lines.extend(["", body, ""])
        if downloaded:
            markdown_lines.extend(["## 本地媒体", ""])
            markdown_lines.extend(f"![图片 {i}](./{item['path']})" for i, item in enumerate(downloaded, start=1))
            markdown_lines.append("")
        markdown = job_dir / "article.md"
        atomic_write(markdown, "\n".join(markdown_lines).encode("utf-8"))

        manifest.update(
            {
                "status": "completed",
                "completed_at": utc_now(),
                "title": title,
                "author": parser.author,
                "media": {"downloaded": downloaded, "failed": failed_media},
                "outputs": [
                    {"path": "original.html", "sha256": sha256_file(original)},
                    {"path": "article.md", "sha256": sha256_file(markdown)},
                ],
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        return {"ok": True, "job_id": job_id, "status": "completed", "job_dir": str(job_dir), "title": title}
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "completed_at": utc_now(),
                "error": {"code": getattr(exc, "code", "unexpected_error"), "message": str(exc)},
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        raise


def archive_article(url: str, root: Path) -> dict:
    safe_url = validate_https_url(url, ARTICLE_HOSTS)
    context = new_job(root, "article", safe_url)
    job_id, job_dir, manifest = context
    try:
        data, content_type, final_url = fetch_limited(safe_url, exact_hosts=ARTICLE_HOSTS, max_bytes=MAX_HTML_BYTES)
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "completed_at": utc_now(),
                "error": {"code": getattr(exc, "code", "unexpected_error"), "message": str(exc)},
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        raise
    if content_type not in {"text/html", "application/xhtml+xml"}:
        error = ArchiveError("unexpected_content_type", f"文章响应类型不是 HTML：{content_type}")
        manifest.update({"status": "failed", "completed_at": utc_now(), "error": {"code": error.code, "message": str(error)}})
        write_json(job_dir / "manifest.json", manifest)
        raise error

    def fetch_media(media_url: str) -> tuple[bytes, str, str]:
        return fetch_limited(
            media_url,
            exact_hosts=set(),
            suffixes=MEDIA_HOST_SUFFIXES,
            max_bytes=MAX_MEDIA_BYTES,
        )

    return archive_article_html(final_url, data, root, media_fetcher=fetch_media, job_context=(job_id, job_dir, manifest))


def submission_url(raw: str) -> tuple[str, str]:
    if not raw or len(raw) > 4096:
        raise ArchiveError("invalid_url", "URL 为空或过长。")
    parsed = _split_url(raw)
    host = (parsed.hostname or "").rstrip(".").lower()
    port = _safe_port(parsed)
    if parsed.scheme not in {"http", "https"} or not host:
        raise ArchiveError("invalid_url", "只接受 HTTP(S) 分享链接。")
    if parsed.username or parsed.password or port not in (None, 80, 443):
        raise ArchiveError("invalid_url", "URL 不得包含账号信息或非标准端口。")
    if host_allowed(host, XHS_WEB_HOSTS | XHS_SHORT_HOSTS, (".xiaohongshu.com", ".rednote.com")):
        platform = "xiaohongshu"
    elif host == "douyin.com" or host.endswith(".douyin.com"):
        platform = "douyin"
    elif host in WEIBO_WEB_HOSTS or host.endswith(".weibo.com"):
        platform = "weibo"
    else:
        raise ArchiveError("unsupported_url", f"暂不支持该链接主机：{host}")
    if parsed.scheme == "http" and host not in XHS_SHORT_HOSTS:
        raise ArchiveError("invalid_url", "该平台只接受 HTTPS 分享链接。")
    netloc = host if port in (None, 80, 443) else parsed.netloc.lower()
    path = parsed.path or "/"
    query = parsed.query
    if platform == "weibo":
        uid_match = re.fullmatch(r"/(?:u|profile)/(\d+)/?", path) or re.fullmatch(r"/p/100505(\d+)(?:/.*)?", path)
        if uid_match:
            return f"https://weibo.com/u/{uid_match.group(1)}", "weibo"
        if netloc == "www.weibo.com":
            netloc = "weibo.com"
    return urlunsplit(("https", netloc, path, query, "")), platform


def canonical_url(raw: str) -> str:
    parsed = _split_url(raw)
    host = (parsed.hostname or "").rstrip(".").lower()
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if host == "mp.weixin.qq.com":
        query_items = [(key, value) for key, value in query_items if key in OFFICIAL_ARTICLE_QUERY_KEYS]
    else:
        query_items = [(key, value) for key, value in query_items if key.lower() not in SENSITIVE_QUERY_KEYS]
    query = urlencode(query_items)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", query, ""))


def is_creator_profile_url(url: str, platform: str) -> bool:
    path = _split_url(url).path.rstrip("/")
    if platform == "douyin":
        return bool(re.fullmatch(r"/user/[^/]+", path))
    if platform == "xiaohongshu":
        return bool(re.fullmatch(r"/user/profile/[^/]+", path))
    if platform == "weibo":
        return bool(re.fullmatch(r"/(?:u|profile)/\d+", path) or re.fullmatch(r"/p/100505\d+(?:/.*)?", path))
    return False


def stable_content_id(platform: str, url: str) -> str:
    return hashlib.sha256(f"{platform}\0{canonical_url(url)}".encode()).hexdigest()[:16]


def submission_content_id(platform: str, url: str) -> str:
    host = (_split_url(url).hostname or "").lower()
    if host in {"v.douyin.com"} | XHS_SHORT_HOSTS:
        url = resolve_transparent_core_url(url, platform)
    patterns = {
        "xiaohongshu": r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})(?:[/?]|$)",
        "douyin": r"/video/(\d+)(?:[/?]|$)",
        "weibo": r"/(?:\d+|detail|status)/([0-9A-Za-z]+)(?:[/?]|$)",
    }
    match = re.search(patterns.get(platform, r"(?!)"), url)
    if match:
        content_id = match.group(1)
        if platform == "bilibili":
            page = dict(parse_qsl(_split_url(url).query)).get("p", "")
            if page.isdecimal() and int(page) > 1:
                return f"{content_id}:p{int(page)}"
        return content_id
    if platform == "wechat_official_account":
        return official_article_metadata(url)["content_id"]
    return stable_content_id(platform, url)


def existing_content(root: Path, platform: str, content_id: str, *, refresh_quality=False,
                     include_old_douyin=False) -> tuple[dict, Path] | None:
    # ponytail: linear scans fit local archives; add an index only when job counts reach the thousands.
    candidates: list[tuple[dict, Path]] = []
    for manifest_path in sorted((root / "jobs").glob("content-????????T??????Z-????????/manifest.json")):
        manifest = read_json_if_valid(manifest_path)
        if (not manifest or manifest.get("platform") != platform or manifest.get("status") == "failed"
                or manifest.get("media_ids") or manifest.get("non_highest")
                or (manifest.get("allow_lower_quality") and manifest.get("status") != "completed")):
            continue
        # Older XHS image-text children were completed with web/profile
        # derivatives.  Do not reuse them after the original-CDN pipeline was
        # introduced; a fresh child will upgrade the same note identity.
        if _xhs_image_child_needs_quality_upgrade(manifest):
            continue
        if (refresh_quality and manifest.get("status") == "completed"
                and manifest.get("media_quality_revision") != MEDIA_QUALITY_REVISION):
            continue
        if (platform == "douyin" and manifest.get("status") == "completed" and not include_old_douyin
                and manifest.get("douyin_clean_revision") != DOUYIN_CLEAN_REVISION):
            continue
        if (platform == "douyin" and manifest.get("status") == "completed" and not include_old_douyin
                and manifest.get("content_type") == "live_photo"
                and manifest.get("douyin_live_quality_revision") != DOUYIN_LIVE_QUALITY_REVISION):
            continue
        identities = {str(manifest.get("submission_id") or ""), str(manifest.get("content_id") or "")}
        if content_id in identities:
            candidates.append((manifest, manifest_path))
            continue
        source = str(manifest.get("source") or "")
        identities.add(submission_content_id(platform, source))
        if content_id in identities:
            candidates.append((manifest, manifest_path))
    if not candidates:
        return None
    # 【修复】completed 状态的 child 必须验证文件实际存在；否则返回 None 让 submit_content 重建
    for candidate in candidates:
        if candidate[0].get("status") == "completed":
            if _child_output_files_exist(root, candidate[0]):
                if platform == "douyin" and not include_old_douyin and not douyin_archive_matches(root, candidate[0]):
                    continue
                return candidate
            # 文件丢失 → 跳过这个 candidate，让 submit_content 重新生成新 child
            continue
    # 没有 completed 但有别的状态 → 用第一个
    if candidates:
        first = candidates[0]
        if first[0].get("status") != "completed":
            return first
    return None


_MEDIA_VALIDITY = {}
_MEDIA_VALIDITY_LOCK = threading.Lock()

def valid_media_file(path: Path, role: str) -> bool:
    try:
        stat = path.stat()
        if not path.is_file() or not stat.st_size:
            return False
        key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, role)
        with _MEDIA_VALIDITY_LOCK:
            if key in _MEDIA_VALIDITY:
                return _MEDIA_VALIDITY[key]
        if role == "image":
            from PIL import Image
            with Image.open(path) as img:
                img.verify()
            with Image.open(path) as img:
                img.load()
        else:
            ensure_video_readable(path)
        with _MEDIA_VALIDITY_LOCK:
            if len(_MEDIA_VALIDITY) > 4096:
                _MEDIA_VALIDITY.clear()
            _MEDIA_VALIDITY[key] = True
        return True
    except Exception:
        return False

def _child_output_files_exist(root: Path, child: dict, fallback_item: dict | None = None) -> bool:
    """验证必需媒体已完整落盘。

    子任务可能在媒体已经写入后、清理/转写/Live-photo 收尾阶段失败。此时
    ``status=failed`` 不能单独否定磁盘上的完整结果；同时，XHS 图文需要按
    预期图片/动态照片数量校验，避免把半成品误报成成功。
    """
    output_dir_rel = child.get("output_dir")
    # A completed legacy XHS image child still needs the one-time original-CDN
    # upgrade. Failed children are checked by their actual staged media below.
    if child.get("status") == "completed" and _xhs_image_child_needs_quality_upgrade(child):
        return False
    if str(child.get("platform") or "") == "xiaohongshu" and str(child.get("content_type") or "") == "image_text":
        if child.get("image_quality") == "preview_fallback":
            return False
    if not output_dir_rel:
        # Older failed manifests did not persist output_dir until finalization.
        # Recover it from a recorded media path or the frozen creator item.
        for record in child.get("outputs") or []:
            if not isinstance(record, dict) or record.get("role") not in {
                "video", "image", "live_motion", "live_photo_motion", "audio"
            }:
                continue
            rel = str(record.get("path") or "")
            if not rel:
                continue
            candidate = (root / rel).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue
            output_dir_rel = str(Path(rel).parent.parent if Path(rel).parent.name == "配图" else Path(rel).parent)
            break
    if not output_dir_rel and isinstance(fallback_item, dict):
        content_id = str(fallback_item.get("id") or fallback_item.get("content_id") or "")
        title = str(fallback_item.get("title") or "未命名小红书作品")
        if content_id and fallback_item.get("media_kind") in {"image_gallery", "video"}:
            platform_dir = PLATFORM_DIRS.get(str(child.get("platform") or ""))
            if platform_dir:
                output_dir_rel = archive_relative(
                    root,
                    root / "content" / platform_dir / f"{safe_content_title(title, '未命名作品')}--{content_id}",
                )
    if not output_dir_rel:
        return False
    root_resolved = root.resolve()
    out = (root_resolved / str(output_dir_rel)).resolve()
    try:
        out.relative_to(root_resolved)
    except ValueError:
        return False
    if not out.is_dir():
        return False

    def valid_output(record: dict, minimum: int) -> bool:
        rel = record.get("path")
        if not rel:
            return False
        candidate = (root_resolved / str(rel)).resolve()
        try:
            candidate.relative_to(root_resolved)
            return valid_media_file(candidate, "image" if record.get("role") == "image" else "video")
        except (ValueError, OSError):
            return False

    outputs = [record for record in child.get("outputs") or [] if isinstance(record, dict)]
    if child.get("media_ids"):
        layout = child.get("selected_media_roles") or {}
        return all(layout.get(media_id) and all(
            any(record.get("media_id") == media_id and record.get("role") == role and valid_output(record, 500)
                for record in outputs) for role in layout[media_id]) for media_id in child["media_ids"])
    media = child.get("douyin_media") or child.get("weibo_media") or child.get("x_media") or {}
    kind = str(media.get("kind") or child.get("content_type") or "")
    if kind == "video":
        videos = [record for record in outputs if record.get("role") == "video"]
        return any(valid_output(record, 1024) for record in videos) or (
            not child.get("author_folder") and valid_media_file(out / "video.mp4", "video")
        )
    if kind in {"image_gallery", "live_photo"}:
        frozen_items = [item for item in media.get("items") or [] if isinstance(item, dict)]
        expected_images = sum(bool(item.get("has_still") or item.get("image_urls")) for item in frozen_items)
        expected_motions = sum(bool(item.get("has_motion") or item.get("motion_urls")) for item in frozen_items)
        expected_videos = sum(bool(item.get("video_choices")) for item in frozen_items)
        actual_images = sum(
            valid_output(record, 500) for record in outputs if record.get("role") == "image"
        )
        actual_motions = sum(
            valid_output(record, 1024) for record in outputs if record.get("role") == "live_motion"
        )
        actual_videos = sum(
            valid_output(record, 1024) for record in outputs if record.get("role") == "video"
        )
        return (expected_images + expected_videos) > 0 and actual_images >= expected_images and actual_motions >= expected_motions and actual_videos >= expected_videos

    if str(child.get("platform") or "") == "xiaohongshu" and str(child.get("content_type") or "") == "image_text":
        images = [record for record in outputs if record.get("role") == "image"]
        expected_images = int(child.get("expected_image_count") or 0)
        if not expected_images and isinstance(fallback_item, dict):
            expected_images = len([url for url in (fallback_item.get("preview_urls") or []) if url])
        expected_images = max(expected_images, 1)
        if sum(valid_output(record, 500) for record in images) < expected_images:
            return False
        expected_motion = int(child.get("expected_live_motion_count") or 0)
        motions = [record for record in outputs if record.get("role") in {"live_motion", "live_photo_motion"}]
        return sum(valid_output(record, 1024) for record in motions) >= expected_motion

    # 其他平台沿用 outputs 记录验证，但正文、字幕和封面不能单独代表媒体下载完成。
    media_roles = {"video", "image", "live_motion", "audio"}
    return any(
        record.get("role") in media_roles and valid_output(record, 500)
        for record in outputs
    )


def _xhs_image_child_needs_quality_upgrade(child: dict) -> bool:
    """Return whether an XHS image-text child predates original-CDN output.

    The marker is deliberately narrow: XHS videos and Live-photo motion
    downloads retain their existing reuse semantics, while image-text notes
    without an explicit ``original_cdn`` marker are reprocessed once.
    """
    if not isinstance(child, dict) or child.get("platform") != "xiaohongshu":
        return False
    content_type = str(child.get("content_type") or "").strip().lower()
    if content_type != "image_text":
        return False
    if child.get("image_quality") not in {"original_cdn", "full_size_render", "api_fallback"}:
        return True
    # The first original-CDN build could receive application/octet-stream and
    # save valid WebP bytes as .bin.  Treat those outputs as format-stale once
    # so the current build can sniff the bytes and leave a normal image file.
    return any(
        str(record.get("role") or "") == "image"
        and Path(str(record.get("path") or "")).suffix.lower() == ".bin"
        for record in (child.get("outputs") or [])
        if isinstance(record, dict)
    )


def official_article_id(biz: str, mid: str, idx: str, sn: str) -> str:
    return hashlib.sha256(f"wechat_official_account\0{biz}\0{mid}\0{idx}\0{sn}".encode()).hexdigest()[:16]


def official_article_metadata(raw_url: str, html_bytes: bytes | None = None) -> dict:
    text = html_bytes.decode("utf-8", errors="replace") if html_bytes else ""

    def value(name: str) -> str:
        parsed = dict(parse_qsl(urlsplit(html.unescape(raw_url)).query, keep_blank_values=True))
        query_name = "__biz" if name == "biz" else name
        if parsed.get(query_name):
            return parsed[query_name]
        match = re.search(rf"var\s+{name}\s*=\s*['\"]([^'\"]+)['\"]", text)
        return html.unescape(match.group(1)) if match else ""

    biz, mid, idx, sn = (value(name) for name in ("biz", "mid", "idx", "sn"))
    if all((biz, mid, idx, sn)):
        article_url = urlunsplit(
            (
                "https",
                "mp.weixin.qq.com",
                "/s",
                urlencode({"__biz": biz, "mid": mid, "idx": idx, "sn": sn}),
                "",
            )
        )
        content_id = official_article_id(biz, mid, idx, sn)
    else:
        article_url = canonical_url(raw_url)
        content_id = stable_content_id("wechat_official_account", article_url)
    nickname = ""
    match = re.search(r"var\s+nickname\s*=\s*htmlDecode\(['\"]([^'\"]*)['\"]\)", text)
    if match:
        nickname = html.unescape(match.group(1))
    published_at = None
    publish_match = re.search(r"var\s+(?:ct|publish_time)\s*=\s*['\"]?(\d{10})['\"]?", text)
    if publish_match:
        published_at = datetime.fromtimestamp(int(publish_match.group(1)), timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "biz": biz,
        "account_id": hashlib.sha256(f"wechat_official_account\0{biz}".encode()).hexdigest()[:16] if biz else "",
        "account_name": nickname,
        "content_id": content_id,
        "canonical_url": article_url,
        "published_at": published_at,
    }


def archive_relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def output_record(root: Path, path: Path, role: str) -> dict:
    return {"role": role, "path": archive_relative(root, path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def safe_content_title(value: str, fallback: str) -> str:
    # Windows forbids these characters in file and directory names.  The
    # previous sanitizer omitted ``*?"<>|``, so a perfectly ordinary XHS title
    # could fail late with WinError 123 after its media had downloaded.
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value).strip(" .") or fallback
    while len(cleaned.encode("utf-8")) > 180:
        cleaned = cleaned[:-1]
    return cleaned or fallback


def official_account_dir(account: dict) -> str:
    name = safe_content_title(str(account.get("name") or "未知公众号"), "未知公众号")
    account_id = str(account.get("account_id") or "")
    if not account_id and account.get("biz"):
        account_id = hashlib.sha256(f"wechat_official_account\0{account['biz']}".encode()).hexdigest()[:16]
    account_id = safe_content_title(account_id, "")
    if not account_id:
        raise ArchiveError("official_account_identity_missing", "公众号缺少稳定账号标识，已停止落盘。", 65)
    return f"{name}--{account_id}"


def submit_content(url: str, root: Path, *, refresh_quality=False, fresh=False) -> dict:
    normalized, platform = submission_url(url)
    if is_creator_profile_url(normalized, platform):
        return inspect_creator(normalized, root)
    safe_source = canonical_url(normalized)
    content_id = submission_content_id(platform, safe_source)
    jobs_dir = root / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = jobs_dir / ".content-submit.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        prior = None if fresh else (existing_content(root, platform, content_id, refresh_quality=True)
                                    if refresh_quality else existing_content(root, platform, content_id))
        if prior:
            manifest, manifest_path = prior
            return {
                "ok": True,
                "job_id": manifest["job_id"],
                "status": manifest["status"],
                "platform": platform,
                "manifest": archive_relative(root, manifest_path),
                "reused": True,
            }
        job_id, job_dir, manifest = new_job(root, "content", safe_source)
        manifest.update(
            {
                "status": "queued",
                "updated_at": utc_now(),
                "platform": platform,
                "submission_id": content_id,
                "content_id": content_id,
                "content_type": "article" if platform == "wechat_official_account" else "video",
                "canonical_url": safe_source,
                "route": None,
                "output_dir": None,
            }
        )
        if platform == "wechat_channels":
            manifest["auto_resume"] = True
        if refresh_quality:
            for old_path in sorted(jobs_dir.glob("content-*/manifest.json"), reverse=True):
                old = read_json_if_valid(old_path) or {}
                if (old.get("platform") == platform and old.get("status") == "completed"
                        and content_id in {old.get("content_id"), old.get("submission_id")}):
                    manifest["work_file_prefix"] = old.get("work_file_prefix")
                    manifest["previous_output_dir"] = old.get("output_dir") if not old.get("author_folder") else None
                    break
        manifest_path = job_dir / "manifest.json"
        write_json(manifest_path, manifest)
        return {
            "ok": True,
            "job_id": job_id,
            "status": "queued",
            "platform": platform,
            "manifest": archive_relative(root, manifest_path),
            "reused": False,
        }
    finally:
        os.close(descriptor)


def frozen_channel_child_job_id(parent_job_id: str, item: dict) -> str:
    match = re.fullmatch(r"batch-(\d{8}T\d{6}Z)-[0-9a-f]{8}", parent_job_id)
    if not match:
        raise ArchiveError("invalid_job_id", "父任务 Job ID 格式错误。")
    identity = f"{parent_job_id}\0{item.get('id')}".encode()
    return f"content-{match.group(1)}-{hashlib.sha256(identity).hexdigest()[:8]}"


def submit_frozen_channel_content(item: dict, root: Path, parent_job_id: str) -> dict:
    normalized, platform = submission_url(str(item["url"]))
    if platform != "wechat_channels":
        raise ArchiveError("invalid_channels_url", "冻结的视频号子任务必须使用视频号链接。")
    safe_source = canonical_url(normalized)
    frozen = item.get("payload") or {}
    if not isinstance(frozen, dict) or not frozen:
        raise ArchiveError("channel_inventory_invalid", "冻结的视频号作品对象缺失。", 69)
    job_id = frozen_channel_child_job_id(parent_job_id, item)
    job_dir = root / "jobs" / job_id
    manifest_path = job_dir / "manifest.json"
    existing = read_json_if_valid(manifest_path) if manifest_path.is_file() else None
    if existing:
        if existing.get("parent_job_id") != parent_job_id or str(existing.get("content_id")) != str(item["id"]):
            raise ArchiveError("creator_child_identity_conflict", "冻结子任务身份冲突。", 69)
        return {
            "ok": True,
            "job_id": job_id,
            "status": existing.get("status"),
            "platform": "wechat_channels",
            "manifest": archive_relative(root, manifest_path),
        }
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "tool_version": VERSION,
        "job_id": job_id,
        "kind": "content",
        "status": "staged",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "source": safe_source,
        "outputs": [],
        "platform": "wechat_channels",
        "content_id": str(item["id"]),
        "content_type": "video",
        "canonical_url": safe_source,
        "title": str(item["title"]),
        "route": "wechat_channels_backend",
        "output_dir": None,
        "auto_resume": True,
        "channel_object": frozen,
        "parent_job_id": parent_job_id,
    }
    write_json(manifest_path, manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": "staged",
        "platform": "wechat_channels",
        "manifest": archive_relative(root, manifest_path),
    }


def activate_staged_channel_content(manifest_path: Path, parent_job_id: str) -> dict:
    lock_path = manifest_path.with_suffix(".json.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        manifest = read_json_if_valid(manifest_path) or {}
        if manifest.get("parent_job_id") != parent_job_id:
            raise ArchiveError("creator_child_identity_conflict", "冻结子任务父任务身份冲突。", 69)
        if manifest.get("status") == "staged":
            manifest.update({"status": "downloading", "updated_at": utc_now()})
            write_json(manifest_path, manifest)
        return manifest
    finally:
        os.close(descriptor)


def adopt_legacy_frozen_channel_content(
    manifest_path: Path,
    item: dict,
    parent_job_id: str,
) -> dict:
    lock_path = manifest_path.with_suffix(".json.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        manifest = read_json_if_valid(manifest_path) or {}
        frozen = item.get("payload") or {}
        if (
            manifest.get("platform") != "wechat_channels"
            or str(manifest.get("content_id") or "") != str(item.get("id") or "")
            or not isinstance(frozen, dict)
            or not frozen
        ):
            raise ArchiveError("creator_child_identity_conflict", "旧版冻结子任务身份冲突。", 69)
        existing_parent = manifest.get("parent_job_id")
        if existing_parent and existing_parent != parent_job_id:
            raise ArchiveError("creator_child_identity_conflict", "旧版冻结子任务父任务身份冲突。", 69)
        submitted = bool(manifest.get("upstream_task_ids"))
        manifest.update(
            {
                "parent_job_id": parent_job_id,
                "channel_object": frozen,
                "title": str(item.get("title") or manifest.get("title") or "未命名视频"),
                "route": "wechat_channels_backend",
                "status": manifest.get("status") if submitted else "downloading",
                "updated_at": utc_now(),
            }
        )
        write_json(manifest_path, manifest)
        return manifest
    finally:
        os.close(descriptor)


def submit_official_batch(url: str, root: Path) -> dict:
    normalized, platform = submission_url(url)
    if platform != "wechat_official_account":
        raise ArchiveError("invalid_official_account_url", "公众号历史批次只接受公众号文章链接。")
    job_id, job_dir, manifest = new_job(root, "batch", canonical_url(normalized))
    manifest.update(
        {
            "status": "queued",
            "updated_at": utc_now(),
            "platform": platform,
            "account": {"name": "", "account_id": ""},
            "pagination": {"pages": 0, "next_offset": 0, "can_continue": True, "complete": False},
            "counts": {
                "discovered": 0,
                "submitted": 0,
                "skipped_existing": 0,
                "processing": 0,
                "completed": 0,
                "unavailable": 0,
                "failed": 0,
            },
            "items": [],
        }
    )
    manifest_path = job_dir / "manifest.json"
    write_json(manifest_path, manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "platform": platform,
        "manifest": archive_relative(root, manifest_path),
    }


def finalize_official_article(manifest: dict, manifest_path: Path, root: Path, final_url: str) -> dict:
    job_dir = manifest_path.parent
    title = str(manifest.get("title") or "未命名公众号文章")
    content_id = str(manifest["content_id"])
    account = manifest.get("account") if isinstance(manifest.get("account"), dict) else {}
    output_dir = (
        root
        / "content"
        / PLATFORM_DIRS["wechat_official_account"]
        / official_account_dir(account)
        / f"{safe_content_title(title, '未命名公众号文章')}--{content_id}"
    )
    image_dir = output_dir / "配图"
    output_dir.mkdir(parents=True, exist_ok=True)

    original = output_dir / "original.html"
    body = output_dir / "正文.md"
    os.replace(job_dir / "original.html", original)
    markdown = (job_dir / "article.md").read_text(encoding="utf-8").replace("./media/", "./配图/")
    atomic_write(body, markdown.encode())
    (job_dir / "article.md").unlink()

    outputs = [output_record(root, original, "original_html"), output_record(root, body, "body_markdown")]
    source_images = job_dir / "media"
    if source_images.is_dir():
        image_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(source_images.iterdir()):
            target = image_dir / source.name
            os.replace(source, target)
            outputs.append(output_record(root, target, "image"))
        source_images.rmdir()

    manifest.pop("media", None)
    manifest.pop("error", None)
    manifest.pop("last_retry_error", None)
    manifest.pop("official_fetch_retries", None)
    manifest.update(
        {
            "status": "completed",
            "updated_at": utc_now(),
            "completed_at": utc_now(),
            "platform": "wechat_official_account",
            "content_type": "article",
            "canonical_url": canonical_url(final_url),
            "route": "wechat_article_html",
            "output_dir": archive_relative(root, output_dir),
            "outputs": outputs,
        }
    )
    write_json(manifest_path, manifest)
    return manifest


def process_official_article(manifest: dict, manifest_path: Path, root: Path) -> dict:
    safe_url = validate_https_url(str(manifest["source"]), ARTICLE_HOSTS)
    data, content_type, final_url = fetch_official_article_with_session(safe_url)
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ArchiveError("unexpected_content_type", f"文章响应类型不是 HTML：{content_type}")
    article = official_article_metadata(final_url, data)
    manifest.update({"content_id": article["content_id"], "canonical_url": article["canonical_url"]})
    account = manifest.get("account") if isinstance(manifest.get("account"), dict) else {}
    manifest["account"] = {
        "name": str(account.get("name") or article.get("account_name") or ""),
        "account_id": str(account.get("account_id") or article.get("account_id") or ""),
    }
    if not manifest.get("published_at") and article.get("published_at"):
        manifest["published_at"] = article["published_at"]
    write_json(manifest_path, manifest)

    def fetch_media(media_url: str) -> tuple[bytes, str, str]:
        return fetch_limited(media_url, exact_hosts=set(), suffixes=MEDIA_HOST_SUFFIXES, max_bytes=MAX_MEDIA_BYTES)

    archive_article_html(
        article["canonical_url"],
        data,
        root,
        media_fetcher=fetch_media,
        job_context=(str(manifest["job_id"]), manifest_path.parent, manifest),
    )
    if (manifest.get("media") or {}).get("failed"):
        raise ArchiveError("article_media_incomplete", "公众号文章存在未取得配图，内容包未完成。", 69)
    return finalize_official_article(manifest, manifest_path, root, article["canonical_url"])


def pause_official_parent(manifest: dict, root: Path, exc: ArchiveError) -> None:
    parent_id = str(manifest.get("parent_job_id") or "")
    if not JOB_ID_RE.fullmatch(parent_id) or not parent_id.startswith("batch-"):
        return
    parent_path = root / "jobs" / parent_id / "manifest.json"
    descriptor = acquire_creator_batch_lock(parent_path)
    if descriptor is None:
        return
    try:
        parent = read_json_if_valid(parent_path)
        if not parent or parent.get("kind") != "batch" or parent.get("platform") != "wechat_official_account":
            return
        backend_unavailable = exc.code == "channels_backend_unavailable"
        parent.update(
            {
                "status": "waiting_for_authorization" if backend_unavailable else "waiting_for_reauthentication",
                "updated_at": utc_now(),
                "next_action": (
                    "公众号本地会话后端不可用，请先运行安装状态检查并恢复本地服务，再继续同一 Job。"
                    if backend_unavailable
                    else "请在 Mac 微信中手动打开任意一篇该公众号文章，会话恢复后继续同一 Job。"
                ),
                "last_retry_error": {"code": exc.code, "message": str(exc)},
            }
        )
        parent.pop("completed_at", None)
        parent.pop("error", None)
        write_json(parent_path, parent)
    finally:
        os.close(descriptor)

def _process_content_job_unlocked(
    manifest_path: Path,
    root: Path,
    deadline: float | None = None,
    start_only: bool = False,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"queued", "downloading", "transcribing"}:
        return manifest
    manifest.update({"status": "downloading", "updated_at": utc_now()})
    write_json(manifest_path, manifest)
    try:
        if manifest.get("platform") == "wechat_channels":
            if start_only and manifest.get("upstream_task_ids"):
                return manifest
            return process_channel_content(manifest, manifest_path, root, deadline=deadline)
        if manifest.get("platform") == "wechat_official_account":
            return process_official_article(manifest, manifest_path, root)
        if manifest.get("media_ids"):
            return process_selected_media(manifest, manifest_path, root)
        if manifest.get("platform") == "xiaohongshu":
            return process_xiaohongshu(manifest, manifest_path, root)
        if manifest.get("platform") == "douyin":
            return process_transparent_video(manifest, manifest_path, root)
        if manifest.get("platform") == "weibo":
            return process_weibo_work(manifest, manifest_path, root)
        raise ArchiveError("platform_not_implemented", f"平台尚未接入 worker：{manifest.get('platform')}", 69)
    except Exception as exc:
        platform = str(manifest.get("platform"))
        try:
            operation_wait()
        except TimeoutError:
            manifest["status"] = "queued"
            write_json(manifest_path, manifest)
            raise
        if manifest.get("guest_only") and isinstance(exc, ArchiveError) and exc.code == "reauthentication_required":
            exc = ArchiveError("guest_media_unavailable", "平台当前未向访客提供这个作品的下载媒体。", 69)
        if (
            platform == "wechat_official_account"
            and isinstance(exc, ArchiveError)
            and exc.code == "official_article_fetch_failed"
        ):
            retries = int(manifest.get("official_fetch_retries") or 0) + 1
            manifest["official_fetch_retries"] = retries
            if retries <= OFFICIAL_FETCH_MAX_RETRIES:
                manifest.update(
                    {
                        "status": "queued",
                        "updated_at": utc_now(),
                        "last_retry_error": {"code": exc.code, "message": str(exc)},
                    }
                )
                write_json(manifest_path, manifest)
                return manifest
        official_session_error = (
            platform == "wechat_official_account"
            and isinstance(exc, ArchiveError)
            and exc.code in OFFICIAL_SESSION_ERROR_CODES
        )
        official_backend_unavailable = (
            platform == "wechat_official_account"
            and isinstance(exc, ArchiveError)
            and exc.code == "channels_backend_unavailable"
        )
        channels_transient = isinstance(exc, ArchiveError) and exc.code in {
            "channels_backend_unavailable",
            "channels_backend_error",
            "channels_share_resolve_failed",
            "channels_task_create_failed",
        }
        recovery_expired = deadline is not None and isinstance(exc, ArchiveError) and exc.code == "recovery_window_expired"
        if isinstance(exc, ArchiveError) and (
            exc.code in {"reauthentication_required", "channels_authorization_required"}
            or official_session_error
            or channels_transient
            or recovery_expired
        ):
            cookie_jar = platform_cookie_jar(platform)
            channels = platform == "wechat_channels"
            if official_session_error:
                pause_official_parent(manifest, root, exc)
            manifest.update(
                {
                    "status": (
                        "waiting_for_authorization"
                        if official_backend_unavailable
                        else "waiting_for_reauthentication" if platform == "wechat_official_account"
                        else "waiting_for_authorization" if channels or not cookie_jar.is_file() else "waiting_for_reauthentication"
                    ),
                    "updated_at": utc_now(),
                    "next_action": (
                        (
                            "视频号会话当前不可用，原任务已保留。请等用户方便使用 Mac 时，由用户在 Mac 微信中手动打开下面的原链接并停留 10 秒；后台检测到会话恢复后会继续同一 Job：\n\n"
                            f"{manifest.get('source')}"
                        )
                        if channels
                        else (
                            (
                                "公众号本地会话后端不可用，原任务已保留。请先运行安装状态检查并恢复本地服务，再继续同一 Job。"
                                if official_backend_unavailable
                                else "公众号会话当前不可用，原任务已保留。请在 Mac 微信中手动打开任意一篇该公众号文章，会话恢复后继续同一 Job。"
                            )
                            if platform == "wechat_official_account"
                            else "请在所选 Firefox、Chrome 或 Edge 中重新登录，关闭浏览器后重新导入 Cookie，再继续原任务。"
                        )
                    ),
                }
            )
            manifest.pop("completed_at", None)
            manifest.pop("failed_stage", None)
            manifest["last_retry_error"] = {"code": exc.code, "message": str(exc)}
            write_json(manifest_path, manifest)
            return manifest
        logging.exception("content failed job=%s platform=%s code=%s", manifest.get("job_id"), platform, getattr(exc, "code", "unexpected_error"))
        code = getattr(exc, "code", "unexpected_error")
        attempts = int(manifest.get("auto_attempts", 0))
        transient = code in {"network_timeout", "rate_limited", "upstream_unavailable", "xiaohongshu_detail_timeout", "xiaohongshu_video_failed", "media_download_failed"}
        if transient and attempts < 2:
            import random
            if platform == "xiaohongshu":
                invalidate_redbook_cache()
            manifest.update(status="queued", auto_attempts=attempts + 1,
                            retry_at=time.time() + max(getattr(exc, "retry_after", 0), 2 ** (attempts + 1) + random.random()),
                            last_retry_error={"code": code, "message": str(exc)}, updated_at=utc_now())
            write_json(manifest_path, manifest)
            return manifest
        failed_stage = "transcription" if manifest.get("status") == "transcribing" else "downloading"
        unavailable = (
            isinstance(exc, ArchiveError) and exc.code in {"article_not_found", "media_unavailable"}
        )
        manifest.update(
            {
                "status": "unavailable" if unavailable else "failed",
                "updated_at": utc_now(),
                "completed_at": utc_now(),
                "failed_stage": failed_stage,
                "error": {"code": getattr(exc, "code", "unexpected_error"), "message": str(exc)},
            }
        )
        write_json(manifest_path, manifest)
        return manifest

def process_content_job(
    manifest_path: Path,
    root: Path,
    resume_waiting: bool = False,
    resume_delivery: bool = False,
    deadline: float | None = None,
    start_only: bool = False,
) -> dict:
    lock_path = manifest_path.with_suffix(".json.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return read_json_if_valid(manifest_path) or {}
        if resume_waiting:
            manifest = read_json_if_valid(manifest_path) or {}
            if manifest.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}:
                manifest.update({"status": "queued", "updated_at": utc_now()})
                manifest.pop("next_action", None)
                manifest.pop("error", None)
                write_json(manifest_path, manifest)
        if resume_delivery:
            manifest = read_json_if_valid(manifest_path) or {}
            recoverable = (
                manifest.get("platform") == "wechat_channels"
                and manifest.get("status") == "failed"
                and (manifest.get("error") or {}).get("code") == "channel_video_missing"
            )
            if not recoverable or len(channel_local_video_candidates(manifest, manifest_path, root)) != 1:
                return manifest
            manifest.update(
                {
                    "status": "downloading",
                    "updated_at": utc_now(),
                    "channel_delivery_recovery": True,
                }
            )
            manifest.pop("completed_at", None)
            manifest.pop("failed_stage", None)
            manifest.pop("error", None)
            write_json(manifest_path, manifest)
        result = _process_content_job_unlocked(manifest_path, root, deadline=deadline, start_only=start_only)
        if result.get("status") == "completed" and result.get("platform") in {"douyin", "xiaohongshu"}:
            if not _child_output_files_exist(root, result):
                result.update(status="failed", failed_stage="verification",
                              error={"code": "invalid_media", "message": "媒体缺失、数量不齐或无法解码，请重试该作品。"})
                write_json(manifest_path, result)
        return result
    finally:
        os.close(descriptor)


def parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json_if_valid(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def requeue_waiting_content_job(manifest_path: Path, frozen_object: dict | None = None) -> bool:
    lock_path = manifest_path.with_suffix(".json.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        manifest = read_json_if_valid(manifest_path) or {}
        if manifest.get("platform") != "wechat_channels" or manifest.get("status") not in {
            "waiting_for_authorization",
            "waiting_for_reauthentication",
        }:
            return False
        if not manifest.get("channel_object"):
            if not isinstance(frozen_object, dict) or not frozen_object:
                return False
            manifest["channel_object"] = frozen_object
        manifest.update({"status": "queued", "updated_at": utc_now()})
        manifest.pop("next_action", None)
        manifest.pop("error", None)
        write_json(manifest_path, manifest)
        return True
    finally:
        os.close(descriptor)


def _resume_creator_batch_children_unlocked(manifest: dict, manifest_path: Path, root: Path) -> tuple[dict, int]:
    resumed = 0
    items_by_child = {
        str(item.get("child_job_id")): item
        for item in ((manifest.get("inventory") or {}).get("items") or [])
        if item.get("child_job_id")
    }
    for child_id in manifest.get("child_job_ids") or []:
        child_path = root / "jobs" / str(child_id) / "manifest.json"
        item = items_by_child.get(str(child_id)) or {}
        frozen = item.get("payload") if isinstance(item, dict) else None
        if requeue_waiting_content_job(child_path, frozen_object=frozen):
            resumed += 1
    if resumed:
        manifest.pop("next_action", None)
        manifest.pop("error", None)
        manifest.pop("session_retry_after", None)
    return _refresh_creator_batch_unlocked(manifest, manifest_path, root), resumed


def resume_creator_batch_children(manifest: dict, manifest_path: Path, root: Path) -> tuple[dict, int]:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or manifest, 0
    try:
        current = read_json_if_valid(manifest_path) or manifest
        return _resume_creator_batch_children_unlocked(current, manifest_path, root)
    finally:
        os.close(descriptor)


def _resume_waiting_channel_creators_unlocked(
    root: Path,
    retry_interval: int = 900,
    ignore_retry_after: bool = False,
    deadline: float | None = None,
) -> int:
    """Low-frequency, non-UI retry for the waiting creator backlog.

    The retry only calls the existing local Channels backend. It never opens or
    controls WeChat. When a short realtime window becomes available, every due
    task is advanced in the same pass. Probing stops as soon as that realtime
    window is unavailable again.
    """
    if retry_interval < 60:
        raise ArchiveError("invalid_retry_interval", "视频号会话重试间隔不能小于 60 秒。")
    now = datetime.now(timezone.utc)
    resumed_total = 0
    manifest_paths = list((root / "jobs").glob("batch-????????T??????Z-????????/manifest.json"))

    def recovery_priority(path: Path) -> tuple[int, str]:
        candidate = read_json_if_valid(path) or {}
        frozen = bool(candidate.get("selection") or candidate.get("child_job_ids"))
        return (0 if frozen else 1, str(path))

    for manifest_path in sorted(manifest_paths, key=recovery_priority):
        if deadline is not None and time.monotonic() >= deadline:
            break
        descriptor = acquire_creator_batch_lock(manifest_path)
        if descriptor is None:
            continue
        try:
            manifest = read_json_if_valid(manifest_path)
            if not manifest:
                continue
            if (
                manifest.get("kind") != "creator_batch"
                or manifest.get("platform") != "wechat_channels"
                or manifest.get("status") not in {"waiting_for_authorization", "waiting_for_reauthentication"}
            ):
                continue
            retry_after = parse_utc_timestamp(manifest.get("session_retry_after"))
            if not ignore_retry_after and retry_after is not None and retry_after > now:
                continue
            source = str(manifest.get("source") or "")
            selected = bool(manifest.get("selection") or manifest.get("child_job_ids"))
            if not selected and not source:
                continue
            next_retry = (now + timedelta(seconds=retry_interval)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            try:
                manifest["session_retry_after"] = next_retry
                write_json(manifest_path, manifest)
                if selected:
                    _, resumed_children = _resume_creator_batch_children_unlocked(manifest, manifest_path, root)
                    if resumed_children:
                        resumed_total += 1
                    continue
                result = _inspect_channel_creator_unlocked(
                    source,
                    root,
                    existing_job=(str(manifest.get("job_id") or manifest_path.parent.name), manifest_path.parent, manifest),
                    deadline=deadline,
                )
            except Exception as exc:
                try:
                    refreshed = read_json_if_valid(manifest_path) or manifest
                    refreshed["session_retry_after"] = next_retry
                    refreshed["last_retry_error"] = {
                        "code": getattr(exc, "code", "unexpected_error"),
                        "message": str(exc),
                    }
                    write_json(manifest_path, refreshed)
                except OSError:
                    pass
                return resumed_total
            if result.get("status") in {
                "awaiting_download_count",
                "submitting",
                "processing",
                "completed",
                "completed_with_failures",
            }:
                resumed_total += 1
                continue
            refreshed = read_json_if_valid(manifest_path) or manifest
            refreshed["session_retry_after"] = next_retry
            write_json(manifest_path, refreshed)
            return resumed_total
        finally:
            os.close(descriptor)
    return resumed_total


def resume_waiting_channel_creators_once(
    root: Path,
    retry_interval: int = 900,
    ignore_retry_after: bool = False,
    deadline: float | None = None,
) -> int:
    lock_path = root / "state" / "channels-recovery.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_path.parent, 0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if ignore_retry_after:
                raise ArchiveError("recovery_window_busy", "已有视频号恢复流程正在处理任务，请稍后重试。", 75)
            return 0
        return _resume_waiting_channel_creators_unlocked(root, retry_interval, ignore_retry_after, deadline)
    finally:
        os.close(descriptor)


def resume_waiting_channel_content(root: Path, deadline: float | None = None) -> int:
    resumed = 0
    pattern = "content-????????T??????Z-????????/manifest.json"
    for manifest_path in sorted((root / "jobs").glob(pattern)):
        if deadline is not None and time.monotonic() >= deadline:
            break
        manifest = read_json_if_valid(manifest_path)
        if not manifest or manifest.get("platform") != "wechat_channels":
            continue
        waiting = manifest.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}
        frozen_unsubmitted = (
            manifest.get("status") in {"queued", "downloading"}
            and bool(manifest.get("channel_object"))
            and not manifest.get("upstream_task_ids")
        )
        if not waiting and not frozen_unsubmitted:
            continue
        result = process_content_job(
            manifest_path,
            root,
            resume_waiting=True,
            deadline=deadline,
            start_only=True,
        )
        if result.get("status") in {"downloading", "transcribing", "completed"}:
            resumed += 1
    return resumed


def recover_channel_session(
    root: Path,
    timeout: int = 300,
    poll_interval: int = 5,
    started_at: float | None = None,
    cleanup_reserve: int = 0,
) -> dict:
    """Wait for one user-opened Channels page, then drain the waiting backlog."""
    if timeout < 10 or timeout > 900:
        raise ArchiveError("invalid_recovery_timeout", "视频号恢复窗口必须在 10 到 900 秒之间。")
    if poll_interval < 1 or poll_interval > 10:
        raise ArchiveError("invalid_recovery_poll_interval", "视频号恢复检测间隔必须在 1 到 10 秒之间。")
    if cleanup_reserve < 0 or cleanup_reserve >= timeout:
        raise ArchiveError("invalid_cleanup_reserve", "恢复窗口清理预留时间无效。")
    elapsed = max(0.0, time.time() - started_at) if started_at is not None else 0.0
    remaining_budget = timeout - cleanup_reserve - elapsed
    if remaining_budget <= 0:
        return {
            "ok": False,
            "platform": "wechat_channels",
            "status": "recovery_window_expired",
            "resumed_creator_batches": 0,
            "resumed_content_jobs": 0,
        }
    deadline = time.monotonic() + remaining_budget
    probe_keyword = f"__hermes_session_probe_{uuid.uuid4().hex}__"
    while True:
        status = channel_session_status(root, probe_keyword=probe_keyword, deadline=deadline)
        if status.get("ok"):
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "platform": "wechat_channels",
                    "status": "recovery_window_expired",
                    "resumed_creator_batches": 0,
                    "resumed_content_jobs": 0,
                }
            resumed_batches = resume_waiting_channel_creators_once(
                root,
                retry_interval=60,
                ignore_retry_after=True,
                deadline=deadline,
            )
            resumed_content = resume_waiting_channel_content(root, deadline=deadline)
            if time.monotonic() >= deadline:
                return {
                    "ok": False,
                    "platform": "wechat_channels",
                    "status": "recovery_window_expired",
                    "resumed_creator_batches": resumed_batches,
                    "resumed_content_jobs": resumed_content,
                }
            return {
                "ok": True,
                "platform": "wechat_channels",
                "status": "realtime_window_ready",
                "resumed_creator_batches": resumed_batches,
                "resumed_content_jobs": resumed_content,
                "last_realtime_ready_at": status.get("last_realtime_ready_at"),
            }
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "platform": "wechat_channels",
                "status": "recovery_window_expired",
                "resumed_creator_batches": 0,
                "resumed_content_jobs": 0,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        time.sleep(min(poll_interval, remaining))


def refresh_creator_parent_for_child(child: dict, root: Path) -> bool:
    if child.get("status") != "completed":
        return False
    parent_id = str(child.get("parent_job_id") or "")
    if not JOB_ID_RE.fullmatch(parent_id) or not parent_id.startswith("batch-"):
        return False
    parent_path = root / "jobs" / parent_id / "manifest.json"
    parent = read_json_if_valid(parent_path)
    if not parent or parent.get("kind") != "creator_batch" or parent.get("platform") != "wechat_channels":
        return False
    refreshed = refresh_creator_batch(parent, parent_path, root)
    return refreshed.get("status") == "completed"


def reconcile_terminal_creator_batches_once(root: Path) -> int:
    jobs_root = root / "jobs"
    for parent_path in sorted(jobs_root.glob("batch-????????T??????Z-????????/manifest.json")):
        parent = read_json_if_valid(parent_path)
        if not parent or parent.get("kind") != "creator_batch" or parent.get("status") != "completed_with_failures":
            continue
        needs_refresh = False
        for item in (parent.get("inventory") or {}).get("items") or []:
            if item.get("error_code") != "channel_video_missing" or not item.get("child_job_id"):
                continue
            child_path = jobs_root / str(item["child_job_id"]) / "manifest.json"
            child = read_json_if_valid(child_path)
            if child and child.get("status") == "completed":
                needs_refresh = True
                break
        if not needs_refresh:
            continue
        refreshed = refresh_creator_batch(parent, parent_path, root)
        if refreshed.get("status") == "completed":
            return 1
    return 0


def content_worker_once(root: Path, max_parallel: int | None = None) -> tuple[dict, bool]:
    """Run one worker pass.

    The CLI keeps the historical serial default. The local GUI may opt into a
    bounded pool because every content manifest owns its own directory and
    lock. Capping the pool avoids turning a creator batch into an unbounded
    burst that could trigger platform throttling or overload a mechanical disk.
    """
    if max_parallel is None:
        max_parallel = os.environ.get("WECHAT_ARCHIVE_DOWNLOAD_CONCURRENCY", "1")
    try:
        max_parallel = max(1, min(int(max_parallel), 8))
    except (TypeError, ValueError):
        max_parallel = 1
    jobs_root = root / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(jobs_root, 0o700)
    windows_without_wechat = os.name == "nt" and not windows_experimental_wechat_enabled()
    resumed_creators = 0 if windows_without_wechat else resume_waiting_channel_creators_once(root)
    reconciled_creators = 0 if windows_without_wechat else reconcile_terminal_creator_batches_once(root)
    if not windows_without_wechat:
        progress_channel_jobs_once(root)
    for manifest_path in sorted(jobs_root.glob("batch-????????T??????Z-????????/manifest.json")):
        manifest = read_json_if_valid(manifest_path)
        if not manifest:
            continue
        if windows_without_wechat and manifest.get("platform") in WINDOWS_UNSUPPORTED_PLATFORMS:
            continue
        if manifest.get("kind") == "creator_batch" and manifest.get("status") in {
            "submitting",
            "processing",
            "waiting_for_authorization",
            "waiting_for_reauthentication",
        }:
            if manifest.get("status") == "submitting":
                submit_creator_batch_children(manifest, manifest_path, root)
            else:
                refresh_creator_batch(manifest, manifest_path, root)
    processed = bool(resumed_creators or reconciled_creators)
    waiting_official_parents = set() if windows_without_wechat else {
        str(parent.get("job_id"))
        for parent_path in jobs_root.glob("batch-????????T??????Z-????????/manifest.json")
        if (parent := read_json_if_valid(parent_path))
        and parent.get("platform") == "wechat_official_account"
        and parent.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}
    }
    actionable_content: list[Path] = []
    for manifest_path in sorted(jobs_root.glob("content-????????T??????Z-????????/manifest.json")):
        manifest = read_json_if_valid(manifest_path)
        if not manifest:
            continue
        if windows_without_wechat and manifest.get("platform") in WINDOWS_UNSUPPORTED_PLATFORMS:
            continue
        if manifest.get("parent_job_id") in waiting_official_parents:
            continue
        if manifest.get("status") in {"queued", "downloading", "transcribing"}:
            actionable_content.append(manifest_path)
            if len(actionable_content) >= max_parallel:
                break
    if actionable_content:
        # Reuse a bounded process-wide pool across worker passes.  The GUI
        # invokes this function repeatedly; recreating a pool every pass
        # discarded each thread's persistent HTTPS connection cache and made
        # large creator batches pay unnecessary TLS/DNS setup repeatedly.
        pool = _content_executor(max_parallel)
        futures = [pool.submit(process_content_job, path, root) for path in actionable_content]
        for future in futures:
            try:
                future.result()
            except Exception:
                # process_content_job normally records failures itself; keep
                # one unexpected child exception from stopping the pool.
                logging.exception("parallel content worker child crashed")
        processed = True
    if not processed and not windows_without_wechat:
        for manifest_path in sorted(jobs_root.glob("content-????????T??????Z-????????/manifest.json")):
            manifest = read_json_if_valid(manifest_path)
            recoverable = (
                manifest
                and manifest.get("platform") == "wechat_channels"
                and manifest.get("status") == "failed"
                and (manifest.get("error") or {}).get("code") == "channel_video_missing"
                and len(channel_local_video_candidates(manifest, manifest_path, root)) == 1
            )
            if not recoverable:
                continue
            recovered = process_content_job(manifest_path, root, resume_delivery=True)
            refresh_creator_parent_for_child(recovered, root)
            processed = True
            break
    if not processed:
        actionable_batch_states = {"queued", "discovering", "processing"}
        waiting_batch_states = {"waiting_for_authorization", "waiting_for_reauthentication"}
        official_batches: list[tuple[int, Path]] = []
        for manifest_path in jobs_root.glob("batch-????????T??????Z-????????/manifest.json"):
            manifest = read_json_if_valid(manifest_path)
            if not manifest or manifest.get("kind") != "batch":
                continue
            if windows_without_wechat and manifest.get("platform") in WINDOWS_UNSUPPORTED_PLATFORMS:
                continue
            status = manifest.get("status")
            if status in actionable_batch_states:
                official_batches.append((0, manifest_path))
            elif status in waiting_batch_states:
                official_batches.append((1, manifest_path))
        if official_batches:
            _, manifest_path = min(official_batches, key=lambda item: (item[0], item[1].name))
            process_official_batch(manifest_path, root)
            processed = True
    statuses = []
    for pattern in ("content-????????T??????Z-????????/manifest.json", "batch-????????T??????Z-????????/manifest.json"):
        for manifest_path in jobs_root.glob(pattern):
            manifest = read_json_if_valid(manifest_path)
            if manifest:
                statuses.append(manifest.get("status"))
    counts = {status: statuses.count(status) for status in sorted({s for s in statuses if s})}
    worker = {
        "schema_version": 1,
        "tool_version": VERSION,
        "kind": "content_worker",
        "status": "running",
        "pid": os.getpid(),
        "updated_at": utc_now(),
        "counts": {"total": len(statuses), **counts},
    }
    write_json(jobs_root / "content-worker" / "manifest.json", worker)
    return worker, processed


def watch_content(root: Path, interval: int, once: bool) -> dict:
    if interval < 1:
        raise ArchiveError("invalid_interval", "轮询间隔必须至少为 1 秒。")
    while True:
        worker, processed = content_worker_once(root)
        if once:
            worker["status"] = "stopped"
            write_json(root / "jobs" / "content-worker" / "manifest.json", worker)
            return {"ok": True, "manifest": "jobs/content-worker/manifest.json", "counts": worker["counts"]}
        if not processed:
            time.sleep(interval)


def content_worker_status(root: Path) -> dict:
    manifest_path = root / "jobs" / "content-worker" / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("content_worker_not_started", "内容 worker 尚未生成 manifest。", 66)
    worker = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {"ok": True, "worker": worker, "manifest": "jobs/content-worker/manifest.json"}


def transparent_core_sha256(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        return ""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            return ""
        if "__pycache__" in path.parts or path.suffix.lower() == ".pyc":
            continue
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def transparent_core_root() -> Path:
    import importlib.util
    bundled = Path(__file__).resolve().parent / "vendor" / "transparent-core"
    installed = local_data_root() / "transparent-core" / TRANSPARENT_CORE_REVISION
    for core in (bundled, installed):
        if not core.exists():
            continue
        if transparent_core_sha256(core) != TRANSPARENT_CORE_SHA256:
            raise ArchiveError("transparent_core_invalid", f"透明派生核心校验失败：{core}", 69)
        return core
    spec = importlib.util.find_spec("yt_dlp")
    if spec and spec.origin:
        origin_path = Path(spec.origin)
        if origin_path.exists():
            return origin_path.resolve().parents[1]
        return Path(__file__).resolve().parent
    raise ArchiveError("transparent_core_missing", "媒体解析核心 yt-dlp 未安装，请执行 pip install -r requirements.txt。", 69)


def platform_cookie_jar(platform: str) -> Path:
    if os.name == "nt":
        return local_data_root() / "cookies" / f"{platform}.txt"
    return Path.home() / "Library" / "Application Support" / "WorksAppreciationDownloader" / "cookies" / f"{platform}.txt"


class SilentCookieLogger:
    def debug(self, message: str) -> None:
        pass

    info = debug
    warning = debug
    error = debug


def transparent_core_needs_login(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in ("cookie", "login", "fresh", "401", "403", "captcha", "verify", "验证", "访问频繁"))


def extract_platform_browser_cookies(platform: str, browser: str):
    domains = PLATFORM_COOKIE_DOMAINS.get(platform)
    if not domains:
        raise ArchiveError("invalid_platform", "Cookie 导入只支持 B站、小红书、抖音、微博和 X。")
    if browser not in SUPPORTED_BROWSERS:
        raise ArchiveError("unsupported_browser", f"当前系统不支持从 {browser} 导入 Cookie。", 69)
    core = transparent_core_root()
    sys.path.insert(0, str(core))
    try:
        from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveError("transparent_core_invalid", "透明派生核心无法加载 Cookie 模块。", 69) from exc
    try:
        browser_jar = extract_cookies_from_browser(browser, logger=SilentCookieLogger())
    except Exception as exc:
        raise ArchiveError("cookie_import_failed", "无法从浏览器导入 Cookie；请确认已登录并允许访问。", 69) from exc
    filtered = YoutubeDLCookieJar()
    for cookie in browser_jar:
        domain = cookie.domain.lower()
        if any(domain == suffix.lstrip(".") or domain.endswith(suffix) for suffix in domains):
            filtered.set_cookie(cookie)
    if not len(filtered):
        raise ArchiveError("platform_cookies_missing", "所选浏览器中没有找到该平台的登录 Cookie。", 69)
    return filtered


def save_platform_cookies(platform: str, jar) -> None:
    target = platform_cookie_jar(platform)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{platform}-", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        jar.save(filename=str(temporary))
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def import_browser_cookies(platform: str, browser: str, root: Path) -> dict:
    filtered = extract_platform_browser_cookies(platform, browser)
    save_platform_cookies(platform, filtered)

    resumed = 0
    for manifest_path in (root / "jobs").glob("content-????????T??????Z-????????/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("platform") == platform and manifest.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}:
            manifest.update({"status": "queued", "updated_at": utc_now()})
            manifest.pop("next_action", None)
            manifest.pop("error", None)
            write_json(manifest_path, manifest)
            resumed += 1
    for manifest_path in (root / "jobs").glob("batch-????????T??????Z-????????/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("kind") == "creator_batch" and manifest.get("platform") == platform and manifest.get("status") == "waiting_for_reauthentication":
            manifest.update({"status": "processing", "updated_at": utc_now()})
            manifest.pop("next_action", None)
            write_json(manifest_path, manifest)
    return {"ok": True, "platform": platform, "browser": browser, "status": "imported", "cookie_count": len(filtered), "resumed_jobs": resumed}


def resume_job(job_id: str, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("platform") == "wechat_official_account":
        if manifest.get("kind") != "batch" or not manifest.get("selection"):
            raise ArchiveError("job_not_resumable", "该公众号任务不支持手动恢复。", 66)
        descriptor = acquire_creator_batch_lock(manifest_path)
        if descriptor is None:
            raise ArchiveError("job_busy", "该任务正在更新，请稍后重试。", 75)
        try:
            manifest = read_json_if_valid(manifest_path) or manifest
            resumed = 0
            limit = int((manifest.get("selection") or {}).get("limit") or 0)
            for item in (manifest.get("items") or [])[:limit]:
                child_path = root / "jobs" / str(item.get("child_job_id") or "") / "manifest.json"
                child = read_json_if_valid(child_path)
                if not child:
                    continue
                error_code = str(
                    (child.get("error") or child.get("last_retry_error") or {}).get("code") or ""
                )
                retryable = child.get("status") in {
                    "waiting_for_authorization",
                    "waiting_for_reauthentication",
                } or (child.get("status") == "failed" and error_code in OFFICIAL_SESSION_ERROR_CODES)
                if not retryable:
                    continue
                if child.get("error"):
                    child["last_retry_error"] = child["error"]
                child.update({"status": "queued", "updated_at": utc_now()})
                for field in ("completed_at", "failed_stage", "error", "next_action", "official_fetch_retries"):
                    child.pop(field, None)
                write_json(child_path, child)
                item.update({"result": "processing", "error_code": None})
                resumed += 1
            if not resumed:
                raise ArchiveError("job_not_waiting", "该任务没有可恢复的公众号子任务。", 66)
            manifest.update({"status": "processing", "updated_at": utc_now()})
            for field in ("completed_at", "error", "next_action"):
                manifest.pop(field, None)
            manifest = _refresh_official_batch_unlocked(manifest, manifest_path, root)
            return {
                "ok": True,
                "job_id": job_id,
                "status": manifest["status"],
                "platform": manifest.get("platform"),
                "resumed_jobs": resumed,
            }
        finally:
            os.close(descriptor)
    if manifest.get("platform") != "wechat_channels":
        raise ArchiveError("job_not_resumable", "只有视频号授权等待任务可以手动恢复。", 66)
    if manifest.get("status") not in {"waiting_for_authorization", "waiting_for_reauthentication"}:
        raise ArchiveError("job_not_waiting", "该任务当前不在授权等待状态。", 66)

    if manifest.get("kind") == "creator_batch":
        if manifest.get("selection") or manifest.get("child_job_ids"):
            manifest, _ = resume_creator_batch_children(manifest, manifest_path, root)
            return {
                "ok": manifest.get("status") != "failed",
                "job_id": job_id,
                "status": manifest["status"],
                "platform": manifest.get("platform"),
            }
        source = str(manifest.get("source") or "")
        if not source:
            raise ArchiveError("job_source_missing", "任务缺少原始视频号链接。", 70)
        result = inspect_channel_creator(source, root, existing_job=(job_id, manifest_path.parent, manifest))
        return result

    if manifest.get("kind") != "content":
        raise ArchiveError("job_not_resumable", "该视频号任务类型不支持手动恢复。", 66)
    manifest = process_content_job(manifest_path, root, resume_waiting=True)
    return {"ok": manifest.get("status") != "failed", "job_id": job_id, "status": manifest["status"], "platform": manifest.get("platform")}


def resolve_transparent_core_url(url: str, platform: str) -> str:
    host = (_split_url(url).hostname or "").lower()
    if platform == "bilibili" and host == "b23.tv":
        exact_hosts, suffixes = {"b23.tv", "bilibili.com"}, (".bilibili.com",)
    elif platform == "xiaohongshu" and host in XHS_SHORT_HOSTS:
        exact_hosts, suffixes = XHS_SHORT_HOSTS | XHS_WEB_HOSTS, (".xiaohongshu.com", ".rednote.com")
    elif platform == "douyin" and host == "v.douyin.com":
        exact_hosts, suffixes = {"v.douyin.com", "douyin.com"}, (".douyin.com", ".iesdouyin.com")
    else:
        return url
    try:
        _, _, final_url = fetch_limited(url, exact_hosts=exact_hosts, suffixes=suffixes, max_bytes=8 * 1024 * 1024)
        return final_url
    except ArchiveError:
        if platform != "bilibili":
            raise
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with build_opener(SafeRedirectHandler(exact_hosts, suffixes)).open(request, timeout=operation_wait(25)) as response:
                return response.geturl()
        except HTTPError as exc:
            final_url = exc.geturl()
            parsed = _split_url(final_url)
            host = (parsed.hostname or "").lower()
            if host_allowed(host, exact_hosts, suffixes) and re.search(r"/video/BV[0-9A-Za-z]{10}", parsed.path):
                return final_url
            raise ArchiveError("fetch_failed", f"下载失败：{exc}", 69) from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise ArchiveError("fetch_failed", f"下载失败：{exc}", 69) from exc


def download_with_transparent_core(url: str, platform: str, work_dir: Path, *, use_cookies=True) -> tuple[dict, Path]:
    url = resolve_transparent_core_url(url, platform)
    core = transparent_core_root()
    sys.path.insert(0, str(core))
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
        from yt_dlp.utils import DownloadError  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveError("transparent_core_invalid", "透明派生核心无法加载。", 69) from exc

    work_dir.mkdir(parents=True, exist_ok=True)
    cookie_jar = platform_cookie_jar(platform)
    options = {
        "cachedir": False,
        "continuedl": True,
        # 优先 1080p 原画质：先选最大分辨率的视频 + 最佳音频；否则就拿最大视频；最后兜底单个 best。
        # yt_dlp 默认按文件大小排序，可能漏选最高码率版本，所以强制使用 hasvid+res 排序。
        "format_sort": ["res", "fps", "br", "size"],
        "format_sort_force": True,
        "format": "bestvideo[ext=mp4][height>=720]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
        "fragment_retries": 3,
        "ffmpeg_location": require_executable("WECHAT_FFMPEG", "ffmpeg"),
        "merge_output_format": "mp4",
        "logger": SilentCookieLogger(),
        "noplaylist": True,
        "noprogress": True,
        "outtmpl": str(work_dir / "source.%(ext)s"),
        "overwrites": True,
        "quiet": True,
        "retries": 3,
        "socket_timeout": 30,
        "no_warnings": True,
    }
    if use_cookies and cookie_jar.is_file():
        options["cookiefile"] = str(cookie_jar)
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
    except DownloadError as exc:
        if transparent_core_needs_login(exc):
            raise ArchiveError("reauthentication_required", "平台登录态缺失或已失效。", 69) from exc
        raise ArchiveError("transparent_core_failed", "透明派生核心提取失败。", 69) from exc
    candidates = [
        path
        for path in work_dir.glob("source.*")
        if path.is_file() and path.suffix.lower() not in {".part", ".ytdl", ".json"}
    ]
    if not candidates:
        raise ArchiveError("video_missing", "提取核心完成后没有找到视频文件。", 69)
    return info, max(candidates, key=lambda path: path.stat().st_size)


def inspect_with_transparent_core(url: str, platform: str, *, use_cookies=True) -> dict:
    url = resolve_transparent_core_url(url, platform)
    core = transparent_core_root()
    sys.path.insert(0, str(core))
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
        from yt_dlp.utils import DownloadError  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveError("transparent_core_invalid", "透明派生核心无法加载。", 69) from exc
    options = {"cachedir": False, "quiet": True, "no_warnings": True, "logger": SilentCookieLogger(),
               "socket_timeout": 12}
    cookie_jar = platform_cookie_jar(platform)
    if use_cookies and cookie_jar.is_file():
        options["cookiefile"] = str(cookie_jar)
    try:
        with YoutubeDL(options) as downloader:
            return downloader.extract_info(url, download=False, process=False)
    except DownloadError as exc:
        if transparent_core_needs_login(exc):
            raise ArchiveError("reauthentication_required", "平台登录态缺失或已失效。", 69) from exc
        raise ArchiveError("transparent_core_failed", "透明派生核心解析失败。", 69) from exc


def timeline_text(json_path: Path) -> bytes:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    segments = payload.get("transcription") or payload.get("segments") or []
    lines = []
    for segment in segments:
        timestamps = segment.get("timestamps") or {}
        start = str(timestamps.get("from") or "").replace(".", ",").split(",", 1)[0]
        end = str(timestamps.get("to") or "").replace(".", ",").split(",", 1)[0]
        text = str(segment.get("text") or "").strip()
        if start and end and text:
            lines.extend([f"[{start} → {end}]", text, ""])
    if not lines:
        raise ArchiveError("transcript_segments_missing", "结构化逐字稿没有可用时间分段。", 70)
    return ("\n".join(lines).rstrip() + "\n").encode()


def canonical_video_url(platform: str, info: dict, fallback: str) -> str:
    content_id = str(info.get("id") or "")
    if platform == "bilibili":
        match = re.search(r"BV[0-9A-Za-z]{10}", str(info.get("bvid") or content_id or fallback))
        if match:
            page_match = re.search(r"_p(\d+)$", content_id)
            page = int(page_match.group(1)) if page_match else int(info.get("page_number") or 1)
            suffix = f"?p={page}" if page > 1 else ""
            return f"https://www.bilibili.com/video/{match.group(0)}/{suffix}"
    if platform == "xiaohongshu" and content_id:
        backend = _redbook_backend(str(info.get("webpage_url") or fallback))
        return f"https://{_redbook_domain(backend)}/explore/{content_id}"
    if platform == "douyin" and content_id:
        return f"https://www.douyin.com/video/{content_id}"
    return canonical_url(str(info.get("webpage_url") or fallback))


def transcribe_content_video(video: Path, output_dir: Path, root: Path) -> list[dict]:
    prefix = output_dir / "原始逐字稿"
    _, generated = run_whisper(
        video,
        root,
        output_dir / ".content-audio.wav",
        prefix,
        ["-otxt", "-osrt", "-oj"],
        (".txt", ".srt", ".json"),
    )
    by_suffix = {path.suffix: path for path in generated}
    if set(by_suffix) != {".txt", ".srt", ".json"}:
        raise ArchiveError("transcript_incomplete", "三种原始逐字稿未全部生成。", 70)
    atomic_write(by_suffix[".txt"], timeline_text(by_suffix[".json"]))
    return [
        output_record(root, by_suffix[".txt"], "transcript_txt"),
        output_record(root, by_suffix[".srt"], "transcript_srt"),
        output_record(root, by_suffix[".json"], "transcript_json"),
    ]


def run_background_process(args, **options):
    """Run CLI helpers without allocating a Windows console for the GUI."""
    if os.name == "nt":
        options["creationflags"] = options.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    options["timeout"] = operation_wait(options.get("timeout"))
    return subprocess.run(args, **options)


def ensure_video_readable(video: Path) -> None:
    ffprobe = sibling_executable(require_executable("WECHAT_FFMPEG", "ffmpeg"), "ffprobe")
    result = run_background_process(
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        capture_output=True,
        text=True,
        env=safe_subprocess_environment(),
        timeout=30,
    )
    if result.returncode or result.stdout.strip() != "video":
        raise ArchiveError("invalid_video", "下载结果不是可读取的视频。", 69)


def frames_per_second(value):
    try:
        parts = str(value or "0").split("/")
        return float(parts[0]) / float(parts[1]) if len(parts) == 2 else float(parts[0])
    except (ValueError, ZeroDivisionError):
        return 0.0


def video_rank(quality):
    width, height = positive_number(quality.get("width")), positive_number(quality.get("height"))
    return (width * height or max(width, height) ** 2, bool(quality.get("original")),
            positive_number(quality.get("bitrate")), frames_per_second(quality.get("fps")),
            positive_number(quality.get("bytes")))


def probe_video_file(video):
    ffprobe = sibling_executable(require_executable("WECHAT_FFMPEG", "ffmpeg"), "ffprobe")
    result = run_background_process(
        [ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate,codec_name,bit_rate:format=bit_rate",
         "-of", "json", str(video)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
    )
    try:
        payload = json.loads(result.stdout)
        video_info = payload["streams"][0]
        if result.returncode or not video_info.get("width") or not video_info.get("height"):
            raise ValueError("missing dimensions")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ArchiveError("invalid_video", "视频文件无法确认实际画质。", 69) from exc
    return {"width": video_info["width"], "height": video_info["height"],
            "codec": video_info.get("codec_name"), "fps": frames_per_second(video_info.get("avg_frame_rate")),
            "bitrate": positive_number(video_info.get("bit_rate") or (payload.get("format") or {}).get("bit_rate")),
            "bytes": video.stat().st_size}


def source_dimensions_match(actual, expected):
    return all(actual) and all(expected) and sorted(actual) == sorted(expected)


def source_video_matches(actual, advertised):
    dimensions = (actual["width"], actual["height"])
    expected = tuple(positive_number(advertised.get(key)) for key in ("width", "height"))
    if not all(expected):
        return bool(actual.get("original"))
    if not source_dimensions_match(dimensions, expected):
        return False
    if actual.get("original"):
        return True
    size = positive_number(advertised.get("bytes"))
    if size and actual.get("bytes") == size:
        return True
    bitrate = positive_number(advertised.get("bitrate"))
    fps = frames_per_second(advertised.get("fps"))
    return bool(bitrate and fps and actual.get("bitrate", 0) >= bitrate * 0.95
                and abs(actual.get("fps", 0) - fps) <= 0.1)


def download_best_video(choices, target, referer, download, *, allow_lower=False, reference_size=None):
    """Try the platform's source and ranked encodes, checking the saved media."""
    seen = set()
    best = None
    error = None
    temporary = target.with_name("candidate-" + target.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(choices, key=lambda item: (bool(item.get("original") or item.get("inspect_first")),
                                                not item.get("alternate_hd_probe"), video_rank(item)), reverse=True)
    adaptive_pixels = max((video_rank(item)[0] for item in choices if not item.get("inspect_first")), default=0)
    for item in ordered:
        if item.get("alternate_hd_probe") and best and video_rank(best)[0] > adaptive_pixels:
            continue
        urls = [url for url in item.get("urls", []) if url not in seen]
        if not urls or (best and not (item.get("original") or item.get("inspect_first")) and video_rank(item) <= video_rank(best)):
            continue
        expected = (positive_number(item.get("width")), positive_number(item.get("height")))
        for url in prefer_media_routes(urls):
            seen.add(url)
            temporary.unlink(missing_ok=True)
            try:
                download([url], temporary, referer)
                actual = probe_video_file(temporary)
            except (ArchiveError, subprocess.TimeoutExpired) as exc:
                error = exc
                continue
            actual["original"] = bool(item.get("original"))
            if item.get("live_playback_quality"):
                actual["live_playback_quality"] = item["live_playback_quality"]
            if best is None or video_rank(actual) > video_rank(best):
                os.replace(temporary, target)
                best = actual
            if matches_dimensions((actual["width"], actual["height"]), expected):
                break
    temporary.unlink(missing_ok=True)
    if best:
        advertised = advertised_video_choice(choices, best, reference_size)
        expected = (positive_number(advertised.get("width")), positive_number(advertised.get("height")))
        if not matches_dimensions((best["width"], best["height"]), expected):
            if not allow_lower:
                target.unlink(missing_ok=True)
                raise ArchiveError("video_resolution_unavailable",
                                   f"平台列出 {expected[0]}×{expected[1]} 视频，实际只取得 {best['width']}×{best['height']}，已跳过低清文件。", 69)
            best["non_highest"] = True
            best["advertised_size"] = expected
        best["source_match"] = not best.get("non_highest") and source_video_matches(best, advertised)
        return best
    if isinstance(error, ArchiveError):
        raise error
    raise ArchiveError("video_missing", "没有取得可读取的视频文件。", 69)


def advertised_video_choice(choices, actual, reference_size=None):
    def has_matching_shape(width, height, reference):
        short, long = sorted((width, height))
        ref_short, ref_long = sorted(reference)
        return abs(short / long - ref_short / ref_long) <= .05

    candidates = []
    for item in choices:
        width, height = positive_number(item.get("width")), positive_number(item.get("height"))
        if not (width and height):
            continue
        # Douyin can label an alias as a square even when its bytes belong to
        # the same portrait encode. Compare it with the decoded Live still.
        alias_size = positive_number(item.get("bytes"))
        alias_of_adaptive = alias_size and not (item.get("bitrate") or item.get("fps")) and any(
            alias_size == positive_number(other.get("bytes"))
            and positive_number(other.get("width")) != positive_number(other.get("height"))
            for other in choices)
        placeholder = (width == height and not item.get("original")
                       and (width * height <= actual["width"] * actual["height"] or alias_of_adaptive))
        if (placeholder and reference_size and all(reference_size)
                and not has_matching_shape(width, height, reference_size)
                and has_matching_shape(actual["width"], actual["height"], reference_size)):
            continue
        candidates.append(item)
    return max(candidates, key=video_rank, default={})


def commit_video(source, target, quality, *, trust_existing=True):
    if target.is_file():
        try:
            old = probe_video_file(target)
        except (ArchiveError, OSError, subprocess.TimeoutExpired):
            old = None
        if old:
            quality = quality or probe_video_file(source)
        if old and video_rank(old)[0] >= video_rank(quality)[0]:
            source.unlink()
            old["non_highest"] = not matches_dimensions((old["width"], old["height"]), quality.get("advertised_size", (0, 0)))
            old["retained_existing"] = True
            if not trust_existing:
                old["unverified_clean_source"] = True
            return old
    os.replace(source, target)
    return quality


def douyin_verified_archive_files(root, content_id):
    hashes = {}
    for path in (root / "jobs").glob("content-*/manifest.json"):
        prior = read_json_if_valid(path) or {}
        if (prior.get("platform") == "douyin" and prior.get("content_id") == content_id
                and prior.get("status") == "completed" and prior.get("douyin_clean_revision") == DOUYIN_CLEAN_REVISION):
            for row in prior.get("outputs") or []:
                hashes.setdefault(row["path"], set()).add(row.get("sha256"))

    def trusted(path):
        checksums = hashes.get(archive_relative(root, path))
        return bool(checksums and path.is_file() and sha256_file(path) in checksums)

    return trusted


def douyin_archive_matches(root, manifest):
    for row in manifest.get("outputs") or []:
        if row["role"] not in {"image", "video", "live_motion"}:
            continue
        path = (root / row["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or sha256_file(path) != row.get("sha256"):
            return False
    return bool(manifest.get("outputs"))


def work_archive_layout(root, manifest, platform, title, content_id):
    direct = manifest.get("direct_archive") and platform in {"douyin", "xiaohongshu", "weibo", "x", "bilibili"}
    platform_folder = root if direct else root / "content" / PLATFORM_DIRS[platform]
    author_folder = manifest.get("author_folder")
    if not author_folder:
        previous = manifest.get("previous_output_dir")
        if previous:
            saved = (root / previous).resolve()
            if saved.parent == platform_folder.resolve() and saved.name.endswith("--" + content_id) and saved.is_dir():
                return saved, None
        return platform_folder / f"{safe_content_title(title, '未命名作品')}--{content_id}", None
    relative = Path(str(author_folder))
    directory = (root / relative).resolve()
    layout_valid = len(relative.parts) == 1 if direct else relative.parts[:2] == ("content", PLATFORM_DIRS[platform]) and len(relative.parts) == 3
    if relative.is_absolute() or not layout_valid or directory.parent != platform_folder.resolve():
        raise ArchiveError("invalid_author_folder", "作者归档目录无效，请重新盘点作者主页。", 69)
    slug = safe_content_title(title, "未命名作品")[:40].rstrip(" .")
    identity = safe_content_title(content_id, "作品")
    prefix = f"{slug}--{identity}"
    previous = str(manifest.get("work_file_prefix") or "")
    if previous.endswith("--" + identity) and len(previous) <= 80 and safe_content_title(previous, "") == previous:
        prefix = previous
    manifest["work_file_prefix"] = prefix
    return directory, prefix


def finalize_video_content(
    manifest: dict,
    manifest_path: Path,
    root: Path,
    info: dict,
    source_video: Path,
    route: str,
) -> dict:
    title = str(info.get("title") or "未命名视频")
    content_id = str(info.get("id") or manifest["content_id"])
    platform = str(manifest["platform"])
    output_dir, prefix = work_archive_layout(root, manifest, platform, title, content_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    video = output_dir / (f"{prefix}.mp4" if prefix else "video.mp4")
    if source_video.suffix.lower() != ".mp4":
        raise ArchiveError("unexpected_video_container", f"提取结果不是 MP4：{source_video.suffix}", 69)
    ensure_video_readable(source_video)
    quality = info.get("_video_quality")
    if quality is None and info.get("formats"):
        variants = [
            {"width": item.get("width"), "height": item.get("height"), "fps": item.get("fps"),
             "bitrate": positive_number(item.get("vbr")) * 1000}
            for item in info["formats"] if item.get("vcodec") != "none" and item.get("width") and item.get("height")
        ]
        if variants:
            actual = probe_video_file(source_video)
            actual["source_match"] = source_video_matches(actual, max(variants, key=video_rank))
            quality = actual
    trusted = platform != "douyin" or (video.is_file() and douyin_verified_archive_files(root, content_id)(video))
    quality = commit_video(source_video, video, quality, trust_existing=trusted)
    manifest.update(
        {
            "status": "transcribing",
            "updated_at": utc_now(),
            "title": title,
            "content_id": content_id,
            "content_type": "video",
            "canonical_url": canonical_video_url(platform, info, str(manifest["source"])),
            "route": route,
            "output_dir": archive_relative(root, output_dir),
            "outputs": [{**output_record(root, video, "video"), "retained_existing": bool((quality or {}).get("retained_existing"))}],
        }
    )
    if quality:
        manifest["outputs"][0]["source_match"] = bool(quality.get("source_match"))
        manifest["video_quality"] = quality
        manifest["non_highest"] = bool(quality.get("non_highest"))
        manifest["media_quality_revision"] = MEDIA_QUALITY_REVISION
    if platform == "douyin" and not (quality or {}).get("unverified_clean_source"):
        manifest["douyin_clean_revision"] = DOUYIN_CLEAN_REVISION
    elif platform == "douyin":
        manifest.pop("douyin_clean_revision", None)
    write_json(manifest_path, manifest)
    # 【修复】自动从 video.mp4 抽一帧做 cover.jpg，方便前端列表预览
    cover = output_dir / (f"{prefix}--封面.jpg" if prefix else "cover.jpg")
    if platform == "douyin" and not (quality or {}).get("retained_existing"):
        cover.unlink(missing_ok=True)
    if not cover.is_file():
        try:
            ffmpeg = require_executable("WECHAT_FFMPEG", "ffmpeg")
            run_background_process(
                [ffmpeg, "-y", "-ss", "00:00:00.500", "-i", str(video),
                 "-frames:v", "1", "-q:v", "2", str(cover)],
                capture_output=True, timeout=20,
            )
        except Exception:
            pass
    if cover.is_file() and cover.stat().st_size > 500:
        manifest["outputs"].append(output_record(root, cover, "cover_image"))
    transcripts = transcribe_content_video(video, output_dir, root)
    manifest["outputs"].extend(transcripts)
    manifest.update({"status": "completed", "updated_at": utc_now(), "completed_at": utc_now()})
    manifest.pop("error", None)
    manifest.pop("channel_delivery_recovery", None)
    write_json(manifest_path, manifest)
    return manifest


def process_transparent_video(manifest: dict, manifest_path: Path, root: Path) -> dict:
    if manifest.get("platform") == "douyin" and (manifest.get("douyin_media") or {}).get("kind") in {
        "image_gallery", "live_photo"
    }:
        return process_douyin_gallery(manifest, manifest_path, root)
    work_dir = manifest_path.parent / "work"
    platform = str(manifest["platform"])
    if platform == "douyin":
        info, source = download_douyin_inventory_fallback(manifest, work_dir)
        return finalize_video_content(manifest, manifest_path, root, info, source, "douyin_web_media")
    use_cookies = not bool(manifest.get("guest_only"))
    try:
        info, source_video = download_with_transparent_core(str(manifest["source"]), platform, work_dir, use_cookies=use_cookies)
        route = "transparent_core"
    except ArchiveError as exc:
        if platform != "bilibili" or exc.code not in {"transparent_core_failed", "reauthentication_required", "video_missing"}:
            raise
        else:
            try:
                info, source_video = download_bilibili_fallback(str(manifest["source"]), work_dir, use_cookies=use_cookies)
            except ArchiveError:
                if exc.code == "reauthentication_required":
                    raise exc
                raise
            route = "bilibili_api_cdn"
    return finalize_video_content(manifest, manifest_path, root, info, source_video, route)


def resolve_xiaohongshu_url(url: str) -> str:
    return resolve_transparent_core_url(url, "xiaohongshu")


_XHS_IMAGE_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="xhs-image")


def positive_number(value):
    try:
        result = int(float(value))
        return max(0, result)
    except (TypeError, ValueError, OverflowError):
        return 0


def image_size(source):
    from PIL import Image
    if isinstance(source, bytes):
        source = BytesIO(source)
    with Image.open(source) as image:
        size = image.size
        image.load()
    return size


def matches_dimensions(size, expected):
    return not all(expected) or all(have + 1 >= need for have, need in zip(sorted(size), sorted(expected)))


def commit_image(temporary, target, size):
    candidates = []
    extensions = dict.fromkeys((target.suffix, ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bin"))
    for extension in extensions:
        path = target.with_suffix(extension)
        if not path.is_file():
            continue
        try:
            old = image_size(path)
        except (OSError, ValueError):
            old = (0, 0)
        candidates.append((path, old))
    if candidates:
        path, old = max(candidates, key=lambda item: (item[1][0] * item[1][1], item[0].suffix.lower() == ".png"))
        if old[0] * old[1] >= size[0] * size[1]:
            temporary.unlink()
            return path, old, True
    os.replace(temporary, target)
    for path, _ in candidates:
        if path != target:
            path.unlink(missing_ok=True)
    return target, size, False


def xhs_image_source(url):
    parts = urlsplit(url)
    if "!" in parts.path:
        return "display"
    return "full_size_render" if parts.query else "cdn_object"


def _xhs_cdn_referer(url: str, fallback_referer: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host == "ci.xiaohongshu.com" or host.endswith(".xhscdn.com"):
        return "https://www.xiaohongshu.com/"
    return fallback_referer


def _xhs_cdn_object_matches_cloud_original(url: str, data: bytes, size: tuple[int, int]) -> bool:
    """Confirm via Xiaohongshu's cloud ?imageInfo that a raw cdn_object is the
    untransformed stored file (e.g. when uploads exceeding 2560px are capped by
    the platform while note metadata still reports pre-upload dimensions)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.lstrip("/")
    if not path or "!" in path or parts.query:
        return False
    if host != "ci.xiaohongshu.com" and not host.endswith(".xhscdn.com"):
        return False
    info_url = f"https://ci.xiaohongshu.com/{path}?imageInfo"
    try:
        raw, _, _ = fetch_limited(
            info_url,
            exact_hosts=XHS_MEDIA_EXACT_HOSTS,
            suffixes=XHS_MEDIA_HOST_SUFFIXES,
            max_bytes=64 * 1024,
            headers={"Referer": "https://www.xiaohongshu.com/", "User-Agent": DOUYIN_USER_AGENT},
        )
        meta = json.loads(raw.decode("utf-8", errors="replace"))
    except (ArchiveError, OSError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    cloud_size = (positive_number(meta.get("width")), positive_number(meta.get("height")))
    if not all(cloud_size) or not matches_dimensions(size, cloud_size):
        return False
    cloud_md5 = str(meta.get("md5") or "").strip().lower()
    if cloud_md5:
        if hashlib.md5(data).hexdigest().lower() == cloud_md5:
            return True
    cloud_bytes = positive_number(meta.get("size"))
    if cloud_bytes and len(data) == cloud_bytes:
        return True
    return not cloud_md5 and not cloud_bytes


def fetch_xhs_image(originals, alternatives, expected, referer, *, allow_lower=False):
    best = None
    best_rank = None
    sources = sorted(originals, key=lambda url: xhs_image_source(url) != "cdn_object")
    for candidate, fallback in [(url, False) for url in sources] + [(url, True) for url in alternatives]:
        try:
            fetched = fetch_limited(candidate, exact_hosts=XHS_MEDIA_EXACT_HOSTS,
                                    suffixes=XHS_MEDIA_HOST_SUFFIXES, max_bytes=MAX_SOURCE_IMAGE_BYTES,
                                    headers={"Referer": _xhs_cdn_referer(candidate, referer), "User-Agent": DOUYIN_USER_AGENT},
                                    complete_image=not fallback and xhs_image_source(candidate) == "cdn_object")
            if media_extension_from_bytes("", "", fetched[0]) == ".bin":
                continue
            size = image_size(fetched[0])
        except (ArchiveError, OSError, ValueError):
            continue
        if matches_dimensions(size, expected) and (all(expected) or not fallback):
            return fetched, fallback and not all(expected), size
        rank = (size[0] * size[1], not fallback and xhs_image_source(fetched[2]) == "cdn_object", len(fetched[0]))
        if best is None or rank > best_rank:
            best = (fetched, fallback, size)
            best_rank = rank
    if best and all(expected):
        if not best[1] and xhs_image_source(best[0][2]) == "cdn_object":
            if _xhs_cdn_object_matches_cloud_original(best[0][2], best[0][0], best[2]):
                return best[0], False, best[2]
        if allow_lower:
            return best[0], True, best[2]
        width, height = best[2]
        raise ArchiveError("image_resolution_unavailable",
                           f"作品标注 {expected[0]}×{expected[1]}，目前只能取得 {width}×{height}，已跳过低清图片。", 69)
    return best or (None, False, None)


def finalize_xiaohongshu_image_text(
    manifest: dict,
    manifest_path: Path,
    root: Path,
    info: dict,
    route: str = "transparent_core_page",
) -> dict:
    title = str(info.get("title") or "未命名小红书图文")
    content_id = str(info.get("id") or manifest["content_id"])
    output_dir, prefix = work_archive_layout(root, manifest, "xiaohongshu", title, content_id)
    image_dir = output_dir if prefix else output_dir / "配图"
    image_dir.mkdir(parents=True, exist_ok=True)
    stage = manifest_path.parent / "work" / "original-images"
    stage.mkdir(parents=True, exist_ok=True)
    image_outputs = []
    staged = []
    seen = set()
    missing_images = 0
    preview_fallback_images = 0
    allow_lower = bool(manifest.get("allow_lower_quality"))
    initial_live_groups = list(info.get("_xhs_live_photo_groups") or [])
    if not initial_live_groups and info.get("_xhs_live_photo_urls"):
        initial_live_groups = [list(info.get("_xhs_live_photo_urls") or [])]
    expected_live_motion_count = sum(bool(group) for group in initial_live_groups[:MAX_MEDIA_ITEMS])
    # Persist the destination and expected media counts before the first network
    # fetch. If a later fetch/cleanup step fails, the parent can still determine
    # whether all required files are already present on disk.
    manifest.update(
        {
            "title": title,
            "content_id": content_id,
            "content_type": "image_text",
            "output_dir": archive_relative(root, output_dir),
            "expected_image_count": len([item for item in (info.get("thumbnails") or []) if isinstance(item, dict)]),
            "expected_live_motion_count": expected_live_motion_count,
            "image_quality": "original_cdn",
            "outputs": [],
        }
    )
    write_json(manifest_path, manifest)
    candidates = []
    for item in info.get("thumbnails") or []:
        originals = list(dict.fromkeys(filter(None, map(_xhs_safe_media_url, item.get("original_urls") or []))))
        variants = list(dict.fromkeys(filter(None, map(_xhs_safe_media_url, [item.get("url"), *(item.get("urls") or [])]))))
        if not (originals or variants):
            missing_images += 1
            continue
        key = Path(urlsplit((variants or originals)[0]).path).name.split("!", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        expected = (positive_number(item.get("width")), positive_number(item.get("height")))
        domain = _redbook_domain(_redbook_backend(str(manifest["source"])))
        candidates.append((originals, [url for url in variants if url not in originals], expected, f"https://{domain}/"))
    manifest["expected_image_count"] = len(candidates) + missing_images

    for start in range(0, len(candidates), 3):
        futures = [_XHS_IMAGE_POOL.submit(copy_context().run, fetch_xhs_image, *pair, allow_lower=allow_lower)
                   for pair in candidates[start:start + 3]]
        for future, (_, _, expected, _) in zip(futures, candidates[start:start + 3]):
            fetched, using_fallback, size = future.result()
            if fetched is None:
                missing_images += 1
                continue
            data, content_type, final_url = fetched
            number = len(staged) + 1
            basename = f"{prefix}--图{number:03d}" if prefix else f"{number:02d}"
            target = image_dir / f"{basename}{media_extension_from_bytes(content_type, final_url, data)}"
            temporary = stage / f"{number:03d}{target.suffix}"
            atomic_write(temporary, data)
            report_media_bytes(len(data))
            origin = xhs_image_source(final_url)
            effective_expected = (size if not using_fallback and origin == "cdn_object"
                                  and all(expected) and not matches_dimensions(size, expected) else expected)
            staged.append((temporary, target, size, origin, using_fallback, effective_expected))
    if not staged:
        raise ArchiveError("image_missing", "小红书图文没有可归档配图。", 69)
    if missing_images:
        raise ArchiveError("image_incomplete", f"小红书图文有 {missing_images} 张配图无法取得。", 69)

    # A quality upgrade writes the same numbered images with the correct
    # original format.  Remove stale derivative files only after every image
    # and Live-photo stream has succeeded, so a transient failure never erases
    # the previous usable archive.

    canonical = canonical_video_url("xiaohongshu", info, str(manifest["source"]))
    live_outputs = []
    staged_live = []
    live_groups = list(info.get("_xhs_live_photo_groups") or [])
    motion_variants = info.get("_xhs_live_variants") or []
    if not live_groups and info.get("_xhs_live_photo_urls"):
        live_groups = [list(info.get("_xhs_live_photo_urls") or [])]
    expected_live_motion_count = sum(bool(group) for group in live_groups[:MAX_MEDIA_ITEMS])
    manifest["expected_live_motion_count"] = expected_live_motion_count
    write_json(manifest_path, manifest)
    for index, live_group in enumerate(live_groups[:MAX_MEDIA_ITEMS], start=1):
        motion = output_dir / (f"{prefix}--实况{index:03d}.mp4" if prefix else f"动态照片-{index:02d}.mp4")
        temporary = stage / f"live-{index:03d}.mp4"
        try:
            choices = motion_variants[index - 1] if index <= len(motion_variants) else [{"urls": live_group}]
            quality = download_best_video(choices, temporary, canonical, _download_xhs_video, allow_lower=allow_lower)
        except ArchiveError as exc:
            if exc.code == "video_resolution_unavailable":
                raise
            raise ArchiveError("live_photo_incomplete", "小红书 Live 图的动态视频不可用。", 69) from exc
        staged_live.append((temporary, motion, quality))
    for temporary, target, size, source_quality, using_fallback, expected in staged:
        kept, size, retained = commit_image(temporary, target, size)
        if retained:
            source_quality = "previous_archive"
            if all(expected):
                using_fallback = not matches_dimensions(size, expected)
        preview_fallback_images += using_fallback
        image_outputs.append({**output_record(root, kept, "image"), "width": size[0], "height": size[1],
                              "source_quality": source_quality, "retained_existing": retained,
                              "source_match": not retained and not using_fallback and source_quality == "cdn_object"
                              and (not all(expected) or source_dimensions_match(size, expected))})
    for temporary, motion, quality in staged_live:
        quality = commit_video(temporary, motion, quality)
        live_outputs.append({**output_record(root, motion, "live_photo_motion"), **quality})
    manifest.update(
        {
            "status": "completed",
            "updated_at": utc_now(),
            "completed_at": utc_now(),
            "title": title,
            "content_id": content_id,
            "content_type": "image_text",
            "canonical_url": canonical,
            "route": route,
            "output_dir": archive_relative(root, output_dir),
            "outputs": [*image_outputs, *live_outputs],
            "image_quality": ("api_fallback" if preview_fallback_images else "full_size_render"
                              if any(image["source_quality"] in {"full_size_render", "display"} for image in image_outputs)
                              else "original_cdn"),
            "non_highest": bool(allow_lower and preview_fallback_images) or any(record.get("non_highest") for record in live_outputs),
            "media_quality_revision": MEDIA_QUALITY_REVISION,
        }
    )
    if preview_fallback_images:
        manifest["quality_warning"] = (
            f"有 {preview_fallback_images} 张图片使用平台提供的备用图。"
        )
    else:
        manifest.pop("quality_warning", None)
    manifest.pop("error", None)
    write_json(manifest_path, manifest)
    return manifest



def previous_xiaohongshu_url(manifest, root, current):
    content_id = str(manifest.get("content_id") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{24}", content_id):
        return None
    backend = _redbook_backend(current)

    def usable(value):
        if not value:
            return None
        try:
            url = _redbook_note_url(value, backend)
            parts = _split_url(url)
            if url != current and parts.path.rstrip("/").split("/")[-1] == content_id and parse_qs(parts.query).get("xsec_token"):
                return url
        except (ArchiveError, ValueError):
            pass
        return None

    previous = usable(manifest.get("previous_xiaohongshu_source"))
    if previous:
        return previous
    for path in sorted((root / "jobs").glob("content-*/manifest.json"), reverse=True):
        child = read_json_if_valid(path) or {}
        if child.get("platform") != "xiaohongshu" or content_id not in (child.get("content_id"), child.get("submission_id")):
            continue
        previous = usable(child.get("previous_xiaohongshu_source")) or usable(child.get("source"))
        if previous:
            return previous
    return None


def xhs_work_source(manifest):
    resolved_url = resolve_xiaohongshu_url(str(manifest["source"]))
    if manifest.get("guest_only"):
        return resolved_url, ""
    if manifest.get("xiaohongshu_backend") == "rednote":
        resolved_url = _redbook_note_url(resolved_url, "rednote")
    # Creator children inherit the browser used for the authenticated full
    # inventory.  Older manifests may not have that field; Firefox is a safe
    # local fallback when its XHS session is present.
    browser = str(
        manifest.get("xiaohongshu_browser")
        or (manifest.get("creator") or {}).get("browser")
        or os.environ.get("WECHAT_XHS_CREATOR_BROWSER")
        or ""
    ).strip().lower()
    if browser not in {"firefox", "chrome"} and _redbook_cookie_file("firefox", _redbook_backend(resolved_url)):
        browser = "firefox"
    return resolved_url, browser


def xhs_work_info(manifest, root, resolved_url, browser):
    if manifest.get("guest_only"):
        note = _rednote_page_note(resolved_url, None, guest_only=True)
        if not note:
            raise ArchiveError("guest_media_unavailable", "小红书未向访客提供这个作品的下载媒体。", 69)
        return _redbook_note_info(note, resolved_url), resolved_url
    try:
        note = _redbook_read_note(resolved_url, browser)
    except ArchiveError as exc:
        if not exc.code.startswith("xiaohongshu_detail"):
            raise
        previous = previous_xiaohongshu_url(manifest, root, resolved_url)
        if not previous:
            raise
        note = _redbook_read_note(previous, browser)
        resolved_url = previous
    return _redbook_note_info(note, resolved_url), resolved_url


def process_xiaohongshu(manifest: dict, manifest_path: Path, root: Path) -> dict:
    resolved_url, browser = xhs_work_source(manifest)

    direct_error: ArchiveError | None = None
    try:
        info, resolved_url = xhs_work_info(manifest, root, resolved_url, browser)
        note_type = str(info.get("_xhs_note_type") or "normal")
        if note_type == "video" or info.get("_xhs_video_urls"):
            video_urls = list(info.get("_xhs_video_urls") or [])
            if not video_urls:
                raise ArchiveError("xiaohongshu_video_missing", "小红书视频详情没有可用直链。", 69)
            work_dir = manifest_path.parent / "work"
            work_dir.mkdir(parents=True, exist_ok=True)
            source_video = work_dir / "source.mp4"
            info["_video_quality"] = download_best_video(info["_xhs_video_variants"], source_video, resolved_url, _download_xhs_video,
                                                        allow_lower=bool(manifest.get("allow_lower_quality")))
            return finalize_video_content(manifest, manifest_path, root, info, source_video, "redbook_api")
        return finalize_xiaohongshu_image_text(manifest, manifest_path, root, info, "redbook_api")
    except ArchiveError as exc:
        direct_error = exc

    if manifest.get("guest_only") or direct_error.code in {"image_resolution_unavailable", "video_resolution_unavailable"}:
        raise direct_error

    if direct_error and direct_error.code in {"xiaohongshu_video_failed", "media_download_failed", "fetch_failed", "media_download_incomplete"} and not manifest.get("refreshing_stream"):
        invalidate_redbook_cache()
        try:
            return process_xiaohongshu({**manifest, "refreshing_stream": True}, manifest_path, root)
        finally:
            manifest.pop("refreshing_stream", None)

    # Direct API reads are the primary route.  Keep the transparent core as a
    # compatibility fallback for standalone links or a temporary redbook API
    # outage; surface the direct error if both routes fail so users no longer
    # see an opaque blanket ``transparent_core_failed`` for an XHS auth issue.
    try:
        info = inspect_with_transparent_core(resolved_url, "xiaohongshu")
        if info.get("formats"):
            work_dir = manifest_path.parent / "work"
            downloaded_info, source_video = download_with_transparent_core(resolved_url, "xiaohongshu", work_dir)
            downloaded_info["_video_quality"] = probe_video_file(source_video)
            return finalize_video_content(manifest, manifest_path, root, downloaded_info, source_video, "transparent_core_fallback")
        return finalize_xiaohongshu_image_text(manifest, manifest_path, root, info)
    except ArchiveError:
        if direct_error is not None:
            raise direct_error
        raise


def _douyin_url_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_douyin_url_list(item))
        return result
    if isinstance(value, dict):
        for key in ("url_list", "download_url_list", "uri", "url", "url_https", "image_url"):
            urls = _douyin_url_list(value.get(key))
            if urls:
                return urls
    return []


def douyin_aweme_preview_urls(aweme: dict) -> list[str]:
    """从主页作品 API 的现成响应中提取预览图，不额外逐作品请求详情。"""
    candidates: list[str] = []
    video = aweme.get("video") or {}
    if isinstance(video, dict):
        for key in (
            "dynamic_cover", "animated_cover", "cover", "origin_cover",
            "origin_dynamic_cover", "cover_hd", "cover_large", "thumbnail",
        ):
            candidates.extend(_douyin_url_list(video.get(key)))
    # Douyin has returned gallery images under several equivalent containers
    # over time (``images``, ``image_post_info.images``, ``image_list``).  The
    # downloader already understands all of these for media; use the same
    # tolerant extraction for the thumbnail so a schema variant cannot make
    # an otherwise downloadable card stay blank.
    image_containers = [aweme.get("images"), aweme.get("image_list"), aweme.get("image_infos")]
    image_post_info = aweme.get("image_post_info") or {}
    if isinstance(image_post_info, dict):
        image_containers.extend(
            [image_post_info.get("images"), image_post_info.get("image_list"), image_post_info.get("image_infos")]
        )
    seen_containers = set()
    for images in image_containers:
        if not isinstance(images, list) or id(images) in seen_containers:
            continue
        seen_containers.add(id(images))
        # A gallery may omit the first image's display_image while later image
        # entries still have a usable thumbnail. Try a few entries in order.
        for image in images[:4]:
            if not isinstance(image, dict):
                continue
            for key in (
                "origin_image", "original_image", "url_list", "display_image",
                "thumbnail", "cover", "image_url", "url",
            ):
                candidates.extend(_douyin_url_list(image.get(key)))
    if isinstance(image_post_info, dict):
        for key in ("cover", "cover_url", "cover_urls", "thumbnail", "thumb"):
            candidates.extend(_douyin_url_list(image_post_info.get(key)))
    for key in ("cover_url", "cover_urls", "thumbnail", "thumb"):
        candidates.extend(_douyin_url_list(aweme.get(key)))
    # 保序去重。主机白名单会在实际代理请求时再次校验。
    return douyin_clean_media_urls(candidates)


def douyin_video_choices(video):
    choices = []
    excluded = douyin_download_renditions(video)

    def add(address, entry, original=False):
        urls = [url for url in douyin_clean_media_urls(address) if douyin_rendition_key(url) not in excluded]
        if not urls:
            return
        details = address if isinstance(address, dict) else {}
        choices.append({"urls": urls, "width": details.get("width") or entry.get("width"),
                        "height": details.get("height") or entry.get("height"),
                        "fps": entry.get("FPS") or entry.get("fps"), "bitrate": entry.get("bit_rate"),
                        "bytes": details.get("data_size"), "original": original, "clean_playback": True})

    for field in ("origin_play_addr", "original_play_addr", "origin_video", "original_video"):
        add(video.get(field), {}, original=True)
    for entry in video.get("bit_rate") or []:
        if isinstance(entry, dict):
            add(entry.get("play_addr"), entry)
    for field in ("play_addr", "play_addr_h264", "play_addr_265", "play_addr_bytevc1"):
        add(video.get(field), {})
    # The API also advertises official playback URLs for the same video ID.
    # Their default rendition is sometimes larger than every adaptive CDN entry.
    address = video.get("play_addr") or {}
    media_id = str(address.get("uri") or "") if isinstance(address, dict) else ""
    if re.fullmatch(r"[A-Za-z0-9]{10,80}", media_id):
        for entry in choices:
            links = (urlsplit(url) for url in entry["urls"])
            if any(link.hostname in DOUYIN_PLAY_HOSTS and link.path.rstrip("/") == "/aweme/v1/play"
                   and parse_qs(link.query).get("video_id") == [media_id] for link in links):
                query = urlencode({"video_id": media_id, "ratio": "default", "watermark": 0})
                choices.append({"urls": ["https://www.douyin.com/aweme/v1/play/?" + query],
                                "inspect_first": True, "clean_playback": True})
                break
    return sorted(choices, key=video_rank, reverse=True)


def douyin_aweme_media(aweme: dict) -> dict | None:
    content_id = str(aweme.get("aweme_id") or "")
    if not content_id:
        return None
    post = aweme.get("image_post_info") or {}
    images = (aweme.get("images") or aweme.get("image_list") or aweme.get("image_infos")
              or post.get("images") or post.get("image_list") or post.get("image_infos") or [])
    media_items = []
    has_motion = False
    for image in images if isinstance(images, list) else []:
        if not isinstance(image, dict):
            continue
        still_urls: list[str] = []
        for key in ("origin_image", "original_image", "origin_url", "url_list", "display_image"):
            still_urls.extend(_douyin_url_list(image.get(key)))
        has_still = bool(still_urls or _douyin_url_list(image.get("download_url_list")))
        still_urls = douyin_clean_media_urls(still_urls)
        motion_urls: list[str] = []
        choices = []
        video = image.get("video")
        motion_present = isinstance(video, dict) and any(video.get(field) for field in (
            "play_addr", "play_addr_h264", "play_addr_265", "play_addr_bytevc1", "download_addr", "bit_rate",
            "origin_play_addr", "original_play_addr", "origin_video", "original_video",
        ))
        if isinstance(video, dict):
            choices = douyin_video_choices(video)
            if choices:
                motion_urls = choices[0]["urls"]
        if not motion_urls:
            for key in (
                "animated_url_list", "animated_url", "gif_url_list", "gif_url",
                "live_url_list", "live_url", "motion_url_list", "motion_url",
            ):
                raw = _douyin_url_list(image.get(key))
                motion_present = motion_present or bool(raw)
                motion_urls = douyin_clean_media_urls(raw)
                if motion_urls:
                    choices = [{"urls": motion_urls, "clean_playback": True}]
                    break
        has_motion = has_motion or motion_present
        if has_still or motion_present:
            origin = image.get("origin_image") or image.get("original_image") or {}
            details = origin if isinstance(origin, dict) else {}
            media_items.append({"image_urls": still_urls, "motion_urls": motion_urls, "motion_variants": choices,
                                "has_still": has_still, "has_motion": motion_present,
                                "width": details.get("width") or image.get("width"),
                                "height": details.get("height") or image.get("height")})
    if media_items:
        return {
            "content_id": content_id,
            "kind": "live_photo" if has_motion else "image_gallery",
            "clean_source_revision": DOUYIN_CLEAN_REVISION,
            "item_count": len(media_items),
            "items": media_items,
        }
    video = aweme.get("video") or {}
    choices = douyin_video_choices(video) if isinstance(video, dict) else []
    if isinstance(video, dict) and (choices or video.get("download_addr") or video.get("has_watermark")):
        # Preserve the already-authorized CDN candidates from the creator
        # inventory.  Download workers can use these frozen URLs directly and
        # avoid one aweme_detail request per child.  A refresh is still used as
        # a fallback when a signed URL has expired.
        return {
            "content_id": content_id,
            "kind": "video",
            "clean_source_revision": DOUYIN_CLEAN_REVISION,
            "item_count": 1,
            "video_urls": choices[0]["urls"] if choices else [],
            "video_variants": choices,
        }
    return None


DOUYIN_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

def douyin_api_request(endpoint: str, query: dict, jar, referer: str) -> Request:
    """Bind current web request signature to the user's existing valid UIFID.

    Protocol reference: ucmao/media-parser, src/parsers/douyin_parser.py,
    _sign_secsdk (reviewed 2026-09-22). No new credential is generated.
    """
    from urllib.parse import quote
    if urlsplit(endpoint).hostname != "www.douyin.com":
        raise ArchiveError("invalid_endpoint", "抖音会话仅能发送到抖音 API。", 69)
    values = {c.name: c.value for c in jar
              if c.domain.lstrip(".") in {"douyin.com", "www.douyin.com"} and not c.is_expired()}
    uifid = values.get("UIFID") or values.get("UIFID_TEMP")
    if not uifid:
        raise ArchiveError("reauthentication_required", "抖音登录态缺少有效 UIFID，请重新登录抖音并导入 Cookie。", 69)
    params = {k: str(v) for k, v in query.items() if k not in {"uifid", "timestamp", "x-secsdk-web-signature"}}
    timestamp = str(int(time.time()))
    params.update(uifid=uifid, timestamp=timestamp)
    canonical = "&".join(k + "=" + quote(v, safe="!*'()") for k, v in params.items())
    digest = hashlib.md5(f"{uifid}_{timestamp}_A96D855A08C0A9707F8BEF0D9A527E4E_{canonical}".encode()).hexdigest()
    return Request(endpoint + "?" + canonical + "&x-secsdk-web-signature=" + digest,
                   headers={"User-Agent": DOUYIN_USER_AGENT, "Referer": referer, "Accept": "application/json", "uifid": uifid})

def read_douyin_response(opener, make_request, stage: str) -> dict:
    for attempt in range(3):
        try:
            return _read_douyin_response_once(opener, make_request(), stage)
        except ArchiveError as exc:
            if exc.code not in {"network_timeout", "upstream_unavailable", "rate_limited"} or attempt == 2:
                raise
            logging.warning("douyin retry stage=%s attempt=%s code=%s", stage, attempt + 1, exc.code)
            time.sleep(operation_wait(2 ** attempt))

def _read_douyin_response_once(opener, request: Request, stage: str) -> dict:
    try:
        with opener.open(request, timeout=operation_wait(30)) as response:
            raw = read_limited_response(response, 4 * 1024 * 1024)
            status = response.status
        if not raw.strip():
            logging.warning("douyin stage=%s http=%s code=douyin_empty_response", stage, status)
            raise ArchiveError(
                "douyin_empty_response",
                f"抖音{stage}返回空响应（HTTP {status}）。请在所选浏览器确认能打开该主页，再重新盘点。",
                69,
            )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("expected JSON object")
        return payload
    except HTTPError as exc:
        body = exc.read(1024).decode("utf-8", "replace")
        if "Uifid Not Found" in body:
            code, message = "reauthentication_required", "抖音缺少有效会话标识，请重新导入抖音 Cookie。"
        elif "Signature Not Found" in body or "Signature" in body:
            code, message = "douyin_signature_rejected", "抖音接口拒绝请求签名，请保留日志供检查，不要反复提交。"
        elif exc.code in {401, 403}:
            code, message = "reauthentication_required", "抖音拒绝当前登录态，请在浏览器确认可访问该主页后重新导入 Cookie。"
        elif exc.code == 429:
            code, message = "rate_limited", "抖音请求频率受限，请稍后再试。"
        else:
            code, message = "upstream_unavailable" if exc.code >= 500 else "http_error", f"抖音{stage}返回 HTTP {exc.code}。"
        logging.error("douyin stage=%s http=%s code=%s", stage, exc.code, code)
        raise ArchiveError(code, message, 69) from exc
    except (URLError, TimeoutError) as exc:
        logging.error("douyin stage=%s network_type=%s", stage, type(exc).__name__)
        raise ArchiveError("network_timeout", f"抖音{stage}网络连接失败或超时。", 69) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        logging.error("douyin stage=%s invalid_json", stage)
        raise ArchiveError("invalid_api_response", f"抖音{stage}返回无效 JSON，请查看日志。", 69) from exc


def douyin_author_fields(author, creator_id):
    if not isinstance(author, dict) or (author.get("sec_uid") and author["sec_uid"] != creator_id):
        return {}
    avatars = _douyin_safe_media_urls(author.get("avatar_thumb") or author.get("avatar_medium"))
    account = next((str(author[key]) for key in ("unique_id", "short_id") if author.get(key) and str(author[key]) != "0"), "")
    fields = {"name": author.get("nickname"), "account": account, "bio": author.get("signature"),
              "avatar": avatars[0] if avatars else "", "user_id": author.get("uid")}
    return {key: str(value) for key, value in fields.items() if value and str(value) != "0"}


def douyin_creator_inventory(url: str, *, cookie_jar=None, include_profile=True, known_ids=()) -> tuple[dict, list[dict]]:
    resolved = resolve_transparent_core_url(url, "douyin")
    profile_match = re.fullmatch(r"/user/([^/]+)", _split_url(resolved).path.rstrip("/"))
    creator_id = profile_match.group(1) if profile_match else ""
    if not creator_id:
        raise ArchiveError("douyin_creator_missing", "抖音主页链接没有包含作者标识。", 69)
    jar = cookie_jar
    if jar is None:
        cookie_path = platform_cookie_jar("douyin")
        if not cookie_path.is_file():
            raise ArchiveError("reauthentication_required", "请先在所选浏览器登录抖音。", 69)
        jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as exc:
            raise ArchiveError("reauthentication_required", "抖音会话缓存无法读取，请重新导入。", 69) from exc
    opener = build_opener(HTTPCookieProcessor(jar))
    items = []
    cursor = 0
    seen_cursors = set()
    seen_ids = set()
    known = set(known_ids)
    known_pages = 0
    creator_profile = {}
    referer = f"https://www.douyin.com/user/{creator_id}"
    common = {"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
              "sec_user_id": creator_id, "version_code": "170400", "version_name": "17.4.0", "pc_client_type": 1}
    if include_profile:
        try:
            details = _read_douyin_response_once(opener, douyin_api_request(
                "https://www.douyin.com/aweme/v1/web/user/profile/other/", common, jar, referer), "作者资料")
            if details.get("status_code") == 0:
                creator_profile = douyin_author_fields(details.get("user"), creator_id)
        except ArchiveError as exc:
            logging.info("douyin profile unavailable code=%s", exc.code)
    while True:
        query = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "sec_user_id": creator_id,
            "max_cursor": cursor,
            "count": 18,
            "publish_video_strategy_type": 2,
            "locate_query": "false",
            "pc_client_type": 1,
            "version_code": "170400",
            "version_name": "17.4.0",
            "cookie_enabled": "true",
            "browser_language": "zh-CN",
            "browser_platform": "Win32" if os.name == "nt" else "MacIntel",
            "browser_name": "Chrome",
            "platform": "PC",
        }
        payload = read_douyin_response(opener, lambda: douyin_api_request(
            "https://www.douyin.com/aweme/v1/web/aweme/post/", query, jar, referer), "作者作品列表")
        if payload.get("status_code") != 0:
            raise ArchiveError("reauthentication_required", "抖音登录态已失效或触发平台验证。", 69)
        posts = payload.get("aweme_list") or []
        for aweme in posts:
            if not isinstance(aweme, dict):
                continue
            if len(creator_profile) < 5:
                for key, value in douyin_author_fields(aweme.get("author"), creator_id).items():
                    creator_profile.setdefault(key, value)
            content_id = str(aweme.get("aweme_id") or "")
            if content_id in known:
                continue
            if known:
                if content_id and content_id not in seen_ids:
                    seen_ids.add(content_id)
                    items.append({"id": content_id, "published_at": aweme.get("create_time")})
                continue
            media = douyin_aweme_media(aweme)
            if media and content_id and content_id not in seen_ids:
                seen_ids.add(content_id)
                items.append(
                    {
                        "id": content_id,
                        "url": f"https://www.douyin.com/video/{content_id}",
                        "title": str((aweme.get("desc") or "未命名作品")).strip(),
                        "published_at": aweme.get("create_time"),
                        "media_kind": media["kind"],
                        "media_item_count": media["item_count"],
                        "douyin_media": media,
                        "preview_urls": douyin_aweme_preview_urls(aweme),
                        "child_job_id": None,
                    }
                )
        if known:
            ids = [str(post.get("aweme_id") or "") for post in posts if isinstance(post, dict)]
            known_pages = known_pages + 1 if ids and all(post_id in known for post_id in ids) else 0
        if not payload.get("has_more") or known_pages >= 2:
            break
        next_cursor = int(payload.get("max_cursor") or 0)
        if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
            raise ArchiveError("douyin_cursor_invalid", "抖音作者分页游标没有前进。", 69)
        seen_cursors.add(cursor)
        cursor = next_cursor
    items.sort(key=lambda item: int(item.get("published_at") or 0), reverse=True)
    return {
        "id": creator_id,
        "url": referer,
        **{"name": creator_id, **creator_profile},
    }, items


def _walk_json(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _xiaohongshu_initial_state(page: str) -> dict:
    match = re.search(
        r"<script[^>]*>\s*window\.__INITIAL_STATE__\s*=\s*(.*?)\s*</script>",
        page,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        raise ArchiveError("xiaohongshu_creator_state_missing", "小红书主页没有返回可读取的作品清单。", 69)
    payload = match.group(1).strip().removesuffix(";")
    tokens = r'"(?:\\.|[^"\\])*"|new\s+(?:Set|Map)\(\s*(?:\[\s*\])?\s*\)|\bundefined\b'
    payload = re.sub(tokens, lambda item: item[0] if item[0].startswith('"') else "null" if item[0] == "undefined" else "[]", payload)
    try:
        state = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ArchiveError("xiaohongshu_creator_state_invalid", "小红书主页作品清单格式已变化。", 69) from exc
    if not isinstance(state, dict):
        raise ArchiveError("xiaohongshu_creator_state_invalid", "小红书主页作品清单格式无效。", 69)
    return state


def prepare_runtime(group):
    """Standalone mode uses the tools already configured on the host."""
    return None


def _redbook_command() -> list[str]:
    prepare_runtime("node")
    configured = os.environ.get("WECHAT_REDBOOK_CLI", "").strip()
    candidates = [configured] if configured else []
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        candidates.append(
            str(Path(os.environ["LOCALAPPDATA"]) / "wechat-archive" / "tools" / "redbook" / "node_modules" / ".bin" / "redbook.cmd")
        )
        # Prefer the real JS entry point when the npm-generated .cmd shim is
        # unreadable/blocked by Windows security software.  This is equivalent
        # to invoking the shim and avoids a misleading "WinError 5".
        candidates.append(
            str(Path(os.environ["LOCALAPPDATA"]) / "wechat-archive" / "tools" / "redbook" / "node_modules" / "@lucasygu" / "redbook" / "dist" / "cli.js")
        )
    candidates.extend(["redbook.cmd", "redbook.exe", "redbook"])
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate)
        if resolved:
            path = Path(resolved)
        else:
            path = Path(candidate).expanduser()
        try:
            is_file = path.is_file()
        except OSError:
            # A stale/inaccessible .cmd shim should not prevent trying the
            # direct JS entry point or another configured installation.
            continue
        if is_file:
            path = path.resolve()
            if os.name == "nt" and path.suffix.lower() == ".cmd":
                cli = path.parent.parent / "@lucasygu" / "redbook" / "dist" / "cli.js"
                node = shutil.which("node.exe") or shutil.which("node")
                try:
                    cli_exists = cli.is_file()
                except OSError:
                    cli_exists = False
                if cli_exists and node:
                    return [node, str(cli)]
            if os.name == "nt" and path.suffix.lower() == ".js":
                node = shutil.which("node.exe") or shutil.which("node")
                if node:
                    return [node, str(path)]
            return [str(path)]
    raise ArchiveError(
        "xiaohongshu_full_backend_missing",
        "未找到小红书全量分页后端 redbook。请先运行 Install-XiaohongshuBackend.ps1。",
        69,
    )


def _firefox_xiaohongshu_cookie_file(backend="xhs") -> Path | None:
    if os.name != "nt":
        return None
    firefox_root = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox"
    profiles_ini = firefox_root / "profiles.ini"
    try:
        profiles_available = profiles_ini.is_file()
    except OSError:
        return None
    if not profiles_available:
        return None
    parser = configparser.RawConfigParser()
    parser.read(profiles_ini, encoding="utf-8")
    candidates: list[tuple[int, Path]] = []
    for section in parser.sections():
        if not section.startswith("Profile"):
            continue
        raw_path = parser.get(section, "Path", fallback="").strip()
        if not raw_path:
            continue
        profile = firefox_root / raw_path if parser.getboolean(section, "IsRelative", fallback=True) else Path(raw_path)
        database = profile / "cookies.sqlite"
        try:
            if database.is_file():
                candidates.append((1 if parser.get(section, "Default", fallback="0") == "1" else 0, database))
        except OSError:
            continue
    # Prefer a profile that actually contains a valid XHS session over an empty
    # profile merely marked Default in profiles.ini.
    domain = "rednote.com" if backend == "rednote" else "xiaohongshu.com"
    for _, database in sorted(candidates, reverse=True):
        try:
            # A live readonly SQLite connection sees Firefox's committed WAL writes.
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                rows = connection.execute(
                    "SELECT name, value FROM moz_cookies "
                    "WHERE (host = ? OR host LIKE ?) AND expiry > ? AND originAttributes = '' "
                    "ORDER BY lastAccessed",
                    (domain, "%." + domain, int(time.time())),
                ).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            continue
        cookies = {str(name): str(value) for name, value in rows if name and value}
        if not cookies.get("a1") or not cookies.get("web_session"):
            continue
        descriptor, name = tempfile.mkstemp(prefix="wechat-xhs-redbook-", suffix=".json")
        cookie_file = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({"version": 1, "platform": backend, "cookies": cookies}, handle, ensure_ascii=False)
                handle.write("\n")
            try:
                os.chmod(cookie_file, 0o600)
            except OSError:
                pass
            return cookie_file
        except Exception:
            cookie_file.unlink(missing_ok=True)
            raise
    return None


# The redbook CLI accepts a small JSON cookie file.  Creating that file by
# copying Firefox's SQLite database is reliable, but doing it for every child
# in a large creator batch is needlessly expensive.  Keep one short-lived,
# permission-restricted snapshot per worker process and remove it at exit.
_REDBOOK_COOKIE_CACHE: dict[str, Path | None] = {}
_REDBOOK_COOKIE_CACHE_LOCK = threading.Lock()
_REDBOOK_NOTE_CACHE: dict[str, dict] = {}
_REDBOOK_NOTE_CACHE_LOCK = threading.Lock()
_REDBOOK_RETIRED_COOKIES = []
_REDBOOK_CACHE_TIMES = {}
_REDBOOK_NOTE_TIMES = {}

def invalidate_redbook_cache():
    # Existing subprocesses may still be reading the previous snapshot.
    # Retain it until process exit instead of deleting another worker's input.
    with _REDBOOK_COOKIE_CACHE_LOCK:
        _REDBOOK_RETIRED_COOKIES.extend(path for path in _REDBOOK_COOKIE_CACHE.values() if path is not None)
        _REDBOOK_COOKIE_CACHE.clear()
        _REDBOOK_CACHE_TIMES.clear()
    with _REDBOOK_NOTE_CACHE_LOCK:
        _REDBOOK_NOTE_CACHE.clear()
        _REDBOOK_NOTE_TIMES.clear()



def _redbook_cookie_file(browser: str | None, backend="xhs") -> Path | None:
    browser = str(browser or "").strip().lower()
    if browser != "firefox":
        return None
    with _REDBOOK_COOKIE_CACHE_LOCK:
        key = (browser, backend)
        cached = _REDBOOK_COOKIE_CACHE.get(key)
        if cached is not None and cached.is_file() and time.monotonic() - _REDBOOK_CACHE_TIMES.get(key, 0) < 300:
            return cached
        if cached is not None:
            _REDBOOK_RETIRED_COOKIES.append(cached)
        snapshot = _firefox_xiaohongshu_cookie_file(backend)
        _REDBOOK_COOKIE_CACHE[key] = snapshot
        _REDBOOK_CACHE_TIMES[key] = time.monotonic()
        return snapshot


def _cleanup_redbook_cookie_cache() -> None:
    with _REDBOOK_COOKIE_CACHE_LOCK:
        paths = [path for path in _REDBOOK_COOKIE_CACHE.values() if path is not None] + _REDBOOK_RETIRED_COOKIES[:]
        _REDBOOK_RETIRED_COOKIES.clear()
        _REDBOOK_COOKIE_CACHE.clear()
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


atexit.register(_cleanup_redbook_cookie_cache)


def _redbook_safe_backend_text(value: str) -> str:
    """Keep backend diagnostics useful without echoing URLs or cookies."""
    text = re.sub(r"https?://\S+", "<url>", str(value or ""))
    text = re.sub(r"(?i)(?:a1|web_session|cookie)\s*[=:]\s*[^\s,;]+", "<cookie>", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "；".join(lines[-3:])[:600]


def _redbook_backend(url: str) -> str:
    return "rednote" if host_allowed(_split_url(url).hostname or "", {"rednote.com"}, (".rednote.com",)) else "xhs"


def _redbook_domain(backend: str) -> str:
    return "www.rednote.com" if backend == "rednote" else "www.xiaohongshu.com"


def _redbook_note_url(url: str, backend: str) -> str:
    normalized, platform = submission_url(url)
    if platform != "xiaohongshu":
        raise ArchiveError("invalid_xiaohongshu_url", "小红书详情链接的域名无效。", 69)
    parsed = _split_url(normalized)
    if not re.fullmatch(r"/(?:explore|discovery/item)/[0-9a-fA-F]{24}/?", parsed.path):
        raise ArchiveError("invalid_xiaohongshu_url", "小红书详情链接缺少有效作品标识。", 69)
    return urlunsplit(("https", _redbook_domain(backend), parsed.path, parsed.query, ""))


def _redbook_auth_error(completed, backend):
    detail = completed.stderr or completed.stdout
    if re.search(r"(?i)session expired|code:\s*-10[01]\b|no cookies|missing or incomplete login cookies", detail):
        domain = _redbook_domain(backend)
        return ArchiveError(
            "reauthentication_required",
            f"小红书接口拒绝当前登录态。请在 Firefox 打开 {domain} 确认登录，然后复制浏览器地址栏中的完整作者主页链接。",
            69,
        )
    if re.search(r"(?i)captcha required|verification required", detail):
        return ArchiveError("reauthentication_required", "小红书要求安全验证，请在所选浏览器打开作品完成验证。", 69)
    return None


def _rednote_page_note(url, browser, *, guest_only=False):
    """Read note data embedded in the platform's normal webpage."""
    note_id = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[0-9a-fA-F]{24}", note_id):
        return None
    try:
        if guest_only:
            page_url, platform = submission_url(url)
            if platform != "xiaohongshu":
                return None
            opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                  SafeRedirectHandler(XHS_WEB_HOSTS, (".xiaohongshu.com", ".rednote.com")))
        else:
            jar = extract_platform_browser_cookies("xiaohongshu", browser)
            page_url = _redbook_note_url(url, "rednote")
            opener = build_opener(HTTPCookieProcessor(jar), SafeRedirectHandler({"www.rednote.com", "rednote.com"}, ()))
        request = Request(page_url, headers={"User-Agent": DOUYIN_USER_AGENT, "Accept": "text/html"})
        with opener.open(request, timeout=operation_wait(12)) as response:
            body = response.read(MAX_HTML_BYTES + 1)
        if len(body) > MAX_HTML_BYTES:
            return None
        state = _xiaohongshu_initial_state(body.decode("utf-8", errors="replace"))
        note_state = state.get("note") or {}
        details = (note_state.get("noteDetailMap") or {}).get(note_id) or {}
        note = details.get("note") or {}
        note_key = str(note.get("noteId") or note.get("note_id") or note.get("id") or "")
        if note_key.lower() == note_id.lower() and (note.get("imageList") or note.get("image_list") or note.get("video")):
            return note
    except (ArchiveError, URLError, OSError, ValueError):
        pass
    return None


def _redbook_read_note(url: str, browser: str | None = None) -> dict:
    """Read one XHS note through the authenticated redbook API backend.

    The embedded yt-dlp extractor can recognize a note URL but frequently
    returns no media for modern XHS pages.  redbook's API route returns the
    authenticated image list/video streams directly, so it is the primary
    detail path for creator children.  The returned object is the raw note;
    callers should pass it through ``_redbook_note_info`` before archiving.
    """
    normalized, platform = submission_url(url)
    if platform != "xiaohongshu":
        raise ArchiveError("invalid_xiaohongshu_url", "小红书详情后端收到的不是小红书链接。", 69)
    cache_key = (str(browser or ""), canonical_url(normalized))
    with _REDBOOK_NOTE_CACHE_LOCK:
        cached_note = _REDBOOK_NOTE_CACHE.get(cache_key)
        if cached_note is not None and time.monotonic() - _REDBOOK_NOTE_TIMES.get(cache_key, 0) < 60:
            return cached_note
    backend = _redbook_backend(normalized)
    if backend == "rednote" and browser in {"firefox", "chrome"}:
        page_note = _rednote_page_note(normalized, browser)
        if page_note:
            with _REDBOOK_NOTE_CACHE_LOCK:
                _REDBOOK_NOTE_CACHE[cache_key] = page_note
                _REDBOOK_NOTE_TIMES[cache_key] = time.monotonic()
            return page_note
    command = [*_redbook_command(), "read", "--api", "--platform", backend, "--json", normalized]
    if browser in {"firefox", "chrome"}:
        command.extend(["--cookie-source", browser])
    environment = os.environ.copy()
    cookie_file = _redbook_cookie_file(browser, backend)
    if cookie_file:
        environment["REDBOOK_COOKIE_FILE"] = str(cookie_file)
        environment["REDBOOK_PLATFORM"] = backend
    try:
        completed = run_background_process(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArchiveError("xiaohongshu_detail_timeout", "小红书详情接口超时，请稍后重试。", 69) from exc
    except OSError as exc:
        raise ArchiveError("xiaohongshu_detail_backend_failed", "小红书详情后端无法运行。", 69) from exc
    if completed.returncode != 0:
        auth = _redbook_auth_error(completed, backend)
        if auth:
            raise auth
        if re.search(r"(?i)fetch failed|timeout|timed out", completed.stderr or ""):
            raise ArchiveError("xiaohongshu_detail_network", "小红书作品详情网络请求失败或超时。", 69)
        raise ArchiveError("xiaohongshu_detail_failed", "小红书详情接口读取失败，请使用可打开的作品分享链接重试。", 69)
    try:
        # JSON mode is clean today, but accepting a leading informational line
        # makes the worker tolerant of future redbook releases.
        raw = completed.stdout.strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("JSON object missing")
        payload = json.loads(raw[start : end + 1])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        detail = _redbook_safe_backend_text(completed.stdout or completed.stderr)
        raise ArchiveError("xiaohongshu_detail_invalid", f"小红书详情接口返回无效数据{('：' + detail) if detail else '。'}", 69) from exc
    if not isinstance(payload, dict):
        raise ArchiveError("xiaohongshu_detail_invalid", "小红书详情接口返回的数据格式无效。", 69)
    # Keep the cache keyed by the canonical URL.  Do not cache an empty or
    # malformed response: a later retry may succeed after a transient CDN/API
    # response.
    if payload.get("note_id") or payload.get("noteId") or payload.get("id"):
        with _REDBOOK_NOTE_CACHE_LOCK:
            _REDBOOK_NOTE_CACHE[cache_key] = payload
            _REDBOOK_NOTE_TIMES[cache_key] = time.monotonic()
        return payload
    raise ArchiveError("xiaohongshu_detail_missing", "小红书详情接口没有返回作品内容。", 69)


def _xhs_safe_media_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = _split_url(raw)
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            return ""
        if not (
            host in {"xhscdn.com", "xhsimg.com"}
            or host in XHS_MEDIA_EXACT_HOSTS
            or any(host.endswith(suffix) for suffix in XHS_MEDIA_HOST_SUFFIXES)
        ):
            return ""
        return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))
    except ArchiveError:
        return ""


def _redbook_stream_urls(stream: object) -> list[str]:
    if not isinstance(stream, dict):
        return []
    values: list[str] = []
    for key in ("master_url", "url"):
        candidate = _xhs_safe_media_url(stream.get(key))
        if candidate and candidate not in values:
            values.append(candidate)
    backups = stream.get("backup_urls") or stream.get("backupUrls") or []
    if isinstance(backups, str):
        backups = [backups]
    if isinstance(backups, list):
        for value in backups:
            candidate = _xhs_safe_media_url(value)
            if candidate and candidate not in values:
                values.append(candidate)
    return values


def redbook_video_choices(streams):
    choices = []
    if not isinstance(streams, dict):
        return choices
    for codec, entries in streams.items():
        for entry in entries if isinstance(entries, list) else []:
            urls = _redbook_stream_urls(entry)
            if urls:
                choices.append({"urls": urls, "width": entry.get("width"), "height": entry.get("height"),
                                "fps": entry.get("fps"), "bitrate": entry.get("video_bit_rate") or entry.get("avg_bit_rate"),
                                "bytes": entry.get("size"), "codec": codec})
    return sorted(choices, key=video_rank, reverse=True)


def _xhs_original_image_urls(value: object, file_id=None) -> list[str]:
    """Recover the media object path from a file ID or cache URL."""
    safe = _xhs_safe_media_url(value)
    parsed = _split_url(safe) if safe else None
    segments = [part for part in parsed.path.split("!", 1)[0].split("/") if part] if parsed else []
    if len(segments) >= 3 and re.fullmatch(r"\d{8,14}", segments[0]):
        segments = segments[2:]
    paths = list(dict.fromkeys(path for path in ("/".join(segments), str(file_id or ""))
                              if re.fullmatch(r"[\w.-]+(?:/[\w.-]+)*", path, flags=re.ASCII)
                              and not any(part in {".", ".."} for part in path.split("/"))))
    regional = bool(parsed and (parsed.hostname or "").endswith(".rednotecdn.com"))
    candidates = []
    for path in paths:
        native = [f"https://sns-img-bd.xhscdn.com/{path}", f"https://sns-img-hw.xhscdn.com/{path}"]
        endpoint = f"https://ci.xiaohongshu.com/{path}"
        full_size = [endpoint, endpoint + "?imageView2/2/w/0/format/jpg/v3"]
        candidates.extend([*full_size, *native] if regional else [*native, *full_size])
    return list(dict.fromkeys(candidates))


def _xhs_web_field_names(value):
    if isinstance(value, dict):
        return {re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", key).lower(): _xhs_web_field_names(child)
                for key, child in value.items()}
    if isinstance(value, list):
        return [_xhs_web_field_names(item) for item in value]
    return value


def _redbook_note_info(note: dict, source_url: str) -> dict:
    """Normalize a redbook API note to the archive worker's info shape."""
    note = _xhs_web_field_names(note)
    note_id = str(note.get("note_id") or note.get("id") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{24}", note_id):
        raise ArchiveError("xiaohongshu_detail_invalid", "小红书详情缺少有效作品标识。", 69)
    image_items: list[dict] = []
    live_photo_urls: list[str] = []
    live_photo_groups: list[list[str]] = []
    live_variants = []
    for image in note.get("image_list") or []:
        if not isinstance(image, dict):
            continue
        candidates: list[object] = [image.get("url_default"), image.get("url"), image.get("url_pre")]
        info_list = image.get("info_list") or []
        if isinstance(info_list, list):
            # Prefer the full/default scene, then any valid URL.
            ordered = sorted(
                (entry for entry in info_list if isinstance(entry, dict)),
                key=lambda entry: 0 if str(entry.get("image_scene") or "").upper() in {"WB_DFT", "ORIGINAL"} else 1,
            )
            candidates.extend(entry.get("url") for entry in ordered)
        image_urls: list[str] = []
        original_urls: list[str] = []
        for value in candidates:
            safe_value = _xhs_safe_media_url(value)
            if safe_value and safe_value not in image_urls:
                image_urls.append(safe_value)
            for original in _xhs_original_image_urls(safe_value, image.get("file_id")):
                if original not in original_urls:
                    original_urls.append(original)
        image_url = image_urls[0] if image_urls else ""
        if not (image_urls or original_urls):
            continue
        image_items.append(
            {
                "url": image_url,
                "urls": image_urls,
                "original_urls": original_urls,
                "live_photo": bool(image.get("live_photo")),
                "width": image.get("width"),
                "height": image.get("height"),
            }
        )
        if image.get("live_photo"):
            choices = redbook_video_choices(image.get("stream"))
            image_live_urls = list(dict.fromkeys(url for choice in choices for url in choice["urls"]))
            if image_live_urls:
                live_photo_groups.append(image_live_urls)
                live_variants.append(choices)
                image_items[-1]["motion_index"] = len(live_photo_groups)
                live_photo_urls.extend(url for url in image_live_urls if url not in live_photo_urls)
        if len(image_items) >= MAX_MEDIA_ITEMS:
            break

    video = note.get("video") if isinstance(note.get("video"), dict) else {}
    media = video.get("media") if isinstance(video, dict) and isinstance(video.get("media"), dict) else {}
    choices = redbook_video_choices(media.get("stream"))
    consumer = video.get("consumer") or {}
    origin_key = consumer.get("origin_video_key") if isinstance(consumer, dict) else None
    if isinstance(origin_key, str) and re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*", origin_key) and ".." not in origin_key.split("/"):
        choices.insert(0, {"urls": [f"https://sns-video-{edge}.xhscdn.com/{origin_key}" for edge in ("bd", "hw")], "original": True})
    video_urls = choices[0]["urls"] if choices else []
    return {
        "id": note_id,
        "title": str(note.get("title") or note.get("desc") or "未命名小红书作品").strip(),
        "description": str(note.get("desc") or "").strip(),
        "thumbnails": image_items,
        "_xhs_video_urls": video_urls,
        "_xhs_video_variants": choices,
        "_xhs_live_photo_urls": live_photo_urls,
        "_xhs_live_photo_groups": live_photo_groups,
        "_xhs_live_variants": live_variants,
        "_xhs_note_type": str(note.get("type") or "normal").strip().lower(),
        "webpage_url": source_url,
    }


def _download_xhs_video(urls: list[str], target: Path, referer: str) -> None:
    last_error: Exception | None = None
    for url in prefer_media_routes(urls)[:4]:
        try:
            download_public_https(url, target, referer=referer, max_bytes=MAX_AUDIO_BYTES)
            if target.is_file() and target.stat().st_size > 1024:
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            target.unlink(missing_ok=True)
        # XHS live-photo streams are often small MP4 responses whose edge
        # closes a persistent TLS connection when a Range header is present.
        # Retry the same safe URL through the bounded in-memory fetcher before
        # moving to a backup URL; this avoids a false ``live_photo_incomplete``.
        try:
            data, content_type, _ = fetch_limited(
                url,
                exact_hosts=set(),
                suffixes=XHS_MEDIA_HOST_SUFFIXES,
                max_bytes=MAX_MEDIA_BYTES,
            )
            if len(data) > 1024 and (content_type.startswith("video/") or data[4:8] == b"ftyp"):
                atomic_write(target, data)
                report_media_bytes(len(data))
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if isinstance(last_error, ArchiveError):
        raise last_error
    raise ArchiveError("xiaohongshu_video_missing", "小红书视频直链不可用。", 69) from last_error


def _xiaohongshu_full_inventory(url: str, browser: str, *, target_ids=None, include_profile=True, known_ids=()) -> tuple[dict, list[dict]]:
    if browser not in {"chrome", "firefox"}:
        raise ArchiveError("xiaohongshu_browser_invalid", "小红书全量盘点只接受 chrome 或 firefox。")
    backend = _redbook_backend(url)
    command = [*_redbook_command(), "user-posts", url, "--cookie-source", browser, "--platform", backend, "--all", "--json"]
    if target_ids:
        command.extend(["--target-ids", ",".join(sorted(target_ids))])
    elif include_profile:
        command.append("--with-profile")
    if not known_ids:
        command.extend(["--delay", "450"])
        remaining_budget = operation_wait(1800)
        if remaining_budget is not None and remaining_budget < 120:
            max_ms = max(6000, int((remaining_budget - 4.0) * 1000))
            command.extend(["--max-duration", str(max_ms)])
    cookie_file = _firefox_xiaohongshu_cookie_file(backend) if browser == "firefox" else None
    environment = os.environ.copy()
    if cookie_file:
        environment["REDBOOK_COOKIE_FILE"] = str(cookie_file)
        environment["REDBOOK_PLATFORM"] = backend
    try:
        try:
            if known_ids:
                with tempfile.TemporaryDirectory(prefix="recent-notes-") as directory:
                    known = Path(directory) / "known.json"
                    known.write_text(json.dumps(list(known_ids)), encoding="utf-8")
                    remaining_budget = operation_wait(60)
                    known_cmd = [*command, "--known-posts", str(known), "--delay", "500"]
                    if remaining_budget is not None:
                        known_cmd.extend(["--max-duration", str(max(5000, int((remaining_budget - 3.5) * 1000)))])
                    completed = run_background_process(
                        known_cmd,
                        capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment,
                        timeout=60,
                    )
            else:
                completed = run_background_process(
                    command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=120 if target_ids else 1800, env=environment,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArchiveError("xiaohongshu_full_inventory_failed", "小红书全量分页后端无法运行或超时。", 69) from exc
    finally:
        if cookie_file:
            cookie_file.unlink(missing_ok=True)
    if completed.returncode != 0:
        auth = _redbook_auth_error(completed, backend)
        if auth:
            raise auth
        raise ArchiveError(
            "xiaohongshu_full_inventory_failed",
            "小红书作品列表读取失败。请确认作者主页能在所选浏览器正常打开，并复制包含查询参数的完整链接。",
            69,
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ArchiveError("xiaohongshu_full_inventory_invalid", "小红书全量分页后端没有返回有效 JSON。", 69) from exc
    notes = payload.get("notes") if isinstance(payload, dict) else None
    if not isinstance(notes, list):
        raise ArchiveError("xiaohongshu_full_inventory_invalid", "小红书全量分页结果缺少作品列表。", 69)
    normalized, _ = submission_url(url)
    user_id = _split_url(normalized).path.rstrip("/").split("/")[-1]
    items: list[dict] = []
    seen: set[str] = set()
    profile = payload.get("profile") or {}
    basic = _xhs_web_field_names(profile.get("basic_info") or profile.get("basicInfo") or {}) if isinstance(profile, dict) else {}
    creator_name = str(basic.get("nickname") or "").strip()
    creator_avatar = _xhs_safe_media_url(basic.get("imageb") or basic.get("images"))

    def preview_urls(note: dict) -> list[str]:
        """Extract only image-like CDN URLs exposed by user-posted cards.

        The redbook API has changed the shape of ``cover``/``image_list``
        several times.  Keep the frozen manifest forward-compatible without
        copying arbitrary description text or video URLs into it.
        """
        urls: list[str] = []
        seen_urls: set[str] = set()
        key_hints = {
            "cover", "thumbnail", "thumb", "image", "images", "imagelist",
            "image_list", "urldefault", "url_default", "urlpre", "url_pre",
            "imageinfo", "image_info", "info_list", "url",
        }

        def walk(value, hint: str = ""):
            if isinstance(value, dict):
                for key, child in value.items():
                    walk(child, str(key).lower().replace("-", "_"))
                return
            if isinstance(value, list):
                for child in value:
                    walk(child, hint)
                return
            if not isinstance(value, str) or not value.startswith(("https://", "http://")):
                return
            normalized_hint = hint.replace("_", "")
            if not (hint in key_hints or normalized_hint in {"cover", "thumbnail", "imagelist", "imagelist"}):
                return
            if value not in seen_urls:
                seen_urls.add(value)
                urls.append(value)

        for key in ("cover", "image_list", "imageList", "images", "thumbnail", "image_info", "imageInfo"):
            if key in note:
                walk(note.get(key), key.lower().replace("-", "_"))
        return urls[:12]

    for note in notes:
        if not isinstance(note, dict):
            continue
        note_id = str(note.get("note_id") or note.get("noteId") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{24}", note_id) or note_id in seen:
            continue
        web_url = str(note.get("web_url") or note.get("webUrl") or "").strip()
        if not web_url:
            token = str(note.get("xsec_token") or note.get("xsecToken") or "").strip()
            query = {"xsec_source": "pc_user"}
            if token:
                query["xsec_token"] = token
            web_url = f"https://{_redbook_domain(backend)}/explore/{note_id}?{urlencode(query)}"
        # Redbook 0.8.2 serializes all signed note URLs using the mainland host.
        # Route each note back to the same regional service that listed it.
        item_url = _redbook_note_url(web_url, backend)
        user = note.get("user") or note.get("user_info") or {}
        if not creator_name and isinstance(user, dict):
            creator_name = str(user.get("nickname") or user.get("nick_name") or "").strip()
        if not creator_avatar and isinstance(user, dict):
            creator_avatar = _xhs_safe_media_url(user.get("image") or user.get("avatar"))
        seen.add(note_id)
        note_type = str(note.get("type") or "").strip().lower()
        items.append({
            "id": note_id,
            "url": item_url,
            "title": str(note.get("display_title") or note.get("displayTitle") or note.get("title") or "未命名小红书作品").strip(),
            "published_at": note.get("time") or note.get("publish_time") or note.get("publishTime"),
            "xiaohongshu_media_type": note.get("type"),
            "media_kind": "video" if note_type == "video" else "image_gallery",
            "preview_urls": preview_urls(note),
            "child_job_id": None,
        })
    if not items and not (known_ids and payload.get("scanned")):
        raise ArchiveError("creator_inventory_empty", "小红书全量分页没有返回可归档作品。", 66)
    inventory_complete = bool(payload.get("complete", True))
    return {
        "id": user_id,
        "user_id": user_id,
        "name": creator_name or user_id,
        "account": str(basic.get("red_id") or ""),
        "bio": str(basic.get("desc") or ""),
        "avatar": creator_avatar,
        "url": normalized,
        "browser": browser,
        "cookie_source": browser,
        "inventory_status": "complete_via_redbook" if inventory_complete else "budget_capped_via_redbook",
        "inventory_complete": inventory_complete,
        "inventory_backend": "@lucasygu/redbook@0.8.2",
        "xiaohongshu_backend": backend,
    }, items


def xiaohongshu_creator_inventory(url: str, browser: str | None = None, *, include_profile=True, known_ids=()) -> tuple[dict, list[dict]]:
    full_browser = (browser or os.environ.get("WECHAT_XHS_CREATOR_BROWSER", "")).strip().lower()
    if full_browser:
        if not include_profile:
            return _xiaohongshu_full_inventory(url, full_browser, include_profile=False, known_ids=known_ids)
        return _xiaohongshu_full_inventory(url, full_browser)
    if os.environ.get("WECHAT_WINDOWS_EXPERIMENTAL_XHS_CREATOR") != "1":
        raise ArchiveError(
            "xiaohongshu_creator_experimental",
            "小红书主页批量仍是实验功能；确认接受仅盘点当前网页可见作品后设置 "
            "WECHAT_WINDOWS_EXPERIMENTAL_XHS_CREATOR=1。",
            69,
        )
    normalized, _ = submission_url(url)
    cookie_path = platform_cookie_jar("xiaohongshu")
    if not cookie_path.is_file():
        raise ArchiveError("reauthentication_required", "请先导入持久小红书 Cookie。", 69)
    jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise ArchiveError("reauthentication_required", "小红书 Cookie 无法读取，请重新导入。", 69) from exc
    request = Request(
        normalized,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        with build_opener(
            HTTPCookieProcessor(jar),
            SafeRedirectHandler({"xiaohongshu.com"}, (".xiaohongshu.com",)),
        ).open(request, timeout=30) as response:
            page = response.read(MAX_HTML_BYTES + 1).decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise ArchiveError("xiaohongshu_creator_failed", "小红书用户主页读取失败。", 69) from exc
    if len(page.encode("utf-8")) > MAX_HTML_BYTES:
        raise ArchiveError("response_too_large", "小红书主页响应过大。", 69)
    if any(marker in page for marker in VERIFICATION_MARKERS):
        raise ArchiveError("reauthentication_required", "小红书要求登录或安全验证。", 69)
    state = _xiaohongshu_initial_state(page)
    user_id = _split_url(normalized).path.rstrip("/").split("/")[-1]
    items = []
    seen = set()
    creator_name = ""
    for node in _walk_json(state):
        if not isinstance(node, dict):
            continue
        if not creator_name:
            creator_name = str(node.get("nickname") or node.get("nickName") or "").strip()
        note_id = str(node.get("noteId") or node.get("note_id") or "")
        token = str(node.get("xsecToken") or node.get("xsec_token") or "")
        title = str(node.get("displayTitle") or node.get("display_title") or node.get("title") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{24}", note_id) or note_id in seen:
            continue
        if not token and not title:
            continue
        seen.add(note_id)
        query = {"xsec_source": "pc_user"}
        if token:
            query["xsec_token"] = token
        items.append(
            {
                "id": note_id,
                "url": f"https://www.xiaohongshu.com/explore/{note_id}?{urlencode(query)}",
                "title": title or "未命名小红书作品",
                "published_at": node.get("time") or node.get("publishTime"),
                "child_job_id": None,
            }
        )
    if not items:
        raise ArchiveError("creator_inventory_empty", "小红书主页当前没有返回可归档作品。", 66)
    return {
        "id": user_id,
        "name": creator_name or user_id,
        "url": normalized,
        "inventory_status": "experimental_first_page",
        "inventory_complete": False,
    }, items


class GuestProfilePage(HTMLParser):
    """Read public site state scripts and publisher-provided pagination."""
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.script = None
        self.next_href = ""
        self.feed(page)

    def handle_starttag(self, tag, attributes):
        fields = dict(attributes)
        if tag == "script" and not fields.get("src"):
            self.script = []
        if tag in {"a", "link"} and "next" in (fields.get("rel") or "").lower().split():
            self.next_href = fields.get("href") or self.next_href

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None


def guest_profile_state(page, html_page, platform):
    if platform == "xiaohongshu":
        try:
            return [_xiaohongshu_initial_state(page)]
        except ArchiveError:
            return []
    states = []
    for text in html_page.scripts:
        text = text.strip()
        assignment = re.match(r"(?:window\.)?(?:_ROUTER_DATA|__INITIAL_STATE__)\s*=\s*", text)
        try:
            if assignment:
                state, _ = json.JSONDecoder().raw_decode(text[assignment.end():])
            elif text.startswith(("{", "[", "%7B", "%5B", "%7b", "%5b")):
                state = json.loads(unquote(text) if text.startswith("%") else text)
            else:
                continue
            states.append(state)
        except ValueError:
            continue
    return states


def guest_douyin_card(row):
    note_id = str(row.get("aweme_id") or row.get("awemeId") or "")
    if not note_id.isdecimal():
        return None
    media = douyin_aweme_media(row)
    kind = (media or {}).get("kind")
    address = "note" if kind in {"image_gallery", "live_photo"} else "video"
    card = {"id": note_id, "url": f"https://www.douyin.com/{address}/{note_id}",
            "title": row.get("desc") or row.get("title") or "抖音作品",
            "published_at": row.get("create_time") or row.get("createTime"),
            "preview_urls": douyin_aweme_preview_urls(row), "guest_only": True}
    if media:
        card.update(douyin_media=media, media_kind=kind, media_item_count=media["item_count"])
    if kind == "video":
        card["public_video"] = True
    return card


def guest_profile_cards(states, platform, profile_url, creator):
    creator_id = creator["id"]
    for state in states:
        if platform == "xiaohongshu":
            user = state.get("user") or {}
            profile = user.get("userPageData") or {}
            basic = profile.get("basicInfo") or {}
            if basic.get("nickname"):
                creator["name"] = basic["nickname"]
            if basic.get("redId"):
                creator["account"] = basic["redId"]
            notes = user.get("notes") or profile.get("notes") or state.get("notes") or []
            for entry in _walk_json(notes):
                if not isinstance(entry, dict):
                    continue
                note = entry.get("noteCard") or entry
                author = note.get("user") or {}
                owner = author.get("userId") or author.get("user_id")
                if owner and owner != creator_id:
                    continue
                note_id = str(entry.get("noteId") or note.get("noteId") or note.get("note_id")
                              or (entry.get("id") if entry.get("noteCard") else "") or "")
                if not re.fullmatch(r"[0-9a-fA-F]{24}", note_id):
                    continue
                token = entry.get("xsecToken") or entry.get("xsec_token") or note.get("xsecToken")
                query = {"xsec_source": "pc_user"}
                if token:
                    query["xsec_token"] = token
                cover = note.get("cover") or {}
                thumbnail = cover.get("urlDefault") or cover.get("url")
                yield {"id": note_id, "url": urljoin(profile_url, f"/explore/{note_id}?{urlencode(query)}"),
                       "title": note.get("displayTitle") or note.get("title") or "小红书作品",
                       "published_at": note.get("time") or note.get("publishTime"),
                       "media_kind": "video" if note.get("type") == "video" else None,
                       "preview_urls": [thumbnail] if thumbnail else [], "guest_only": True}
        else:
            for row in _walk_json(state):
                if not isinstance(row, dict):
                    continue
                author = row.get("author") or {}
                if not isinstance(author, dict) or (author.get("sec_uid") or author.get("secUid")) != creator_id:
                    continue
                creator.update(douyin_author_fields(author, creator_id))
                if card := guest_douyin_card(row):
                    yield card


def guest_creator_inventory(url, platform):
    parsed = urlsplit(url)
    creator_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    creator = {"id": creator_id, "name": creator_id, "url": url,
               "access": "public", "inventory_status": "public_partial", "inventory_complete": False}
    current = url
    seen_pages = set()
    items = {}
    hosts = {"douyin": (".douyin.com", ".iesdouyin.com"),
             "xiaohongshu": (".xiaohongshu.com", ".rednote.com")}[platform]
    while current and current not in seen_pages:
        seen_pages.add(current)
        try:
            data, _, final = fetch_limited(current, exact_hosts={parsed.hostname}, suffixes=hosts,
                                           max_bytes=MAX_HTML_BYTES, headers={"User-Agent": DOUYIN_USER_AGENT,
                                                                               "Accept": "text/html"})
        except ArchiveError as exc:
            if items:
                break
            raise ArchiveError("guest_profile_unavailable", "免登录作者主页暂时无法读取。请重试或切换登录态盘点。", 69) from exc
        if urlsplit(final).path.rstrip("/") != parsed.path.rstrip("/"):
            break
        page = data.decode("utf-8", errors="replace")
        html_page = GuestProfilePage(page)
        states = guest_profile_state(page, html_page, platform)
        for item in guest_profile_cards(states, platform, final, creator):
            previous = items.get(item["id"])
            if previous is None or not previous.get("preview_urls") and item["preview_urls"]:
                items[item["id"]] = item
        # A first screen is partial unless the platform explicitly supplies a total.
        totals = [node["aweme_count"] for state in states for node in _walk_json(state)
                  if platform == "douyin" and isinstance(node, dict)
                  and (node.get("sec_uid") or node.get("secUid")) == creator_id
                  and isinstance(node.get("aweme_count"), int)]
        if totals and len(items) == max(totals):
            creator.update(inventory_complete=True, inventory_status="complete")
            break
        current = urljoin(final, html_page.next_href) if html_page.next_href else ""
        following = urlsplit(current)
        if following.scheme != "https" or following.netloc != urlsplit(final).netloc or following.path != urlsplit(final).path:
            break
    if not items:
        raise ArchiveError("guest_profile_unavailable", "平台当前未向访客公开该作者的作品列表。可改用登录态盘点。", 69)
    return creator, list(items.values())


def guest_douyin_work(url, work_id):
    data, _, _ = fetch_limited(url, exact_hosts={"www.douyin.com", "douyin.com"}, suffixes=(".douyin.com", ".iesdouyin.com"),
                               max_bytes=MAX_HTML_BYTES, headers={"User-Agent": DOUYIN_USER_AGENT, "Accept": "text/html"})
    page = data.decode("utf-8", errors="replace")
    for state in guest_profile_state(page, GuestProfilePage(page), "douyin"):
        for row in _walk_json(state):
            if isinstance(row, dict) and str(row.get("aweme_id") or row.get("awemeId")) == work_id:
                if card := guest_douyin_card(row):
                    if card.get("douyin_media"):
                        card["url"] = url
                        return card
    return None


def _weibo_uid_from_url(url: str) -> str:
    path = _split_url(url).path.rstrip("/")
    match = re.fullmatch(r"/(?:u|profile)/(\d+)", path) or re.fullmatch(r"/p/100505(\d+)(?:/.*)?", path)
    if not match:
        raise ArchiveError("weibo_profile_invalid", "请粘贴微博作者主页链接，例如 https://weibo.com/u/2132012180。", 65)
    return match.group(1)


def _weibo_load_cookie_jar(cookie_jar=None):
    if cookie_jar is not None:
        return cookie_jar
    jar_path = platform_cookie_jar("weibo")
    if not jar_path.is_file():
        raise ArchiveError("reauthentication_required", "需要登录微博并导入浏览器 Cookie 后才能盘点或下载。", 69)
    jar = http.cookiejar.MozillaCookieJar(str(jar_path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        raise ArchiveError("reauthentication_required", "已保存的微博 Cookie 无法读取，请重新导入浏览器 Cookie。", 69) from exc
    if not len(jar):
        raise ArchiveError("reauthentication_required", "已保存的微博 Cookie 为空，请重新导入浏览器 Cookie。", 69)
    return jar


def _weibo_opener(cookie_jar=None):
    jar = _weibo_load_cookie_jar(cookie_jar)
    xsrf = next((c.value for c in jar if c.name == "XSRF-TOKEN"), "")
    opener = build_opener(HTTPCookieProcessor(jar), SafeRedirectHandler(WEIBO_WEB_HOSTS, (".weibo.com", ".weibo.cn", ".sina.com.cn")))
    return opener, jar, xsrf


def _weibo_api_get(opener, xsrf: str, url: str, referer: str, label: str) -> dict:
    headers = {
        "User-Agent": DOUYIN_USER_AGENT,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    if xsrf:
        headers["X-XSRF-TOKEN"] = xsrf
    last_error = None
    for attempt in range(2):
        operation_wait()
        try:
            with opener.open(Request(url, headers=headers), timeout=operation_wait(18)) as response:
                final_host = (_split_url(response.geturl()).hostname or "").lower()
                if "passport.weibo" in final_host or "login.sina" in final_host:
                    raise ArchiveError("reauthentication_required", "微博登录态已失效，请在浏览器重新登录微博。", 69)
                raw = read_limited_response(response, 8 * 1024 * 1024)
            text = raw.decode("utf-8", errors="replace").strip()
            if not text or text.startswith("<"):
                raise ArchiveError("reauthentication_required", f"读取微博{label}被拦截或登录态已过期，请在浏览器确认已登录微博。", 69)
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ArchiveError("weibo_api_invalid", f"微博{label}返回格式异常。", 69)
            if data.get("ok") == -100:
                raise ArchiveError("reauthentication_required", "微博登录态已过期，请刷新浏览器微博登录态后重试。", 69)
            return data
        except ArchiveError:
            raise
        except HTTPError as exc:
            last_error = exc
            if exc.code in {401, 403, 414}:
                raise ArchiveError("reauthentication_required", f"读取微博{label}返回 HTTP {exc.code}，请确认浏览器已登录微博。", 69) from exc
            if exc.code < 500 and exc.code not in {408, 429}:
                break
        except (URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc
        if attempt == 0:
            time.sleep(operation_wait(0.8))
    raise ArchiveError("weibo_api_failed", f"读取微博{label}失败：{last_error}", 69) from last_error


def _weibo_parse_created_at(value: object) -> int | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdecimal():
        return int(text)
    try:
        from email.utils import parsedate_to_datetime
        return int(parsedate_to_datetime(text).timestamp())
    except Exception:
        return None


def _weibo_status_title(status: dict, fallback: str = "微博作品") -> str:
    raw = str(status.get("text_raw") or status.get("text") or "")
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"https?://t\.cn/[A-Za-z0-9]+", "", raw)
    raw = raw.replace("\u200b", " ").replace("\ufeff", " ")
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:80] if raw else fallback


def _weibo_safe_media_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("http://"):
        raw = "https://" + raw[len("http://"):]
    try:
        parsed = _split_url(raw)
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme != "https" or not host:
            return None
        if not host_allowed(host, WEIBO_MEDIA_EXACT_HOSTS, WEIBO_MEDIA_HOST_SUFFIXES):
            return None
        return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
    except ArchiveError:
        return None


def _weibo_pic_item(pid: str, pic_info: dict | None = None, wall_entry: dict | None = None) -> dict:
    pic_info = pic_info if isinstance(pic_info, dict) else {}
    wall_entry = wall_entry if isinstance(wall_entry, dict) else {}
    pic_type = str(pic_info.get("type") or wall_entry.get("type") or "pic").lower()
    is_gif = pic_type == "gif"
    ext = "gif" if is_gif else "jpg"
    largest = pic_info.get("largest") if isinstance(pic_info.get("largest"), dict) else {}
    mw2000 = pic_info.get("mw2000") if isinstance(pic_info.get("mw2000"), dict) else {}
    original = pic_info.get("original") if isinstance(pic_info.get("original"), dict) else {}
    large = pic_info.get("large") if isinstance(pic_info.get("large"), dict) else {}
    bmiddle = pic_info.get("bmiddle") if isinstance(pic_info.get("bmiddle"), dict) else {}

    raw_candidates = [
        largest.get("url"),
        f"https://wx1.sinaimg.cn/large/{pid}.{ext}",
        f"https://wx2.sinaimg.cn/large/{pid}.{ext}",
        f"https://wx3.sinaimg.cn/large/{pid}.{ext}",
        f"https://wx4.sinaimg.cn/large/{pid}.{ext}",
        mw2000.get("url"),
        original.get("url"),
        large.get("url"),
    ]
    image_urls = list(dict.fromkeys(filter(None, (_weibo_safe_media_url(u) for u in raw_candidates))))
    preview_candidates = [
        bmiddle.get("url"),
        f"https://wx1.sinaimg.cn/orj360/{pid}.{ext}",
        f"https://wx2.sinaimg.cn/orj360/{pid}.{ext}",
        large.get("url"),
        *image_urls[:2],
    ]
    preview_urls = list(dict.fromkeys(filter(None, (_weibo_safe_media_url(u) for u in preview_candidates))))
    video_raw = pic_info.get("video") or wall_entry.get("video")
    motion_url = _weibo_safe_media_url(video_raw)
    has_motion = pic_type == "livephoto" or bool(motion_url)
    width = positive_number(largest.get("width") or mw2000.get("width") or original.get("width"))
    height = positive_number(largest.get("height") or mw2000.get("height") or original.get("height"))
    return {
        "pid": pid,
        "type": pic_type,
        "has_still": True,
        "has_motion": has_motion,
        "width": width,
        "height": height,
        "image_urls": image_urls,
        "preview_urls": preview_urls,
        "motion_urls": [motion_url] if motion_url else [],
    }


def _weibo_video_choices_from_media_info(media_info: dict | None) -> list[dict]:
    if not isinstance(media_info, dict):
        return []
    choices = []
    for entry in media_info.get("playback_list") or []:
        if not isinstance(entry, dict):
            continue
        play = entry.get("play_info") or {}
        if not isinstance(play, dict):
            continue
        mime = str(play.get("mime") or "").lower()
        url = _weibo_safe_media_url(play.get("url"))
        if not url:
            continue
        path_lower = _split_url(url).path.lower()
        if ("image/" in mime) or path_lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        choices.append({
            "urls": [url],
            "width": positive_number(play.get("width")),
            "height": positive_number(play.get("height")),
            "fps": positive_number(play.get("fps")),
            "bitrate": positive_number(play.get("bitrate")),
            "bytes": positive_number(play.get("size")),
            "quality_label": str((entry.get("meta") or {}).get("quality_label") or play.get("label") or ""),
        })
    for key in ("mp4_1080p_mp4", "mp4_720p_mp4", "stream_url_hd", "mp4_hd_url", "stream_url", "mp4_sd_url"):
        url = _weibo_safe_media_url(media_info.get(key))
        if url and not any(url in c["urls"] for c in choices):
            choices.append({"urls": [url], "width": 0, "height": 0, "bitrate": 0, "bytes": 0})
    return sorted(choices, key=video_rank, reverse=True)


def _weibo_status_to_media(status: dict, uid: str | None = None) -> dict | None:
    mid = str(status.get("mid") or status.get("idstr") or status.get("id") or "")
    items = []
    pic_infos = status.get("pic_infos") if isinstance(status.get("pic_infos"), dict) else {}
    pic_ids = status.get("pic_ids") if isinstance(status.get("pic_ids"), list) else list(pic_infos.keys())
    seen_pids = set()
    for pid in pic_ids:
        pid_str = str(pid or "").strip()
        if not pid_str or pid_str in seen_pids:
            continue
        seen_pids.add(pid_str)
        items.append(_weibo_pic_item(pid_str, pic_infos.get(pid_str)))

    # Handle mixed media posts (photos + videos in mix_media_info.items)
    mix = status.get("mix_media_info") if isinstance(status.get("mix_media_info"), dict) else {}
    for entry in mix.get("items") or []:
        if not isinstance(entry, dict):
            continue
        etype = str(entry.get("type") or "").lower()
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        if etype == "pic":
            pid_str = str(entry.get("id") or data.get("pic_id") or "").strip()
            if pid_str and pid_str not in seen_pids:
                seen_pids.add(pid_str)
                items.append(_weibo_pic_item(pid_str, data))
        elif etype == "video":
            choices = _weibo_video_choices_from_media_info(data.get("media_info"))
            if choices:
                cover = _weibo_safe_media_url(data.get("page_pic"))
                items.append({
                    "type": "video",
                    "has_still": False,
                    "has_motion": False,
                    "video_choices": choices,
                    "preview_urls": [cover] if cover else [],
                    "width": choices[0].get("width") or 0,
                    "height": choices[0].get("height") or 0,
                })

    page_info = status.get("page_info") if isinstance(status.get("page_info"), dict) else {}
    video_choices = []
    if str(page_info.get("object_type") or "").lower() == "video" or page_info.get("media_info"):
        video_choices = _weibo_video_choices_from_media_info(page_info.get("media_info"))

    if items:
        if video_choices and not any(it.get("video_choices") for it in items):
            cover = _weibo_safe_media_url(page_info.get("page_pic"))
            items.append({
                "type": "video",
                "has_still": False,
                "has_motion": False,
                "video_choices": video_choices,
                "preview_urls": [cover] if cover else [],
                "width": video_choices[0].get("width") or 0,
                "height": video_choices[0].get("height") or 0,
            })
        has_live = any(it.get("has_motion") for it in items)
        return {
            "kind": "live_photo" if has_live else "image_gallery",
            "content_id": mid,
            "uid": str(uid or (status.get("user") or {}).get("id") or ""),
            "item_count": len(items),
            "items": items,
        }
    if video_choices:
        return {
            "kind": "video",
            "content_id": mid,
            "uid": str(uid or (status.get("user") or {}).get("id") or ""),
            "item_count": 1,
            "video_variants": video_choices,
        }
    return None


def _weibo_status_preview_urls(status: dict, media: dict | None = None) -> list[str]:
    urls = []
    if media:
        for item in media.get("items") or []:
            urls.extend(item.get("preview_urls") or [])
            if len(urls) >= 3:
                break
    page_info = status.get("page_info") if isinstance(status.get("page_info"), dict) else {}
    cover = _weibo_safe_media_url(page_info.get("page_pic"))
    if cover:
        urls.append(cover)
    big_pic = ((page_info.get("media_info") or {}).get("big_pic_info") or {}).get("pic_big") or {}
    if isinstance(big_pic, dict):
        big_url = _weibo_safe_media_url(big_pic.get("url"))
        if big_url:
            urls.append(big_url)
    return list(dict.fromkeys(urls))[:4]


def fetch_weibo_status(mid_or_mblogid: str, *, cookie_jar=None) -> dict:
    opener, _, xsrf = _weibo_opener(cookie_jar)
    data = _weibo_api_get(
        opener,
        xsrf,
        f"https://weibo.com/ajax/statuses/show?id={quote(str(mid_or_mblogid))}",
        "https://weibo.com/",
        "作品详情",
    )
    if not data.get("mid") and not data.get("idstr") and not data.get("id"):
        raise ArchiveError("weibo_status_unavailable", "找不到该微博作品，可能已被删除或仅好友可见。", 69)
    return data


def _weibo_remaining_seconds(default: float = 60.0) -> float:
    try:
        remaining = operation_wait(default)
        return float(remaining) if remaining is not None else default
    except TimeoutError:
        return 0.0


def _weibo_is_own_original_status(status: dict, uid: str) -> bool:
    """Return True only for original statuses authored by ``uid`` (exclude likes/reposts)."""
    if not isinstance(status, dict) or status.get("retweeted_status"):
        return False
    title = status.get("title")
    if isinstance(title, dict) and "赞过" in str(title.get("text") or ""):
        return False
    user = status.get("user")
    if isinstance(user, dict):
        author_uid = str(user.get("idstr") or user.get("id") or "").strip()
        if author_uid and author_uid != str(uid):
            return False
    return True


def weibo_creator_inventory(url: str, *, cookie_jar=None, include_profile=True, known_ids=()) -> tuple[dict, list[dict]]:
    uid = _weibo_uid_from_url(url)
    referer = f"https://weibo.com/u/{uid}"
    _, jar, xsrf = _weibo_opener(cookie_jar)
    known = {str(x) for x in known_ids if x}

    def new_opener():
        return build_opener(HTTPCookieProcessor(jar), SafeRedirectHandler(WEIBO_WEB_HOSTS, (".weibo.com", ".weibo.cn", ".sina.com.cn")))

    creator = {
        "id": uid,
        "name": uid,
        "account": uid,
        "avatar": "",
        "url": referer,
        "inventory_complete": True,
    }

    def fetch_profile():
        info = _weibo_api_get(new_opener(), xsrf, f"https://weibo.com/ajax/profile/info?uid={uid}", referer, "作者资料")
        return (info.get("data") or {}).get("user") or {}

    def fetch_album():
        opener = new_opener()
        groups: dict[str, list[dict]] = {}
        dates: dict[str, str] = {}
        since_id = "0"
        seen_since = set()
        complete = True
        for page_no in range(60):
            if page_no > 0 and _weibo_remaining_seconds(60.0) < 4.5:
                complete = False
                break
            operation_wait()
            try:
                wall = _weibo_api_get(
                    opener, xsrf,
                    f"https://weibo.com/ajax/profile/getImageWall?uid={uid}&sinceid={quote(str(since_id))}&has_album=true",
                    referer, "相册",
                )
            except (ArchiveError, TimeoutError):
                if groups:
                    complete = False
                    break
                raise
            data = wall.get("data") or {}
            rows = data.get("list") or []
            hit_known = False
            for row in rows:
                if not isinstance(row, dict):
                    continue
                mid = str(row.get("mid") or "").strip()
                pid = str(row.get("pid") or "").strip()
                if not mid or not pid:
                    continue
                if known and mid in known:
                    hit_known = True
                groups.setdefault(mid, []).append(row)
                if mid not in dates and row.get("timeline_year"):
                    dates[mid] = f"{row.get('timeline_year')}-{str(row.get('timeline_month') or '01').zfill(2)}"
            nxt = str(data.get("since_id") or "")
            if hit_known or not nxt or nxt in {"0", "-1"} or nxt in seen_since or not rows:
                break
            seen_since.add(nxt)
            since_id = nxt
            if page_no >= 59:
                complete = False
            time.sleep(operation_wait(0.1))
        return groups, dates, complete

    def fetch_videos():
        opener = new_opener()
        vitems: dict[str, dict] = {}
        cursor = "0"
        seen_cursor = set()
        complete = True
        for page_no in range(30):
            if page_no > 0 and _weibo_remaining_seconds(60.0) < 4.5:
                complete = False
                break
            operation_wait()
            try:
                wf = _weibo_api_get(
                    opener, xsrf,
                    f"https://weibo.com/ajax/profile/getWaterFallContent?cursor={quote(str(cursor))}&uid={uid}",
                    referer, "视频列表",
                )
            except (ArchiveError, TimeoutError):
                complete = False
                break
            data = wf.get("data") or {}
            rows = data.get("list") or []
            hit_known = False
            for st in rows:
                if not _weibo_is_own_original_status(st, uid):
                    continue
                mid = str(st.get("mid") or st.get("idstr") or st.get("id") or "").strip()
                if not mid:
                    continue
                if known and mid in known:
                    hit_known = True
                media = _weibo_status_to_media(st, uid=uid)
                if not media:
                    continue
                mblogid = str(st.get("mblogid") or mid)
                vitems[mid] = {
                    "id": mid,
                    "url": f"https://weibo.com/{uid}/{mblogid}",
                    "title": _weibo_status_title(st, "微博视频"),
                    "published_at": _weibo_parse_created_at(st.get("created_at")),
                    "media_kind": media["kind"],
                    "media_item_count": media["item_count"],
                    "weibo_media": media,
                    "preview_urls": _weibo_status_preview_urls(st, media),
                    "child_job_id": None,
                }
            nxt = str(data.get("next_cursor") or "")
            if hit_known or not nxt or nxt in {"0", "-1"} or nxt in seen_cursor or not rows:
                break
            seen_cursor.add(nxt)
            cursor = nxt
            if page_no >= 29:
                complete = False
            time.sleep(operation_wait(0.1))
        return vitems, complete

    def fetch_recent_posts():
        enriched: dict[str, dict] = {}
        extra_videos: dict[str, dict] = {}
        try:
            operation_wait()
            mblog = _weibo_api_get(new_opener(), xsrf, f"https://weibo.com/ajax/statuses/mymblog?uid={uid}&page=1&feature=1", referer, "微博列表")
        except (ArchiveError, TimeoutError):
            return enriched, extra_videos
        mlist = (mblog.get("data") or {}).get("list") or []
        for st in mlist:
            if not _weibo_is_own_original_status(st, uid):
                continue
            mid = str(st.get("mid") or st.get("idstr") or "").strip()
            if not mid:
                continue
            enriched[mid] = st
            media = _weibo_status_to_media(st, uid=uid)
            if media:
                mblogid = str(st.get("mblogid") or mid)
                extra_videos[mid] = {
                    "id": mid,
                    "url": f"https://weibo.com/{uid}/{mblogid}",
                    "title": _weibo_status_title(st, "微博作品"),
                    "published_at": _weibo_parse_created_at(st.get("created_at")),
                    "media_kind": media["kind"],
                    "media_item_count": media["item_count"],
                    "weibo_media": media,
                    "preview_urls": _weibo_status_preview_urls(st, media),
                    "child_job_id": None,
                }
        return enriched, extra_videos

    # 并发拉取资料、相册、视频瀑布流与主页近期微博，显著缩短盘点耗时
    with ThreadPoolExecutor(max_workers=4 if include_profile else 2) as pool:
        f_album = pool.submit(copy_context().run, fetch_album)
        f_videos = pool.submit(copy_context().run, fetch_videos)
        f_profile = pool.submit(copy_context().run, fetch_profile) if include_profile else None
        f_recent = pool.submit(copy_context().run, fetch_recent_posts) if include_profile else None

        if f_profile is not None:
            user = f_profile.result()
            if user:
                creator["name"] = str(user.get("screen_name") or uid)
                creator["account"] = str(user.get("domain") or user.get("idstr") or uid)
                creator["avatar"] = _weibo_safe_media_url(user.get("avatar_hd") or user.get("avatar_large") or "") or ""
        album_groups, album_dates, wall_complete = f_album.result()
        video_items, video_complete = f_videos.result()
        enriched_statuses, extra_recent = f_recent.result() if f_recent is not None else ({}, {})

    # 合并相册与视频作品（按 mid 去重，按时间倒序）
    merged: dict[str, dict] = dict(video_items)
    for mid, item in extra_recent.items():
        if mid not in merged and mid not in album_groups:
            merged[mid] = item
    for mid, wall_rows in album_groups.items():
        st = enriched_statuses.get(mid)
        if st:
            media = _weibo_status_to_media(st, uid=uid)
            if media:
                mblogid = str(st.get("mblogid") or mid)
                merged[mid] = {
                    "id": mid,
                    "url": f"https://weibo.com/{uid}/{mblogid}",
                    "title": _weibo_status_title(st, f"微博相册 {album_dates.get(mid, '')}".strip()),
                    "published_at": _weibo_parse_created_at(st.get("created_at")),
                    "media_kind": media["kind"],
                    "media_item_count": media["item_count"],
                    "weibo_media": media,
                    "preview_urls": _weibo_status_preview_urls(st, media),
                    "child_job_id": None,
                }
                continue
        pic_items = []
        seen_pids = set()
        for row in wall_rows:
            pid = str(row.get("pid") or "").strip()
            if pid and pid not in seen_pids:
                seen_pids.add(pid)
                pic_items.append(_weibo_pic_item(pid, wall_entry=row))
        if not pic_items:
            continue
        has_live = any(it.get("has_motion") for it in pic_items)
        ym = album_dates.get(mid, "")
        kind = "live_photo" if has_live else "image_gallery"
        label = "实况图集" if has_live else "相册图集"
        title = f"微博{label} {ym}（{len(pic_items)}图）".strip()
        media = {"kind": kind, "content_id": mid, "uid": uid, "item_count": len(pic_items), "items": pic_items}
        pub_ts = None
        if ym:
            try:
                y, m = (int(p) for p in ym.split("-", 1))
                pub_ts = int(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
            except ValueError:
                pub_ts = None
        merged[mid] = {
            "id": mid,
            "url": f"https://weibo.com/{uid}/{mid}",
            "title": title,
            "published_at": pub_ts,
            "media_kind": kind,
            "media_item_count": len(pic_items),
            "weibo_media": media,
            "preview_urls": [u for it in pic_items[:2] for u in it.get("preview_urls")[:2]][:4],
            "child_job_id": None,
        }

    creator["inventory_complete"] = wall_complete and video_complete
    ordered = sorted(merged.values(), key=lambda it: int(it["id"]) if str(it["id"]).isdecimal() else 0, reverse=True)
    return creator, ordered


def _download_weibo_media_url(url: str, target: Path, referer: str, *, max_bytes: int, cookie_jar=None) -> None:
    safe_url = _weibo_safe_media_url(url)
    if not safe_url:
        raise ArchiveError("invalid_media_url", "微博媒体地址无效。", 69)
    assert_public_dns(_split_url(safe_url).hostname or "")
    headers = {"User-Agent": DOUYIN_USER_AGENT, "Referer": referer or "https://weibo.com/"}
    handlers = [SafeRedirectHandler(WEIBO_MEDIA_EXACT_HOSTS, WEIBO_MEDIA_HOST_SUFFIXES)]
    if cookie_jar is not None:
        handlers.insert(0, HTTPCookieProcessor(cookie_jar))
    opener = build_opener(*handlers)
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.unlink(missing_ok=True)
    transfer_started = time.monotonic()
    last_error = None
    for attempt in range(2):
        operation_wait()
        try:
            with opener.open(Request(safe_url, headers=headers), timeout=operation_wait(30)) as response:
                length = response.headers.get("Content-Length")
                if length and length.isdecimal() and int(length) > max_bytes:
                    raise ArchiveError("response_too_large", "微博媒体超过允许大小。", 69)
                expected = whole_media_size(response.status, response.headers)
                with temporary.open("wb") as output:
                    size = copy_media_stream(response, output, max_bytes, transfer_started)
                if expected is not None and size != expected:
                    raise ArchiveError("media_download_incomplete", "微博媒体下载不完整。", 69)
            if temporary.stat().st_size <= 0:
                raise ArchiveError("video_missing", "下载的微博媒体为空。", 69)
            os.replace(temporary, target)
            return
        except ArchiveError:
            temporary.unlink(missing_ok=True)
            raise
        except HTTPError as exc:
            temporary.unlink(missing_ok=True)
            last_error = exc
            if exc.code < 500 and exc.code not in {408, 429}:
                break
        except (URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            temporary.unlink(missing_ok=True)
            last_error = exc
        if attempt == 0:
            time.sleep(operation_wait(0.6))
    raise ArchiveError("media_download_failed", f"微博媒体下载失败：{last_error}", 69) from last_error


def _download_weibo_video(urls: list[str], target: Path, referer: str, *, cookie_jar=None) -> None:
    last_error = None
    for url in urls:
        try:
            _download_weibo_media_url(url, target, referer, max_bytes=MAX_INPUT_MEDIA_BYTES, cookie_jar=cookie_jar)
            return
        except ArchiveError as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise ArchiveError("video_missing", "微博视频直链不可用。", 69)


def download_weibo_still(urls: list[str], target: Path, referer: str, expected: tuple[int, int], *, allow_lower=False, cookie_jar=None):
    best = None
    temporary = target.with_name("candidate-" + target.name)
    for url in list(dict.fromkeys(filter(None, (_weibo_safe_media_url(u) for u in urls)))):
        temporary.unlink(missing_ok=True)
        try:
            _download_weibo_media_url(url, temporary, referer, max_bytes=MAX_SOURCE_IMAGE_BYTES, cookie_jar=cookie_jar)
            size = image_size(temporary)
            with temporary.open("rb") as handle:
                extension = media_extension_from_bytes("", url, handle.read(32))
            if extension == ".bin":
                continue
        except (ArchiveError, OSError, ValueError):
            continue
        is_large = "/large/" in _split_url(url).path
        quality = (size[0] * size[1], is_large, temporary.stat().st_size)
        if best is None or quality > best[0]:
            os.replace(temporary, target)
            best = quality, size, extension, is_large
        if is_large and (not all(expected) or matches_dimensions(size, expected)):
            break
    temporary.unlink(missing_ok=True)
    if best is None:
        raise ArchiveError("image_missing", "微博图片下载失败，请检查网络连接或稍后重试。", 69)
    _, size, extension, is_large = best
    if not allow_lower and all(expected) and not matches_dimensions(size, expected):
        target.unlink(missing_ok=True)
        raise ArchiveError("image_resolution_unavailable",
                           f"作品标注 {expected[0]}×{expected[1]}，目前只能取得 {size[0]}×{size[1]}，已跳过低清图片。", 69)
    return size, extension, is_large


def weibo_work_media(manifest: dict, *, refresh: bool = True, cookie_jar=None):
    frozen = manifest.get("weibo_media") if isinstance(manifest.get("weibo_media"), dict) else {}
    content_id = str(manifest.get("content_id") or frozen.get("content_id") or "")
    detail = None
    media = frozen if frozen.get("items") or frozen.get("video_variants") else None
    need_detail = refresh or not media or any(
        it.get("has_motion") and "Expires=" not in str((it.get("motion_urls") or [""])[0])
        for it in (media.get("items") or [])
    ) or media.get("kind") == "video"
    if need_detail and content_id:
        try:
            detail = fetch_weibo_status(content_id, cookie_jar=cookie_jar)
            refreshed = _weibo_status_to_media(detail, uid=frozen.get("uid"))
            if refreshed:
                media = refreshed
        except ArchiveError:
            if media is None:
                raise
    if not media:
        raise ArchiveError("weibo_media_missing", "该微博作品没有可下载的图片或视频。", 69)
    title = _weibo_status_title(detail, str(manifest.get("title") or "微博作品")) if detail else str(manifest.get("title") or "微博作品")
    return detail, media, title


def process_weibo_work(manifest: dict, manifest_path: Path, root: Path) -> dict:
    jar = None
    try:
        jar = _weibo_load_cookie_jar()
    except ArchiveError:
        if not manifest.get("weibo_media"):
            raise
    detail, media, title = weibo_work_media(manifest, refresh=True, cookie_jar=jar)
    content_id = str((detail or {}).get("mid") or media.get("content_id") or manifest.get("content_id") or "")
    referer = str(manifest.get("source") or "https://weibo.com/")
    allow_lower = bool(manifest.get("allow_lower_quality"))

    if media.get("kind") == "video":
        choices = media.get("video_variants") or []
        if not choices:
            raise ArchiveError("video_missing", "该微博视频没有可用播放源。", 69)
        work_dir = manifest_path.parent / "work" / "video"
        work_dir.mkdir(parents=True, exist_ok=True)
        staged_video = work_dir / "video.mp4"
        quality = download_best_video(
            choices, staged_video, "https://weibo.com/",
            lambda urls, target, ref: _download_weibo_video(urls, target, ref, cookie_jar=jar),
            allow_lower=allow_lower,
        )
        info = {"id": content_id, "title": title, "webpage_url": referer, "_video_quality": quality}
        return finalize_video_content(manifest, manifest_path, root, info, staged_video, "weibo_web_video")

    work_dir = manifest_path.parent / "work" / "gallery"
    work_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    for index, item in enumerate(media.get("items") or [], start=1):
        if item.get("has_still") or item.get("image_urls"):
            expected = (positive_number(item.get("width")), positive_number(item.get("height")))
            image = work_dir / f"{index:03d}.img"
            size, extension, is_large = download_weibo_still(
                item.get("image_urls") or [], image, "https://weibo.com/", expected,
                allow_lower=allow_lower, cookie_jar=jar,
            )
            staged.append((image, f"图片/{index:03d}{extension}", "image", {
                "width": size[0],
                "height": size[1],
                "advertised_size": expected,
                "is_large": is_large,
                "non_highest": all(expected) and not matches_dimensions(size, expected),
            }))
        if item.get("has_motion") and item.get("motion_urls"):
            motion = work_dir / f"{index:03d}.mov"
            _download_weibo_media_url(item["motion_urls"][0], motion, "https://weibo.com/", max_bytes=MAX_INPUT_MEDIA_BYTES, cookie_jar=jar)
            quality = probe_video_file(motion)
            quality["original"] = True
            quality["source_match"] = True
            staged.append((motion, f"Live图/{index:03d}.mov", "live_motion", quality))
        if item.get("video_choices"):
            vid = work_dir / f"video-{index:03d}.mp4"
            quality = download_best_video(
                item["video_choices"], vid, "https://weibo.com/",
                lambda urls, target, ref: _download_weibo_video(urls, target, ref, cookie_jar=jar),
                allow_lower=allow_lower,
            )
            staged.append((vid, f"视频/{index:03d}.mp4", "video", quality))

    if not staged:
        raise ArchiveError("weibo_media_missing", "该微博作品没有可归档的图片或视频。", 69)

    output_dir, prefix = work_archive_layout(root, manifest, "weibo", title, content_id)
    outputs = []
    for source, relative, role, quality in staged:
        basename = Path(relative).name
        label = "实况" if role == "live_motion" else ("视频" if role == "video" else "图")
        target = output_dir / (f"{prefix}--{label}{basename}" if prefix else relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if role == "image":
            target, size, retained = commit_image(source, target, (quality["width"], quality["height"]))
            expected = quality.pop("advertised_size")
            is_large = quality.pop("is_large", True)
            quality.update(width=size[0], height=size[1])
            quality["non_highest"] = all(expected) and not matches_dimensions(size, expected)
            quality["source_match"] = not retained and is_large and not quality["non_highest"]
            if retained:
                quality["retained_existing"] = True
        else:
            quality = commit_video(source, target, quality)
        outputs.append({**output_record(root, target, role), **quality})

    manifest.update({
        "status": "completed",
        "updated_at": utc_now(),
        "completed_at": utc_now(),
        "title": title,
        "content_id": content_id,
        "content_type": media["kind"],
        "canonical_url": referer,
        "route": "weibo_web_media",
        "media_quality_revision": MEDIA_QUALITY_REVISION,
        "non_highest": any(record.get("non_highest") for record in outputs),
        "output_dir": archive_relative(root, output_dir),
        "outputs": outputs,
        "weibo_media": media,
    })
    manifest.pop("error", None)
    write_json(manifest_path, manifest)
    return manifest



def inspect_single_work(url, root, *, public_only=False, xhs_browser="firefox", douyin_jar=None, weibo_jar=None):
    normalized, platform = submission_url(url)
    path = _split_url(normalized).path.rstrip("/")
    patterns = {
        "douyin": r"/(?:video|note)/(\d+)",
        "xiaohongshu": r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})",
        "weibo": r"/(?:\d+|detail|status)/([0-9A-Za-z]+)",
    }
    match = re.fullmatch(patterns.get(platform, r"(?!)"), path)
    if not match:
        raise ArchiveError("work_url_required", "请粘贴作者主页或网页作品链接。", 65)
    work_id = match[1]
    item = {"id": work_id, "url": normalized, "child_job_id": None}
    if public_only and platform == "weibo":
        raise ArchiveError("guest_profile_unavailable", "微博暂不支持免登录盘点，请在浏览器登录微博后使用「盘点作品」。", 69)
    if public_only and platform == "xiaohongshu":
        info, _ = xhs_work_info({"guest_only": True}, root, normalized, "")
        item.update(title=info["title"], media_kind="video" if info["_xhs_video_urls"] else "image_gallery",
                    preview_urls=[image["url"] for image in info["thumbnails"][:2]], guest_only=True)
    elif public_only:
        if platform == "douyin" and "/note/" in path:
            card = guest_douyin_work(normalized, work_id)
            if not card:
                raise ArchiveError("guest_media_unavailable", "公开页面未提供该抖音图集的媒体信息。", 69)
            item.update(card)
            creator = {"url": normalized, "single_work": True, "inventory_complete": True, "access": "public"}
            return save_creator_inventory(normalized, platform, root, creator, [item], [])
        info = inspect_with_transparent_core(normalized, platform, use_cookies=False)
        if str(info.get("id") or "") != work_id or not info.get("formats"):
            raise ArchiveError("public_video_unavailable", "公开网页没有返回可下载的视频流。", 69)
        item.update(title=info.get("title") or "视频作品", published_at=info.get("timestamp"),
                    media_kind="video", public_video=True,
                    preview_urls=[info["thumbnail"]] if info.get("thumbnail") else [])
        if platform == "douyin":
            item["guest_only"] = True
            choices = douyin_public_video_choices(info)
            if not choices:
                raise ArchiveError("douyin_clean_source_unavailable", "公开网页没有返回无水印播放源。", 69)
            item["douyin_media"] = {"kind": "video", "content_id": work_id, "video_variants": choices,
                                     "clean_source_revision": DOUYIN_CLEAN_REVISION}
    elif platform == "douyin":
        detail = fetch_douyin_aweme_detail({"content_id": work_id, "source": normalized}, cookie_jar=douyin_jar)
        media = douyin_aweme_media(detail)
        item.update(title=detail.get("desc") or "抖音作品", published_at=detail.get("create_time"),
                    media_kind=media["kind"], media_item_count=media["item_count"], douyin_media=media,
                    preview_urls=douyin_aweme_preview_urls(detail))
    elif platform == "xiaohongshu":
        info = _redbook_note_info(_redbook_read_note(normalized, xhs_browser), normalized)
        item.update(title=info["title"], media_kind="video" if info["_xhs_video_urls"] else "image_gallery",
                    preview_urls=[image["url"] for image in info["thumbnails"][:2]])
    elif platform == "weibo":
        detail = fetch_weibo_status(work_id, cookie_jar=weibo_jar)
        media = _weibo_status_to_media(detail)
        if not media:
            raise ArchiveError("weibo_media_missing", "这条微博没有可下载的图片或视频。", 69)
        mid = str(detail.get("mid") or detail.get("idstr") or work_id)
        uid = str((detail.get("user") or {}).get("id") or "")
        mblogid = str(detail.get("mblogid") or mid)
        canonical = f"https://weibo.com/{uid}/{mblogid}" if uid else normalized
        item.update(id=mid, url=canonical, title=_weibo_status_title(detail, "微博作品"),
                    published_at=_weibo_parse_created_at(detail.get("created_at")),
                    media_kind=media["kind"], media_item_count=media["item_count"], weibo_media=media,
                    preview_urls=_weibo_status_preview_urls(detail, media))
    else:
        info = inspect_with_transparent_core(normalized, platform)
        item.update(title=info.get("title") or "视频作品", published_at=info.get("timestamp"), media_kind="video")
    creator = {"url": normalized, "browser": xhs_browser, "single_work": True,
               "inventory_complete": True, "access": "public" if public_only else "session"}
    return save_creator_inventory(normalized, platform, root, creator, [item], [])


def inspect_creator(url: str, root: Path, *, xhs_browser: str | None = None, douyin_jar=None, weibo_jar=None) -> dict:
    normalized, platform = submission_url(url)
    if platform == "wechat_channels":
        return inspect_channel_creator(normalized, root)
    if platform == "douyin":
        creator, items = douyin_creator_inventory(normalized, cookie_jar=douyin_jar)
        collections = []
    elif platform == "xiaohongshu":
        creator, items = xiaohongshu_creator_inventory(normalized, xhs_browser) if xhs_browser else xiaohongshu_creator_inventory(normalized)
        collections = []
    elif platform == "weibo":
        creator, items = weibo_creator_inventory(normalized, cookie_jar=weibo_jar)
        collections = []
    else:
        raise ArchiveError("creator_batch_unsupported", "作者历史批量当前只支持抖音、小红书和微博。")
    if not items:
        raise ArchiveError("creator_inventory_empty", "该作者当前没有可下载作品。", 66)
    return save_creator_inventory(normalized, platform, root, creator, items, collections)


def save_creator_inventory(normalized, platform, root, creator, items, collections):
    operation_wait()
    job_id, job_dir, manifest = new_job(root, "batch", canonical_url(normalized))
    classified = sum(bool(item.get("collection_ids")) for item in items)
    collections_status = str(creator.get("collections_status") or "not_applicable")
    coverage = {
        "status": collections_status,
        "classified": classified if collections_status == "ready" else None,
        "unclassified": len(items) - classified if collections_status == "ready" else None,
    }
    if creator.get("single_work"):
        next_action = "已读取这件作品，可以勾选后下载。"
    elif platform == "bilibili":
        collection_note = (
            f"{len(collections)} 个合集覆盖 {classified} 个，未分类 {len(items) - classified} 个。"
            if collections_status == "ready"
            else "合集接口暂时不可用。"
        )
        next_action = f"该博主当前可下载视频共 {len(items)} 个，{collection_note}你要按数量、合集、指定作品、全部还是仅缺失项下载？"
    elif creator.get("access") == "public":
        next_action = f"已盘点 {len(items)} 个游客可见作品，可选择免登录下载。"
        if not creator.get("inventory_complete"):
            next_action += "平台未公开完整列表，可用登录态补全。"
    elif platform == "weibo":
        next_action = f"已从微博相册与视频列表盘点到 {len(items)} 件作品，可勾选下载原图、实况图或视频。"
    elif platform == "x":
        next_action = f"已盘点到 {len(items)} 件 X 原创作品（每分钟限 10 件），可勾选下载原图或视频。"
    elif platform == "xiaohongshu":
        if creator.get("inventory_complete"):
            next_action = f"小红书主页分页已遍历到末页，共冻结 {len(items)} 个可访问作品。你要下载多少个、全部还是仅缺失项？"
        else:
            next_action = (
                f"实验性盘点只读取到当前主页可见作品 {len(items)} 个，尚未证明覆盖全部历史；"
                "可以选择下载这批可见作品，但不能把结果称为全量归档。"
            )
    else:
        kinds = {str(item.get("douyin_media", {}).get("kind") or "video") for item in items}
        next_action = f"该抖音主页当前可下载作品共 {len(items)} 个（类型：{', '.join(sorted(kinds))}）。你要下载多少个或全部？"
    manifest.update(
        {
            "kind": "creator_batch",
            "status": "awaiting_download_count",
            "updated_at": utc_now(),
            "platform": platform,
            "creator": creator,
            "inventory": {
                "captured_at": utc_now(),
                "available": len(items),
                "complete": bool(creator.get("inventory_complete", platform != "xiaohongshu")),
                "status": creator.get("inventory_status", "complete"),
                "items": items,
            },
            "collections": collections,
            "coverage": coverage,
            "selection": None,
            "child_job_ids": [],
            "counts": {"selected": 0, "processing": 0, "completed": 0, "failed": 0},
            "next_action": next_action,
        }
    )
    write_json(job_dir / "manifest.json", manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "platform": platform,
        "creator": creator,
        "available": len(items),
        "collections": [{key: collection[key] for key in ("id", "name", "available")} for collection in collections],
        "coverage": coverage,
        "question": manifest["next_action"],
        "manifest": archive_relative(root, job_dir / "manifest.json"),
    }


def selected_creator_items(manifest: dict) -> list[dict]:
    items = (manifest.get("inventory") or {}).get("items") or []
    selection = manifest.get("selection") or {}
    selected_ids = selection.get("ids")
    if isinstance(selected_ids, list):
        wanted = {str(content_id) for content_id in selected_ids}
        return [item for item in items if str(item.get("id")) in wanted]
    return items[: int(selection.get("limit") or 0)]


def _refresh_creator_batch_unlocked(manifest: dict, manifest_path: Path, root: Path) -> dict:
    processing = completed = failed = waiting_authorization = waiting_reauthentication = 0
    for item in selected_creator_items(manifest):
        child_id = item.get("child_job_id")
        if not child_id:
            continue
        child_path = root / "jobs" / str(child_id) / "manifest.json"
        if not child_path.is_file():
            processing += 1
            continue
        child = read_json_if_valid(child_path)
        if not child:
            item["result"] = "failed"
            item["error_code"] = "child_manifest_invalid"
            failed += 1
            continue
        child_status = str(child.get("status") or "")
        # 磁盘是最终事实：媒体已经完整落盘时，即使子任务在转写/收尾阶段
        # 报错，也不能让父任务和 UI 把它继续列为失败。
        if child_status in {"completed", "failed", "unavailable"} and _child_output_files_exist(root, child, item):
            recovered = child_status != "completed"
            item["result"] = "completed"
            if child.get("image_quality") == "api_fallback":
                item["image_quality"] = "api_fallback"
            else:
                item.pop("image_quality", None)
            item.pop("error_code", None)
            item.pop("error_message", None)
            completed += 1
            if recovered:
                child["status"] = "completed"
                child["recovered_from_failure"] = True
                child["recovery_note"] = "媒体文件已完整落盘；忽略下载后收尾阶段错误。"
                child.pop("error", None)
                child["updated_at"] = utc_now()
                child["completed_at"] = child.get("completed_at") or utc_now()
                write_json(child_path, child)
            continue
        if child_status == "completed":
            # 文件丢了/不完整 → 标记 queued 让 worker 重跑。
            item["result"] = "failed"
            item["error_code"] = "files_missing_on_disk"
            failed += 1
            child["status"] = "failed"
            child["error"] = {"code": "files_missing_on_disk", "message": "媒体缺失或损坏，请重试该作品。"}
            write_json(child_path, child)
        elif child_status in {"failed", "unavailable"}:
            item["result"] = "failed"
            item["error_code"] = (child.get("error") or {}).get("code")
            failed += 1
        elif child.get("status") == "waiting_for_authorization":
            item["result"] = "waiting_for_authorization"
            waiting_authorization += 1
        elif child.get("status") == "waiting_for_reauthentication":
            item["result"] = "waiting_for_reauthentication"
            waiting_reauthentication += 1
        else:
            item["result"] = "processing"
            processing += 1
    selected = len(selected_creator_items(manifest))
    manifest["counts"] = {
        "selected": selected,
        "processing": processing,
        "waiting_for_authorization": waiting_authorization,
        "waiting_for_reauthentication": waiting_reauthentication,
        "completed": completed,
        "failed": failed,
    }
    if waiting_reauthentication:
        manifest["status"] = "waiting_for_reauthentication"
    elif waiting_authorization:
        manifest["status"] = "waiting_for_authorization"
    elif selected and not processing:
        manifest.update({"status": "completed_with_failures" if failed else "completed", "completed_at": utc_now()})
    elif selected:
        manifest["status"] = "processing"
    manifest["updated_at"] = utc_now()
    write_json(manifest_path, manifest)
    return manifest


def _submit_creator_batch_children_unlocked(manifest: dict, manifest_path: Path, root: Path) -> dict:
    items = selected_creator_items(manifest)
    parent_job_id = str(manifest.get("job_id") or manifest_path.parent.name)
    for item in items:
        existing_child_id = str(item.get("child_job_id") or "")
        if existing_child_id and manifest.get("platform") != "wechat_channels":
            if manifest.get("platform") != "xiaohongshu":
                continue
            # A parent inventory from the pre-original-quality release may
            # still point at a completed image-text child.  Retire that child
            # and create a replacement instead of silently reusing its
            # low-quality files.
            stale_path = root / "jobs" / existing_child_id / "manifest.json"
            stale_child = read_json_if_valid(stale_path)
            if not _xhs_image_child_needs_quality_upgrade(stale_child or {}):
                continue
            if stale_child:
                stale_child["status"] = "failed"
                stale_child["updated_at"] = utc_now()
                stale_child["error"] = {
                    "code": "quality_upgrade_required",
                    "message": "旧版小红书图文子任务使用的是网页缩略图，正在升级为原图 CDN 对象。",
                }
                write_json(stale_path, stale_child)
            item.pop("child_job_id", None)
            item.pop("result", None)
            item.pop("error_code", None)
            existing_child_id = ""
        if manifest.get("platform") == "wechat_channels":
            deterministic_id = frozen_channel_child_job_id(parent_job_id, item)
            if existing_child_id and existing_child_id != deterministic_id:
                child_path = root / "jobs" / existing_child_id / "manifest.json"
                adopted = adopt_legacy_frozen_channel_content(child_path, item, parent_job_id)
                child = {
                    "job_id": existing_child_id,
                    "manifest": archive_relative(root, child_path),
                    "status": adopted.get("status"),
                }
            else:
                child = submit_frozen_channel_content(item, root, parent_job_id)
        else:
            if item.get("media_ids") or manifest.get("allow_lower_quality") or (manifest.get("selection") or {}).get("refresh_quality") or item.get("guest_only"):
                child = submit_content(str(item["url"]), root, refresh_quality=True, fresh=True)
                child_path = root / child["manifest"]
                archived = read_json_if_valid(child_path) or {}
                if item.get("media_ids"):
                    archived["media_ids"] = item["media_ids"]
                if manifest.get("allow_lower_quality"):
                    archived["allow_lower_quality"] = True
                if item.get("guest_only"):
                    archived["guest_only"] = True
                write_json(child_path, archived)
            elif manifest.get("direct_archive"):
                child = submit_content(str(item["url"]), root, refresh_quality=True)
            else:
                child = submit_content(str(item["url"]), root)
        if (manifest.get("author_folder") or manifest.get("direct_archive")) and child.get("status") != "completed":
            child_path = root / str(child["manifest"])
            archived = read_json_if_valid(child_path) or {}
            if manifest.get("author_folder"):
                archived["author_folder"] = manifest["author_folder"]
            if manifest.get("direct_archive"):
                archived["direct_archive"] = True
            archived["title"] = archived.get("title") or item.get("title") or "未命名作品"
            write_json(child_path, archived)
        if item.get("public_video"):
            child_path = root / str(child["manifest"])
            child_manifest = read_json_if_valid(child_path) or {}
            child_manifest["public_video"] = True
            write_json(child_path, child_manifest)
        if manifest.get("platform") == "douyin":
            child_path = root / str(child["manifest"])
            child_manifest = json.loads(child_path.read_text(encoding="utf-8"))
            if item.get("douyin_media") or not item.get("public_video"):
                child_manifest["douyin_media"] = dict(item.get("douyin_media") or {"content_id": item["id"], "kind": "video"})
            write_json(child_path, child_manifest)
        elif manifest.get("platform") == "weibo":
            child_path = root / str(child["manifest"])
            child_manifest = read_json_if_valid(child_path) or {}
            if item.get("weibo_media"):
                child_manifest["weibo_media"] = dict(item["weibo_media"])
            if item.get("id"):
                child_manifest["content_id"] = str(item["id"])
            write_json(child_path, child_manifest)
        elif manifest.get("platform") == "x":
            child_path = root / str(child["manifest"])
            child_manifest = read_json_if_valid(child_path) or {}
            if item.get("x_media"):
                child_manifest["x_media"] = dict(item["x_media"])
            if item.get("id"):
                child_manifest["content_id"] = str(item["id"])
            write_json(child_path, child_manifest)
        elif manifest.get("platform") == "xiaohongshu":
            child_path = root / str(child["manifest"])
            child_manifest = read_json_if_valid(child_path) or {}
            xhs_browser = str(
                (manifest.get("creator") or {}).get("browser")
                or (manifest.get("creator") or {}).get("cookie_source")
                or ""
            ).strip().lower()
            if xhs_browser in {"chrome", "firefox"}:
                child_manifest["xiaohongshu_browser"] = xhs_browser
            creator = manifest.get("creator") or {}
            if _redbook_backend(str(creator.get("url") or "")) == "rednote":
                child_manifest["xiaohongshu_backend"] = "rednote"
            write_json(child_path, child_manifest)
        if existing_child_id and existing_child_id != child["job_id"]:
            raise ArchiveError("creator_child_identity_conflict", "父任务中的冻结子任务身份冲突。", 69)
        item.update({"child_job_id": child["job_id"], "result": "processing"})
        if child["job_id"] not in manifest["child_job_ids"]:
            manifest["child_job_ids"].append(child["job_id"])
        manifest.update({"status": "submitting", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
        if manifest.get("platform") == "wechat_channels":
            activate_staged_channel_content(root / str(child["manifest"]), parent_job_id)
    return _refresh_creator_batch_unlocked(manifest, manifest_path, root)


def creator_batch_lock_path(manifest_path: Path) -> Path:
    return manifest_path.with_suffix(".json.batch.lock")


def acquire_creator_batch_lock(manifest_path: Path) -> int | None:
    lock_path = creator_batch_lock_path(manifest_path)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def refresh_creator_batch(manifest: dict, manifest_path: Path, root: Path) -> dict:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or manifest
    try:
        latest = read_json_if_valid(manifest_path) or manifest
        return _refresh_creator_batch_unlocked(latest, manifest_path, root)
    finally:
        os.close(descriptor)


def submit_creator_batch_children(manifest: dict, manifest_path: Path, root: Path) -> dict:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or manifest
    try:
        current = read_json_if_valid(manifest_path) or manifest
        return _submit_creator_batch_children_unlocked(current, manifest_path, root)
    finally:
        os.close(descriptor)


def apply_creator_selection_unlocked(
    manifest: dict,
    manifest_path: Path,
    root: Path,
    selection: dict,
) -> dict:
    items = (manifest.get("inventory") or {}).get("items") or []
    available_ids = {str(item.get("id")) for item in items}
    selected_ids = [str(content_id) for content_id in selection.get("ids") or []]
    if len(selected_ids) != len(set(selected_ids)) or not set(selected_ids) <= available_ids:
        raise ArchiveError("creator_selection_invalid", "选择中包含重复或不属于该博主的作品。")
    selection = {**selection, "ids": selected_ids, "selected": len(selected_ids)}
    manifest.update({"status": "submitting", "updated_at": utc_now(), "selection": selection})
    manifest.pop("next_action", None)
    write_json(manifest_path, manifest)
    if not selected_ids:
        manifest.update(
            {
                "status": "completed",
                "completed_at": utc_now(),
                "counts": {"selected": 0, "processing": 0, "completed": 0, "failed": 0},
            }
        )
        write_json(manifest_path, manifest)
        return manifest
    return _submit_creator_batch_children_unlocked(manifest, manifest_path, root)


def download_creator_plan(job_id: str, limit: int, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        raise ArchiveError("job_busy", "该任务正在提交，请稍后查询同一任务状态。", 75)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("kind") != "creator_batch" or manifest.get("status") != "awaiting_download_count":
            raise ArchiveError("job_not_waiting_for_count", "该任务当前不在等待下载数量。", 66)
        items = (manifest.get("inventory") or {}).get("items") or []
        if limit < 1 or limit > len(items):
            raise ArchiveError("invalid_download_count", f"下载数量应在 1 到 {len(items)} 之间。")
        manifest = apply_creator_selection_unlocked(
            manifest,
            manifest_path,
            root,
            {"mode": "count", "limit": limit, "order": "newest", "ids": [item["id"] for item in items[:limit]]},
        )
    finally:
        os.close(descriptor)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "platform": manifest["platform"],
        "counts": manifest["counts"],
        "child_job_ids": manifest["child_job_ids"],
        "manifest": archive_relative(root, manifest_path),
    }


def download_creator_selection(
    job_id: str,
    mode: str,
    root: Path,
    collection_id: str | None = None,
    content_ids: list[str] | None = None,
) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        raise ArchiveError("job_busy", "该任务正在提交，请稍后查询同一任务状态。", 75)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("kind") != "creator_batch" or manifest.get("status") != "awaiting_download_count":
            raise ArchiveError("job_not_waiting_for_count", "该任务当前不在等待下载选择。", 66)
        inventory = manifest.get("inventory") or {}
        items = inventory.get("items") or []
        if manifest.get("platform") == "xiaohongshu" and mode in {"all", "missing"} and not inventory.get("complete"):
            raise ArchiveError(
                "creator_inventory_incomplete",
                "小红书实验清单尚未证明覆盖完整主页，不能把当前可见页当作全部作品。",
                69,
            )
        if mode == "all":
            selected = [str(item["id"]) for item in items]
            selection = {"mode": mode, "order": "newest", "ids": selected}
        elif mode == "missing":
            selected = [
                str(item["id"])
                for item in items
                if existing_content(root, str(manifest.get("platform")), str(item["id"])) is None
            ]
            selection = {"mode": mode, "order": "newest", "ids": selected}
        elif mode == "collection":
            collection = next(
                (entry for entry in manifest.get("collections") or [] if str(entry.get("id")) == str(collection_id or "")),
                None,
            )
            if not collection:
                raise ArchiveError("creator_collection_not_found", "冻结清单中没有这个合集。", 66)
            selected = [str(content_id) for content_id in collection.get("item_ids") or []]
            selection = {
                "mode": mode,
                "collection_id": str(collection["id"]),
                "collection_name": str(collection.get("name") or ""),
                "ids": selected,
            }
        elif mode == "explicit":
            selected = [str(content_id) for content_id in content_ids or []]
            if not selected or any(not re.fullmatch(r"BV[0-9A-Za-z]{10}", content_id) for content_id in selected):
                raise ArchiveError("creator_selection_invalid", "请提供至少一个有效 BVID。")
            selection = {"mode": mode, "ids": selected}
        else:
            raise ArchiveError("creator_selection_mode_invalid", "下载选择只支持 all、missing、collection 或 explicit。")
        manifest = apply_creator_selection_unlocked(manifest, manifest_path, root, selection)
    finally:
        os.close(descriptor)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "platform": manifest["platform"],
        "selection": {key: manifest["selection"].get(key) for key in ("mode", "selected", "collection_id", "collection_name") if manifest["selection"].get(key) is not None},
        "counts": manifest["counts"],
        "child_job_ids": manifest["child_job_ids"],
        "manifest": archive_relative(root, manifest_path),
    }


def download_bilibili_fallback(url: str, work_dir: Path, *, use_cookies: bool = True) -> tuple[dict, Path]:
    final_url = resolve_transparent_core_url(url, "bilibili")
    match = re.search(r"BV[0-9A-Za-z]{10}", final_url)
    if not match:
        page, _, final_url = fetch_limited(
            final_url,
            exact_hosts={"b23.tv", "bilibili.com"},
            suffixes=(".bilibili.com",),
            max_bytes=8 * 1024 * 1024,
        )
        match = re.search(r"BV[0-9A-Za-z]{10}", final_url) or re.search(rb"BV[0-9A-Za-z]{10}", page)
    if not match:
        raise ArchiveError("bilibili_id_missing", "B站链接没有解析出作品标识。", 69)
    bvid = match.group(0).decode() if isinstance(match.group(0), bytes) else match.group(0)
    jar, logged_in = _bili_load_cookie_jar(guest_only=not use_cookies)
    opener = build_opener(SafeRedirectHandler({"api.bilibili.com", "www.bilibili.com"}, (".bilibili.com",)))
    guest_vals = _bili_ensure_guest_buvid(opener)
    cookie_header = _bili_cookie_header(jar, guest_vals)
    referer = f"https://www.bilibili.com/video/{bvid}/"
    try:
        view = _bili_api_get(opener, cookie_header, "/x/web-interface/view", {"bvid": bvid}, referer=referer)
    except ArchiveError:
        view = bilibili_json("/x/web-interface/view", {"bvid": bvid})
    pages = view.get("pages") or []
    page_number = max(1, int(dict(parse_qsl(urlsplit(final_url).query)).get("p") or 1))
    if page_number > len(pages):
        raise ArchiveError("bilibili_page_missing", "B站分P不存在。", 69)
    cid = pages[page_number - 1].get("cid")
    target_qn = 80 if (logged_in and use_cookies) else 64
    play_params = {"bvid": bvid, "cid": cid, "qn": target_qn, "fnval": 0, "fourk": 1, "platform": "html5"}
    if not logged_in:
        play_params["try_look"] = 1
    try:
        play = _bili_api_get(opener, cookie_header, "/x/player/wbi/playurl", play_params, referer=referer, sign_wbi=True)
    except ArchiveError:
        play = bilibili_json("/x/player/playurl", {"bvid": bvid, "cid": cid, "qn": 64, "fnval": 0, "fourk": 1, "platform": "html5"})
    durl = (play.get("durl") or [None])[0] or {}
    candidates = [durl.get("url"), *(durl.get("backup_url") or [])]
    target = work_dir / "source.mp4"
    last_error = None
    for candidate in [candidate for candidate in candidates if candidate] * 3:
        try:
            download_public_https(str(candidate), target, referer=f"https://www.bilibili.com/video/{bvid}/")
            ensure_video_readable(target)
            break
        except ArchiveError as exc:
            target.unlink(missing_ok=True)
            last_error = exc
    else:
        raise last_error or ArchiveError("bilibili_media_missing", "B站没有返回可用媒体地址。", 69)
    title = str(view.get("title") or "未命名视频")
    if len(pages) > 1:
        part = str(pages[page_number - 1].get("part") or "")
        title = f"{title} - P{page_number} {part}".strip()
    return {
        "id": f"{bvid}_p{page_number}",
        "bvid": bvid,
        "page_number": page_number,
        "title": title,
        "webpage_url": f"https://www.bilibili.com/video/{bvid}/?p={page_number}",
    }, target


def fetch_douyin_aweme_detail(manifest: dict, *, cookie_jar=None) -> dict:
    content_id = str((manifest.get("douyin_media") or {}).get("content_id") or manifest.get("content_id") or "")
    jar = cookie_jar
    if jar is None:
        cookie_path = platform_cookie_jar("douyin")
        if not cookie_path.is_file():
            raise ArchiveError("reauthentication_required", "请先导入持久抖音 Cookie。", 69)
        jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError) as exc:
            raise ArchiveError("reauthentication_required", "抖音 Cookie 无法读取，请重新导入。", 69) from exc
    query = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "aweme_id": content_id,
        "pc_client_type": 1,
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "browser_language": "zh-CN",
        "browser_platform": "Win32" if os.name == "nt" else "MacIntel",
        "browser_name": "Chrome",
        "platform": "PC",
    }
    payload = read_douyin_response(build_opener(HTTPCookieProcessor(jar)), lambda: douyin_api_request(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/", query, jar, str(manifest["source"])), "作品详情")
    detail = payload.get("aweme_detail") or {}
    if payload.get("status_code") != 0 or str(detail.get("aweme_id") or "") != content_id:
        raise ArchiveError("reauthentication_required", "抖音登录态已失效或触发平台验证。", 69)
    if not douyin_aweme_media(detail):
        raise ArchiveError("douyin_media_missing", "抖音作品没有返回可归档媒体。", 69)
    return detail


def download_douyin_inventory_fallback(manifest: dict, work_dir: Path) -> tuple[dict, Path]:
    frozen = manifest.get("douyin_media") or {}
    choices = douyin_clean_video_choices(frozen.get("video_variants"))
    detail = {}
    title = None
    if not manifest.get("guest_only"):
        try:
            detail = fetch_douyin_aweme_detail(manifest)
            choices = douyin_video_choices(detail.get("video") or {}) + choices
        except ArchiveError:
            pass
    if not choices:
        try:
            info = inspect_with_transparent_core(str(manifest["source"]), "douyin", use_cookies=False)
        except ArchiveError as exc:
            if detail:
                raise ArchiveError("douyin_clean_source_unavailable", "抖音没有返回可确认的无水印视频来源。", 69) from exc
            raise
        if str(info.get("id") or "") == str(manifest["content_id"]):
            choices = douyin_public_video_choices(info)
            title = info.get("title")
    if not choices:
        raise ArchiveError("douyin_clean_source_unavailable", "抖音没有返回可确认的无水印视频来源。", 69)
    target = work_dir / "source.mp4"
    quality = download_best_video(choices, target, str(manifest["source"]),
                                  _download_douyin_video, allow_lower=bool(manifest.get("allow_lower_quality")))
    return {
        "id": str(detail.get("aweme_id") or frozen.get("content_id") or manifest.get("content_id") or ""),
        "title": str(detail.get("desc") or title or manifest.get("title") or "未命名视频"),
        "webpage_url": manifest["source"],
        "_video_quality": quality,
    }, target


def _douyin_safe_media_urls(urls: object) -> list[str]:
    result = []
    seen = set()
    for raw in _douyin_url_list(urls):
        parsed = _split_url(raw)
        host = (parsed.hostname or "").lower()
        playback_url = host in DOUYIN_PLAY_HOSTS and parsed.path.rstrip("/") == "/aweme/v1/play"
        if parsed.scheme not in {"http", "https"} or not (playback_url or host_allowed(host, set(), DOUYIN_MEDIA_HOST_SUFFIXES)):
            continue
        normalized = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def douyin_url_has_watermark(url):
    parsed = urlsplit(url)
    path = unquote(parsed.path).lower()
    if any(part in path for part in ("tplv-dy-water", "watermark", "/playwm", "/aweme/v1/download")):
        return True
    marked = {"watermark", "has_watermark", "with_watermark", "is_watermark", "logo", "wm"}
    return any(key.lower() in marked and value.lower() in {"1", "true", "yes", "on"}
               for key, value in parse_qsl(parsed.query))


def douyin_clean_media_urls(urls):
    return [url for url in _douyin_safe_media_urls(urls) if not douyin_url_has_watermark(url)]


def require_clean_douyin_url(url):
    if not douyin_clean_media_urls(url):
        raise ArchiveError("douyin_clean_source_unavailable", "抖音返回了带水印或来源无法确认的媒体。", 69)


def douyin_rendition_key(url):
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if "/video/tos/" in path:
        return path.partition("/video/tos/")[2]
    return url


def douyin_download_renditions(video):
    return {douyin_rendition_key(url) for url in _douyin_safe_media_urls(video.get("download_addr"))}


def douyin_clean_video_choices(choices):
    return [{**item, "urls": urls} for item in choices or [] if item.get("clean_playback")
            and (urls := douyin_clean_media_urls(item.get("urls")))]


def douyin_public_video_choices(info):
    choices = []
    formats = info.get("formats") or []
    download_urls = {douyin_rendition_key(url) for fmt in formats
                     if "download" in str(fmt.get("format_id", "")).lower() or "watermarked" in str(fmt.get("format_note", "")).lower()
                     for url in _douyin_safe_media_urls(fmt.get("url"))}
    for fmt in formats:
        urls = [url for url in douyin_clean_media_urls(fmt.get("url")) if douyin_rendition_key(url) not in download_urls]
        if urls and fmt.get("vcodec") != "none" and str(fmt.get("protocol") or "https") in {"http", "https"}:
            choices.append({"urls": urls, "clean_playback": True, "width": fmt.get("width"), "height": fmt.get("height"),
                            "fps": fmt.get("fps"), "bitrate": positive_number(fmt.get("tbr")) * 1000,
                            "bytes": fmt.get("filesize")})
    return choices


def _douyin_pick_best_urls(urls: object) -> list[str]:
    """从 url_list 中按 bitrate= 由大到小排序，返回原画质优先的 URL 列表。
    抖音视频 play_addr.url_list 通常按码率从低到高排列，
    URL query 里有 ?bitrate=xxx (单位 bps)；取最大即原画质 (1080p / 高码率)。
    若 URL 没有 bitrate 参数，则按出现顺序倒序（最后一个一般是最高）。
    """
    candidates = douyin_clean_media_urls(urls)
    if not candidates:
        return []

    def _bitrate_of(url: str) -> int:
        try:
            qs = parse_qs(urlsplit(url).query)
            br = (qs.get("bitrate") or ["0"])[0]
            return int(br) if str(br).isdigit() else 0
        except Exception:
            return 0

    # 按 bitrate 倒序（同码率按出现顺序倒序，即最后一个优先）
    return sorted(candidates, key=lambda u: (_bitrate_of(u), candidates.index(u)), reverse=True)


def _download_first_douyin_media(urls: object, target: Path, referer: str, *, video: bool = False) -> None:
    last_error = None
    candidates = douyin_clean_media_urls(urls)
    if video:
        candidates = prefer_media_routes(candidates)
    for candidate in candidates:
        try:
            download_public_https(candidate, target, referer=referer, clean_douyin=True)
            if video:
                ensure_video_readable(target)
            return
        except ArchiveError as exc:
            target.unlink(missing_ok=True)
            last_error = exc
    raise last_error or ArchiveError("douyin_clean_source_unavailable", "抖音没有返回可用的无水印媒体地址。", 69)


def _download_douyin_video(urls, target, referer):
    return _download_first_douyin_media(urls, target, referer, video=True)


def download_douyin_live_motion(choices, target, referer, *, allow_lower, reference_size):
    probes = []
    for choice in choices:
        probes.append({**choice, "live_playback_quality": "adaptive"})
        if not choice.get("inspect_first"):
            continue
        primary = urlsplit(choice["urls"][0])
        if primary.hostname != "www.douyin.com":
            continue
        probes[-1]["live_playback_quality"] = "hd_playback"
        secondary = urlunsplit(primary._replace(netloc="www.iesdouyin.com"))
        probes.append({**choice, "urls": [secondary], "alternate_hd_probe": True,
                       "live_playback_quality": "hd_playback_mirror"})
    return download_best_video(probes, target, referer, _download_douyin_video,
                               allow_lower=allow_lower, reference_size=reference_size)


def download_douyin_still(urls, target, referer, expected, *, allow_lower=False):
    best = None
    temporary = target.with_name("candidate-" + target.name)
    verified_paths = set()
    for url in douyin_clean_media_urls(urls):
        path = urlsplit(url).path
        if path in verified_paths:
            continue
        temporary.unlink(missing_ok=True)
        try:
            download_public_https(url, temporary, referer=referer, max_bytes=MAX_SOURCE_IMAGE_BYTES, clean_douyin=True)
            size = image_size(temporary)
            with temporary.open("rb") as handle:
                extension = media_extension_from_bytes("", "", handle.read(32))
            if extension == ".bin":
                continue
        except (ArchiveError, OSError, ValueError):
            continue
        if matches_dimensions(size, expected):
            verified_paths.add(path)
        quality = size[0] * size[1], temporary.stat().st_size
        if best is None or quality > best[0]:
            os.replace(temporary, target)
            best = quality, size, extension
    temporary.unlink(missing_ok=True)
    if best is None:
        raise ArchiveError("douyin_clean_source_unavailable", "抖音没有返回可打开的无水印图片。", 69)
    _, size, extension = best
    if not allow_lower and not matches_dimensions(size, expected):
        target.unlink(missing_ok=True)
        raise ArchiveError("image_resolution_unavailable",
                           f"作品标注 {expected[0]}×{expected[1]}，目前只能取得 {size[0]}×{size[1]}，已跳过低清图片。", 69)
    return size, extension


def douyin_gallery_source(manifest):
    frozen_media = manifest.get("douyin_media") or {}
    if manifest.get("guest_only"):
        if frozen_media.get("items") and frozen_media.get("clean_source_revision") == DOUYIN_CLEAN_REVISION:
            return None, frozen_media
        card = guest_douyin_work(str(manifest["source"]), str(manifest["content_id"]))
        media = (card or {}).get("douyin_media") or {}
        if media.get("items"):
            return None, media
        raise ArchiveError("guest_media_unavailable", "游客网页未提供这个抖音图集或 Live 图的无水印素材。", 69)
    # Gallery/Live-photo URLs are also frozen during inventory.  Reuse them so
    # normal children do not each reload the cookie jar and call aweme_detail.
    # Older manifests without frozen items retain the detail-API path.
    detail = None
    media = frozen_media if frozen_media.get("items") and frozen_media.get("clean_source_revision") == DOUYIN_CLEAN_REVISION else None
    try:
        detail = fetch_douyin_aweme_detail(manifest)
        refreshed = douyin_aweme_media(detail)
        if refreshed and (media is None or (refreshed.get("kind"), refreshed.get("item_count")) == (media.get("kind"), media.get("item_count"))):
            if media and refreshed.get("items") and media.get("items"):
                for fresh, old in zip(refreshed["items"], media["items"]):
                    for field in ("image_urls", "motion_urls"):
                        fresh[field] = list(dict.fromkeys([*fresh.get(field, []), *old.get(field, [])]))
                    fresh["motion_variants"] = [*(fresh.get("motion_variants") or []), *(old.get("motion_variants") or [])]
                    if not fresh.get("width") or not fresh.get("height"):
                        fresh["width"], fresh["height"] = old.get("width"), old.get("height")
            media = refreshed
    except ArchiveError:
        if media is None:
            raise
    if not media or media.get("kind") not in {"image_gallery", "live_photo"}:
        raise ArchiveError("douyin_media_type_changed", "抖音作品类型已变化，无法按冻结清单归档。", 69)
    return detail, media


def process_douyin_gallery(manifest: dict, manifest_path: Path, root: Path) -> dict:
    detail, media = douyin_gallery_source(manifest)
    content_id = str((detail or {}).get("aweme_id") or media.get("content_id") or manifest.get("content_id") or "")
    title = str((detail or {}).get("desc") or manifest.get("title") or "未命名图集").strip()
    referer = f"https://www.douyin.com/video/{content_id}"
    work_dir = manifest_path.parent / "work" / "gallery"
    work_dir.mkdir(parents=True, exist_ok=True)
    allow_lower = bool(manifest.get("allow_lower_quality"))
    def stage_gallery(source_media):
        staged = []
        for index, item in enumerate(source_media.get("items") or [], start=1):
            image_urls = item.get("image_urls") or []
            motion_urls = item.get("motion_urls") or []
            expected = (positive_number(item.get("width")), positive_number(item.get("height")))
            reference = expected
            if item.get("has_still") or image_urls:
                image = work_dir / f"{index:03d}.jpg"
                size, extension = download_douyin_still(image_urls, image, referer, expected, allow_lower=allow_lower)
                reference = size
                staged.append((image, f"图片/{index:03d}{extension}", "image",
                               {"width": size[0], "height": size[1], "advertised_size": expected,
                                "non_highest": not matches_dimensions(size, expected)}))
            if item.get("has_motion") or motion_urls:
                motion = work_dir / f"{index:03d}.mp4"
                choices = douyin_clean_video_choices(item.get("motion_variants"))
                if not choices:
                    raise ArchiveError("douyin_clean_source_unavailable", f"第 {index} 张 Live 图没有无水印动态播放源。", 69)
                quality = download_douyin_live_motion(choices, motion, referer,
                                                      allow_lower=allow_lower, reference_size=reference)
                staged.append((motion, f"Live图/{motion.name}", "live_motion", quality))
        return staged

    try:
        staged = stage_gallery(media)
    except ArchiveError:
        if detail is not None or manifest.get("guest_only"):
            raise
        refreshed = douyin_aweme_media(fetch_douyin_aweme_detail(manifest)) or {}
        if (refreshed.get("kind") != media.get("kind")
                or refreshed.get("item_count") != media.get("item_count")):
            raise
        staged = stage_gallery(refreshed)
    if not staged:
        raise ArchiveError("douyin_media_missing", "抖音图集没有可归档的图片或 Live 图视频。", 69)
    output_dir, prefix = work_archive_layout(root, manifest, "douyin", title, content_id)
    trusted = douyin_verified_archive_files(root, content_id) if output_dir.is_dir() else lambda _path: False
    outputs = []
    for source, relative, role, quality in staged:
        basename = Path(relative).name
        label = "实况" if role == "live_motion" else "图"
        target = output_dir / (f"{prefix}--{label}{basename}" if prefix else relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if role == "image":
            target, size, retained = commit_image(source, target, (quality["width"], quality["height"]))
            quality.update(width=size[0], height=size[1])
            expected = quality.pop("advertised_size")
            quality["non_highest"] = not matches_dimensions(size, expected)
            quality["source_match"] = not retained and source_dimensions_match(size, expected)
            if retained:
                quality.update(retained_existing=True, unverified_clean_source=not trusted(target))
        else:
            quality = commit_video(source, target, quality, trust_existing=trusted(target))
        outputs.append({**output_record(root, target, role), **quality})
    manifest.update(
        {
            "status": "completed",
            "updated_at": utc_now(),
            "completed_at": utc_now(),
            "title": title,
            "content_id": content_id,
            "content_type": media["kind"],
            "canonical_url": referer,
            "route": "douyin_web_media",
            "media_quality_revision": MEDIA_QUALITY_REVISION,
            "douyin_clean_revision": None if any(row.get("unverified_clean_source") for row in outputs) else DOUYIN_CLEAN_REVISION,
            "douyin_live_quality_revision": DOUYIN_LIVE_QUALITY_REVISION if media["kind"] == "live_photo" else None,
            "non_highest": any(record.get("non_highest") for record in outputs),
            "output_dir": archive_relative(root, output_dir),
            "outputs": outputs,
        }
    )
    manifest.pop("error", None)
    write_json(manifest_path, manifest)
    return manifest



def work_media_parts(manifest, root, *, refresh=False):
    platform = manifest["platform"]
    source = str(manifest["source"])
    title = manifest.get("title") or "未命名作品"
    parts = []
    if platform == "douyin":
        media = manifest.get("douyin_media") or {}
        if media.get("kind") == "video" or manifest.get("public_video") or (not media.get("items") and not refresh):
            parts.append({"id": "video-1", "kind": "video", "index": 1,
                          "choices": douyin_clean_video_choices(media.get("video_variants"))})
        else:
            if refresh or media.get("clean_source_revision") != DOUYIN_CLEAN_REVISION:
                detail, media = douyin_gallery_source(manifest)
                title = str((detail or {}).get("desc") or title).strip()
            for index, item in enumerate(media["items"], 1):
                choices = douyin_clean_video_choices(item.get("motion_variants"))
                has_motion = item.get("has_motion") or item.get("motion_urls")
                parts.append({"id": f"part-{index}", "kind": "live_photo" if has_motion else "image", "index": index,
                              "motion_index": index, "image": item, "choices": choices})
    elif platform == "weibo":
        jar = None
        try:
            jar = _weibo_load_cookie_jar()
        except ArchiveError:
            pass
        _, media, title = weibo_work_media(manifest, refresh=refresh, cookie_jar=jar)
        if media.get("kind") == "video":
            parts.append({"id": "video-1", "kind": "video", "index": 1, "choices": media.get("video_variants") or []})
        else:
            for index, item in enumerate(media.get("items") or [], 1):
                if item.get("video_choices"):
                    parts.append({"id": f"part-{index}", "kind": "video", "index": index,
                                  "choices": item["video_choices"], "image": {"urls": item.get("preview_urls") or []}})
                else:
                    motion = [{"urls": item["motion_urls"]}] if item.get("motion_urls") else []
                    parts.append({"id": f"part-{index}", "kind": "live_photo" if item.get("has_motion") and motion else "image",
                                  "index": index, "motion_index": index, "image": item, "choices": motion})
    elif platform == "x":
        jar = None
        if not manifest.get("guest_only"):
            try:
                jar = _x_load_cookie_jar()
            except ArchiveError:
                pass
        _, media, title = x_work_media(manifest, refresh=refresh, cookie_jar=jar)
        if media.get("kind") == "video":
            parts.append({"id": "video-1", "kind": "video", "index": 1, "choices": media.get("video_variants") or []})
        else:
            for index, item in enumerate(media.get("items") or [], 1):
                if item.get("video_choices"):
                    parts.append({"id": f"part-{index}", "kind": "video", "index": index,
                                  "choices": item["video_choices"], "image": {"urls": item.get("preview_urls") or []}})
                else:
                    parts.append({"id": f"part-{index}", "kind": "image", "index": index,
                                  "motion_index": 0, "image": item, "choices": []})
    elif platform == "xiaohongshu":
        resolved, browser = xhs_work_source(manifest)
        info, source = xhs_work_info(manifest, root, resolved, browser)
        title = info["title"]
        choices = info["_xhs_video_variants"]
        if choices:
            parts.append({"id": "video-1", "kind": "video", "index": 1, "choices": choices})
        seen = set()
        for image in info["thumbnails"]:
            urls = image["urls"] or image["original_urls"]
            key = Path(urlsplit(urls[0]).path).name.split("!", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            index = len(seen)
            motion_index = image.get("motion_index") or 0
            motion = info["_xhs_live_variants"][motion_index - 1] if motion_index else []
            parts.append({"id": f"part-{index}", "kind": "live_photo" if motion else "image", "index": index,
                          "motion_index": motion_index, "image": image, "choices": motion,
                          "cover": bool(choices)})
    elif platform == "bilibili":
        previews = [u for u in (_bili_safe_media_url(x) for x in (manifest.get("preview_urls") or [])) if u]
        parts.append({"id": "video-1", "kind": "video", "index": 1, "choices": [],
                      "image": {"urls": previews} if previews else {}})
    return title, source, parts


def process_selected_media(manifest, manifest_path, root):
    platform = manifest["platform"]
    content_id = manifest["content_id"]
    allow_lower = bool(manifest.get("allow_lower_quality"))
    title, referer, parts = work_media_parts(manifest, root, refresh=True)
    requested = set(manifest["media_ids"])
    chosen = [part for part in parts if part["id"] in requested]
    if len(chosen) != len(requested):
        raise ArchiveError("media_selection_changed", "可用媒体已变化，请打开作品媒体重新选择。", 69)
    directory, prefix = work_archive_layout(root, manifest, platform, title, content_id)
    stage = manifest_path.parent / "work" / "selection"
    stage.mkdir(parents=True, exist_ok=True)
    staged = []
    roles = {part["id"]: [] for part in chosen}
    for part in chosen:
        media_id, index = part["id"], part["index"]
        image = part.get("image") or {}
        size = (positive_number(image.get("width")), positive_number(image.get("height")))
        live_reference = size
        if image.get("has_still") or image.get("image_urls") or image.get("urls") or image.get("original_urls"):
            temporary = stage / f"{media_id}.img"
            source_quality = None
            if platform == "douyin":
                actual, suffix = download_douyin_still(image["image_urls"], temporary, referer, size, allow_lower=allow_lower)
                live_reference = actual
                lower = not matches_dimensions(actual, size)
                filename = f"图片/{index:03d}{suffix}"
            elif platform == "weibo":
                jar = None
                try:
                    jar = _weibo_load_cookie_jar()
                except ArchiveError:
                    pass
                actual, suffix, is_large = download_weibo_still(image.get("image_urls") or image.get("urls") or [], temporary, "https://weibo.com/", size, allow_lower=allow_lower, cookie_jar=jar)
                live_reference = actual
                lower = all(size) and not matches_dimensions(actual, size)
                source_quality = "large" if is_large else "display"
                filename = f"图片/{index:03d}{suffix}"
            elif platform == "x":
                actual, suffix, is_orig = download_x_still(image.get("image_urls") or image.get("urls") or [], temporary, referer, size, allow_lower=allow_lower)
                live_reference = actual
                lower = all(size) and not matches_dimensions(actual, size) and not is_orig
                source_quality = "orig" if is_orig else "large"
                filename = f"图片/{index:03d}{suffix}"
            else:
                originals = image["original_urls"]
                fetched, lower, actual = fetch_xhs_image(originals, [url for url in image["urls"] if url not in originals],
                                                       size, referer, allow_lower=allow_lower)
                if not fetched:
                    raise ArchiveError("image_missing", f"第 {index} 张图片未取得可读取的文件。", 69)
                data, content_type, final_url = fetched
                source_quality = xhs_image_source(final_url)
                if not lower and source_quality == "cdn_object" and all(size) and not matches_dimensions(actual, size):
                    size = actual
                suffix = media_extension_from_bytes(content_type, final_url, data)
                atomic_write(temporary, data)
                report_media_bytes(len(data))
                filename = f"配图/{index:02d}{suffix}"
            target = directory / (f"{prefix}--图{index:03d}{suffix}" if prefix else filename)
            staged.append((media_id, "image", temporary, target,
                           {"width": actual[0], "height": actual[1], "non_highest": lower,
                            **({"source_quality": source_quality} if source_quality else {})}, size))
            roles[media_id].append("image")
        if part["kind"] in {"video", "live_photo"}:
            temporary = stage / f"{media_id}.mp4"
            if part["kind"] == "video":
                if platform == "douyin":
                    info, temporary = download_douyin_inventory_fallback(manifest, stage)
                    quality = info["_video_quality"]
                elif platform == "weibo":
                    jar = None
                    try:
                        jar = _weibo_load_cookie_jar()
                    except ArchiveError:
                        pass
                    quality = download_best_video(
                        part["choices"], temporary, "https://weibo.com/",
                        lambda urls, target, ref: _download_weibo_video(urls, target, ref, cookie_jar=jar),
                        allow_lower=allow_lower,
                    )
                elif platform == "x":
                    quality = download_best_video(part["choices"], temporary, referer, _download_x_video, allow_lower=allow_lower)
                else:
                    quality = download_best_video(part["choices"], temporary, referer, _download_xhs_video, allow_lower=allow_lower)
                target = directory / (f"{prefix}.mp4" if prefix else "video.mp4")
                role = "video"
            else:
                motion_index = part["motion_index"]
                if platform == "douyin":
                    if not part["choices"]:
                        raise ArchiveError("douyin_clean_source_unavailable", f"第 {index} 张 Live 图没有无水印动态播放源。", 69)
                    quality = download_douyin_live_motion(part["choices"], temporary, referer,
                                                          allow_lower=allow_lower, reference_size=live_reference)
                elif platform == "weibo":
                    jar = None
                    try:
                        jar = _weibo_load_cookie_jar()
                    except ArchiveError:
                        pass
                    temporary = stage / f"{media_id}.mov"
                    motion_urls = [u for c in part["choices"] for u in c.get("urls", [])]
                    if not motion_urls:
                        raise ArchiveError("video_missing", f"第 {index} 张微博实况图没有可用视频链接。", 69)
                    _download_weibo_media_url(motion_urls[0], temporary, "https://weibo.com/", max_bytes=MAX_INPUT_MEDIA_BYTES, cookie_jar=jar)
                    quality = probe_video_file(temporary)
                    quality["original"] = True
                    quality["source_match"] = True
                else:
                    quality = download_best_video(part["choices"], temporary, referer, _download_xhs_video, allow_lower=allow_lower)
                ext = ".mov" if platform == "weibo" else ".mp4"
                filename = f"Live图/{motion_index:03d}{ext}" if platform in {"douyin", "weibo"} else f"动态照片-{motion_index:02d}.mp4"
                target = directory / (f"{prefix}--实况{motion_index:03d}{ext}" if prefix else filename)
                role = "live_motion" if platform in {"douyin", "weibo"} else "live_photo_motion"
            staged.append((media_id, role, temporary, target, quality, size))
            roles[media_id].append(role)
    if not all(roles.values()):
        raise ArchiveError("media_selection_empty", "所选媒体没有可用的文件。", 69)
    outputs = []
    trusted = (douyin_verified_archive_files(root, content_id) if directory.is_dir() else lambda _path: False) if platform == "douyin" else None
    for media_id, role, temporary, target, quality, expected in staged:
        target.parent.mkdir(parents=True, exist_ok=True)
        if role == "image":
            kept, size, retained = commit_image(temporary, target, (quality["width"], quality["height"]))
            if retained:
                quality["retained_existing"] = True
                if quality.get("source_quality"):
                    quality["source_quality"] = "previous_archive"
                if trusted is not None and not trusted(kept):
                    quality["unverified_clean_source"] = True
            matches_source = source_dimensions_match(size, expected)
            if platform == "xiaohongshu":
                matches_source = quality.get("source_quality") == "cdn_object" and (matches_source or not all(expected))
            elif platform == "weibo":
                matches_source = quality.get("source_quality") == "large" and (not all(expected) or matches_dimensions(size, expected))
            elif platform == "x":
                matches_source = quality.get("source_quality") == "orig" and (not all(expected) or matches_dimensions(size, expected))
            quality.update(width=size[0], height=size[1],
                           source_match=not retained and matches_source and not quality.get("non_highest"))
            if all(expected):
                quality["non_highest"] = not matches_dimensions(size, expected)
            target = kept
        else:
            quality = commit_video(temporary, target, quality, trust_existing=trusted is None or trusted(target))
        outputs.append({**output_record(root, target, role), **quality, "media_id": media_id})
    manifest.update(status="completed", updated_at=utc_now(), completed_at=utc_now(),
                    title=title, content_type="selected_media", output_dir=archive_relative(root, directory),
                    outputs=outputs, selected_media_roles=roles, non_highest=any(row.get("non_highest") for row in outputs),
                    media_selection_complete=len(chosen) == len(parts), media_quality_revision=MEDIA_QUALITY_REVISION,
                    route="selected_media")
    if platform == "douyin":
        manifest["douyin_clean_revision"] = None if any(row.get("unverified_clean_source") for row in outputs) else DOUYIN_CLEAN_REVISION
        if any(part["kind"] == "live_photo" for part in chosen):
            manifest["douyin_live_quality_revision"] = DOUYIN_LIVE_QUALITY_REVISION
    manifest.pop("error", None)
    write_json(manifest_path, manifest)
    return manifest


def recovery_timeout(deadline: float | None, maximum: float = 20.0) -> float:
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ArchiveError("recovery_window_expired", "视频号恢复窗口已结束，未完成任务保持原状态。", 69)
    return min(maximum, max(0.1, remaining))


def channels_api(
    path: str,
    *,
    query: dict | None = None,
    body: dict | None = None,
    deadline: float | None = None,
) -> dict:
    url = CHANNELS_API_BASE + path
    if path.startswith("/api/mp/"):
        query = dict(query or {}, token=mp_api_token())
    if query:
        url += "?" + urlencode(query)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=recovery_timeout(deadline)) as response:
            payload = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ArchiveError("channels_backend_unavailable", f"视频号采集后端不可用：{exc}", 69) from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        message = payload.get("msg", "视频号采集后端返回错误。") if isinstance(payload, dict) else "视频号采集后端响应无效。"
        if isinstance(payload, dict) and payload.get("code") == 400 and str(message) in {
            "请先初始化客户端 socket 连接",
            "please initialize the client socket connection first",
        }:
            raise ArchiveError("channels_authorization_required", "视频号采集会话尚未就绪。", 69)
        raise ArchiveError("channels_backend_error", str(message), 69)
    return payload.get("data") or {}


def channels_payload_data(payload: dict) -> dict:
    if payload.get("errCode", 0) != 0:
        message = str(payload.get("errMsg") or "视频号接口返回错误。")
        if message == "WXU.API.finderGetCommentDetail is not a function":
            raise ArchiveError("channels_authorization_required", "当前连接的页面尚未提供视频号详情接口。", 69)
        raise ArchiveError("channels_backend_error", message, 69)
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ArchiveError("channels_backend_error", "视频号接口响应无效。", 69)
    return data


def safe_channel_name(value: str, fallback: str) -> str:
    return (re.sub(r"[\\/:\x00-\x1f]", "_", value).strip(" .")[:180] or fallback)


def resolve_channel_share_metadata(share_url: str, deadline: float | None = None) -> dict:
    short_uri = urlsplit(share_url).path.rstrip("/").rsplit("/", 1)[-1]
    page_url = f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={short_uri}"
    request = Request(
        "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info?" + urlencode({"_pageUrl": page_url}),
        data=json.dumps({"baseReq": {"generalToken": ""}, "shortUri": short_uri}).encode(),
        headers={"Content-Type": "application/json", "Origin": "https://channels.weixin.qq.com", "Referer": page_url},
        method="POST",
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=recovery_timeout(deadline)) as response:
            payload = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ArchiveError("channels_share_resolve_failed", f"视频号分享链接解析失败：{exc}", 69) from exc
    data = payload.get("data") or {}
    eid = ((data.get("sceneInfo") or {}).get("dynamicExportId"))
    if payload.get("errCode") != 0 or not eid:
        raise ArchiveError("channels_share_resolve_failed", str(payload.get("errMsg") or "分享链接没有返回视频标识。"), 69)
    author = data.get("authorInfo") or {}
    return {
        "eid": str(eid),
        "nickname": str(author.get("nickname") or ""),
        "avatar": str(author.get("headImgUrl") or ""),
    }


def resolve_channel_share_eid(share_url: str, deadline: float | None = None) -> str:
    return str(resolve_channel_share_metadata(share_url, deadline=deadline)["eid"])


def recent_channel_author(captured_after_ms: int) -> dict | None:
    database = Path(
        os.environ.get(
            "WECHAT_CHANNELS_DATA_DB",
            str(Path.home() / ".local" / "share" / "wx_channels_download" / "v260810" / "runtime" / "data.db"),
        )
    )
    if not database.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT a.external_id, COALESCE(a.nickname, ''), COALESCE(a.avatar_url, '')
                FROM browse_history AS h
                JOIN browse_history_account AS ha ON ha.browse_history_id = h.id
                JOIN account AS a ON a.id = ha.account_id
                WHERE h.updated_at >= ? AND ha.role = 'author'
                ORDER BY h.updated_at DESC
                """,
                (captured_after_ms,),
            ).fetchall()
    except sqlite3.Error:
        return None
    if len(rows) != 1 or not rows[0][0]:
        return None
    return {"username": rows[0][0], "nickname": rows[0][1], "avatar": rows[0][2], "signature": ""}


def known_official_account_for_source(source_url: str, root: Path) -> dict | None:
    source = canonical_url(source_url)
    candidates: dict[str, dict] = {}
    database = Path(
        os.environ.get(
            "WECHAT_CHANNELS_DATA_DB",
            str(Path.home() / ".local" / "share" / "wx_channels_download" / "v260810" / "runtime" / "data.db"),
        )
    )
    if database.is_file():
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                rows = connection.execute(
                    """
                    SELECT COALESCE(h.source_url, ''), h.url, COALESCE(a.nickname, '')
                    FROM browse_history AS h
                    JOIN browse_history_account AS ha ON ha.browse_history_id = h.id
                    JOIN account AS a ON a.id = ha.account_id
                    WHERE h.type = 'article' AND ha.role = 'author'
                    """
                ).fetchall()
        except sqlite3.Error:
            rows = []
        for captured_source, article_url, nickname in rows:
            if canonical_url(str(captured_source or "")) != source:
                continue
            metadata = official_article_metadata(html.unescape(str(article_url or "")))
            biz = str(metadata.get("biz") or "")
            if biz:
                candidates[biz] = {
                    "biz": biz,
                    "account_id": hashlib.sha256(f"wechat_official_account\0{biz}".encode()).hexdigest()[:16],
                    "account_name": str(nickname or ""),
                }
    for manifest_path in (root / "jobs").glob("batch-????????T??????Z-????????/manifest.json"):
        candidate = read_json_if_valid(manifest_path)
        if not candidate or canonical_url(str(candidate.get("source") or "")) != source:
            continue
        account = candidate.get("account") or {}
        biz = str(account.get("biz") or "")
        if biz:
            candidates[biz] = {
                "biz": biz,
                "account_id": str(account.get("account_id") or "")
                or hashlib.sha256(f"wechat_official_account\0{biz}".encode()).hexdigest()[:16],
                "account_name": str(account.get("name") or candidates.get(biz, {}).get("account_name") or ""),
            }
    if len(candidates) != 1:
        return None
    return next(iter(candidates.values()))


def resolve_channel_author_from_url(share_url: str, deadline: float | None = None) -> dict:
    """Resolve a Channels share URL without opening or controlling WeChat."""
    safe_url = validate_https_url(share_url, {"weixin.qq.com", "channels.weixin.qq.com"})
    metadata = resolve_channel_share_metadata(safe_url, deadline=deadline)
    public_avatar = str(metadata.get("avatar") or "").strip()
    try:
        profile = channels_payload_data(
            channels_api("/api/channels/feed/profile", query={"eid": metadata["eid"]}, deadline=deadline)
        )
        contact = (profile.get("object") or {}).get("contact") or {}
        if contact.get("username"):
            public_nickname = str(metadata.get("nickname") or "").strip()
            profile_nickname = str(contact.get("nickname") or "").strip()
            if public_nickname and profile_nickname != public_nickname:
                raise ArchiveError(
                    "channel_author_selection_required",
                    "公开分享页与详情接口返回的博主昵称不一致，无法安全自动绑定；请用户在手机端选择正确博主。",
                    66,
                )
            return {
                "username": contact["username"],
                "nickname": contact.get("nickname", ""),
                "avatar": contact.get("headUrl", ""),
                "signature": contact.get("signature", ""),
            }
    except ArchiveError as exc:
        if exc.code != "channels_authorization_required":
            raise

    nickname = str(metadata.get("nickname") or "").strip()
    avatar = str(metadata.get("avatar") or "").strip()
    if nickname:
        if not avatar:
            raise ArchiveError(
                "channel_author_selection_required",
                "公开分享页缺少可核对的博主头像，不能只按昵称自动绑定；请用户在手机端选择正确博主。",
                66,
            )
        search = search_channel_author(nickname, deadline=deadline)
        exact = [candidate for candidate in search["candidates"] if candidate.get("nickname") == nickname]
        avatar_matches = [candidate for candidate in exact if avatar and candidate.get("avatar") == avatar]
        if avatar and exact and not avatar_matches:
            raise ArchiveError(
                "channel_author_selection_required",
                "公开分享页头像与同名搜索结果不一致，请用户在手机端选择正确博主。",
                66,
            )
        choices = avatar_matches if avatar else exact
        if len(choices) == 1:
            return choices[0]
        if len(choices) > 1:
            raise ArchiveError("channel_author_selection_required", "公开分享页对应多个同名视频号，请用户在手机端选择正确博主。", 66)

    raise ArchiveError(
        "channels_authorization_required",
        "视频号会话当前无法从分享链接唯一识别博主。任务已保留；请用户方便使用 Mac 时，在 Mac 微信中手动打开原链接后继续。",
        69,
    )


def search_channel_author(query: str, deadline: float | None = None) -> dict:
    data = channels_payload_data(
        channels_api("/api/channels/contact/search", query={"keyword": query}, deadline=deadline)
    )
    candidates = []
    for item in data.get("infoList") or []:
        contact = item.get("contact") or {}
        if contact.get("username"):
            candidates.append(
                {
                    "username": contact["username"],
                    "nickname": contact.get("nickname", ""),
                    "avatar": contact.get("headUrl", ""),
                    "signature": contact.get("signature", ""),
                }
            )
    return {"ok": True, "query": query, "count": len(candidates), "candidates": candidates}


def download_channel_url(url: str, root: Path) -> dict:
    submitted = submit_content(url, root)
    manifest_path = root / "jobs" / submitted["job_id"] / "manifest.json"
    try:
        creator = resolve_channel_author_from_url(url)
        register_channel_creator(root, url, creator)
        manifest = process_content_job(manifest_path, root)
    except ArchiveError as exc:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        waiting = exc.code in {
            "channels_authorization_required",
            "channels_backend_unavailable",
            "channels_backend_error",
            "channels_share_resolve_failed",
            "channels_task_create_failed",
        }
        manifest.update(
            {
                "status": "waiting_for_authorization" if waiting else "failed",
                "updated_at": utc_now(),
                "error": {"code": exc.code, "message": str(exc)},
            }
        )
        if not waiting:
            manifest["completed_at"] = utc_now()
        else:
            manifest["next_action"] = str(exc)
        write_json(manifest_path, manifest)
    return {
        "ok": manifest["status"] != "failed",
        "job_id": submitted["job_id"],
        "status": manifest["status"],
        "platform": "wechat_channels",
        "manifest": archive_relative(root, manifest_path),
    }


def ordinary_channel_video(obj: dict) -> bool:
    desc = obj.get("objectDesc") or {}
    return desc.get("mediaType") == CHANNEL_VIDEO_MEDIA_TYPE and bool(desc.get("media")) and not obj.get("liveInfo")



def submit_channel_objects(
    objects: list[dict],
    output_dir: Path,
    deadline: float | None = None,
) -> tuple[list[dict], list[int]]:
    if not objects:
        return [], []
    result = channels_api(
        "/api/v1/download_task/create",
        body={
            "objects": [
                {
                    "platform": "wxchannels",
                    "content": obj,
                    "download_dir": str(output_dir),
                    "config": {"spec": "original"},
                }
                for obj in objects
            ]
        },
        deadline=deadline,
    )
    task_items = result.get("tasks") or []
    records = []
    task_ids = []
    for index, obj in enumerate(objects):
        item = task_items[index] if index < len(task_items) else {}
        data = item.get("data") or {}
        task_id = int(data.get("id") or 0)
        code = item.get("code")
        if code == 0 and data.get("skipped"):
            disposition = "skipped"
        elif code == 0 and task_id:
            disposition = "submitted"
        elif task_id:
            disposition = "skipped"
        else:
            disposition = "failed"
        if task_id:
            task_ids.append(task_id)
        records.append(
            {
                "object_id": obj.get("id", ""),
                "upstream_task_id": task_id or None,
                "disposition": disposition,
                "message": item.get("msg", ""),
            }
        )
    return records, task_ids


def process_channel_content(
    manifest: dict,
    manifest_path: Path,
    root: Path,
    deadline: float | None = None,
) -> dict:
    work_dir = manifest_path.parent / "work"
    if manifest.get("channel_delivery_recovery") is True:
        videos = wait_for_channel_video([], manifest, manifest_path, root, deadline=deadline)
        info = {"id": manifest["content_id"], "title": manifest["title"], "webpage_url": manifest["source"]}
        return finalize_video_content(manifest, manifest_path, root, info, videos[0], "wechat_channels_backend")
    task_ids = manifest.get("upstream_task_ids") or []
    if not task_ids:
        obj = manifest.get("channel_object") or {}
        if not obj:
            safe_url = validate_https_url(str(manifest["source"]), {"weixin.qq.com", "channels.weixin.qq.com"})
            eid = resolve_channel_share_eid(safe_url, deadline=deadline)
            profile = channels_payload_data(
                channels_api("/api/channels/feed/profile", query={"eid": eid}, deadline=deadline)
            )
            obj = profile.get("object") or {}
        if not ordinary_channel_video(obj):
            raise ArchiveError("channel_video_not_found", "分享链接中没有找到普通视频。")
        records, task_ids = submit_channel_objects([obj], work_dir, deadline=deadline)
        if not task_ids:
            raise ArchiveError("channels_task_create_failed", records[0].get("message") or "创建视频下载任务失败。", 69)
        description = str((obj.get("objectDesc") or {}).get("description") or "未命名视频号视频")
        manifest.pop("channel_object", None)
        manifest.update(
            {
                "status": "downloading",
                "updated_at": utc_now(),
                "title": description,
                "content_id": str(obj.get("id") or manifest["content_id"]),
                "route": "wechat_channels_backend",
                "upstream_task_ids": task_ids,
            }
        )
        write_json(manifest_path, manifest)
        return manifest

    records = channel_task_records(task_ids, auto_resume=manifest.get("auto_resume") is True)
    statuses = [record.get("status") for record in records]
    if any(status in {6, 7} for status in statuses):
        raise ArchiveError("channels_download_failed", "视频号后端下载任务失败。", 69)
    if not statuses or any(status != 5 for status in statuses):
        manifest.update({"status": "downloading", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
        return manifest

    videos = wait_for_channel_video(records, manifest, manifest_path, root, deadline=deadline)
    info = {"id": manifest["content_id"], "title": manifest["title"], "webpage_url": manifest["source"]}
    return finalize_video_content(manifest, manifest_path, root, info, videos[0], "wechat_channels_backend")


def usable_channel_video(path: Path) -> bool:
    try:
        return path.suffix.lower() == ".mp4" and path.is_file() and not path.is_symlink() and path.stat().st_size > 0
    except OSError:
        return False


def channel_video_within(path: Path, allowed_root: Path) -> Path | None:
    try:
        if path.is_symlink():
            return None
        resolved_root = allowed_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_path if usable_channel_video(resolved_path) else None


def channel_local_video_candidates(manifest: dict, manifest_path: Path, root: Path) -> list[Path]:
    candidates: list[Path] = []
    work_dir = manifest_path.parent / "work"
    if work_dir.is_dir() and not work_dir.is_symlink():
        for path in work_dir.rglob("*.mp4"):
            trusted = channel_video_within(path, work_dir)
            if trusted:
                candidates.append(trusted)

    output_root = root / "content" / PLATFORM_DIRS["wechat_channels"]
    for output in manifest.get("outputs") or []:
        if output.get("role") != "video" or not output.get("path"):
            continue
        trusted = channel_video_within(root / str(output["path"]), output_root)
        if trusted:
            candidates.append(trusted)

    title = safe_content_title(str(manifest.get("title") or "未命名视频"), "未命名视频")
    content_id = str(manifest.get("content_id") or "")
    if content_id:
        archived = output_root / f"{title}--{content_id}" / "video.mp4"
        trusted = channel_video_within(archived, output_root)
        if trusted:
            candidates.append(trusted)

    unique = {str(path): path for path in candidates}
    return list(unique.values())


def channel_video_candidates(records: list[dict], manifest: dict, manifest_path: Path, root: Path) -> list[Path]:
    candidates = channel_local_video_candidates(manifest, manifest_path, root)
    work_dir = manifest_path.parent / "work"
    for record in records:
        for item in record.get("files") or []:
            path = (Path(item.get("download_dir") or "") / (item.get("name") or "")).expanduser().absolute()
            trusted = channel_video_within(path, work_dir)
            if trusted:
                candidates.append(trusted)
    unique = {str(path): path for path in candidates}
    return list(unique.values())


def wait_for_channel_video(
    records: list[dict],
    manifest: dict,
    manifest_path: Path,
    root: Path,
    deadline: float | None = None,
) -> list[Path]:
    end = time.monotonic() + max(0.0, CHANNEL_VIDEO_FINALIZE_WAIT_SECONDS)
    if deadline is not None:
        end = min(end, deadline)
    previous_state: tuple[str, int, int] | None = None
    while True:
        videos = channel_video_candidates(records, manifest, manifest_path, root)
        if len(videos) == 1:
            try:
                stat = videos[0].stat()
                current_state = (str(videos[0]), stat.st_size, stat.st_mtime_ns)
            except OSError:
                current_state = None
            if current_state is not None and current_state == previous_state:
                return videos
            previous_state = current_state
        else:
            previous_state = None
        if len(videos) > 1:
            raise ArchiveError("channel_video_ambiguous", "视频号后端返回了多个 MP4 文件，无法安全选择。", 69)
        now = time.monotonic()
        if now >= end:
            raise ArchiveError("channel_video_missing", "视频号后端完成后仍未找到稳定且唯一的 MP4 文件。", 69)
        time.sleep(min(CHANNEL_VIDEO_FINALIZE_POLL_SECONDS, end - now))


def official_batch_page_items(page: dict) -> list[dict]:
    try:
        messages = json.loads(str(page.get("general_msg_list") or "{}"))
    except json.JSONDecodeError as exc:
        raise ArchiveError("official_account_page_invalid", "公众号历史页响应无效。", 69) from exc
    items = []
    for message in messages.get("list") or []:
        published = int((message.get("comm_msg_info") or {}).get("datetime") or 0)
        primary = message.get("app_msg_ext_info") or {}
        for article in [primary, *(primary.get("multi_app_msg_item_list") or [])]:
            raw_url = html.unescape(str(article.get("content_url") or ""))
            if not raw_url:
                continue
            parsed = urlsplit(raw_url)
            normalized = urlunsplit(("https", "mp.weixin.qq.com", parsed.path or "/s", parsed.query, ""))
            normalized, platform = submission_url(normalized)
            if platform != "wechat_official_account":
                continue
            metadata = official_article_metadata(normalized)
            items.append(
                {
                    "content_id": metadata["content_id"],
                    "title": str(article.get("title") or "未命名公众号文章"),
                    "published_at": datetime.fromtimestamp(published, timezone.utc).isoformat().replace("+00:00", "Z") if published else None,
                    "canonical_url": metadata["canonical_url"],
                    "child_job_id": None,
                    "result": "discovered",
                    "error_code": None,
                }
            )
    return items


def existing_official_content(root: Path, content_id: str) -> dict | None:
    candidates = []
    pattern = "content-????????T??????Z-????????/manifest.json"
    for manifest_path in (root / "jobs").glob(pattern):
        child = read_json_if_valid(manifest_path)
        if not child:
            continue
        if child.get("platform") == "wechat_official_account" and child.get("content_id") == content_id:
            candidates.append(child)
    if not candidates:
        return None
    return next((child for child in candidates if child.get("status") == "completed"), candidates[0])


def group_legacy_official_output(manifest: dict, root: Path, account: dict) -> bool:
    changed = manifest.get("account") != account
    relative = Path(str(manifest.get("output_dir") or ""))
    platform_dir = Path("content") / PLATFORM_DIRS["wechat_official_account"]
    if relative.parent != platform_dir:
        manifest["account"] = account
        return changed

    target_relative = platform_dir / official_account_dir(account) / relative.name
    source = root / relative
    target = root / target_relative
    try:
        source.resolve().relative_to(root.resolve())
        target.parent.resolve().relative_to(root.resolve())
    except ValueError:
        raise ArchiveError("official_output_outside_root", "公众号迁移路径位于归档根目录之外，迁移已停止。", 65)
    target_prefix = f"{target_relative.as_posix()}/"
    outputs = manifest.get("outputs") or []
    if not outputs:
        raise ArchiveError("official_output_records_missing", "公众号文章没有可验证的输出记录，迁移已停止。", 65)
    output_paths = []
    for output in outputs:
        path = str(output.get("path") or "")
        try:
            suffix = Path(path).relative_to(relative)
        except ValueError:
            raise ArchiveError("official_output_path_mismatch", "公众号输出记录不属于文章目录，迁移已停止。", 65)
        if not suffix.parts or ".." in suffix.parts:
            raise ArchiveError("official_output_path_mismatch", "公众号输出记录不属于文章目录，迁移已停止。", 65)
        output_paths.append(f"{target_prefix}{suffix.as_posix()}")
    if source.is_dir() and target.exists():
        raise ArchiveError("official_output_conflict", "公众号博主目录已存在同名文章，迁移已停止。", 73)
    if source.is_symlink() or target.is_symlink():
        raise ArchiveError("official_output_symlink", "公众号文章目录不能是符号链接，迁移已停止。", 65)
    source_exists = source.is_dir()
    if not source_exists and not target.is_dir():
        raise ArchiveError("official_output_missing", "公众号文章目录不存在，迁移已停止。", 66)

    verification_paths = [root / str(output["path"]) for output in outputs] if source_exists else [root / path for path in output_paths]
    verification_root = source.resolve() if source_exists else target.resolve()
    for output, path in zip(outputs, verification_paths):
        try:
            path.resolve().relative_to(verification_root)
        except ValueError:
            raise ArchiveError("official_output_path_mismatch", "公众号输出记录不属于文章目录，迁移已停止。", 65)
        if (
            not path.is_file()
            or path.stat().st_size != int(output.get("bytes") if output.get("bytes") is not None else -1)
            or sha256_file(path) != str(output.get("sha256") or "")
        ):
            raise ArchiveError("official_output_recovery_mismatch", "公众号迁移目标校验失败，迁移已停止。", 65)

    if source_exists:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)

    for output, path in zip(outputs, output_paths):
        output["path"] = path
    manifest["account"] = account
    manifest["output_dir"] = target_relative.as_posix()
    return True


def _refresh_official_batch_unlocked(manifest: dict, manifest_path: Path, root: Path) -> dict:
    if manifest.get("status") == "awaiting_download_count" and not manifest.get("selection"):
        return manifest
    completed = unavailable = failed = processing = 0
    selected_items = (manifest.get("items") or [])[: int((manifest.get("selection") or {}).get("limit") or 0)]
    skipped = sum(item.get("result") == "skipped_existing" for item in selected_items)
    for item in selected_items:
        skipped_existing = item.get("result") == "skipped_existing"
        child_id = item.get("child_job_id")
        child_path = root / "jobs" / str(child_id) / "manifest.json"
        if not child_id or not child_path.is_file():
            processing += 1
            continue
        child = read_json_if_valid(child_path)
        if not child:
            item.update({"result": "failed", "error_code": "child_manifest_invalid"})
            failed += 1
            continue
        status = child.get("status")
        if status == "completed":
            account = manifest.get("account") if isinstance(manifest.get("account"), dict) else {}
            if group_legacy_official_output(child, root, account):
                write_json(child_path, child)
            if skipped_existing:
                continue
            item.update({"result": "completed", "error_code": None})
            completed += 1
        elif status in {"failed", "unavailable"}:
            code = str((child.get("error") or {}).get("code") or "unexpected_error")
            is_unavailable = status == "unavailable" or code in {"article_unavailable", "article_not_found"}
            if status == "failed" and is_unavailable:
                child["status"] = "unavailable"
                write_json(child_path, child)
            item.update({"result": "unavailable" if is_unavailable else "failed", "error_code": code})
            if is_unavailable:
                unavailable += 1
            else:
                failed += 1
        else:
            item.update({"result": "processing", "error_code": None})
            processing += 1
    discovered = len(manifest.get("items") or [])
    manifest["counts"] = {
        "discovered": discovered,
        "selected": len(selected_items),
        "submitted": len(selected_items) - skipped,
        "skipped_existing": skipped,
        "processing": processing,
        "completed": completed,
        "unavailable": unavailable,
        "failed": failed,
    }
    if processing:
        manifest["status"] = "processing"
    elif unavailable or failed:
        manifest.update({"status": "completed_with_failures", "completed_at": utc_now()})
    else:
        manifest.update({"status": "completed", "completed_at": utc_now()})
    manifest["updated_at"] = utc_now()
    write_json(manifest_path, manifest)
    return manifest


def refresh_official_batch(manifest: dict, manifest_path: Path, root: Path) -> dict:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or manifest
    try:
        latest = read_json_if_valid(manifest_path) or manifest
        return _refresh_official_batch_unlocked(latest, manifest_path, root)
    finally:
        os.close(descriptor)


def _submit_official_batch_children_unlocked(manifest: dict, manifest_path: Path, root: Path) -> dict:
    limit = int((manifest.get("selection") or {}).get("limit") or 0)
    for item in (manifest.get("items") or [])[:limit]:
        if item.get("child_job_id"):
            continue
        existing = existing_official_content(root, str(item["content_id"]))
        if existing:
            existing_path = root / "jobs" / str(existing["job_id"]) / "manifest.json"
            existing_changed = False
            for field in ("title", "published_at"):
                if not existing.get(field) and item.get(field):
                    existing[field] = item[field]
                    existing_changed = True
            if existing.get("account") != manifest.get("account"):
                existing["account"] = manifest.get("account") or {}
                existing_changed = True
            if existing_changed:
                write_json(existing_path, existing)
            item.update(
                {
                    "child_job_id": existing["job_id"],
                    "result": "skipped_existing" if existing.get("status") == "completed" else "processing",
                }
            )
        else:
            submitted = submit_content(str(item["canonical_url"]), root)
            child_path = root / str(submitted["manifest"])
            child = json.loads(child_path.read_text(encoding="utf-8"))
            child.update(
                {
                    "content_id": item["content_id"],
                    "parent_job_id": manifest["job_id"],
                    "title": item.get("title") or child.get("title"),
                    "published_at": item.get("published_at"),
                    "account": manifest.get("account") or {},
                }
            )
            write_json(child_path, child)
            item.update({"child_job_id": submitted["job_id"], "result": "processing"})
        manifest.update({"status": "processing", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
    return _refresh_official_batch_unlocked(manifest, manifest_path, root)


def submit_official_batch_children(manifest: dict, manifest_path: Path, root: Path) -> dict:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or manifest
    try:
        current = read_json_if_valid(manifest_path) or manifest
        return _submit_official_batch_children_unlocked(current, manifest_path, root)
    finally:
        os.close(descriptor)


def discover_official_batch(manifest: dict, manifest_path: Path, root: Path) -> dict:
    data, content_type, final_url = fetch_limited(str(manifest["source"]), exact_hosts=ARTICLE_HOSTS, max_bytes=MAX_HTML_BYTES)
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise ArchiveError("unexpected_content_type", f"文章响应类型不是 HTML：{content_type}")
    reference = official_article_metadata(final_url, data)
    if not reference["biz"]:
        reference = known_official_account_for_source(str(manifest["source"]), root) or reference
    if not reference["biz"]:
        raise ArchiveError("official_account_not_identified", "参考文章和本次采集窗口都没有唯一识别出公众号。", 69)
    manifest["account"] = {
        "name": reference["account_name"],
        "account_id": reference["account_id"],
        "biz": reference["biz"],
    }
    if reference["account_name"]:
        manifest["title"] = reference["account_name"]
    pagination = manifest["pagination"]
    offset = int(pagination.get("next_offset") or 0)
    try:
        page = channels_api("/api/mp/msg/list", query={"biz": reference["biz"], "offset": offset})
    except ArchiveError:
        first_authorization = int(pagination.get("pages") or 0) == 0
        manifest.update(
            {
                "status": "waiting_for_authorization" if first_authorization else "waiting_for_reauthentication",
                "updated_at": utc_now(),
                "next_action": "请启用已说明的微信公众号采集阶段，并在微信中打开参考文章；任务将从已保存游标继续。",
            }
        )
        write_json(manifest_path, manifest)
        return manifest

    known = {item["content_id"] for item in manifest.get("items") or []}
    for item in official_batch_page_items(page):
        if item["content_id"] not in known:
            manifest["items"].append(item)
            known.add(item["content_id"])
    has_more = bool(int(page.get("can_msg_continue") or 0))
    next_offset = int(page.get("next_offset") or 0)
    pagination.update(
        {
            "pages": int(pagination.get("pages") or 0) + 1,
            "next_offset": next_offset if has_more else None,
            "can_continue": has_more,
            "complete": not has_more,
        }
    )
    manifest["counts"]["discovered"] = len(manifest["items"])
    manifest.pop("next_action", None)
    if has_more:
        if next_offset <= offset:
            raise ArchiveError("official_account_cursor_invalid", "公众号分页游标没有前进。", 69)
        manifest.update({"status": "discovering", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
        return manifest
    available = len(manifest["items"])
    manifest["items"].sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
    manifest.update(
        {
            "status": "awaiting_download_count",
            "updated_at": utc_now(),
            "selection": None,
            "next_action": f"该公众号当前可抓取文章共 {available} 篇，默认从最新开始。你要抓取多少篇？",
        }
    )
    write_json(manifest_path, manifest)
    return manifest


def _process_official_batch_unlocked(manifest_path: Path, root: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {
        "queued",
        "discovering",
        "processing",
        "waiting_for_authorization",
        "waiting_for_reauthentication",
    }:
        return manifest
    if manifest.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"} and (
        manifest.get("pagination") or {}
    ).get("complete"):
        return manifest
    try:
        if (manifest.get("pagination") or {}).get("complete"):
            selected_items = (manifest.get("items") or [])[: int((manifest.get("selection") or {}).get("limit") or 0)]
            if any(not item.get("child_job_id") for item in selected_items):
                return _submit_official_batch_children_unlocked(manifest, manifest_path, root)
            return _refresh_official_batch_unlocked(manifest, manifest_path, root)
        manifest.update({"status": "discovering", "updated_at": utc_now()})
        write_json(manifest_path, manifest)
        return discover_official_batch(manifest, manifest_path, root)
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "updated_at": utc_now(),
                "completed_at": utc_now(),
                "error": {"code": getattr(exc, "code", "unexpected_error"), "message": str(exc)},
            }
        )
        write_json(manifest_path, manifest)
        return manifest


def process_official_batch(manifest_path: Path, root: Path) -> dict:
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        return read_json_if_valid(manifest_path) or {}
    try:
        return _process_official_batch_unlocked(manifest_path, root)
    finally:
        os.close(descriptor)


def download_official_batch_plan(job_id: str, limit: int, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        raise ArchiveError("job_busy", "该任务正在提交，请稍后查询同一任务状态。", 75)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("kind") != "batch"
            or manifest.get("platform") != "wechat_official_account"
            or manifest.get("status") != "awaiting_download_count"
        ):
            raise ArchiveError("job_not_waiting_for_count", "该任务当前不在等待抓取数量。", 66)
        items = manifest.get("items") or []
        if limit < 1 or limit > len(items):
            raise ArchiveError("invalid_download_count", f"抓取数量应在 1 到 {len(items)} 之间。")
        manifest.update(
            {
                "status": "processing",
                "updated_at": utc_now(),
                "selection": {"limit": limit, "order": "newest"},
            }
        )
        manifest.pop("next_action", None)
        write_json(manifest_path, manifest)
        manifest = _submit_official_batch_children_unlocked(manifest, manifest_path, root)
    finally:
        os.close(descriptor)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "platform": manifest["platform"],
        "counts": manifest["counts"],
        "manifest": archive_relative(root, manifest_path),
    }


def channel_author_result(root: Path, manifest: dict) -> dict:
    result = {
        "ok": True,
        "job_id": manifest["job_id"],
        "status": manifest["status"],
        "author": manifest["author"],
        "pages": manifest.get("pages", 0),
        "discovered": (manifest.get("counts") or {}).get("discovered", 0),
        "submitted": (manifest.get("counts") or {}).get("submitted", 0),
        "manifest": str(root / "jobs" / manifest["job_id"] / "manifest.json"),
    }
    if manifest.get("status") == "awaiting_download_count":
        result.update(
            {
                "available": (manifest.get("counts") or {}).get("eligible", 0),
                "question": (manifest.get("next_action") or "").split("。请询问", 1)[0] + "。你要下载多少个？",
            }
        )
    return result


def channel_creator_registry_path(root: Path) -> Path:
    return root / "state" / "channels-creators.json"


def channel_session_snapshot_path(root: Path) -> Path:
    return root / "state" / "channels-session.json"


def load_channel_session_snapshot(root: Path) -> dict:
    snapshot = read_json_if_valid(channel_session_snapshot_path(root)) or {}
    realtime_status = snapshot.get("realtime_status")
    if not isinstance(realtime_status, str) or not realtime_status:
        realtime_status = "unknown"
    registered_creators = snapshot.get("registered_creators")
    if not isinstance(registered_creators, int) or isinstance(registered_creators, bool) or registered_creators < 0:
        registered_creators = 0
    return {
        "schema_version": 1,
        "updated_at": snapshot.get("updated_at"),
        "realtime_status": realtime_status,
        "last_ready_at": snapshot.get("last_ready_at"),
        "registered_creators": registered_creators,
    }


def save_channel_session_snapshot(root: Path, realtime_status: str, registered_creators: int) -> dict:
    path = channel_session_snapshot_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            previous = load_channel_session_snapshot(root)
            now = utc_now()
            snapshot = {
                "schema_version": 1,
                "updated_at": now,
                "realtime_status": realtime_status,
                "last_ready_at": now if realtime_status == "ready" else previous.get("last_ready_at"),
                "registered_creators": registered_creators,
            }
            private_atomic_write(path, (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode())
            return snapshot
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def try_save_channel_session_snapshot(root: Path, realtime_status: str, registered_creators: int) -> dict:
    try:
        return save_channel_session_snapshot(root, realtime_status, registered_creators)
    except OSError:
        return load_channel_session_snapshot(root)


def load_channel_creator_registry(root: Path) -> dict:
    path = channel_creator_registry_path(root)
    if not path.is_file():
        return {"schema_version": 1, "updated_at": None, "creators": {}, "sources": {}}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveError("channels_creator_registry_invalid", "视频号博主注册表无法读取。", 70) from exc
    if not isinstance(registry.get("creators"), dict) or not isinstance(registry.get("sources"), dict):
        raise ArchiveError("channels_creator_registry_invalid", "视频号博主注册表格式错误。", 70)
    return registry


def register_channel_creator(root: Path, source: str, creator: dict) -> None:
    username = str(creator.get("username") or "")
    if not username:
        raise ArchiveError("channel_author_not_found", "视频号博主身份为空。", 66)
    canonical_source = canonical_url(source) if source.startswith("https://") else source
    path = channel_creator_registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            registry = load_channel_creator_registry(root)
            registry["creators"][username] = {
                "username": username,
                "nickname": str(creator.get("nickname") or ""),
                "avatar": str(creator.get("avatar") or ""),
                "signature": str(creator.get("signature") or ""),
                "last_source": canonical_source,
                "updated_at": utc_now(),
            }
            registry["sources"][canonical_source] = username
            registry["updated_at"] = utc_now()
            private_atomic_write(path, (json.dumps(registry, ensure_ascii=False, indent=2) + "\n").encode())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def registered_channel_creator(root: Path, source: str) -> dict | None:
    registry = load_channel_creator_registry(root)
    canonical_source = canonical_url(source) if source.startswith("https://") else source
    username = registry["sources"].get(canonical_source)
    creator = registry["creators"].get(username) if username else None
    return dict(creator) if isinstance(creator, dict) else None


def resolve_channel_author(
    author: str,
    root: Path | None = None,
    deadline: float | None = None,
) -> dict:
    root = root or archive_root()
    if author.startswith("https://"):
        registered = registered_channel_creator(root, author)
        if registered:
            return registered
        creator = resolve_channel_author_from_url(author, deadline=deadline)
        register_channel_creator(root, author, creator)
        return creator
    if author.endswith("@finder"):
        registered = load_channel_creator_registry(root)["creators"].get(author)
        return dict(registered) if isinstance(registered, dict) else {"username": author, "nickname": ""}
    search = search_channel_author(author, deadline=deadline)
    candidates = search["candidates"]
    exact = [candidate for candidate in candidates if candidate["nickname"] == author]
    choices = exact or candidates
    if not choices:
        raise ArchiveError("channel_author_not_found", "没有找到该视频号博主。", 66)
    if len(choices) != 1:
        raise ArchiveError("channel_author_selection_required", "博主名称不唯一，请使用视频号分享链接。", 66)
    register_channel_creator(root, author, choices[0])
    return choices[0]


def discover_channel_author(selected: dict, deadline: float | None = None) -> tuple[list[list[dict]], int]:
    pages = []
    discovered = 0
    next_marker = ""
    while True:
        recovery_timeout(deadline)
        page = channels_payload_data(
            channels_api(
                "/api/channels/contact/feed/list",
                query={"username": selected["username"], "next_marker": next_marker},
                deadline=deadline,
            )
        )
        objects = page.get("object") or []
        discovered += len(objects)
        pages.append([obj for obj in objects if ordinary_channel_video(obj)])
        if not selected.get("nickname") and objects:
            selected["nickname"] = ((objects[0].get("contact") or {}).get("nickname") or selected["username"])
        next_marker = str(page.get("lastBuffer") or "")
        if page.get("continueFlag") == 0 or not next_marker:
            return pages, discovered


def inspect_channel_author(author: str, root: Path) -> dict:
    selected = resolve_channel_author(author, root)
    pages, discovered = discover_channel_author(selected)
    objects = [obj for page in pages for obj in page]
    available = len(objects)
    job_id, job_dir, manifest = new_job(root, "channel", author)
    manifest.update(
        {
            "status": "awaiting_download_count",
            "updated_at": utc_now(),
            "author": selected,
            "pages": len(pages),
            "inventory": {
                "captured_at": utc_now(),
                "items": [
                    {
                        "id": str(obj.get("id") or ""),
                        "title": str((obj.get("objectDesc") or {}).get("description") or "未命名视频"),
                        "payload": obj,
                    }
                    for obj in objects
                ],
            },
            "counts": {"discovered": discovered, "eligible": available, "selected": 0, "submitted": 0},
            "upstream_task_ids": [],
            "upstream_tasks": [],
            "auto_resume": False,
            "next_action": f"当前可下载视频共 {available} 个。请询问用户要下载多少个，收到数量后再继续同一 Job。",
        }
    )
    write_json(job_dir / "manifest.json", manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "author": selected,
        "pages": len(pages),
        "available": available,
        "question": f"该博主当前可下载视频共 {available} 个。你要下载多少个？",
        "manifest": str(job_dir / "manifest.json"),
    }


def _inspect_channel_creator_unlocked(
    author: str,
    root: Path,
    *,
    existing_job: tuple[str, Path, dict] | None = None,
    deadline: float | None = None,
) -> dict:
    job_id, job_dir, manifest = existing_job or new_job(root, "batch", author)
    manifest.update({"kind": "creator_batch", "platform": "wechat_channels"})
    try:
        selected = resolve_channel_author(author, root, deadline=deadline)
        pages, _ = discover_channel_author(selected, deadline=deadline)
        objects = [obj for page in pages for obj in page]
        if not objects:
            raise ArchiveError("creator_inventory_empty", "该视频号博主当前没有可下载视频。", 66)
    except ArchiveError as exc:
        retryable = exc.code in {
            "channels_authorization_required",
            "channels_backend_unavailable",
            "channels_backend_error",
            "channels_share_resolve_failed",
            "recovery_window_expired",
        }
        manifest.update(
            {
                "status": "waiting_for_authorization" if retryable else "failed",
                "updated_at": utc_now(),
                "error": {"code": exc.code, "message": str(exc)},
            }
        )
        if retryable:
            manifest["next_action"] = "视频号会话暂不可用。任务已保存；请用户方便使用 Mac 时，在 Mac 微信中手动打开原链接并停留 10 秒，然后继续同一任务。"
        else:
            manifest["completed_at"] = utc_now()
            manifest.pop("next_action", None)
        write_json(job_dir / "manifest.json", manifest)
        return {
            "ok": retryable,
            "job_id": job_id,
            "status": manifest["status"],
            "platform": "wechat_channels",
            "next_action": manifest.get("next_action"),
            "manifest": archive_relative(root, job_dir / "manifest.json"),
        }
    items = [
        {
            "id": str(obj.get("id") or ""),
            "url": author,
            "title": str((obj.get("objectDesc") or {}).get("description") or "未命名视频"),
            "published_at": obj.get("createtime"),
            "payload": obj,
            "child_job_id": None,
        }
        for obj in objects
    ]
    manifest.update(
        {
            "kind": "creator_batch",
            "status": "awaiting_download_count",
            "updated_at": utc_now(),
            "platform": "wechat_channels",
            "creator": {
                "id": selected["username"],
                "name": selected.get("nickname", ""),
                "url": author,
            },
            "inventory": {"captured_at": utc_now(), "available": len(items), "items": items},
            "selection": None,
            "child_job_ids": [],
            "counts": {"selected": 0, "processing": 0, "completed": 0, "failed": 0},
            "next_action": f"该博主当前可下载视频共 {len(items)} 个，默认从最新开始。你要下载多少个？",
        }
    )
    manifest.pop("error", None)
    write_json(job_dir / "manifest.json", manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "platform": "wechat_channels",
        "creator": manifest["creator"],
        "available": len(items),
        "question": manifest["next_action"],
        "manifest": archive_relative(root, job_dir / "manifest.json"),
    }


def inspect_channel_creator(
    author: str,
    root: Path,
    *,
    existing_job: tuple[str, Path, dict] | None = None,
    deadline: float | None = None,
) -> dict:
    if existing_job is None:
        return _inspect_channel_creator_unlocked(author, root, deadline=deadline)
    job_id, job_dir, manifest = existing_job
    manifest_path = job_dir / "manifest.json"
    descriptor = acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        current = read_json_if_valid(manifest_path) or manifest
        return {
            "ok": current.get("status") != "failed",
            "job_id": job_id,
            "status": current.get("status"),
            "platform": "wechat_channels",
            "manifest": archive_relative(root, manifest_path),
        }
    try:
        current = read_json_if_valid(manifest_path) or manifest
        remains_unselected_waiting = (
            current.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}
            and not current.get("selection")
            and not current.get("child_job_ids")
        )
        if not remains_unselected_waiting:
            return {
                "ok": current.get("status") != "failed",
                "job_id": job_id,
                "status": current.get("status"),
                "platform": "wechat_channels",
                "manifest": archive_relative(root, manifest_path),
            }
        return _inspect_channel_creator_unlocked(
            author,
            root,
            existing_job=(job_id, job_dir, current),
            deadline=deadline,
        )
    finally:
        os.close(descriptor)


def download_channel_plan(job_id: str, limit: int, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "channel" or manifest.get("status") != "awaiting_download_count":
        raise ArchiveError("job_not_waiting_for_count", "该任务当前不在等待下载数量。", 66)
    available = int((manifest.get("counts") or {}).get("eligible") or 0)
    if limit < 1 or limit > available:
        raise ArchiveError("invalid_download_count", f"下载数量应在 1 到 {available} 之间。")

    selected = manifest.get("author") or {}
    inventory = (manifest.get("inventory") or {}).get("items") or []
    objects = [item.get("payload") for item in inventory if isinstance(item.get("payload"), dict)]
    eligible = len(objects)
    requested = limit
    if requested > eligible:
        raise ArchiveError("channel_inventory_invalid", "本地冻结的视频号作品清单不完整，请重新盘点。", 69)

    manifest.update(
        {
            "status": "submitting",
            "updated_at": utc_now(),
            "selection": {"limit": requested},
            "auto_resume": True,
            "counts": {
                "discovered": int((manifest.get("counts") or {}).get("discovered") or eligible),
                "eligible": eligible,
                "selected": 0,
                "submitted": 0,
                "skipped": 0,
                "submission_failed": 0,
                "waiting": 0,
                "downloading": 0,
                "completed": 0,
                "failed": 0,
            },
        }
    )
    manifest.pop("next_action", None)
    output_dir = root / "video_channels" / safe_channel_name(selected.get("nickname") or selected["username"], "unknown_author")
    manifest["output_dir"] = str(output_dir)
    chosen_objects = objects[:requested]
    for start in range(0, requested, 15):
        chosen = chosen_objects[start : start + 15]
        records, task_ids = submit_channel_objects(chosen, output_dir)
        manifest["upstream_tasks"].extend(records)
        manifest["upstream_task_ids"].extend(task_ids)
        manifest["counts"]["selected"] += len(chosen)
        manifest["counts"]["submitted"] += sum(record["disposition"] == "submitted" for record in records)
        manifest["counts"]["skipped"] += sum(record["disposition"] == "skipped" for record in records)
        manifest["counts"]["submission_failed"] += sum(record["disposition"] == "failed" for record in records)
        write_json(manifest_path, manifest)
    manifest["upstream_task_ids"] = list(dict.fromkeys(manifest["upstream_task_ids"]))
    manifest["counts"]["failed"] = manifest["counts"]["submission_failed"]
    manifest["status"] = "queued" if manifest["upstream_task_ids"] else "failed"
    if manifest["status"] == "failed":
        manifest["completed_at"] = utc_now()
    write_json(manifest_path, manifest)
    return channel_author_result(root, manifest)


def channel_task_records(task_ids: list, *, auto_resume: bool) -> list[dict]:
    records = [channels_api("/api/v1/download_task/list", query={"task_id": task_id}) for task_id in task_ids]
    statuses = [record.get("status") for record in records]
    paused_ids = [int(record.get("id") or 0) for record in records if record.get("status") == 3]
    if auto_resume and paused_ids and not any(status in {0, 1, 2, 4} for status in statuses):
        channels_api("/api/v1/download_task/resume", body={"task_ids": paused_ids})
        records = [channels_api("/api/v1/download_task/list", query={"task_id": task_id}) for task_id in task_ids]
    return records


def refresh_channel_manifest(manifest: dict, manifest_path: Path, root: Path) -> dict:
    task_ids = manifest.get("upstream_task_ids") or []
    if not task_ids:
        return manifest
    records = channel_task_records(task_ids, auto_resume=manifest.get("auto_resume") is True)
    by_id = {int(record.get("id") or 0): record for record in records}
    waiting = downloading = paused = completed = failed = 0
    outputs = []
    for record in records:
        status = record.get("status")
        if status in {0, 1}:
            waiting += 1
        elif status in {2, 4}:
            downloading += 1
        elif status == 3:
            paused += 1
        elif status == 5:
            completed += 1
        elif status in {6, 7}:
            failed += 1
        files = []
        for item in record.get("files") or []:
            file_path = (Path(item.get("download_dir") or "") / (item.get("name") or "")).expanduser().absolute()
            try:
                exists = file_path.is_file()
            except OSError:
                exists = False
            file_record = {"path": str(file_path), "exists": exists, "status": item.get("status", "")}
            files.append(file_record)
            if file_record["exists"]:
                outputs.append(str(file_path))
        record["files"] = files
    for task in manifest.get("upstream_tasks") or []:
        if task.get("upstream_task_id") in by_id:
            current = by_id[task["upstream_task_id"]]
            task.update({"download_status": current.get("status"), "progress": current.get("progress", 0), "files": current.get("files", [])})
    counts = manifest.setdefault("counts", {})
    counts.update(
        {
            "waiting": waiting,
            "downloading": downloading,
            "paused": paused,
            "completed": completed,
            "failed": int(counts.get("submission_failed", 0)) + failed,
        }
    )
    manifest["outputs"] = list(dict.fromkeys(outputs))
    update_channel_transcriptions(manifest)
    if manifest.get("status") == "paused_by_user":
        pass
    elif downloading:
        manifest["status"] = "downloading"
    elif waiting:
        manifest["status"] = "queued"
    elif paused:
        manifest["status"] = "paused"
    elif counts["failed"] or (manifest.get("transcription_counts") or {}).get("failed"):
        manifest["status"] = "completed_with_failures" if completed else "failed"
    elif (manifest.get("transcription_counts") or {}).get("pending"):
        manifest["status"] = "transcribing"
    else:
        manifest["status"] = "completed"
    if manifest["status"] in {"completed", "completed_with_failures", "failed"}:
        manifest["completed_at"] = utc_now()
    else:
        manifest.pop("completed_at", None)
    manifest["tool_version"] = VERSION
    manifest["updated_at"] = utc_now()
    write_json(manifest_path, manifest)
    return manifest


def create_channel_task(url: str, root: Path) -> dict:
    safe_url = validate_https_url(url, {"channels.weixin.qq.com"}, CHANNEL_HOST_SUFFIXES)
    job_id, job_dir, manifest = new_job(root, "channel", safe_url)
    manifest.update(
        {
            "status": "waiting_for_manual_capture",
            "completed_at": utc_now(),
            "next_action": "请在微信客户端中打开并播放该视频；安全捕获 worker 尚未启用。",
        }
    )
    write_json(job_dir / "manifest.json", manifest)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest["status"],
        "job_dir": str(job_dir),
        "next_action": manifest["next_action"],
    }


def resolve_inbox_file(root: Path, input_name: str) -> Path:
    if not INPUT_NAME_RE.fullmatch(input_name) or input_name in {".", ".."}:
        raise ArchiveError("invalid_input_name", "媒体文件名只能包含字母、数字、空格、点、下划线和连字符。")
    inbox = (root / "inbox").resolve()
    candidate = (inbox / input_name).resolve(strict=True)
    if candidate.parent != inbox or not candidate.is_file():
        raise ArchiveError("unsafe_input_path", "媒体文件必须直接位于归档 inbox 目录。")
    return candidate


def require_executable(config_name: str, default_name: str) -> str:
    if config_name in {"WECHAT_FFMPEG", "WECHAT_FFPROBE"}:
        prepare_runtime("media")
    configured = os.environ.get(config_name)
    resolved = (shutil.which(configured) if configured else None) or shutil.which(default_name)
    if not resolved:
        raise ArchiveError("missing_command", f"缺少命令：{configured or default_name}", 69)
    return resolved


def safe_subprocess_environment() -> dict[str, str]:
    allowed = {key.upper() for key in SAFE_SUBPROCESS_ENV_KEYS}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def sibling_executable(executable: str, name: str) -> str:
    path = Path(executable)
    suffix = ".exe" if path.suffix.lower() == ".exe" else ""
    sibling = path.with_name(name + suffix)
    return str(sibling) if sibling.is_file() else require_executable(f"WECHAT_{name.upper()}", name)


def safe_media_demuxer(source: Path) -> str:
    size = source.stat().st_size
    if size <= 0 or size > MAX_INPUT_MEDIA_BYTES:
        raise ArchiveError("media_size_limit", "媒体文件为空或超过大小限制。", 69)
    with source.open("rb") as handle:
        header = handle.read(16)
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "mov"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "wav"
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        return "mp3"
    if header.startswith(b"OggS"):
        return "ogg"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    raise ArchiveError("unsafe_media_container", "只接受自包含的常见媒体容器。", 69)


def run_whisper(source: Path, root: Path, audio: Path, prefix: Path, flags: list[str], suffixes: tuple[str, ...]) -> tuple[Path, list[Path]]:
    demuxer = safe_media_demuxer(source)
    model_value = os.environ.get("WECHAT_WHISPER_MODEL", "").strip() or str(root / "models" / "ggml-small.bin")
    model = Path(model_value).expanduser().resolve(strict=True)
    if not model.is_file():
        raise ArchiveError("invalid_model", "WECHAT_WHISPER_MODEL 不是文件。", 69)
    ffmpeg = require_executable("WECHAT_FFMPEG", "ffmpeg")
    whisper = require_executable("WECHAT_WHISPER_CLI", "whisper-cli")
    try:
        run_background_process(
            [ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-protocol_whitelist", "file,pipe", "-f", demuxer, "-i", str(source), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-fs", str(MAX_AUDIO_BYTES), str(audio)],
            check=True,
            timeout=1800,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=safe_subprocess_environment(),
        )
        if not audio.is_file() or audio.stat().st_size < 44 or audio.stat().st_size >= MAX_AUDIO_BYTES - 4096:
            raise ArchiveError("audio_size_limit", "解码音频为空或达到大小限制。", 69)
        run_background_process(
            [whisper, "-m", str(model), "-f", str(audio), "-l", "auto", *flags, "-of", str(prefix)],
            check=True,
            timeout=7200,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=safe_subprocess_environment(),
        )
        outputs = []
        output_bytes = 0
        for suffix in suffixes:
            path = prefix.with_suffix(suffix)
            if path.is_file():
                output_bytes += path.stat().st_size
                if output_bytes > MAX_TRANSCRIPT_BYTES:
                    raise ArchiveError("transcript_size_limit", "转写结果超过大小限制。", 69)
                outputs.append(path)
        if not outputs:
            raise ArchiveError("transcript_missing", "转写命令成功退出，但没有生成转录文件。", 70)
        return model, outputs
    finally:
        audio.unlink(missing_ok=True)


def channel_transcription_record(source: Path) -> dict:
    target = source.with_suffix(".txt")
    if target.is_file():
        return {"source": str(source), "path": str(target), "status": "completed", "sha256": sha256_file(target)}
    return {"source": str(source), "path": str(target), "status": "pending"}


def transcribe_channel_video(source: Path, root: Path, work_dir: Path) -> dict:
    target = source.with_suffix(".txt")
    token = uuid.uuid4().hex
    prefix = work_dir / f".channel-transcript-{token}"
    try:
        model, generated = run_whisper(
            source,
            root,
            work_dir / f".channel-audio-{token}.wav",
            prefix,
            ["-otxt"],
            (".txt",),
        )
        atomic_write(target, generated[0].read_bytes())
        return {
            "source": str(source),
            "path": str(target),
            "status": "completed",
            "model": model.name,
            "sha256": sha256_file(target),
        }
    except (ArchiveError, OSError, subprocess.SubprocessError) as exc:
        return {
            "source": str(source),
            "path": str(target),
            "status": "failed",
            "attempt_pid": os.getpid(),
            "error": {"code": getattr(exc, "code", "transcription_failed"), "message": str(exc)},
        }
    finally:
        prefix.with_suffix(".txt").unlink(missing_ok=True)


def update_channel_transcriptions(manifest: dict) -> None:
    records = []
    for value in manifest.get("outputs", []):
        source = Path(value)
        if source.suffix.lower() != ".mp4" or not source.is_file():
            continue
        records.append(channel_transcription_record(source))
    manifest["transcriptions"] = records
    manifest["transcription_counts"] = {
        status: sum(item["status"] == status for item in records) for status in ("completed", "pending", "failed")
    }


def transcribe_channel_directory_once(root: Path) -> tuple[dict, bool]:
    video_root = root / "video_channels"
    work_dir = root / "jobs" / "channel-transcriber"
    manifest_path = work_dir / "manifest.json"
    video_root.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    previous_by_source = {item.get("source"): item for item in previous.get("outputs", [])}
    sources = sorted(video_root.rglob("*.mp4"), key=lambda path: (path.stat().st_size, str(path)))
    records = []
    processed = False
    last_completed_at = previous.get("last_completed_at")
    for source in sources:
        stat = source.stat()
        fingerprint = {"source_bytes": stat.st_size, "source_mtime_ns": stat.st_mtime_ns}
        record = channel_transcription_record(source)
        old = previous_by_source.get(str(source), {})
        if record["status"] == "pending" and old.get("status") == "failed" and old.get("attempt_pid") == os.getpid() and all(old.get(key) == value for key, value in fingerprint.items()):
            record = old
        elif record["status"] == "pending" and not processed:
            processed = True
            record = transcribe_channel_video(source, root, work_dir)
            if record["status"] == "completed":
                last_completed_at = utc_now()
        record.update(fingerprint)
        records.append(record)
    counts = {status: sum(item["status"] == status for item in records) for status in ("completed", "pending", "failed")}
    manifest = {
        "schema_version": 1,
        "tool_version": VERSION,
        "kind": "channel_transcriber",
        "status": "running",
        "pid": os.getpid(),
        "watch_dir": str(video_root),
        "updated_at": utc_now(),
        "last_completed_at": last_completed_at,
        "counts": {"total": len(records), **counts},
        "outputs": records,
    }
    write_json(manifest_path, manifest)
    return manifest, processed


def progress_channel_jobs_once(root: Path) -> None:
    for manifest_path in sorted((root / "jobs").glob("channel-*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                manifest.get("kind") != "channel"
                or manifest.get("auto_resume") is not True
                or manifest.get("status") in {"completed", "completed_with_failures", "failed", "paused_by_user"}
            ):
                continue
            refresh_channel_manifest(manifest, manifest_path, root)
        except (ArchiveError, OSError, json.JSONDecodeError):
            continue


def watch_channel_transcripts(root: Path, interval: int, once: bool) -> dict:
    if interval < 1:
        raise ArchiveError("invalid_interval", "轮询间隔必须至少为 1 秒。")
    while True:
        manifest, processed = transcribe_channel_directory_once(root)
        if once:
            manifest["status"] = "stopped"
            write_json(root / "jobs" / "channel-transcriber" / "manifest.json", manifest)
            return {"ok": True, "manifest": str(root / "jobs" / "channel-transcriber" / "manifest.json"), "counts": manifest["counts"]}
        if not processed:
            time.sleep(interval)


def channel_transcriber_status(root: Path) -> dict:
    manifest_path = root / "jobs" / "channel-transcriber" / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("transcriber_not_started", "无人值守转写 worker 尚未生成 manifest。", 66)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    worker = {key: manifest.get(key) for key in ("tool_version", "status", "pid", "updated_at", "last_completed_at", "counts")}
    return {"ok": True, "worker": worker, "manifest": str(manifest_path)}


def transcribe_media(input_name: str, root: Path) -> dict:
    source = resolve_inbox_file(root, input_name)
    job_id, job_dir, manifest = new_job(root, "media", input_name)
    try:
        model, paths = run_whisper(
            source,
            root,
            job_dir / "audio.wav",
            job_dir / "transcript",
            ["-otxt", "-osrt", "-oj"],
            (".txt", ".srt", ".json"),
        )
        outputs = [{"path": path.name, "sha256": sha256_file(path)} for path in paths]
        manifest.update(
            {
                "status": "completed",
                "completed_at": utc_now(),
                "input_sha256": sha256_file(source),
                "model": model.name,
                "outputs": outputs,
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        return {"ok": True, "job_id": job_id, "status": "completed", "job_dir": str(job_dir), "outputs": outputs}
    except subprocess.TimeoutExpired as exc:
        error = ArchiveError("command_timeout", f"命令超时：{exc.cmd}", 70)
        manifest.update({"status": "failed", "completed_at": utc_now(), "error": {"code": error.code, "message": str(error)}})
        write_json(job_dir / "manifest.json", manifest)
        raise error from exc
    except subprocess.CalledProcessError as exc:
        error = ArchiveError("command_failed", f"命令退出码：{exc.returncode}", 70)
        manifest.update({"status": "failed", "completed_at": utc_now(), "error": {"code": error.code, "message": str(error)}})
        write_json(job_dir / "manifest.json", manifest)
        raise error from exc
    except Exception as exc:
        manifest.update(
            {
                "status": "failed",
                "completed_at": utc_now(),
                "error": {"code": getattr(exc, "code", "unexpected_error"), "message": str(exc)},
            }
        )
        write_json(job_dir / "manifest.json", manifest)
        raise


def job_status(job_id: str, root: Path) -> dict:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ArchiveError("invalid_job_id", "Job ID 格式错误。")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveError("job_not_found", "没有找到该任务。", 66)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") == "channel":
        manifest = refresh_channel_manifest(manifest, manifest_path, root)
    elif manifest.get("kind") == "creator_batch":
        manifest = refresh_creator_batch(manifest, manifest_path, root)
    elif manifest.get("kind") == "batch" and (manifest.get("pagination") or {}).get("complete"):
        manifest = refresh_official_batch(manifest, manifest_path, root)
    summary = {
        key: manifest.get(key)
        for key in ("job_id", "kind", "platform", "status", "title", "created_at", "updated_at", "completed_at", "counts")
        if manifest.get(key) is not None
    }
    if manifest.get("kind") == "creator_batch":
        selection = manifest.get("selection") or {}
        summary["selection"] = {
            key: selection.get(key)
            for key in ("mode", "selected", "limit", "order", "collection_id", "collection_name")
            if selection.get(key) is not None
        }
        if manifest.get("coverage") is not None:
            summary["coverage"] = manifest["coverage"]
        if manifest.get("collections"):
            summary["collections"] = [
                {key: collection.get(key) for key in ("id", "name", "available")}
                for collection in manifest["collections"]
            ]
    if manifest.get("status") == "awaiting_download_count":
        items = manifest.get("items") or (manifest.get("inventory") or {}).get("items") or []
        summary["available"] = len(items)
        summary["question"] = manifest.get("next_action")
    elif manifest.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}:
        summary["next_action"] = manifest.get("next_action") or "请重新登录并导入所选浏览器 Cookie，然后继续同一任务。"
    if manifest.get("error"):
        summary["error"] = {
            key: manifest["error"].get(key)
            for key in ("code", "stage")
            if manifest["error"].get(key) is not None
        }
    summary["outputs"] = [
        {key: output.get(key) for key in ("role", "bytes", "sha256") if output.get(key) is not None}
        for output in manifest.get("outputs") or []
    ]
    return {"ok": True, "job": summary}


def channel_session_status(
    root: Path | None = None,
    probe_keyword: str | None = None,
    deadline: float | None = None,
) -> dict:
    """Probe the existing local Channels session without opening WeChat."""
    root = root or archive_root()
    registry = load_channel_creator_registry(root)
    registered = len(registry["creators"])
    keyword = probe_keyword or f"__hermes_session_probe_{uuid.uuid4().hex}__"
    try:
        search_channel_author(keyword, deadline=deadline)
    except ArchiveError as exc:
        if exc.code == "channels_authorization_required":
            snapshot = try_save_channel_session_snapshot(root, "authorization_required", registered)
            return {
                "ok": False,
                "platform": "wechat_channels",
                "status": "authorization_required",
                "realtime_status": "authorization_required",
                "capabilities": {
                    "realtime_author_search": False,
                    "realtime_creator_feed": False,
                    "registered_creator_lookup": registered > 0,
                    "frozen_job_state": True,
                },
                "registered_creators": registered,
                "last_realtime_ready_at": snapshot.get("last_ready_at"),
                "next_action": "本地登记和冻结任务仍可使用；只有刷新最新作品或识别未登记博主时，才需要用户方便使用 Mac 时手动打开一次视频号链接。",
            }
        try_save_channel_session_snapshot(root, "unavailable", registered)
        return {
            "ok": False,
            "platform": "wechat_channels",
            "status": "unavailable",
            "error": {"code": exc.code, "message": str(exc)},
        }
    snapshot = try_save_channel_session_snapshot(root, "ready", registered)
    return {
        "ok": True,
        "platform": "wechat_channels",
        "status": "author_search_ready",
        "capabilities": {
            "author_search": True,
            "realtime_author_search": True,
            "registered_creator_lookup": registered > 0,
            "creator_feed": False,
            "new_creator_resolution": False,
            "frozen_job_state": True,
        },
        "registered_creators": registered,
        "last_realtime_ready_at": snapshot.get("last_ready_at"),
        "note": "只证明博主搜索接口可用；分享链接资料和作品列表将在真实任务中分别验证。",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    extract = subparsers.add_parser("extract")
    extract.add_argument("--url", required=True)
    official_batch = subparsers.add_parser("extract-official-account")
    official_batch.add_argument("--url", required=True)
    official_plan = subparsers.add_parser("download-official-account-plan")
    official_plan.add_argument("--job-id", required=True)
    official_plan.add_argument("--limit", required=True, type=int, help="总抓取篇数")
    article = subparsers.add_parser("archive-article")
    article.add_argument("--url", required=True)
    channel = subparsers.add_parser("capture-channel")
    channel.add_argument("--url", required=True)
    channel_url = subparsers.add_parser("download-channel-url")
    channel_url.add_argument("--url", required=True)
    channel_search = subparsers.add_parser("search-channel-author")
    channel_search.add_argument("--query", required=True)
    channel_inspect = subparsers.add_parser("inspect-channel-author")
    channel_inspect.add_argument("--author", required=True, help="视频号分享链接")
    channel_plan = subparsers.add_parser("download-channel-plan")
    channel_plan.add_argument("--job-id", required=True)
    channel_plan.add_argument("--limit", required=True, type=int, help="总下载数")
    creator_inspect = subparsers.add_parser("inspect-creator")
    creator_inspect.add_argument("--url", required=True)
    creator_plan = subparsers.add_parser("download-creator-plan")
    creator_plan.add_argument("--job-id", required=True)
    creator_plan.add_argument("--limit", required=True, type=int, help="总下载数")
    creator_selection = subparsers.add_parser("download-creator-selection")
    creator_selection.add_argument("--job-id", required=True)
    creator_selection.add_argument("--mode", required=True, choices=("all", "missing", "collection", "explicit"))
    creator_selection.add_argument("--collection-id")
    creator_selection.add_argument("--bvid", nargs="+", default=[])
    transcribe = subparsers.add_parser("transcribe")
    transcribe.add_argument("--input-name", required=True)
    watcher = subparsers.add_parser("watch-channel-transcripts")
    watcher.add_argument("--interval", type=int, default=10)
    watcher.add_argument("--once", action="store_true")
    content_watcher = subparsers.add_parser("watch-content")
    content_watcher.add_argument("--interval", type=int, default=10)
    content_watcher.add_argument("--once", action="store_true")
    cookie_import = subparsers.add_parser("import-browser-cookies")
    cookie_import.add_argument("--platform", required=True, choices=sorted(PLATFORM_COOKIE_DOMAINS))
    cookie_import.add_argument("--browser", required=True, choices=SUPPORTED_BROWSERS)
    resume = subparsers.add_parser("resume")
    resume.add_argument("--job-id", required=True)
    recover_session = subparsers.add_parser("recover-channel-session")
    recover_session.add_argument("--timeout", type=int, default=300)
    recover_session.add_argument("--poll-interval", type=int, default=5)
    recover_session.add_argument("--started-at", type=float)
    recover_session.add_argument("--cleanup-reserve", type=int, default=0)
    subparsers.add_parser("channel-session-status")
    subparsers.add_parser("transcriber-status")
    subparsers.add_parser("content-worker-status")
    status = subparsers.add_parser("status")
    status.add_argument("--job-id", required=True)
    subparsers.add_parser("self-check")
    return parser


def self_check(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    validate_https_url("https://www.douyin.com/user/example", {"www.douyin.com"})
    try:
        validate_https_url("http://127.0.0.1/test", {"www.douyin.com"})
    except ArchiveError:
        pass
    else:
        raise ArchiveError("self_check_failed", "URL 边界检查失败。", 70)
    expected_platforms = {
        "http://xhslink.cn/o/example": "xiaohongshu",
        "https://www.rednote.com/explore/1234567890abcdef12345678": "xiaohongshu",
        "https://v.douyin.com/example/": "douyin",
        "https://weibo.com/u/1234567890": "weibo",
    }
    for url, expected in expected_platforms.items():
        if submission_url(url)[1] != expected:
            raise ArchiveError("self_check_failed", f"平台识别检查失败：{expected}", 70)
    core = transparent_core_root()
    if str(core) not in sys.path:
        sys.path.insert(0, str(core))
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]  # noqa: F401
    except ImportError as exc:
        raise ArchiveError("transparent_core_invalid", "透明派生核心无法加载。", 69) from exc
    return {
        "ok": True,
        "version": VERSION,
        "platform": sys.platform,
        "supported_platforms": ["douyin", "xiaohongshu", "weibo"],
        "archive_root": str(root),
        "enabled": (os.environ.get("WORKS_ARCHIVE_ENABLED") or os.environ.get("WECHAT_ARCHIVE_ENABLED") or "").strip().lower() in {"1", "true", "yes"},
        "transparent_core": str(core),
        "ffmpeg": shutil.which("ffmpeg"),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = archive_root()
    try:
        if os.name == "nt" and not windows_experimental_wechat_enabled():
            always_wechat = {
                "extract-official-account", "download-official-account-plan", "capture-channel",
                "download-channel-url", "search-channel-author", "inspect-channel-author",
                "download-channel-plan", "recover-channel-session", "channel-session-status",
            }
            if args.action in always_wechat:
                require_windows_platform_support("wechat_channels")
            if args.action in {"extract", "archive-article", "inspect-creator"}:
                require_windows_platform_support(submission_url(args.url)[1])
            if args.action in {"download-creator-plan", "download-creator-selection", "resume"}:
                manifest_path = root / "jobs" / args.job_id / "manifest.json"
                manifest = read_json_if_valid(manifest_path)
                if manifest:
                    require_windows_platform_support(str(manifest.get("platform") or ""))
        if args.action in {"extract", "archive-article"}:
            require_enabled()
            result = submit_content(args.url, root)
        elif args.action == "extract-official-account":
            require_enabled()
            result = submit_official_batch(args.url, root)
        elif args.action == "download-official-account-plan":
            require_enabled()
            result = download_official_batch_plan(args.job_id, args.limit, root)
        elif args.action == "capture-channel":
            require_enabled()
            result = create_channel_task(args.url, root)
        elif args.action == "download-channel-url":
            require_enabled()
            result = download_channel_url(args.url, root)
        elif args.action == "search-channel-author":
            require_enabled()
            result = search_channel_author(args.query)
        elif args.action == "inspect-channel-author":
            require_enabled()
            result = inspect_channel_author(args.author, root)
        elif args.action == "download-channel-plan":
            require_enabled()
            result = download_channel_plan(args.job_id, args.limit, root)
        elif args.action == "inspect-creator":
            require_enabled()
            result = inspect_creator(args.url, root)
        elif args.action == "download-creator-plan":
            require_enabled()
            result = download_creator_plan(args.job_id, args.limit, root)
        elif args.action == "download-creator-selection":
            require_enabled()
            result = download_creator_selection(args.job_id, args.mode, root, args.collection_id, args.bvid)
        elif args.action == "transcribe":
            require_enabled()
            result = transcribe_media(args.input_name, root)
        elif args.action == "watch-channel-transcripts":
            require_enabled()
            result = watch_channel_transcripts(root, args.interval, args.once)
        elif args.action == "watch-content":
            require_enabled()
            result = watch_content(root, args.interval, args.once)
        elif args.action == "import-browser-cookies":
            require_enabled()
            result = import_browser_cookies(args.platform, args.browser, root)
        elif args.action == "resume":
            require_enabled()
            result = resume_job(args.job_id, root)
        elif args.action == "recover-channel-session":
            require_enabled()
            result = recover_channel_session(
                root,
                args.timeout,
                args.poll_interval,
                started_at=args.started_at,
                cleanup_reserve=args.cleanup_reserve,
            )
        elif args.action == "channel-session-status":
            require_enabled()
            result = channel_session_status(root)
        elif args.action == "transcriber-status":
            result = channel_transcriber_status(root)
        elif args.action == "content-worker-status":
            result = content_worker_status(root)
        elif args.action == "status":
            result = job_status(args.job_id, root)
        else:
            result = self_check(root)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ArchiveError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False))
        return exc.exit_code
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "local_error", "message": str(exc)}}, ensure_ascii=False))
        return 70


if __name__ == "__main__":
    sys.exit(main())
