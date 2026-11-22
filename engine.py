# -*- coding: utf-8 -*-
"""
下载引擎：调用项目内置的归档核心模块，提供本地盘点、单条勾选下载、
封面预览与多目录历史记忆能力。
"""

import hashlib
import os
import re
import sys
import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
import importlib.util
from urllib.parse import urlsplit, urlunsplit
from urllib.request import build_opener
from pathlib import Path
from http.cookiejar import CookieJar
from weakref import WeakKeyDictionary

import app_runtime
import bundled_runtime
from quality_verification import add_legacy_badges
import download_history
from functools import lru_cache, wraps
from concurrent.futures import ThreadPoolExecutor

SETTINGS_LOCK = threading.RLock()
MODULE_LOCK = threading.Lock()
COOKIE_LOCK = threading.Lock()
_FAVORITE_SESSIONS = WeakKeyDictionary()
JOB_ROOTS = {}
PLATFORMS = {"douyin", "xiaohongshu", "weibo"}

def serialized_context(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with SETTINGS_LOCK:
            return fn(*args, **kwargs)
    return wrapped


# ---- 常量（可被外部覆盖） ----
_HERE = Path(__file__).resolve().parent
_CANDIDATES = [
    _HERE / "core" / "media_archive.py",
]
CORE_SCRIPT = Path(os.environ.get("WORKS_ARCHIVE_SCRIPT", "")) if os.environ.get("WORKS_ARCHIVE_SCRIPT") else next(
    (p for p in _CANDIDATES if p.is_file()), _CANDIDATES[0]
)
SKILL_SCRIPT = CORE_SCRIPT
_EXPLICIT_ROOT = os.environ.get("WORKS_ARCHIVE_ROOT", "").strip()
def _default_download_dir() -> Path:
    """选择跨机器通用的默认下载根目录：Windows 优先首个非系统固定盘的 <盘符>:\\WorksDownloader，否则回退系统盘。"""
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


_DEFAULT_DOWNLOAD_DIR = _default_download_dir()
_DESTINATION = Path(os.environ.get("WORKS_DESTINATION_BASE") or _DEFAULT_DOWNLOAD_DIR)
AUTHOR_PLATFORMS = {"douyin": "抖音", "xiaohongshu": "小红书", "weibo": "微博"}
PLATFORM_SUBFOLDERS = {"douyin": "抖音", "xiaohongshu": "小红书", "weibo": "微博"}
_ALL_PLATFORM_DIR_CANDIDATES = ("抖音", "小红书", "rednote", "微博")
_CONFIG_PATH = app_runtime.CONFIG_PATH
try:
    DOWNLOAD_CONCURRENCY = max(1, min(int(os.environ.get("WORKS_DOWNLOAD_CONCURRENCY", "8")), 8))
except ValueError:
    DOWNLOAD_CONCURRENCY = 8
os.environ["WORKS_ARCHIVE_ENABLED"] = "1"


def is_destination_configured() -> bool:
    if _EXPLICIT_ROOT or os.environ.get("WORKS_DESTINATION_BASE"):
        return True
    settings = app_runtime.read_settings()
    for key in ("download_base", "archive_root"):
        val = settings.get(key)
        if isinstance(val, str) and val.strip():
            return True
    return False


def get_destination_base() -> Path:
    settings = app_runtime.read_settings()
    configured = settings.get("download_base")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser().resolve()
    return _DESTINATION.expanduser().resolve()


def platform_subfolder_name(base: Path, platform: str) -> str:
    if platform == "xiaohongshu":
        try:
            if (base / "rednote").is_dir() and not (base / "小红书").exists():
                return "rednote"
        except OSError:
            pass
        return "小红书"
    return PLATFORM_SUBFOLDERS.get(platform, platform)


def platform_paths_for_base(base: Path | None = None) -> dict[str, str]:
    target = (Path(base).expanduser().resolve() if base else get_destination_base())
    return {
        platform: str((target / platform_subfolder_name(target, platform)).resolve())
        for platform in ("douyin", "xiaohongshu", "weibo")
    }


def uses_direct_author_layout(platform) -> bool:
    return not _EXPLICIT_ROOT and platform in AUTHOR_PLATFORMS


def get_archive_root(platform=None) -> Path:
    if _EXPLICIT_ROOT:
        return Path(_EXPLICIT_ROOT).expanduser().resolve()
    base = get_destination_base()
    if platform in AUTHOR_PLATFORMS:
        return (base / platform_subfolder_name(base, platform)).resolve()
    settings = app_runtime.read_settings()
    folders = settings.get("platform_folders")
    if platform and isinstance(folders, dict) and isinstance(folders.get(platform), str) and folders[platform].strip():
        return Path(folders[platform]).expanduser().resolve()
    if platform in PLATFORM_SUBFOLDERS:
        return (base / platform_subfolder_name(base, platform)).resolve()
    configured_base = settings.get("download_base")
    if isinstance(configured_base, str) and configured_base.strip():
        return Path(configured_base).expanduser().resolve()
    configured = settings.get("archive_root")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser().resolve()
    legacy = app_runtime.APP_DIR / ".archive_root"
    if legacy.is_file():
        value = legacy.read_text(encoding="utf-8").strip()
        if value:
            return Path(value).expanduser().resolve()
    return base


def known_archive_roots():
    current = [ARCHIVE_ROOT, *(get_archive_root(platform) for platform in sorted(PLATFORMS))]
    if _EXPLICIT_ROOT:
        return list(dict.fromkeys(current))
    settings = app_runtime.read_settings()
    bases = [get_destination_base(), _DESTINATION.expanduser().resolve()]
    saved_bases = settings.get("known_bases")
    if isinstance(saved_bases, list):
        for item in saved_bases:
            if isinstance(item, str) and item.strip():
                try:
                    bases.append(Path(item).expanduser().resolve())
                except OSError:
                    continue
    extra = []
    for base in dict.fromkeys(bases):
        extra.append(base)
        for sub in _ALL_PLATFORM_DIR_CANDIDATES:
            extra.append((base / sub).resolve())
    if not os.environ.get("WORKS_DESTINATION_BASE"):
        folders = settings.get("platform_folders")
        if isinstance(folders, dict):
            for folder in folders.values():
                if isinstance(folder, str) and folder.strip():
                    extra.append(Path(folder).expanduser().resolve())
        for key in ("archive_root",):
            val = settings.get(key)
            if isinstance(val, str) and val.strip():
                extra.append(Path(val).expanduser().resolve())
        saved_roots = settings.get("known_roots")
        if isinstance(saved_roots, list):
            for folder in saved_roots:
                if isinstance(folder, str) and folder.strip():
                    extra.append(Path(folder).expanduser().resolve())
        extra.extend(download_history.known_roots())
    return list(dict.fromkeys([*current, *extra]))


def fixed_archive_path(platform):
    return bool(_EXPLICIT_ROOT)


def prepare_archive_root(root, platform):
    if not uses_direct_author_layout(platform):
        return
    if not root.anchor or not Path(root.anchor).is_dir():
        raise EngineError(f"找不到归档盘，无法使用 {root}。")
    base = get_destination_base()
    expected_name = platform_subfolder_name(base, platform)
    if root.parent != base or root.name.casefold() != expected_name.casefold():
        raise EngineError(f"归档目录指向其他位置，请检查 {root}。")
    root.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import ctypes
        for name in ("jobs", ".preview"):
            directory = root / name
            if not directory.exists():
                directory.mkdir(exist_ok=True)
                ctypes.windll.kernel32.SetFileAttributesW(str(directory), 0x02)


def uses_old_destination(job_id, platform):
    return uses_direct_author_layout(platform) and job_root(job_id) != get_archive_root(platform)


@serialized_context
def set_archive_root(new_path: str, platform=None) -> dict:
    global ARCHIVE_ROOT
    if _EXPLICIT_ROOT:
        raise EngineError("当前通过环境变量指定测试目录。")
    if platform is not None and platform not in PLATFORMS:
        raise EngineError("下载平台无效。")
    raw = str(new_path or "").strip()
    if not raw:
        raise EngineError(f"请输入有效的绝对路径，例如 {_DEFAULT_DOWNLOAD_DIR}。")
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise EngineError(f"请输入绝对路径，例如 {_DEFAULT_DOWNLOAD_DIR}。")
    if not target.anchor or not Path(target.anchor).is_dir():
        raise EngineError(f"找不到目标盘符 {target.anchor or raw}，请先确认磁盘已连接。")
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=target):
        pass
    settings = app_runtime.read_settings()
    previous_base = get_destination_base()
    previous = get_archive_root(platform) if platform else previous_base
    settings["schema_version"] = 3
    if platform:
        for sub in ("content", "jobs", ".preview"):
            (target / sub).mkdir(exist_ok=True)
        folders = settings.get("platform_folders")
        if not isinstance(folders, dict):
            folders = {}
        settings["platform_folders"] = {**folders, platform: str(target)}
        known_roots = [r for r in (settings.get("known_roots") or []) if isinstance(r, str)]
        for r in (str(previous), str(target)):
            if r and r not in known_roots:
                known_roots.append(r)
        settings["known_roots"] = known_roots
    else:
        known_bases = [b for b in (settings.get("known_bases") or []) if isinstance(b, str)]
        for b in (str(_DESTINATION.expanduser().resolve()), str(previous_base), str(target)):
            if b and b not in known_bases:
                known_bases.append(b)
        settings["known_bases"] = known_bases
        known_roots = [r for r in (settings.get("known_roots") or []) if isinstance(r, str)]
        old_folders = settings.get("platform_folders")
        if isinstance(old_folders, dict):
            for val in old_folders.values():
                if isinstance(val, str) and val and val not in known_roots:
                    known_roots.append(val)
        if known_roots:
            settings["known_roots"] = known_roots
        settings["download_base"] = str(target)
        settings["archive_root"] = str(target)
        settings.pop("platform_folders", None)
    app_runtime.atomic_json(_CONFIG_PATH, settings)
    if platform is None:
        ARCHIVE_ROOT = target
        os.environ["WORKS_ARCHIVE_ROOT"] = str(target)
    return {
        "path": str(target),
        "old_path": str(previous),
        "base_path": str(get_destination_base()),
        "platform_paths": platform_paths_for_base(),
    }


ARCHIVE_ROOT = get_archive_root()
os.environ["WORKS_ARCHIVE_ROOT"] = str(ARCHIVE_ROOT)


def job_root(job_id: str) -> Path:
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise EngineError("无效任务 ID。")
    if job_id in JOB_ROOTS:
        return JOB_ROOTS[job_id]
    for root in known_archive_roots():
        if (root / "jobs" / job_id / "manifest.json").is_file():
            JOB_ROOTS[job_id] = root
            return root
    return ARCHIVE_ROOT


class EngineError(Exception):
    """引擎层错误，携带友好中文提示。"""


def _load_module():
    """动态加载内置的 core/media_archive.py 为模块。"""
    if not CORE_SCRIPT.is_file():
        raise EngineError(f"找不到核心归档脚本：{CORE_SCRIPT}")
    spec = importlib.util.spec_from_file_location("media_archive_core", str(CORE_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    vendor_core = CORE_SCRIPT.parent / "vendor" / "transparent-core"
    if vendor_core.is_dir() and str(vendor_core) not in sys.path:
        sys.path.insert(0, str(vendor_core))
    assert spec.loader
    spec.loader.exec_module(mod)
    mod.operation_wait = app_runtime.operation_timeout
    mod.prepare_runtime = bundled_runtime.ensure
    return mod


# 模块级单例（懒加载）
_MOD = None

# 是否跳过转录（语音转文字）。用户选择「只下载，不要转录」时设为 True。
SKIP_TRANSCRIPTION = os.environ.get("WECHAT_SKIP_TRANSCRIPTION", "1").strip().lower() in {
    "1", "true", "yes",
}


def _patch_transcription(mod):
    """
    跳过视频下载后的转录步骤（语音转文字）。
    media_archive.py 的 finalize_video_content 在下载完视频后无条件调用
    transcribe_content_video，而后者依赖 whisper-cli.exe；缺失时会导致
    整个视频任务被标记为 failed（尽管 video.mp4 已经成功落盘）。
    这里把 transcribe_content_video 替换为空操作，让视频直接进入 completed。
    不影响图集/动图（它们本就不走转录）。
    """
    def _noop_transcribe(video, output_dir, root):
        return []
    if hasattr(mod, "transcribe_content_video"):
        mod.transcribe_content_video = _noop_transcribe


@serialized_context
def get_mod():
    global _MOD
    with MODULE_LOCK:
        if _MOD is None:
            _MOD = _load_module()
            if SKIP_TRANSCRIPTION:
                _patch_transcription(_MOD)
    return _MOD


def self_check():
    try:
        if not bundled_runtime.warm():
            return {"ok": True, "download_ready": False, "preparing": True, "app_version": app_runtime.VERSION}
    except (OSError, EOFError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    """运行自检，返回 dict。"""
    try:
        mod = get_mod()
        result = mod.self_check(ARCHIVE_ROOT)
        try:
            redbook = mod._redbook_command()
        except Exception:
            redbook = None
        result.update(app_version=app_runtime.VERSION, log_dir=str(app_runtime.DATA_DIR / "logs"),
                      ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"), redbook=bool(redbook))
        result["download_ready"] = bool(result["ffmpeg"] and result["ffprobe"] and redbook)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def inspect_creator(url: str, browser: str | None = None, *, guest_only=False):
    mod = get_mod()
    url, platform = mod.submission_url(url)
    submitted_url = url
    url = mod.resolve_transparent_core_url(url, platform)
    url, platform = mod.submission_url(url)
    root = get_archive_root(platform)
    prepare_archive_root(root, platform)
    browser = (browser or "firefox").strip().lower()
    is_profile = mod.is_creator_profile_url(url, platform)
    if guest_only:
        if platform not in {"douyin", "xiaohongshu"}:
            raise EngineError("免登录盘点作者主页目前支持抖音和小红书；微博请使用浏览器登录态。")
        elif is_profile:
            creator, items = mod.guest_creator_inventory(url, platform)
            result = mod.save_creator_inventory(url, platform, root, creator, items, [])
        else:
            result = mod.inspect_single_work(url, root, public_only=True)
    elif not is_profile:
        if platform == "douyin":
            try:
                result = _inspect_douyin_with_browser(mod, url, root, browser, single_work=True)
            except (mod.ArchiveError, EngineError):
                result = mod.inspect_single_work(url, root, public_only=True)
        elif platform == "weibo":
            result = _inspect_weibo_with_browser(mod, url, root, browser, single_work=True)
        else:
            try:
                result = mod.inspect_single_work(url, root, public_only=True)
            except mod.ArchiveError as exc:
                if exc.code in {"work_url_required", "transparent_core_invalid"}:
                    raise
                result = mod.inspect_single_work(url, root, xhs_browser=browser)
    elif platform == "xiaohongshu":
        result = mod.inspect_creator(url, root, xhs_browser=browser)
    elif platform == "douyin":
        result = _inspect_douyin_with_browser(mod, url, root, browser)
    elif platform == "weibo":
        result = _inspect_weibo_with_browser(mod, url, root, browser)
    else:
        result = mod.inspect_creator(url, root)
    app_runtime.operation_timeout()
    if not result.get("ok"):
        return result
    job_id = result["job_id"]
    JOB_ROOTS[job_id] = root
    bind_inventory_author(job_id, root, profile_url=submitted_url)
    return {**result, **inventory_view(job_id, prefetch=True)}


def _inspect_douyin_with_browser(mod, url, root, browser, *, single_work=False):
    inspect = mod.inspect_single_work if single_work else mod.inspect_creator
    try:
        with COOKIE_LOCK:
            jar = mod.extract_platform_browser_cookies("douyin", browser)
    except mod.ArchiveError as browser_error:
        if browser_error.code not in {"cookie_import_failed", "platform_cookies_missing"}:
            raise
        if not mod.platform_cookie_jar("douyin").is_file():
            raise
        logging.info("douyin browser session unavailable code=%s", browser_error.code)
        try:
            return inspect(url, root)
        except mod.ArchiveError as cached_error:
            if cached_error.code not in {"reauthentication_required", "douyin_empty_response"}:
                raise
            raise EngineError("读取所选浏览器的抖音登录态失败，缓存也无法盘点。请确认浏览器已登录，"
                              "再点击「导入浏览器 Cookie」或重新盘点。") from cached_error
    result = inspect(url, root, douyin_jar=jar)
    app_runtime.operation_timeout()
    if result.get("ok"):
        try:
            with COOKIE_LOCK:
                mod.save_platform_cookies("douyin", jar)
        except OSError:
            logging.warning("douyin browser session could not be saved", exc_info=True)
    return result


def _inspect_weibo_with_browser(mod, url, root, browser, *, single_work=False):
    inspect = mod.inspect_single_work if single_work else mod.inspect_creator
    try:
        with COOKIE_LOCK:
            jar = mod.extract_platform_browser_cookies("weibo", browser)
    except mod.ArchiveError as browser_error:
        if browser_error.code not in {"cookie_import_failed", "platform_cookies_missing"}:
            raise
        if not mod.platform_cookie_jar("weibo").is_file():
            raise
        logging.info("weibo browser session unavailable code=%s", browser_error.code)
        try:
            return inspect(url, root)
        except mod.ArchiveError as cached_error:
            if cached_error.code not in {"reauthentication_required", "weibo_api_failed"}:
                raise
            raise EngineError("读取所选浏览器的微博登录态失败，缓存也无法盘点。请确认浏览器已登录微博，"
                              "再点击「导入浏览器 Cookie」或重新盘点。") from cached_error
    result = inspect(url, root, weibo_jar=jar)
    app_runtime.operation_timeout()
    if result.get("ok"):
        try:
            with COOKIE_LOCK:
                mod.save_platform_cookies("weibo", jar)
        except OSError:
            logging.warning("weibo browser session could not be saved", exc_info=True)
    return result


def scan_favorite_author(platform, url, browser):
    mod = get_mod()
    known_ids = app_runtime.favorite_baseline.get()
    if platform == "xiaohongshu":
        return mod.xiaohongshu_creator_inventory(url, browser, include_profile=False, known_ids=known_ids)
    budget = app_runtime.current_budget()
    session_key = (platform, browser)
    with COOKIE_LOCK:
        app_runtime.operation_timeout()
        wave = _FAVORITE_SESSIONS.setdefault(budget, {}) if budget is not None else {}
        if session_key not in wave:
            try:
                wave[session_key] = tuple(mod.extract_platform_browser_cookies(platform, browser))
            except mod.ArchiveError as exc:
                if exc.code not in {"cookie_import_failed", "platform_cookies_missing"} or not mod.platform_cookie_jar(platform).is_file():
                    raise
                wave[session_key] = None
    cookies = wave[session_key]
    jar = CookieJar() if cookies is not None else None
    for cookie in cookies or ():
        jar.set_cookie(cookie)
    if platform == "weibo":
        return mod.weibo_creator_inventory(url, cookie_jar=jar, include_profile=False, known_ids=known_ids)
    return mod.douyin_creator_inventory(url, cookie_jar=jar, include_profile=False, known_ids=known_ids)


def favorite_work_published_at(platform, item, browser):
    if platform == "xiaohongshu":
        mod = get_mod()
        try:
            note = mod._redbook_read_note(item["url"], browser)
            return note.get("time") or note.get("publishTime") or note.get("publish_time")
        except mod.ArchiveError:
            return None
    return None


def bind_inventory_author(job_id, root, *, profile_url=None):
    mod = get_mod()
    path = root / "jobs" / job_id / "manifest.json"
    manifest = mod.read_json_if_valid(path) or {}
    platform = manifest.get("platform")
    creator = manifest.get("creator") or {}
    direct = uses_direct_author_layout(platform) and root == get_archive_root(platform)
    if platform not in AUTHOR_PLATFORMS or not creator.get("id") or creator.get("single_work"):
        if direct:
            manifest["direct_archive"] = True
            mod.write_json(path, manifest)
        return
    nickname = mod.safe_content_title(str(creator.get("name") or ""), "作者")[:38].rstrip(" .")
    account = mod.safe_content_title(str(creator.get("account") or creator.get("user_id") or creator["id"]), "未知")[:42].rstrip(" .")
    name = f"{nickname}（{account}）"
    proposed = name if direct else (Path("content") / mod.PLATFORM_DIRS[platform] / name).as_posix()
    folder = download_history.bind_author(root, platform, creator, (manifest.get("inventory") or {}).get("items", []), proposed)
    if manifest.get("author_folder") != folder or profile_url:
        manifest["author_folder"] = folder
        manifest["direct_archive"] = direct
        manifest["author_url"] = profile_url or manifest.get("author_url") or creator.get("url")
        mod.write_json(path, manifest)


def inventory_items(manifest):
    folder = manifest.get("author_folder")
    return [dict(item, _author_folder=folder, _direct_archive=manifest.get("direct_archive"))
            for item in (manifest.get("inventory") or {}).get("items", [])]


def inventory_view(job_id: str, *, prefetch=False):
    manifest = read_manifest(job_id)
    if not manifest:
        raise EngineError("找不到作品清单。")
    root = job_root(job_id)
    items = inventory_items(manifest)
    enrich_items_with_cover(items, root=root)
    labels = download_history.quality_labels(manifest.get("platform"))
    for item in items:
        work_id = str(item.get("id"))
        item["archive_quality"] = labels.get(work_id, "")
        if work_id in labels:
            item["_downloaded"] = True
    add_legacy_badges(items, manifest.get("platform"), download_history.connect, get_mod().JOB_ID_RE)
    if prefetch:
        background_generate_covers(items, root=root, fetch_remote=True)
    # The browser only needs display fields and IDs. Keep signed media URLs in the local manifest.
    fields = {"id", "title", "published_at", "media_kind", "media_item_count",
              "image_quality", "archive_quality", "_cover", "_downloaded"}
    raw_creator = manifest.get("creator") or {}
    return {
        "job_id": job_id, "platform": manifest.get("platform"), "archive_root": str(root),
        "author_folder": manifest.get("author_folder"),
        "creator": raw_creator,
        "items": [{key: value for key, value in item.items() if key in fields} for item in items],
    }


@lru_cache(maxsize=64)
def _media_snapshot(job_id, item_id):
    batch = read_manifest(job_id) or {}
    platform = batch.get("platform")
    item = next((row for row in inventory_items(batch) if str(row["id"]) == item_id), None)
    if platform not in AUTHOR_PLATFORMS or item is None:
        raise EngineError("找不到该作品的媒体清单。")
    creator = batch.get("creator") or {}
    mod = get_mod()
    source = {"platform": platform, "content_id": item_id, "source": item["url"], "title": item.get("title"),
              "douyin_media": item.get("douyin_media"), "weibo_media": item.get("weibo_media"),
              "xiaohongshu_browser": creator.get("browser"),
              "xiaohongshu_backend": creator.get("xiaohongshu_backend"), "guest_only": item.get("guest_only")}
    title, url, parts = mod.work_media_parts(source, job_root(job_id))
    return platform, title, url, parts


def work_media_view(job_id, item_id):
    platform, title, source, parts = _media_snapshot(job_id, item_id)
    return {"title": title, "source": source, "parts": [
        {**{key: part[key] for key in ("id", "kind", "index")}, "cover": bool(part.get("cover")),
         "image": bool(part.get("image")), "playable": bool(part.get("choices"))}
        for part in parts]}


def work_media_preview(job_id, item_id, part_id, motion):
    platform, _, source, parts = _media_snapshot(job_id, item_id)
    part = next((row for row in parts if row["id"] == part_id), {})
    mod = get_mod()
    if motion:
        choices = part.get("choices") or []
        if platform == "douyin":
            choices = mod.douyin_clean_video_choices(choices)
        choices = sorted(choices, key=mod.video_rank, reverse=True)
        urls = [url for choice in reversed(choices) for url in choice["urls"]]
    else:
        image = part.get("image") or {}
        if platform == "douyin":
            urls = image.get("image_urls")
        elif platform == "weibo":
            urls = image.get("preview_urls") or image.get("image_urls") or image.get("urls")
        else:
            urls = image.get("urls")
    if platform == "douyin":
        suffixes = mod.DOUYIN_MEDIA_HOST_SUFFIXES
        exact = mod.DOUYIN_PLAY_HOSTS
        urls = mod.douyin_clean_media_urls(urls)
        referer = source
    elif platform == "weibo":
        suffixes = mod.WEIBO_MEDIA_HOST_SUFFIXES
        exact = mod.WEIBO_MEDIA_EXACT_HOSTS
        referer = "https://weibo.com/"
    else:
        suffixes = mod.XHS_MEDIA_HOST_SUFFIXES
        exact = mod.XHS_MEDIA_EXACT_HOSTS
        referer = source
    for url in dict.fromkeys(urls or []):
        if not mod.host_allowed(urlsplit(url).hostname or "", exact, suffixes):
            continue
        try:
            data, mime, final = mod.fetch_limited(url, exact_hosts=exact, suffixes=suffixes,
                                                   max_bytes=(20 if motion else 8) * 1024 * 1024,
                                                   headers={"User-Agent": mod.DOUYIN_USER_AGENT, "Referer": referer})
            if platform == "douyin":
                mod.require_clean_douyin_url(final)
            if motion and len(data) > 12 and data[4:8] == b"ftyp":
                return data, "video/mp4"
            if not motion and mod.media_extension_from_bytes(mime, final, data) != ".bin":
                from io import BytesIO
                from PIL import Image, ImageOps
                out = BytesIO()
                with Image.open(BytesIO(data)) as image:
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((950, 950))
                    image.convert("RGB").save(out, "JPEG", quality=84)
                return out.getvalue(), "image/jpeg"
        except (mod.ArchiveError, ValueError, OSError):
            continue
    return None


def stage_media_batch(job_id, ids, selected):
    manifest = read_manifest(job_id) or {}
    platform = manifest.get("platform")
    available = {str(row["id"]): row for row in (manifest.get("inventory") or {}).get("items", [])}
    if not set(ids) <= available.keys():
        raise EngineError("选择包含清单中不存在的作品。")
    if selected and (platform not in AUTHOR_PLATFORMS or not set(selected) <= set(ids)):
        raise EngineError("所选媒体与当前作品清单不匹配。")
    mod = get_mod()
    items = []
    for item_id in ids:
        row = dict(available[item_id], child_job_id=None)
        if item_id in selected:
            keys = selected[item_id]
            if (not isinstance(keys, list) or not keys or len(keys) > mod.MAX_MEDIA_ITEMS + 1
                    or not all(isinstance(key, str) and re.fullmatch(r"video-1|part-[1-9]\d{0,2}", key) for key in keys)):
                raise EngineError("请为作品至少勾选一项有效媒体。")
            row["media_ids"] = list(dict.fromkeys(keys))
        items.append(row)
    root = job_root(job_id)
    creator = manifest["creator"]
    result = mod.save_creator_inventory(creator["url"], platform, root, creator, items, manifest.get("collections") or [])
    new_id = result["job_id"]
    JOB_ROOTS[new_id] = root
    bind_inventory_author(new_id, root, profile_url=manifest.get("author_url"))
    return new_id

def download_selection(job_id: str, ids: list[str]):
    """
    下载选中的作品（单条勾选）。
    直接调用 apply_creator_selection_unlocked，构造 explicit 选择，
    绕过 CLI 层的 BVID 正则限制（抖音 ID 是纯数字）。
    """
    root = job_root(job_id)
    mod = get_mod()
    if not mod.JOB_ID_RE.fullmatch(job_id):
        raise EngineError("无效任务 ID")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise EngineError("任务清单不存在，请先盘点作者主页。")
    descriptor = mod.acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        raise EngineError("该任务正在提交，请稍后再试。")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("kind") != "creator_batch":
            raise EngineError("不是作者任务。")
        if manifest.get("status") != "awaiting_download_count":
            if set(map(str, (manifest.get("selection") or {}).get("ids", []))) != set(map(str, ids)):
                raise EngineError("该任务已提交不同选择，请重新盘点。")
            if manifest.get("status") == "submitting":
                manifest = mod._submit_creator_batch_children_unlocked(manifest, manifest_path, root)
            return {"ok": True, "job_id": job_id, "resumed": True, "status": manifest.get("status")}
        unknown = set(map(str, ids)) - {str(it.get("id")) for it in (manifest.get("inventory") or {}).get("items", [])}
        if unknown:
            raise EngineError("选择包含清单中不存在的作品。")
        selection = {"mode": "explicit", "order": "newest", "ids": [str(i) for i in ids], "refresh_quality": True}
        manifest = mod.apply_creator_selection_unlocked(manifest, manifest_path, root, selection)
    finally:
        os.close(descriptor)
    return {
        "ok": True,
        "job_id": job_id,
        "status": manifest.get("status"),
        "counts": manifest.get("counts"),
        "child_job_ids": manifest.get("child_job_ids"),
    }


def worker_once():
    """执行一轮内容 worker，下载排队中的子任务。返回 (worker_dict, processed_bool)。"""
    mod = get_mod()
    try:
        worker, processed = mod.content_worker_once(ARCHIVE_ROOT, max_parallel=DOWNLOAD_CONCURRENCY)
    except TypeError as exc:
        # Keep compatibility with an older installed skill if the project-local
        # bundled core is temporarily unavailable.
        if "max_parallel" not in str(exc):
            raise
        worker, processed = mod.content_worker_once(ARCHIVE_ROOT)
    return worker, processed


def job_status(job_id: str):
    """查询任务状态（会刷新 creator_batch 的子任务结果）。"""
    mod = get_mod()
    return mod.job_status(job_id, job_root(job_id))


def selection_disk_status(job_id: str, ids: list[str]):
    """核对选中作品的子任务状态和真实媒体文件，供 GUI 决定何时宣布完成。"""
    root = job_root(job_id)
    mod = get_mod()
    manifest = read_manifest(job_id) or {}
    wanted = {str(value) for value in ids}
    items = (manifest.get("inventory") or {}).get("items") or []
    ready, failed, pending, missing = [], [], [], []
    history = []
    quality_fallback = 0
    author_folder = manifest.get("author_folder")
    creator = manifest.get("creator") or {}
    author = (manifest.get("platform"), str(creator["id"])) if creator.get("id") and not creator.get("single_work") else None
    author_url = manifest.get("author_url") or creator.get("url")
    for item in items:
        item_id = str(item.get("id") or "")
        if item_id not in wanted:
            continue
        child_id = item.get("child_job_id")
        child_path = root / "jobs" / str(child_id or "") / "manifest.json"
        child = mod.read_json_if_valid(child_path) if child_path.is_file() else None
        if not child:
            retryable, code, message = _creator_item_failure(item, None)
            if retryable:
                failed.append(item_id)
                row = download_history.from_manifest(root, {
                    "platform": manifest.get("platform"), "title": item.get("title"),
                    "updated_at": manifest.get("updated_at"), "error": {"code": code, "message": message},
                }, status="failed", child_job=child_id, batch_job=job_id, work_id=item_id)
                if row:
                    history.append(row)
            else:
                pending.append(item_id)
        elif child.get("status") in {"failed", "unavailable", "completed"}:
            # A worker can report an error after the media has already been
            # flushed. Check the files first so the progress bar cannot remain
            # stuck at “failed” for a complete archive.
            if mod._child_output_files_exist(root, child, item):
                ready.append(item_id)
                if (author_url and author_folder and child.get("output_dir") == author_folder
                        and not uses_old_destination(job_id, manifest.get("platform"))):
                    write_author_address(root / author_folder, author_url)
                row = download_history.from_manifest(root, child, status="completed", child_job=child_id, batch_job=job_id, work_id=item_id)
                if child.get("non_highest") or child.get("image_quality") == "api_fallback":
                    quality_fallback += 1
            elif child.get("status") in {"failed", "unavailable"}:
                failed.append(item_id)
                row = download_history.from_manifest(root, child, child_job=child_id, batch_job=job_id, work_id=item_id)
            else:
                missing.append(item_id)
                row = None
            if row:
                history.append(row)
        else:
            pending.append(item_id)
            if child.get("status") in {"waiting_for_authorization", "waiting_for_reauthentication"}:
                row = download_history.from_manifest(root, child, status="failed", child_job=child_id, batch_job=job_id, work_id=item_id)
                if row:
                    history.append(row)
    return {
        "selected": len(wanted),
        "ready": len(ready),
        "failed": len(failed),
        "pending": len(pending),
        "missing": len(missing),
        "missing_ids": missing,
        "quality_fallback": quality_fallback,
        "author": author,
        "history": history,
    }


def write_author_address(folder, url):
    try:
        with (folder / "作者主页.txt").open("x", encoding="utf-8") as address:
            address.write(url + "\n")
    except FileExistsError:
        pass


def history_work(entry):
    mod = get_mod()
    root = Path(entry["archive_root"]) if entry.get("archive_root") else get_archive_root(entry["platform"])
    child_job = entry.get("child_job") or ""
    batch_job = entry.get("batch_job") or ""
    child = mod.read_json_if_valid(root / "jobs" / child_job / "manifest.json") if mod.JOB_ID_RE.fullmatch(child_job) else None
    batch = mod.read_json_if_valid(root / "jobs" / batch_job / "manifest.json") if mod.JOB_ID_RE.fullmatch(batch_job) else None
    child = child or {}
    batch = batch or {}
    work_id = entry["work_id"]
    rows = (batch.get("inventory") or {}).get("items") or []
    card = next((item for item in rows if str(item.get("id")) == work_id), {})
    item = {"id": work_id, "title": entry["title"], "url": child.get("source") or "",
            "media_kind": child.get("content_type"), **card,
            "guest_only": bool(card.get("guest_only") or child.get("guest_only")),
            "_author_folder": child.get("author_folder") or batch.get("author_folder"),
            "_direct_archive": child.get("direct_archive") or batch.get("direct_archive")}
    return root, child, batch, item


def history_local_path(entry, root, child, *, preview=False):
    if not entry["folder"]:
        return None
    folder = Path(entry["folder"])
    if not folder.is_dir():
        return None
    allowed_bases = ([root] if (child.get("direct_archive") or entry.get("platform") in AUTHOR_PLATFORMS)
                     else [root / "content", root])
    if not any(folder.resolve().is_relative_to(base.resolve()) for base in allowed_bases):
        return None
    roles = ("cover_image", "image") if preview else ("video", "image", "live_motion", "live_photo_motion", "audio")
    for role in roles:
        for output in child.get("outputs") or []:
            if output.get("role") == role:
                path = (root / output["path"]).resolve()
                if path.is_relative_to(folder.resolve()) and path.is_file():
                    return path
    if preview:
        return None
    return folder


def history_preview(entry):
    root, child, _, item = history_work(entry)
    cached = preview_cache_path(item, root=root)
    if cached:
        return cached
    clean_or_other = entry["platform"] != "douyin" or child.get("douyin_clean_revision") == get_mod().DOUYIN_CLEAN_REVISION
    local = history_local_path(entry, root, child, preview=True) if clean_or_other else None
    if local:
        return local
    _, local = resolve_item_cover(item, root=root, generate_missing=True)
    if local:
        return local
    if item["url"]:
        return _PREVIEW_POOL.submit(cache_remote_preview, item, root=root).result()
    return None


def history_retry_job(entry):
    root, child, batch, item = history_work(entry)
    mod = get_mod()
    batch_job = entry.get("batch_job")
    work_id = entry["work_id"]
    if batch and item.get("child_job_id") == entry.get("child_job"):
        JOB_ROOTS[batch_job] = root
        if failed_selection_details(batch_job, [work_id]):
            return batch_job
    if not child.get("source"):
        raise EngineError("找不到原任务清单。请用作品链接重新盘点后下载。")
    source = child["source"]
    platform = entry["platform"]
    creator = {"id": work_id, "name": entry["title"], "url": source,
               "single_work": True, "browser": child.get("xiaohongshu_browser")}
    item.update(url=source, child_job_id=entry["child_job"], result="failed")
    result = mod.save_creator_inventory(source, platform, root, creator, [item], [])
    JOB_ROOTS[result["job_id"]] = root
    return result["job_id"]


def history_retry_groups(entries):
    groups = {}
    unlinked = {}
    mod = get_mod()

    def add(entry, child, batch, item):
        platform = entry["platform"]
        creator = batch.get("creator") or {}
        if creator.get("single_work") and item.get("url") and child.get("source"):
            groups[(platform, entry["work_id"])] = {"legacy": entry, "ids": [entry["work_id"]]}
            return True
        if (batch.get("platform") != platform or not creator.get("id")
                or not creator.get("url") or not mod.is_creator_profile_url(creator["url"], platform)
                or not item.get("url")):
            return False
        group = groups.setdefault((platform, str(creator["id"])), {
            "platform": platform, "creator": creator, "profile_url": batch.get("author_url") or creator["url"],
            "ids": [], "items": [],
        })
        error = (child.get("error") or child.get("last_retry_error") or {}).get("code") or item.get("error_code")
        item = {key: value for key, value in item.items() if key not in {"_author_folder", "_direct_archive"}}
        item.update(child_job_id=None, result="failed", error_code=error or "download_failed")
        if child.get("media_ids"):
            item["media_ids"] = child["media_ids"]
        if not item.get("douyin_media") and child.get("douyin_media"):
            item["douyin_media"] = child["douyin_media"]
        group["ids"].append(entry["work_id"])
        group["items"].append(item)
        return True

    for entry in entries:
        platform = entry["platform"]
        if not uses_direct_author_layout(platform):
            groups[(platform, entry["work_id"])] = {"legacy": entry, "ids": [entry["work_id"]]}
            continue
        _, child, batch, item = history_work(entry)
        if not add(entry, child, batch, item):
            unlinked[(platform, entry["work_id"])] = (entry, child)

    if unlinked:
        roots = dict.fromkeys([*known_archive_roots(), *(Path(entry["archive_root"]) for entry in entries if entry.get("archive_root"))])
        for root in roots:
            if not unlinked:
                break
            for path in sorted((root / "jobs").glob("batch-*/manifest.json"), reverse=True):
                batch = mod.read_json_if_valid(path) or {}
                platform = batch.get("platform")
                for item in (batch.get("inventory") or {}).get("items") or []:
                    key = (platform, str(item.get("id")))
                    if key in unlinked and add(*unlinked[key], batch, item):
                        unlinked.pop(key)
                if not unlinked:
                    break
    return list(groups.values()), [entry for entry, _ in unlinked.values()]


def stage_history_retry(group):
    if group.get("legacy"):
        return history_retry_job(group["legacy"])
    mod = get_mod()
    platform, creator = group["platform"], group["creator"]
    root = get_archive_root(platform)
    prepare_archive_root(root, platform)
    result = mod.save_creator_inventory(creator["url"], platform, root, creator, group["items"], [])
    job_id = result["job_id"]
    JOB_ROOTS[job_id] = root
    bind_inventory_author(job_id, root, profile_url=group["profile_url"])
    return job_id


def retry_batch_entries(job_id, ids):
    manifest = read_manifest(job_id) or {}
    wanted = set(map(str, ids))
    return [{"platform": manifest["platform"], "work_id": str(item["id"]), "title": item.get("title", ""),
             "batch_job": job_id, "child_job": item.get("child_job_id"), "archive_root": str(job_root(job_id))}
            for item in (manifest.get("inventory") or {}).get("items", []) if str(item.get("id")) in wanted]


def _creator_item_failure(item: dict, child: dict | None) -> tuple[bool, str, str]:
    """Return whether a creator item is retryable and its last error detail."""
    parent_error = str(item.get("error_code") or "").strip()
    child_error = (child or {}).get("error") or (child or {}).get("last_retry_error") or {}
    child_status = str((child or {}).get("status") or "")
    parent_result = str(item.get("result") or "")
    retryable = (
        child_status in {"failed", "unavailable", "waiting_for_authorization", "waiting_for_reauthentication"}
        or parent_result in {"failed", "unavailable", "waiting_for_authorization", "waiting_for_reauthentication"}
        or bool(parent_error and parent_result not in {"completed", "processing"})
    )
    code = parent_error or str(child_error.get("code") or "") or "download_failed"
    message = str(child_error.get("message") or item.get("error_message") or "下载失败，未记录更具体的错误信息。")
    return retryable, code, message


def refresh_retry_sources(job_id, ids, root):
    mod = get_mod()
    manifest = mod.read_json_if_valid(root / "jobs" / job_id / "manifest.json") or {}
    creator = manifest.get("creator") or {}
    platform = manifest.get("platform")
    profile = str(creator.get("url") or "")
    browser = creator.get("browser") or "firefox"
    failures = failed_selection_details(job_id, ids)
    try:
        if platform == "xiaohongshu" and mod.is_creator_profile_url(profile, platform):
            detail_failures = {item["id"] for item in failures
                               if (item["error_code"].startswith("xiaohongshu_detail") or item["error_code"] == "reauthentication_required")
                               and re.fullmatch(r"[0-9a-fA-F]{24}", item["id"])}
            if detail_failures:
                _, fresh = mod._xiaohongshu_full_inventory(profile, browser, target_ids=detail_failures)
                return {item["id"]: item for item in fresh if item["id"] in detail_failures}
        if platform == "douyin" and any(item["error_code"] in {
            "douyin_media_failed", "douyin_empty_response", "reauthentication_required", "transparent_core_failed",
            "media_download_failed", "douyin_media_missing",
        } for item in failures):
            with COOKIE_LOCK:
                fresh = mod.extract_platform_browser_cookies("douyin", browser)
                mod.save_platform_cookies("douyin", fresh)
    except (mod.ArchiveError, OSError) as exc:
        logging.warning("retry source refresh unavailable platform=%s code=%s", platform,
                        getattr(exc, "code", type(exc).__name__))
    return {}


def failed_selection_details(job_id: str, ids: list[str] | None = None) -> list[dict]:
    """List failed creator works with enough metadata for a retry UI.

    The parent inventory is the source of truth for titles/covers.  Child
    manifests provide the most useful error code/message.  When ``ids`` is
    omitted all failed items in the frozen inventory are returned, so a retry
    of only part of a batch does not hide failures that were not selected yet.
    """
    root = job_root(job_id)
    mod = get_mod()
    manifest = read_manifest(job_id) or {}
    if manifest.get("kind") != "creator_batch":
        return []
    wanted = {str(value) for value in ids} if ids is not None else None
    details: list[dict] = []
    for item in (manifest.get("inventory") or {}).get("items") or []:
        item_id = str(item.get("id") or item.get("content_id") or "")
        if not item_id or (wanted is not None and item_id not in wanted):
            continue
        child_id = str(item.get("child_job_id") or "")
        child_path = root / "jobs" / child_id / "manifest.json"
        child = mod.read_json_if_valid(child_path) if child_id and child_path.is_file() else None
        if child and mod._child_output_files_exist(root, child, item):
            # The failed state is stale when every required media file exists.
            continue
        retryable, code, message = _creator_item_failure(item, child)
        if not retryable:
            continue
        media = item.get("douyin_media") or {}
        kind = str(media.get("kind") or item.get("media_kind") or "")
        details.append(
            {
                "id": item_id,
                "platform": manifest.get("platform"),
                "title": str(item.get("title") or "未命名作品"),
                "published_at": item.get("published_at"),
                "media_kind": kind,
                "child_job_id": child_id or None,
                "error_code": code,
                "error_message": message,
            }
        )
    return details


def retry_failed_selection(job_id: str, ids: list[str], *, allow_lower=False, guest_only=False) -> dict:
    """Requeue only selected failed creator children for a second attempt."""
    root = job_root(job_id)
    mod = get_mod()
    if not mod.JOB_ID_RE.fullmatch(job_id):
        raise EngineError("无效任务 ID")
    manifest_path = root / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        raise EngineError("任务清单不存在，请先盘点作者主页。")
    mod.invalidate_redbook_cache()
    original = mod.read_json_if_valid(manifest_path) or {}
    session_ids = {str(item.get("id")) for item in (original.get("inventory") or {}).get("items", [])
                   if not item.get("guest_only")}
    refresh_ids = [work_id for work_id in ids if str(work_id) in session_ids]
    refreshed_items = refresh_retry_sources(job_id, refresh_ids, root) if refresh_ids and not guest_only else {}
    app_runtime.operation_timeout()
    descriptor = mod.acquire_creator_batch_lock(manifest_path)
    if descriptor is None:
        raise EngineError("该任务正在更新，请稍后再试。")
    try:
        bind_inventory_author(job_id, root)
        manifest = mod.read_json_if_valid(manifest_path) or {}
        if manifest.get("kind") != "creator_batch":
            raise EngineError("只有作者主页批量任务支持失败重试。")
        requested = []
        seen = set()
        for value in ids or []:
            item_id = str(value)
            if item_id and item_id not in seen:
                requested.append(item_id)
                seen.add(item_id)
        if not requested:
            raise EngineError("请至少选择一个失败作品。")
        wanted = set(requested)
        retried: list[str] = []
        ignored: list[str] = []
        inventory = manifest.get("inventory") or {}
        items = inventory.get("items") or []
        for item in items:
            item_id = str(item.get("id") or item.get("content_id") or "")
            if item_id not in wanted:
                continue
            child_id = str(item.get("child_job_id") or "")
            child_path = root / "jobs" / child_id / "manifest.json" if child_id else None
            child = mod.read_json_if_valid(child_path) if child_path and child_path.is_file() else None
            if child and mod._child_output_files_exist(root, child, item):
                ignored.append(item_id)
                continue
            retryable, _code, _message = _creator_item_failure(item, child)
            if not retryable:
                ignored.append(item_id)
                continue
            original_url = item.get("url")
            refreshed = refreshed_items.get(item_id)
            if refreshed:
                for field in ("url", "preview_urls", "title", "media_kind"):
                    if refreshed.get(field):
                        item[field] = refreshed[field]

            # Normally a failed item already has its frozen child manifest.
            # If an old manifest lost that link, recreate the child from the
            # frozen URL and restore its media metadata before queueing it.
            if child is None:
                source_url = str(item.get("url") or item.get("canonical_url") or "")
                if not source_url:
                    ignored.append(item_id)
                    continue
                child_info = (mod.submit_content(source_url, root, fresh=True) if item.get("media_ids") or allow_lower
                              else mod.submit_content(source_url, root))
                child_id = str(child_info.get("job_id") or "")
                if not child_id:
                    ignored.append(item_id)
                    continue
                item["child_job_id"] = child_id
                child_path = root / "jobs" / child_id / "manifest.json"
                child = mod.read_json_if_valid(child_path) or {}
                if manifest.get("platform") == "douyin":
                    child["douyin_media"] = dict(item.get("douyin_media") or {"content_id": item_id, "kind": "video"})
                elif manifest.get("platform") == "weibo" and item.get("weibo_media"):
                    child["weibo_media"] = dict(item["weibo_media"])
                    child["content_id"] = item_id

            if manifest.get("platform") == "xiaohongshu":
                creator = manifest.get("creator") or {}
                if mod._redbook_backend(str(creator.get("url") or "")) == "rednote":
                    child["xiaohongshu_backend"] = "rednote"
                if creator.get("browser") in {"chrome", "firefox"}:
                    child["xiaohongshu_browser"] = creator["browser"]
                if refreshed:
                    previous = child.get("source")
                    if previous == refreshed["url"]:
                        previous = original_url
                    if previous and previous != refreshed["url"]:
                        child["previous_xiaohongshu_source"] = previous
                    child["source"] = refreshed["url"]
            if manifest.get("author_folder"):
                child["author_folder"] = manifest["author_folder"]
            if manifest.get("direct_archive"):
                child["direct_archive"] = True
            if item.get("media_ids"):
                child["media_ids"] = item["media_ids"]
            if guest_only or item.get("guest_only"):
                child["guest_only"] = True
                child.pop("previous_xiaohongshu_source", None)
                if manifest.get("platform") == "douyin" and not item.get("guest_only"):
                    media_kind = (item.get("douyin_media") or {}).get("kind") or item.get("media_kind")
                    child["douyin_media"] = {"content_id": item_id, "kind": media_kind or "video"}
            elif child.pop("guest_only", False) and manifest.get("platform") == "douyin":
                child["douyin_media"] = dict(item.get("douyin_media") or {"content_id": item_id, "kind": "video"})
            if allow_lower:
                child["allow_lower_quality"] = True
            else:
                child.pop("allow_lower_quality", None)
            child.update({"status": "queued", "updated_at": mod.utc_now(), "auto_attempts": 0})
            for field in ("completed_at", "failed_stage", "error", "next_action", "last_retry_error", "retry_at"):
                child.pop(field, None)
            mod.write_json(child_path, child)
            item.update({"result": "processing", "error_code": None})
            item.pop("error_message", None)
            if child_id and child_id not in (manifest.get("child_job_ids") or []):
                manifest.setdefault("child_job_ids", []).append(child_id)
            retried.append(item_id)

        if not retried:
            raise EngineError("所选作品中没有可重试的失败任务。")
        manifest["selection"] = {
            "mode": "retry_failed",
            "order": "newest",
            "ids": retried,
            "selected": len(retried),
        }
        manifest.update(
            {
                "status": "processing",
                "updated_at": mod.utc_now(),
                "counts": {
                    "selected": len(retried),
                    "processing": len(retried),
                    "waiting_for_authorization": 0,
                    "waiting_for_reauthentication": 0,
                    "completed": 0,
                    "failed": 0,
                },
            }
        )
        for field in ("completed_at", "error", "next_action"):
            manifest.pop(field, None)
        mod.write_json(manifest_path, manifest)
        return {
            "ok": True,
            "job_id": job_id,
            "status": manifest.get("status"),
            "counts": manifest.get("counts"),
            "retried": retried,
            "ignored": ignored,
        }
    finally:
        os.close(descriptor)


def import_browser_cookies(platform: str, browser: str):
    """导入浏览器 Cookie。platform: douyin/xiaohongshu/weibo；browser: chrome/edge/firefox。"""
    if platform not in PLATFORMS:
        raise EngineError("当前版本仅支持抖音、小红书与微博。")
    mod = get_mod()
    with COOKIE_LOCK:
        result = mod.import_browser_cookies(platform, browser, get_archive_root(platform))
    if result.get("ok") and platform == "xiaohongshu":
        mod.invalidate_redbook_cache()
    return result


def read_manifest(job_id: str):
    """直接读取任务 manifest 原始内容。"""
    manifest_path = job_root(job_id) / "jobs" / job_id / "manifest.json"
    if not manifest_path.is_file():
        return None
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def list_content(platform: str = "douyin"):
    """列出已下载内容的目录（用于展示已下载结果）。"""
    content_dir = get_archive_root(platform) / "content"
    platform_dir = content_dir / _platform_display_name(platform)
    if not platform_dir.is_dir():
        return []
    entries = []
    for p in sorted(platform_dir.iterdir()):
        if p.is_dir():
            files = [f.name for f in p.iterdir() if f.is_file()]
            entries.append({"name": p.name, "path": str(p), "files": files})
    return entries


# ---------- 封面缩略图 ----------

# 图集内常见图片扩展名，按文件名排序后第一张就是封面候选
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm"}
_CONTENT_DIRS = {}
_CONTENT_DIRS_LOCK = threading.Lock()
_FLAT_MEDIA = {}
_FLAT_MEDIA_NAME = re.compile(r"--([A-Za-z0-9]+)(?:(--封面)|(--图\d+))?\.[^.]+$")


def _platform_display_name(platform: str) -> str:
    return {"douyin": "抖音", "xiaohongshu": "小红书", "weibo": "微博"}.get(platform, platform)


def _content_dir_for_item(item: dict, *, root=None) -> Path | None:
    """根据 item（带 title 和 id），定位 content/<platform>/<title>--<id> 目录。"""
    pid = (item.get("douyin_media") or {}).get("platform") or _platform_for_item(item)
    if pid not in {"douyin", "xiaohongshu", "weibo"}:
        return None
    title = (item.get("title") or "").strip()
    iid = str(item.get("id") or "").strip()
    if not iid:
        return None
    display = _platform_display_name(pid)
    base = (root or ARCHIVE_ROOT) / "content" / display
    if not base.is_dir():
        return None
    # 精确匹配：{title}--{id}，可能在子目录里
    direct = base / f"{get_mod().safe_content_title(title, '未命名作品')}--{iid}"
    if direct.is_dir():
        return direct
    # Some titles change or have been sanitized differently by earlier builds.
    # Index the destination once for an inventory and invalidate on directory changes.
    try:
        stamp = base.stat().st_mtime_ns
        with _CONTENT_DIRS_LOCK:
            cached = _CONTENT_DIRS.get(base)
        if not cached or cached[0] != stamp:
            by_id = {p.name.rsplit("--", 1)[1]: p
                     for p in base.iterdir() if "--" in p.name and p.is_dir()}
            cached = (stamp, by_id)
            with _CONTENT_DIRS_LOCK:
                _CONTENT_DIRS[base] = cached
        return cached[1].get(iid)
    except OSError:
        return None


def _platform_for_item(item: dict) -> str:
    url = item.get("url") or ""
    if "douyin.com" in url:
        return "douyin"
    if "xiaohongshu" in url or "xhslink" in url or "rednote.com" in url:
        return "xiaohongshu"
    if "weibo.com" in url or "weibo.cn" in url:
        return "weibo"
    return "douyin"


def _pick_image_cover(item_dir: Path) -> Path | None:
    """图集封面：从 图片/ 子目录里挑第一张图（按文件名排序）。"""
    # 常见命名：图片/xxx.jpg
    for sub in ["图片", "图片集", "images", "image", "imgs", "gallery"]:
        sub_dir = item_dir / sub
        if sub_dir.is_dir():
            imgs = sorted(
                (f for f in sub_dir.iterdir()
                 if f.is_file() and f.suffix.lower() in _IMAGE_EXTS),
                key=lambda p: p.name,
            )
            if imgs:
                return imgs[0]
    # 兜底：直接在目录里找图片
    imgs = sorted(
        (f for f in item_dir.iterdir()
         if f.is_file() and f.suffix.lower() in _IMAGE_EXTS and not f.name.startswith("cover_")),
        key=lambda p: p.name,
    )
    return imgs[0] if imgs else None


def _pick_video_cover(item_dir: Path) -> Path | None:
    """视频封面：查找视频文件。"""
    for f in sorted(item_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in _VIDEO_EXTS and "cover" not in f.name.lower():
            return f
    return None


def _pick_live_photo_image(item_dir: Path) -> Path | None:
    """动图封面：通常是首张静态图（位于 图片/ 子目录或目录根）。"""
    # 抖音 live_photo 下载后：图片存到 <dir>/图片/001.jpg，动图视频存到 <dir>/Live图/001.mp4
    # 优先取 图片/ 子目录的第一张图
    for sub in ["图片", "图片集", "images", "image", "imgs", "gallery"]:
        sub_dir = item_dir / sub
        if sub_dir.is_dir():
            imgs = sorted(
                (f for f in sub_dir.iterdir()
                 if f.is_file() and f.suffix.lower() in _IMAGE_EXTS),
                key=lambda p: p.name,
            )
            if imgs:
                return imgs[0]
    # 兜底：在根目录找图片
    for f in sorted(item_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in _IMAGE_EXTS and not f.name.startswith("cover_"):
            return f
    return None


def _ensure_video_cover(video_path: Path, cover_path: Path, ffmpeg: str | None) -> bool:
    """用 ffmpeg 从视频里抽第一帧作为 cover.jpg。如已存在则跳过。"""
    cover_path = cover_path.with_suffix(".jpg") if cover_path.suffix.lower() != ".jpg" else cover_path
    if cover_path.is_file():
        return True
    if not ffmpeg or not Path(ffmpeg).is_file():
        return False
    try:
        # -ss 00:00:00.5 略偏开头避开黑场；-frames:v 1 取一张
        proc = get_mod().run_background_process(
            [ffmpeg, "-y", "-ss", "00:00:00.4", "-i", str(video_path),
             "-frames:v", "1", "-q:v", "3", str(cover_path)],
            capture_output=True, timeout=30,
        )
        return cover_path.is_file() and cover_path.stat().st_size > 100
    except Exception:
        return False


def _find_ffmpeg() -> str | None:
    """查找 ffmpeg 可执行文件路径。"""
    bundled_runtime.ensure("media")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    # 兜底：WinGet 安装位置
    for c in [
        Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe",
    ]:
        if c.is_file():
            return str(c)
    return None


def _cover_for_video(item_dir: Path) -> Path | None:
    """视频封面：在视频文件旁边找/生成 cover.jpg。优先返回已有 jpg。"""
    # 已存在 cover.jpg 直接用
    direct = item_dir / "cover.jpg"
    if direct.is_file():
        return direct
    # 抽帧
    v = _pick_video_cover(item_dir)
    if v is None:
        return None
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None
    if _ensure_video_cover(v, direct, ffmpeg):
        return direct if direct.is_file() else None
    return None


# ---------- 在线封面抓取（用于未下载作品的预览） ----------

_PREVIEW_OPENER_LOCAL = threading.local()


def _http_opener_with_douyin_cookie():
    """构造带抖音 cookie 的 HTTP opener。用于调 aweme_detail API。
    优先用持久化 cookie 文件；如不存在则直接从浏览器实时拉取（Windows/Firefox/Chrome/Edge）。"""
    import urllib.request
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    # 1) 优先尝试持久化 cookie 文件
    cj_path = None
    try:
        mod = get_mod()
        cj_path = mod.platform_cookie_jar("douyin")
    except Exception:
        pass
    try:
        cookie_key = (
            str(cj_path),
            cj_path.stat().st_mtime_ns,
            cj_path.stat().st_size,
        ) if cj_path and Path(cj_path).is_file() else ("browser",)
    except OSError:
        cookie_key = ("browser",)
    cached = getattr(_PREVIEW_OPENER_LOCAL, "value", None)
    if cached and cached[0] == cookie_key:
        return cached[1]
    if cj_path and Path(cj_path).is_file():
        try:
            mjar = http.cookiejar.MozillaCookieJar(str(cj_path))
            mjar.load(ignore_discard=True, ignore_expires=True)
            for c in mjar:
                jar.set_cookie(c)
        except Exception:
            pass
    else:
        # 2) 兜底：从浏览器实时提取（首次运行可能还没有 cookies/douyin.txt）
        try:
            vendor_core = SKILL_SCRIPT.parents[2] / "vendor" / "transparent-core"
            if vendor_core.is_dir() and str(vendor_core) not in sys.path:
                sys.path.insert(0, str(vendor_core))
            from yt_dlp.cookies import extract_cookies_from_browser  # type: ignore
            for browser in ("firefox", "chrome", "edge"):
                try:
                    bj = extract_cookies_from_browser(browser)
                    for c in bj:
                        jar.set_cookie(c)
                    break  # 找到任一浏览器的 cookie 就用
                except Exception:
                    continue
        except Exception:
            pass
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent",
         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        ("Referer", "https://www.douyin.com/"),
    ]
    _PREVIEW_OPENER_LOCAL.value = (cookie_key, opener)
    return opener


def _http_opener_with_xiaohongshu_cookie():
    """Build a small authenticated opener for XHS CDN previews.

    Full-history inventory uses the pinned redbook process and does not copy
    browser credentials into the archive.  Preview URLs returned by its
    ``user_posted`` API are normally public CDN URLs, but an imported XHS jar
    is attached when available so deployments that require a session still
    get a chance to load the card image.
    """
    import http.cookiejar
    import urllib.request

    jar = http.cookiejar.CookieJar()
    try:
        mod = get_mod()
        cj_path = mod.platform_cookie_jar("xiaohongshu")
        if cj_path and Path(cj_path).is_file():
            mjar = http.cookiejar.MozillaCookieJar(str(cj_path))
            mjar.load(ignore_discard=True, ignore_expires=True)
            for cookie in mjar:
                jar.set_cookie(cookie)
    except Exception:
        pass
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"),
        ("Referer", "https://www.xiaohongshu.com/"),
    ]
    return opener


def _http_opener_with_weibo_cookie():
    import urllib.request
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    try:
        mod = get_mod()
        cj_path = mod.platform_cookie_jar("weibo")
        if cj_path and Path(cj_path).is_file():
            mjar = http.cookiejar.MozillaCookieJar(str(cj_path))
            mjar.load(ignore_discard=True, ignore_expires=True)
            for cookie in mjar:
                jar.set_cookie(cookie)
    except Exception:
        pass
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"),
        ("Referer", "https://weibo.com/"),
    ]
    return opener


def fetch_remote_preview(item: dict) -> tuple[bytes, str] | None:
    """从主页/详情 API 取得一张预览图，供本地缓存或 HTTP 回退使用。"""
    iid = str(item.get("id") or "").strip()
    platform = _platform_for_item(item)
    valid_id = iid.isdigit() and len(iid) >= 14
    if platform == "xiaohongshu":
        valid_id = bool(re.fullmatch(r"[0-9a-fA-F]{24}", iid))
    elif platform == "weibo":
        valid_id = bool(re.fullmatch(r"[0-9A-Za-z]{6,32}", iid))
    if not valid_id:
        return None
    mod = get_mod()
    if item.get("guest_only"):
        opener = build_opener()
    elif platform == "xiaohongshu":
        opener = _http_opener_with_xiaohongshu_cookie()
    elif platform == "weibo":
        opener = _http_opener_with_weibo_cookie()
    else:
        opener = _http_opener_with_douyin_cookie()
    candidates = list(item.get("preview_urls") or [])
    if platform == "douyin" and (item.get("douyin_media") or {}).get("clean_source_revision") != mod.DOUYIN_CLEAN_REVISION:
        candidates = []

    def try_candidates(values):
        if platform == "xiaohongshu":
            safe_values = []
            suffixes = tuple(getattr(mod, "XHS_MEDIA_HOST_SUFFIXES", (".xhscdn.com", ".xhsimg.com")))
            for candidate in values:
                try:
                    parsed = urlsplit(str(candidate))
                    host = (parsed.hostname or "").lower().rstrip(".")
                    if parsed.scheme not in {"http", "https"} or not host:
                        continue
                    if not (host in ({"xhscdn.com", "xhsimg.com"} | mod.XHS_MEDIA_EXACT_HOSTS) or any(host.endswith(s) for s in suffixes)):
                        continue
                    if parsed.scheme == "http":
                        candidate = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
                    safe_values.append(str(candidate))
                except Exception:
                    continue
        elif platform == "weibo":
            safe_values = [u for u in (mod._weibo_safe_media_url(c) for c in values) if u]
        else:
            safe_values = mod.douyin_clean_media_urls(values)
        for url in safe_values[:6]:
            try:
                with opener.open(url, timeout=5) as response:
                    if platform == "douyin":
                        mod.require_clean_douyin_url(response.geturl())
                    content_type = str(response.headers.get_content_type() or "").lower()
                    body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024 or len(body) < 100:
                    continue
                # CDN responses occasionally omit Content-Type or return an
                # image type for a challenge page. Validate the magic bytes so
                # the cache never stores HTML as a preview image.
                if body.startswith(b"\xff\xd8\xff"):
                    content_type = "image/jpeg"
                elif body.startswith(b"\x89PNG\r\n\x1a\n"):
                    content_type = "image/png"
                elif body.startswith((b"GIF87a", b"GIF89a")):
                    content_type = "image/gif"
                elif body[:4] == b"RIFF" and body[8:12] == b"WEBP":
                    content_type = "image/webp"
                elif body[4:12] == b"ftypavif" or body[4:12] == b"ftypavis":
                    content_type = "image/avif"
                else:
                    continue
                return body, content_type
            except Exception:
                continue
        return None

    result = try_candidates(candidates)
    if result is not None:
        return result

    # Homepage cover URLs can be short-lived or point to a CDN edge that
    # rejects the server's first request. Always refresh detail once after the
    # frozen candidates fail, rather than treating their presence as proof that
    # no newer preview is available.
    if platform in {"xiaohongshu", "weibo"} or item.get("guest_only"):
        # The redbook / weibo inventory already supplied all currently exposed card
        # URLs.  Unlike Douyin there is no need to hit Douyin's detail endpoint here.
        return None
    try:
        detail = mod.fetch_douyin_aweme_detail({"content_id": iid, "source": f"https://www.douyin.com/video/{iid}"})
        return try_candidates(mod.douyin_aweme_preview_urls(detail))
    except Exception:
        pass
    return None


_AVATAR_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
_AVATAR_EXT_TO_MIME = {ext: mime for mime, ext in _AVATAR_MIME_TO_EXT.items()}


def _detect_image_mime(body: bytes) -> str | None:
    if len(body) < 32 or len(body) > 4 * 1024 * 1024:
        return None
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body[4:12] in {b"ftypavif", b"ftypavis"}:
        return "image/avif"
    return None


def _avatar_host_policy(platform: str):
    mod = get_mod()
    if platform == "weibo":
        return mod.WEIBO_MEDIA_EXACT_HOSTS, mod.WEIBO_MEDIA_HOST_SUFFIXES, "https://weibo.com/"
    if platform == "xiaohongshu":
        return mod.XHS_MEDIA_EXACT_HOSTS | {"xhscdn.com", "xhsimg.com"}, mod.XHS_MEDIA_HOST_SUFFIXES, "https://www.xiaohongshu.com/"
    if platform == "douyin":
        return mod.DOUYIN_PLAY_HOSTS, mod.DOUYIN_MEDIA_HOST_SUFFIXES, "https://www.douyin.com/"
    return set(), (), ""


def fetch_author_avatar(platform: str, url: str = "", key: str = "") -> tuple[bytes, str] | None:
    """Proxy and locally cache author avatars with platform-appropriate Referer headers."""
    if platform not in PLATFORMS:
        return None
    raw_url = str(url or "").strip()
    cache_dir = app_runtime.DATA_DIR / "avatars"
    cache_id = hashlib.sha256(f"{platform}:{key or raw_url}".encode("utf-8")).hexdigest()[:24]
    url_hash = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()[:16] if raw_url else ""
    meta_path = cache_dir / f"{cache_id}.url"

    def read_cached() -> tuple[bytes, str] | None:
        for ext, mime in _AVATAR_EXT_TO_MIME.items():
            candidate = cache_dir / f"{cache_id}{ext}"
            try:
                if candidate.is_file() and candidate.stat().st_size >= 32:
                    return candidate.read_bytes(), mime
            except OSError:
                continue
        return None

    cached = read_cached()
    if cached is not None:
        try:
            saved_hash = meta_path.read_text(encoding="utf-8").strip() if meta_path.is_file() else ""
        except OSError:
            saved_hash = ""
        if not url_hash or saved_hash == url_hash:
            return cached

    if not raw_url:
        return cached

    mod = get_mod()
    exact, suffixes, referer = _avatar_host_policy(platform)
    try:
        parsed = urlsplit(raw_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host or not mod.host_allowed(host, exact, suffixes):
            return cached
        safe_url = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    except ValueError:
        return cached

    opener = build_opener()
    opener.addheaders = [
        ("User-Agent", mod.DOUYIN_USER_AGENT),
        ("Referer", referer),
        ("Accept", "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"),
    ]
    try:
        with opener.open(safe_url, timeout=8) as response:
            final_host = (urlsplit(response.geturl()).hostname or "").lower().rstrip(".")
            if not mod.host_allowed(final_host, exact, suffixes):
                return cached
            body = response.read(4 * 1024 * 1024 + 1)
    except Exception:
        return cached

    mime = _detect_image_mime(body)
    if not mime:
        return cached

    ext = _AVATAR_MIME_TO_EXT[mime]
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for old_ext in _AVATAR_EXT_TO_MIME:
            if old_ext != ext:
                (cache_dir / f"{cache_id}{old_ext}").unlink(missing_ok=True)
        target = cache_dir / f"{cache_id}{ext}"
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{cache_id}.", suffix=".tmp", dir=cache_dir, delete=False) as handle:
            tmp_path = Path(handle.name)
            handle.write(body)
            handle.flush()
        os.replace(tmp_path, target)
        meta_path.write_text(url_hash, encoding="utf-8")
    except OSError:
        pass
    return body, mime


_PREVIEW_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif")
_PREVIEW_STATE_LOCK = threading.Lock()
_PREVIEW_STATE: dict[tuple, dict] = {}
_PREVIEW_ACTIVE = set()
_PREVIEW_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="preview")


def _preview_directory(item, root):
    directory = (root or ARCHIVE_ROOT) / ".preview"
    if _platform_for_item(item) == "douyin":
        directory /= "douyin-clean-v1"
    return directory


def preview_cache_path(item: dict, *, root=None) -> Path | None:
    """返回已有的本地预览缓存路径；不会触发网络请求。"""
    iid = str(item.get("id") or "").strip()
    if not iid or not (iid.isdigit() or re.fullmatch(r"[0-9a-fA-F]{24}", iid)):
        return None
    cache_dir = _preview_directory(item, root)
    for extension in _PREVIEW_EXTENSIONS:
        candidate = cache_dir / f"{iid}{extension}"
        try:
            if candidate.is_file() and candidate.stat().st_size > 100:
                return candidate
        except OSError:
            continue
    return None


def cache_remote_preview(item: dict, fetched: tuple[bytes, str] | None = None, *, root=None) -> Path | None:
    """将一张远程预览图预下载到 ARCHIVE_ROOT/.preview 并返回路径。"""
    cached = preview_cache_path(item, root=root)
    if cached is not None:
        with _PREVIEW_STATE_LOCK:
            _PREVIEW_STATE[(root or ARCHIVE_ROOT, str(item.get("id") or ""))] = {"status": "ready", "attempts": 0}
        return cached
    result = fetched if fetched is not None else fetch_remote_preview(item)
    if result is None:
        iid = str(item.get("id") or "")
        with _PREVIEW_STATE_LOCK:
            previous = _PREVIEW_STATE.get((root or ARCHIVE_ROOT, iid)) or {}
            attempts = int(previous.get("attempts") or 0) + 1
            _PREVIEW_STATE[(root or ARCHIVE_ROOT, iid)] = {
                "status": "failed" if attempts >= 3 else "pending",
                "attempts": attempts,
            }
        return None
    body, content_type = result
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        # Keep AVIF bytes labelled as AVIF.  Saving them as .webp makes the
        # HTTP layer advertise the wrong media type and causes browsers to
        # reject an otherwise valid preview.
        "image/avif": ".avif",
        "image/gif": ".gif",
    }.get(content_type, ".jpg")
    iid = str(item.get("id") or "").strip()
    cache_dir = _preview_directory(item, root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{iid}{extension}"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{iid}.", suffix=".tmp", dir=cache_dir, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
        os.replace(temporary, target)
        if target.is_file() and target.stat().st_size > 100:
            with _PREVIEW_STATE_LOCK:
                _PREVIEW_STATE[(root or ARCHIVE_ROOT, iid)] = {"status": "ready", "attempts": 0}
            return target
        with _PREVIEW_STATE_LOCK:
            previous = _PREVIEW_STATE.get((root or ARCHIVE_ROOT, iid)) or {}
            attempts = int(previous.get("attempts") or 0) + 1
            _PREVIEW_STATE[(root or ARCHIVE_ROOT, iid)] = {"status": "failed" if attempts >= 3 else "pending", "attempts": attempts}
        return None
    except (OSError, ValueError):
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        with _PREVIEW_STATE_LOCK:
            previous = _PREVIEW_STATE.get((root or ARCHIVE_ROOT, iid)) or {}
            attempts = int(previous.get("attempts") or 0) + 1
            _PREVIEW_STATE[(root or ARCHIVE_ROOT, iid)] = {"status": "failed" if attempts >= 3 else "pending", "attempts": attempts}
        return None


def preview_status(items: list[dict], *, root=None) -> dict[str, list[str]]:
    """Return ready/pending/failed preview IDs for the current inventory."""
    ready, pending, failed = [], [], []
    with _PREVIEW_STATE_LOCK:
        state = dict(_PREVIEW_STATE)
    for item in items:
        iid = str(item.get("id") or "")
        if preview_cache_path(item, root=root) is not None or resolve_item_cover(item, root=root)[1] is not None:
            ready.append(iid)
            continue
        if str((state.get((root or ARCHIVE_ROOT, iid)) or {}).get("status") or "") == "failed":
            failed.append(iid)
        else:
            pending.append(iid)
    return {"ready": ready, "pending": pending, "failed": failed}


def _has_already_done(item: dict, *, root=None) -> bool:
    """该 item 是否之前已下载过（用于标记「已下载」状态）。"""
    root = root or ARCHIVE_ROOT
    child_id = str(item.get("child_job_id") or "")
    if child_id:
        mod = get_mod()
        if mod.JOB_ID_RE.fullmatch(child_id):
            child = mod.read_json_if_valid(root / "jobs" / child_id / "manifest.json") or {}
            if child:
                return child.get("status") == "completed" and mod._child_output_files_exist(root, child, item)
    folder = _content_dir_for_item(item, root=root)
    if folder is None:
        return False
    kind = (item.get("douyin_media") or {}).get("kind") or item.get("media_kind")
    if kind == "video":
        return _pick_video_cover(folder) is not None
    if kind == "live_photo":
        return _pick_live_photo_image(folder) is not None
    return _pick_image_cover(folder) is not None or _pick_image_cover(folder / "配图") is not None


def _flat_cover(item, root):
    relative = item.get("_author_folder")
    if not relative:
        return None
    root = root or ARCHIVE_ROOT
    platform_dir = root.resolve() if item.get("_direct_archive") else (root / "content" / _platform_display_name(_platform_for_item(item))).resolve()
    folder = (root / relative).resolve()
    if folder.parent != platform_dir:
        return None
    try:
        stamp = folder.stat().st_mtime_ns
        with _CONTENT_DIRS_LOCK:
            cached = _FLAT_MEDIA.get(folder)
            if not cached or cached[0] != stamp:
                covers = {}
                for path in sorted(folder.iterdir()):
                    if path.suffix.lower() not in _IMAGE_EXTS or not path.is_file() or path.is_symlink():
                        continue
                    match = _FLAT_MEDIA_NAME.search(path.name)
                    if match and (match[2] or match[3]):
                        covers.setdefault(match[1], ("video_frame" if match[2] else "image", path))
                cached = (stamp, covers)
                _FLAT_MEDIA[folder] = cached
        return cached[1].get(str(item.get("id") or ""))
    except (OSError, ValueError):
        return None


def resolve_item_cover(item: dict, *, generate_missing: bool = False, fetch_remote: bool = False, root=None):
    """
    解析某个 item 的封面缩略图。
    返回 (kind, abs_path)：
        - ("image", <path>)            // 已有本地图片（图集/动图首张）
        - ("video_frame", <path>)      // 已下载视频的 ffmpeg 抽帧图
        - ("remote_cover", <path>)     // 在线抓取的远程封面（未下载作品）
        - ("video_placeholder", None)  // 视频已下载但抽帧失败
        - ("placeholder", None)        // 无封面
    """
    if _platform_for_item(item) == "douyin":
        root = root or ARCHIVE_ROOT
        cached = preview_cache_path(item, root=root)
        if cached is not None:
            return "remote_cover", cached
        mod = get_mod()
        job_id = str(item.get("child_job_id") or "")
        if mod.JOB_ID_RE.fullmatch(job_id):
            child = mod.read_json_if_valid(root / "jobs" / job_id / "manifest.json") or {}
            if child.get("douyin_clean_revision") == mod.DOUYIN_CLEAN_REVISION:
                for row in child.get("outputs") or []:
                    if row.get("role") in {"cover_image", "image"}:
                        path = (root / row["path"]).resolve()
                        if path.is_relative_to(root.resolve()) and path.is_file():
                            return ("video_frame" if row["role"] == "cover_image" else "image"), path
        if fetch_remote and (cached := cache_remote_preview(item, root=root)):
            return "remote_cover", cached
        return "placeholder", None
    flat_cover = _flat_cover(item, root)
    if flat_cover:
        return flat_cover
    item_dir = _content_dir_for_item(item, root=root)

    # 已下载：用本地资源
    if item_dir is not None:
        kind = ((item.get("douyin_media") or {}).get("kind")) or item.get("media_kind")
        if kind == "image_gallery":
            p = _pick_image_cover(item_dir)
            if p:
                return ("image", p)
        elif kind == "video":
            v = _pick_video_cover(item_dir)
            if v is not None:
                cover = item_dir / "cover.jpg"
                if cover.is_file():
                    return ("video_frame", cover)
                if generate_missing and _ensure_video_cover(v, cover, _find_ffmpeg()):
                    return ("video_frame", cover)
                return ("video_placeholder", None)
        elif kind == "live_photo":
            p = _pick_live_photo_image(item_dir)
            if p:
                return ("image", p)
        else:
            p = _pick_image_cover(item_dir)
            if p:
                return ("image", p)

    if fetch_remote:
        cached = cache_remote_preview(item, root=root)
        if cached is not None:
            return ("remote_cover", cached)
    return ("placeholder", None)


def background_generate_covers(items: list[dict], *, fetch_remote=False, root=None):
    """Prefetch covers through a single bounded pool without guarding network I/O."""
    preview_root = root or ARCHIVE_ROOT
    for item in items:
        key = (preview_root, str(item.get("id") or ""))
        with _PREVIEW_STATE_LOCK:
            if key in _PREVIEW_ACTIVE:
                continue
            _PREVIEW_ACTIVE.add(key)
            _PREVIEW_STATE[key] = {"status": "pending", "attempts": 0}
        _PREVIEW_POOL.submit(_prefetch_cover, item, preview_root, fetch_remote, key)


def _prefetch_cover(item, root, fetch_remote, key):
    try:
        if preview_cache_path(item, root=root) or resolve_item_cover(item, root=root, generate_missing=True)[1]:
            return
        if fetch_remote:
            for attempt in range(2):
                if cache_remote_preview(item, root=root):
                    return
                if attempt == 0:
                    time.sleep(0.15)
    except (OSError, ValueError):
        pass
    finally:
        with _PREVIEW_STATE_LOCK:
            _PREVIEW_ACTIVE.discard(key)
            if not preview_cache_path(item, root=root):
                _PREVIEW_STATE[key] = {"status": "failed"}


def _normalize_published_at(item: dict):
    """将 published_at 归一化为 YYYY-MM-DD 字符串。前端假设 string。"""
    val = item.get("published_at")
    if val is None or val == "":
        return
    try:
        ts = int(val)
        # 抖音可能返回秒或毫秒；> 1e12 就当毫秒
        if ts > 1_000_000_000_000:
            ts = ts / 1000.0
        dt = __import__("datetime").datetime.fromtimestamp(ts)
        item["published_at"] = dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError, OverflowError):
        # 解析失败就保留原值（避免吞错误）
        item["published_at"] = str(val)


def enrich_items_with_cover(items: list[dict], *, generate_missing: bool = False, fetch_remote: bool = False, root=None):
    """为 items 列表每个 item 附加 _cover 字段（封面路径或 placeholder）。"""
    for it in items:
        _normalize_published_at(it)
        kind, abs_path = resolve_item_cover(
            it, generate_missing=generate_missing, fetch_remote=fetch_remote, root=root
        )
        it["_cover"] = kind
        it["_cover_path"] = str(abs_path) if abs_path else None
        it["_downloaded"] = _has_already_done(it, root=root)
    return items


if __name__ == "__main__":
    # 简单自检
    print(json.dumps(self_check(), ensure_ascii=False, indent=2))


_BATCH_FUTURES = {}
_TRANSFER_LOCK = threading.Lock()
_TRANSFER_BYTES = {}


def _process_selected_child(path, root, job_id, budget=None):
    mod = get_mod()
    def report(size):
        if budget is not None:
            budget.progress()
        with _TRANSFER_LOCK:
            _TRANSFER_BYTES[job_id] = _TRANSFER_BYTES.get(job_id, 0) + size

    mod.TRANSFER_OBSERVER.callback = report
    try:
        if budget is not None:
            return budget.call(mod.process_content_job, path, root)
        return mod.process_content_job(path, root)
    finally:
        mod.TRANSFER_OBSERVER.callback = None


def batch_transferred_bytes(job_id, *, reset=False):
    with _TRANSFER_LOCK:
        if reset:
            _TRANSFER_BYTES.clear()
        return _TRANSFER_BYTES.get(job_id, 0)

def worker_for_selection(job_id, ids):
    """Poll a bounded, selected-only batch without blocking the HTTP progress loop."""
    root = job_root(job_id)
    mod = get_mod()
    parent = read_manifest(job_id) or {}
    wanted = set(map(str, ids))
    paths = [root / "jobs" / str(it["child_job_id"]) / "manifest.json"
             for it in (parent.get("inventory") or {}).get("items", [])
             if str(it.get("id")) in wanted and it.get("child_job_id")]
    processed = False
    for path, future in list(_BATCH_FUTURES.items()):
        if future.done():
            try:
                future.result()
            except Exception:
                import logging
                logging.exception("worker failed job=%s", path.parent.name)
            del _BATCH_FUTURES[path]
            processed = True
    for path in paths:
        if len(_BATCH_FUTURES) >= DOWNLOAD_CONCURRENCY:
            break
        child = mod.read_json_if_valid(path) or {}
        if path not in _BATCH_FUTURES and child.get("status") in {"queued", "downloading", "transcribing"}:
            if float(child.get("retry_at") or 0) <= time.time():
                _BATCH_FUTURES[path] = mod._content_executor(DOWNLOAD_CONCURRENCY).submit(
                    _process_selected_child, path, root, job_id, app_runtime.current_budget())
                processed = True
    return {"active": len(_BATCH_FUTURES)}, processed
