# -*- coding: utf-8 -*-
"""
本地 HTTP 服务：为网页 GUI 提供 JSON API 与静态页面托管。
纯标准库实现（http.server），无第三方依赖。

前端 index.html 通过 fetch 调用下列 API：
    GET  /api/selfcheck
    POST /api/inspect          {url}
    POST /api/download         {job_id, ids:[...]}
    GET  /api/status?job_id=...
    POST /api/import_cookie    {platform, browser}
    GET  /api/content          ?platform=douyin
    GET  /api/cover?job_id=...&item_id=...
    GET  /api/failures?job_id=...
    POST /api/retry-failures   {job_id, ids:[...]}
    GET  /                  -> 返回 index.html
"""

import json
import logging
import os
import string
import subprocess
import sys
import threading
import traceback
import socket
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import engine
import download_history
import favorite_authors
import app_runtime
import time
import uuid
import hashlib
from logging.handlers import RotatingFileHandler

MUTATION_LOCK = threading.RLock()
INVENTORY_TIMEOUT = "盘点超时（60 秒）。请重新盘点或更换网络。"
DOWNLOAD_TIMEOUT = "下载超时（60 秒无进展）。已保留任务，请更换网络或继续上次任务。"

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"

# 把错误日志写到 server.log，方便事后排查；UI 只展示简短异常信息。
# 如果另一个旧版助手暂时锁住日志文件，新服务仍应正常启动，退回控制台
# 输出即可，不能让日志锁把整个 HTTP 服务变成“Failed to fetch”。
LOG_DIR = app_runtime.DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_handler = RotatingFileHandler(LOG_DIR / f"server-{os.getpid()}.log", maxBytes=4*1024*1024, backupCount=4, encoding="utf-8")
_handler.setFormatter(app_runtime.SafeFormatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)


def _placeholder_svg(kind: str, title: str, extra: str = "\u672a\u4e0b\u8f7d\uff0c\u4e0b\u8f7d\u540e\u663e\u793a\u5c01\u9762") -> bytes:
    """生成占位 SVG（避免外链被禁用导致显示空白）。"""
    icon_map = {
        "video": ("\U0001f3ac", "#ef4444", "视频"),
        "image_gallery": ("\U0001f5bc", "#3b82f6", "图集"),
        "live_photo": ("\u2728", "#8b5cf6", "动图"),
        "placeholder": ("\u2b07", "#94a3b8", "待下载"),
    }
    icon, color, label = icon_map.get(kind, icon_map["placeholder"])
    safe_title = (title or "")[:30].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_extra = (extra or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 320">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{color}" stop-opacity="0.18"/>'
        f'<stop offset="1" stop-color="{color}" stop-opacity="0.06"/>'
        '</linearGradient></defs>'
        '<rect width="240" height="320" fill="url(#g)"/>'
        f'<rect x="0.5" y="0.5" width="239" height="319" fill="none" stroke="{color}" stroke-opacity="0.25"/>'
        f'<text x="120" y="135" font-size="64" text-anchor="middle" dominant-baseline="middle">{icon}</text>'
        f'<text x="120" y="200" font-size="18" fill="{color}" text-anchor="middle" '
        f'font-family="system-ui, sans-serif" font-weight="600">{label}</text>'
        f'<text x="120" y="260" font-size="13" fill="#64748b" text-anchor="middle" '
        f'font-family="system-ui, sans-serif">{safe_title}</text>'
        '<text x="120" y="290" font-size="11" fill="#94a3b8" text-anchor="middle" '
        f'font-family="system-ui, sans-serif">{safe_extra}</text>'
        '</svg>'
    )
    return svg.encode("utf-8")


def _find_item(manifest, item_id: str):
    """在 manifest 里查找单个 item。manifest 可为 None 或 dict。"""
    if not manifest or not isinstance(manifest, dict):
        return None
    items = (manifest.get("inventory") or {}).get("items")
    if not items:
        return None
    for it in items:
        if str(it.get("id")) == str(item_id):
            return it
    return None


# ---------- 下载任务后台执行器 ----------
class DownloadRunner:
    """Persistent selected-only runner. HTTP polling never waits for a media request."""
    def __init__(self):
        self.lock = threading.RLock()
        self.running = False
        self.history_run = None
        self.budget = None
        self.history_budget = None
        self.archive_lock = None
        self.state = {"running": False, "job_id": None, "total": 0, "completed": 0,
                      "failed": 0, "processing": 0, "disk_ready": 0, "message": "就绪",
                      "finished": False, "success": False, "status": "idle", "ids": []}
        self.restore()

    def state_path(self):
        return app_runtime.DATA_DIR / "last-download.json"

    def restore(self):
        for file in (self.state_path(), engine.ARCHIVE_ROOT / "jobs" / ".gui-run.json"):
            try:
                saved = json.loads(file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(saved, dict) and saved.get("job_id"):
                self.state.update(saved, running=False, bytes_per_second=0)
                if not saved.get("finished") or saved.get("running"):
                    self.state.update(status="interrupted", finished=True, success=False,
                                      message="上次运行已中断，可点击继续上次任务。")
                break

    def persist(self):
        app_runtime.atomic_json(self.state_path(), self.state)

    def busy(self):
        with self.lock:
            return self.running or self.history_run is not None

    def start(self, job_id, ids, retry=False, prepared=False, *, history=False, allow_lower=False, guest_only=False):
        with self.lock:
            if self.running or (self.history_run is not None and not history):
                return False
            root = engine.job_root(job_id)
            manifest = engine.read_manifest(job_id) or {}
            if manifest.get("kind") != "creator_batch":
                raise engine.EngineError("请先盘点作者主页。")
            ids = list(dict.fromkeys(map(str, ids)))
            if not set(ids) <= {str(item.get("id")) for item in (manifest.get("inventory") or {}).get("items", [])}:
                raise engine.EngineError("选择包含清单中不存在的作品。")
            if not retry and manifest.get("selection") and set(ids) != set(map(str, manifest["selection"].get("ids", []))):
                raise engine.EngineError("该清单已提交其他作品，重新盘点后可继续选择。")
            lock_path = root / "jobs" / ".gui-owner"
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                mod = engine.get_mod()
                mod.fcntl.flock(descriptor, mod.fcntl.LOCK_EX | mod.fcntl.LOCK_NB)
                self.state.update(
                    running=True, job_id=job_id, ids=ids, platform=manifest.get("platform"),
                    author_folder=manifest.get("author_folder"), saved_folder=None,
                    archive_root=str(root), total=len(ids), completed=0, failed=0,
                    processing=0, disk_ready=0, transferred_bytes=0, bytes_per_second=0, quality_fallback=0,
                    message="已接收，正在准备下载…", finished=False, success=False, status="running",
                    prepared=prepared, retry_requested=retry, allow_lower=allow_lower, guest_only=guest_only)
                self.persist()
            except Exception:
                os.close(descriptor)
                self.state.update(running=False, finished=True, status="failed")
                raise
            self.archive_lock = descriptor
            self.budget = self.history_budget if history else app_runtime.TimeBudget(app_runtime.DOWNLOAD_IDLE_SECONDS)
            self.running = True
        if history:
            self._run(job_id, ids, retry, prepared, allow_lower, guest_only)
        else:
            threading.Thread(target=self._run, args=(job_id, ids, retry, prepared, allow_lower, guest_only), daemon=True).start()
        return True

    def start_history(self, entries, *, allow_lower=False, guest_only=False):
        with self.lock:
            if self.busy():
                return False
            self.history_run = {"total": len(entries), "completed": 0, "failed": 0, "transferred_bytes": 0,
                                "job_id": None, "message": "正在按作者整理重下任务…"}
            self.history_budget = app_runtime.TimeBudget(app_runtime.DOWNLOAD_IDLE_SECONDS)
        threading.Thread(target=self._run_history, args=(entries, allow_lower, guest_only), name="history-retry", daemon=True).start()
        return True

    def _run_history(self, entries, allow_lower=False, guest_only=False):
        missing_author = 0
        result = self.history_run
        timed_out = False
        if self.history_budget is None:
            self.history_budget = app_runtime.TimeBudget(app_runtime.DOWNLOAD_IDLE_SECONDS)
        try:
            groups, skipped = self.history_budget.call(engine.history_retry_groups, entries)
            missing_author = len(skipped)
            with self.lock:
                result["failed"] = missing_author
            for group in groups:
                ids = group["ids"]
                try:
                    job_id = self.history_budget.call(engine.stage_history_retry, group)
                    with self.lock:
                        result["job_id"] = job_id
                        result["message"] = "正在重下 " + (group.get("creator") or {}).get("name", "历史作品")
                    if not self.start(job_id, ids, retry=True, history=True, allow_lower=allow_lower, guest_only=guest_only):
                        raise engine.EngineError("上一个下载任务尚未结束。")
                    completed = self.state["completed"]
                    transferred = self.state["transferred_bytes"]
                    timed_out = self.state["status"] == "stalled"
                except TimeoutError:
                    timed_out = True
                    completed = transferred = 0
                except Exception:
                    logging.exception("could not retry historical author batch")
                    completed = transferred = 0
                with self.lock:
                    result["completed"] += completed
                    result["failed"] += len(ids) - completed
                    result["transferred_bytes"] += transferred
                    result["job_id"] = None
                if timed_out:
                    break
                self.history_budget = app_runtime.TimeBudget(app_runtime.DOWNLOAD_IDLE_SECONDS)
        except TimeoutError:
            timed_out = True
        except Exception:
            logging.exception("could not prepare history retry")
        finally:
            with self.lock:
                result["failed"] = result["total"] - result["completed"]
                summary = f"重下结束，成功 {result['completed']} 件，仍失败 {result['failed']} 件。"
                if missing_author:
                    summary += f"其中 {missing_author} 件缺少可核对的作者清单，重新盘点作者主页后可下载。"
                details = self.state if result["total"] == 1 and result["completed"] else {}
                if timed_out and self.state.get("status") == "stalled":
                    details = self.state
                if timed_out:
                    summary = DOWNLOAD_TIMEOUT if details.get("job_id") else (
                        "重下超时（60 秒无进展）。请更换网络后从失败历史重新提交。")
                self.state = {**details, **result, "job_id": details.get("job_id"), "message": summary,
                              "running": False, "finished": True, "processing": 0, "bytes_per_second": 0,
                              "success": not result["failed"],
                              "status": "stalled" if timed_out else "completed_with_failures" if result["failed"] else "completed"}
                self.history_run = None
                self.history_budget = None

    def _run(self, job_id, ids, retry=False, prepared=True, allow_lower=False, guest_only=False):
        if self.budget is None:
            self.budget = app_runtime.TimeBudget(app_runtime.DOWNLOAD_IDLE_SECONDS)
        try:
            engine.batch_transferred_bytes(job_id, reset=True)
            if not prepared:
                if retry:
                    options = {key: True for key, value in (("allow_lower", allow_lower), ("guest_only", guest_only)) if value}
                    prepared_retry = self.budget.call(engine.retry_failed_selection, job_id, ids, **options)
                    ids = prepared_retry["retried"]
                    manifest = engine.read_manifest(job_id) or {}
                    with self.lock:
                        self.state.update(ids=ids, total=len(ids), author_folder=manifest.get("author_folder"))
                else:
                    self.budget.call(engine.download_selection, job_id, ids)
                with self.lock:
                    self.state["prepared"] = True
            previous_bytes, previous_time = 0, time.monotonic()
            last_progress = (0, 0)
            saved_history = set()
            while True:
                try:
                    worker, _ = self.budget.call(engine.worker_for_selection, job_id, ids)
                except app_runtime.OperationTimeout:
                    # Reap active workers without starting queued work after the deadline.
                    worker, _ = engine.worker_for_selection(job_id, [])
                status = engine.job_status(job_id)
                disk = engine.selection_disk_status(job_id, ids)
                unsaved = set(disk.get("history") or []) - saved_history
                if unsaved:
                    try:
                        download_history.save(unsaved, author=disk.get("author"), track_attempts=True)
                        saved_history.update(unsaved)
                    except (OSError, download_history.sqlite3.Error):
                        logging.exception("could not save download history")
                counts = status.get("job", {}).get("counts", {})
                ready, failed = int(disk.get("ready", 0)), int(disk.get("failed", 0))
                pending = int(disk.get("pending", 0))
                waiting = int(counts.get("waiting_for_authorization", 0)) + int(counts.get("waiting_for_reauthentication", 0))
                now = time.monotonic()
                transferred = engine.batch_transferred_bytes(job_id)
                new_bytes = transferred > previous_bytes
                speed = int((transferred - previous_bytes) / max(0.01, now - previous_time))
                previous_bytes, previous_time = transferred, now
                quality_fallback = int(disk.get("quality_fallback", 0))
                key = (ready, failed)
                changed = key != last_progress
                if changed or new_bytes:
                    self.budget.progress()
                    last_progress = key
                with self.lock:
                    self.state.update(completed=ready, disk_ready=ready, failed=failed, processing=pending,
                                      transferred_bytes=transferred, bytes_per_second=speed,
                                      quality_fallback=quality_fallback,
                                      message=f"已核验 {ready}，失败 {failed}，待处理 {pending}，共 {len(ids)}")
                    if not worker.get("active") and waiting:
                        self.state.update(status="waiting_for_reauthentication", message="平台要求登录。可尝试免登录重试，或重新导入 Cookie 后继续。")
                        break
                    if not worker.get("active") and not pending:
                        success = ready == len(ids) and not failed and not disk.get("missing")
                        quality_note = f"，{quality_fallback} 件含非最高画质或备用图" if quality_fallback else ""
                        folders = {row[6] for row in disk.get("history", []) if row[3] == "success" and row[6]}
                        author_folder = self.state.get("author_folder")
                        preferred = str(engine.job_root(job_id) / author_folder) if author_folder else ""
                        saved_folder = preferred if preferred in folders else next(iter(folders)) if len(folders) == 1 else None
                        self.state.update(success=success, status="completed" if success else "completed_with_failures",
                                          saved_folder=saved_folder,
                                          message=f"核验结束：成功 {ready}，失败 {failed}，损坏或缺失 {disk.get('missing', 0)}{quality_note}")
                        break
                    if not self.budget.remaining() and not worker.get("active"):
                        self.state.update(status="stalled", message=DOWNLOAD_TIMEOUT)
                        break
                    if changed:
                        self.persist()
                time.sleep(0.4)
        except TimeoutError:
            with self.lock:
                self.state.update(status="stalled", message=DOWNLOAD_TIMEOUT, success=False)
        except Exception as exc:
            logging.exception("download runner crashed job=%s", job_id)
            with self.lock:
                self.state.update(status="failed", success=False, message="下载失败：" + app_runtime.redact(str(exc)))
        finally:
            with self.lock:
                self.running = False
                self.budget = None
                self.state.update(running=False, finished=True, bytes_per_second=0)
                try:
                    self.persist()
                finally:
                    if self.archive_lock is not None:
                        os.close(self.archive_lock)
                        self.archive_lock = None

    def snapshot(self):
        with self.lock:
            active = dict(self.state)
            budget = self.budget if self.running else self.history_budget
            if budget is not None and not budget.remaining():
                active.update(status="stalled", message="下载超时（60 秒无进展）。正在停止后台请求…")
            if self.history_run is not None:
                state = self.history_run
                timed_out = budget is not None and not budget.remaining()
                message = active["message"] if timed_out else state["message"]
                if not self.running or state["job_id"] != active.get("job_id"):
                    active = {}
                completed = state["completed"] + active.get("completed", 0)
                failed = state["failed"] + active.get("failed", 0)
                return {**state, "completed": completed, "failed": failed, "running": True, "finished": False,
                        "processing": state["total"] - completed - failed, "status": "stalled" if timed_out else "running",
                        "message": message,
                        "bytes_per_second": active.get("bytes_per_second", 0),
                        "transferred_bytes": state["transferred_bytes"] + active.get("transferred_bytes", 0)}
            return active


RUNNER = DownloadRunner()


def restore_download_history():
    roots = engine.known_archive_roots()
    last_download = RUNNER.snapshot()
    if last_download.get("archive_root"):
        roots.append(Path(last_download["archive_root"]))
    download_history.import_archives(roots)
    download_history.import_author_links(roots)
    if last_download.get("job_id") and last_download.get("ids"):
        try:
            disk = engine.selection_disk_status(last_download["job_id"], last_download["ids"])
            download_history.save(disk["history"], author=disk.get("author"), track_attempts=True)
        except (OSError, ValueError, engine.EngineError, download_history.sqlite3.Error):
            logging.exception("could not restore the last batch's history")
INVENTORY_LOCK = threading.Lock()


# ---------- HTTP handler ----------
class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0：每个连接处理一个请求后自动关闭（无 keep-alive）
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass  # 静默日志

    def _send_json(self, obj, code=200):
        receipt = getattr(self, "_receipt", None)
        if receipt is not None and code < 300 and obj.get("ok"):
            app_runtime.atomic_json(receipt, {"fingerprint": self._fingerprint, "response": obj})
            self._receipt = None
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        # 显式声明连接关闭（HTTP/1.1 时需要明确）
        self.close_connection = True

    def _send_file(self, path: Path):
        body = path.read_bytes()
        self.send_response(200)
        ctype = "text/html; charset=utf-8"
        if path.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif path.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True

    def _send_file_bytes(self, body: bytes, content_type: str, cache: bool = False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=3600")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not 0 <= length <= 1024 * 1024:
            raise ValueError("请求数据过大")
        data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("请求必须为 JSON 对象")
        return data

    def _set_archive_root(self, data):
        new_path = (data or {}).get("path")
        if not new_path:
            return {"ok": False, "error": "缺少 path 参数"}
        try:
            result = engine.set_archive_root(str(new_path), data.get("platform"))
            # result: {"path", "old_path", "base_path", "platform_paths", ...}
            result["ok"] = True
            result["archive_root"] = result.get("path")
            threading.Thread(target=restore_download_history, name="history-sync", daemon=True).start()
            return result
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            if INDEX.is_file():
                return self._send_file(INDEX)
            return self._send_json({"ok": False, "error": "index.html \u7f3a\u5931"}, 404)

        if path == "/api/health":
            return self._send_json({"ok": True, "version": app_runtime.VERSION, "pid": os.getpid(), "log_dir": str(LOG_DIR)})

        if path == "/api/selfcheck":
            return self._send_json(engine.self_check())

        if path == "/api/favorite-authors":
            return self._send_json({"ok": True, **favorite_authors.view()})

        if path == "/api/author-avatar":
            key = (qs.get("key") or [""])[0].strip()
            platform = (qs.get("platform") or [""])[0].strip()
            avatar_url = (qs.get("url") or [""])[0].strip()
            if key:
                info = favorite_authors.get_avatar_info(key)
                if info:
                    fav_platform, fav_url = info
                    if not platform:
                        platform = fav_platform
                    if not avatar_url:
                        avatar_url = fav_url
            result = engine.fetch_author_avatar(platform, avatar_url, key=key)
            if result:
                return self._send_file_bytes(*result, cache=True)
            return self._send_json({"ok": False, "error": "头像暂不可用"}, 404)

        if path in {"/api/work-media", "/api/work-media-preview"}:
            job_id = (qs.get("job_id") or [""])[0]
            item_id = (qs.get("item_id") or [""])[0]
            try:
                if path == "/api/work-media":
                    return self._send_json({"ok": True, **engine.work_media_view(job_id, item_id)})
                part = (qs.get("part") or [""])[0]
                response = engine.work_media_preview(job_id, item_id, part, qs.get("motion") == ["1"])
                if response:
                    return self._send_file_bytes(*response, cache=True)
                return self._send_json({"ok": False, "error": "没有可播放的预览"}, 404)
            except Exception as exc:
                return self._send_json({"ok": False, "error": app_runtime.redact(str(exc))}, 422)

        if path == "/api/history":
            try:
                offsets = [max(0, int((qs.get(key) or ["0"])[0])) for key in ("success_offset", "failure_offset")]
                job_id = (qs.get("author_job") or [""])[0]
                author = None
                if job_id:
                    manifest = engine.read_manifest(job_id) or {}
                    creator = manifest.get("creator") or {}
                    if not creator.get("id") or creator.get("single_work"):
                        return self._send_json({"ok": False, "error": "没有可关联的作者。"}, 404)
                    author = (manifest["platform"], str(creator["id"]))
                return self._send_json({"ok": True, **download_history.page(*offsets, author=author)})
            except (ValueError, engine.EngineError):
                return self._send_json({"ok": False, "error": "历史页码无效。"}, 400)
            except (OSError, download_history.sqlite3.Error, OverflowError):
                return self._send_json({"ok": False, "error": "暂时无法读取下载历史。"}, 503)

        if path == "/api/history-cover":
            platform = (qs.get("platform") or [""])[0]
            work_id = (qs.get("work_id") or [""])[0]
            entry = download_history.entry(platform, work_id)
            if entry:
                try:
                    from io import BytesIO
                    from PIL import Image, ImageOps
                    preview = engine.history_preview(entry)
                    if preview:
                        thumb = BytesIO()
                        with Image.open(preview) as image:
                            image = ImageOps.exif_transpose(image)
                            image.thumbnail((192, 192))
                            image.convert("RGB").save(thumb, "JPEG", quality=84)
                        return self._send_file_bytes(thumb.getvalue(), "image/jpeg", cache=True)
                except (OSError, ValueError):
                    pass
            return self._send_file_bytes(_placeholder_svg("placeholder", "预览暂无"), "image/svg+xml")

        if path == "/api/archive_root":
            platform = (qs.get("platform") or [None])[0]
            if platform is not None and platform not in engine.PLATFORMS:
                return self._send_json({"ok": False, "error": "下载平台无效。"}, 400)
            base_path = engine.get_destination_base()
            archive_root = engine.get_archive_root(platform) if platform else base_path
            return self._send_json({
                "ok": True,
                "base_path": str(base_path),
                "archive_root": str(archive_root),
                "platform_paths": engine.platform_paths_for_base(base_path),
                "configured": engine.is_destination_configured(),
                "fixed": engine.fixed_archive_path(platform),
                "explicit_override": bool(engine._EXPLICIT_ROOT),
            })

        if path == "/api/inventory":
            job_id = (qs.get("job_id") or [""])[0]
            try:
                return self._send_json({"ok": True, **engine.inventory_view(job_id)})
            except engine.EngineError as exc:
                return self._send_json({"ok": False, "error": str(exc)}, 404)

        if path == "/api/browse_folder":
            # 简单的目录列表：返回 path 下的子目录与 Windows 盘符列表
            # 特殊值 "__drives__" 返回 Windows 所有盘符（C:\, D:\, ...）
            target = (qs.get("path") or [""])[0]
            try:
                drives = []
                if os.name == "nt":
                    for letter in string.ascii_uppercase:
                        d = f"{letter}:\\"
                        if os.path.exists(d):
                            drives.append({"name": f"{letter}:", "path": d, "is_dir": True, "is_drive": True})
                if target == "__drives__":
                    if os.name == "nt":
                        return self._send_json({"ok": True, "current": "（选择盘符）", "entries": drives, "drives": drives, "is_drives": True})
                    target = "/"
                p = Path(target).expanduser().resolve()
                if not p.is_dir():
                    return self._send_json({"ok": False, "error": "目录不存在"}, 400)
                entries = []
                if os.name == "nt" and len(str(p)) >= 3 and str(p)[1:3] == ":\\":
                    entries.append({"name": "（所有盘符）", "path": "__drives__", "is_dir": True, "is_drive_root": True})
                try:
                    parent = p.parent
                    if parent != p:
                        entries.append({"name": "..", "path": str(parent), "is_dir": True})
                except Exception:
                    pass
                for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    try:
                        if child.is_dir():
                            entries.append({"name": child.name, "path": str(child), "is_dir": True})
                    except Exception:
                        continue
                return self._send_json({
                    "ok": True,
                    "current": str(p),
                    "entries": entries[:200],
                    "drives": drives,
                    "platform_paths": engine.platform_paths_for_base(p),
                })
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"ok": False, "error": str(exc)}, 500)

        if path == "/api/status":
            job_id = (qs.get("job_id") or [""])[0]
            if not job_id:
                return self._send_json({"ok": False, "error": "\u7f3a\u5c11 job_id"}, 400)
            try:
                result = engine.job_status(job_id)
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"ok": False, "error": str(exc)}, 500)
            return self._send_json(result)

        if path == "/api/progress":
            return self._send_json({"ok": True, "state": RUNNER.snapshot()})

        if path == "/api/content":
            platform = (qs.get("platform") or ["douyin"])[0]
            return self._send_json({"ok": True, "entries": engine.list_content(platform)})

        if path == "/api/cover":
            job_id = (qs.get("job_id") or [""])[0]
            item_id = (qs.get("item_id") or [""])[0]
            if not job_id or not item_id:
                return self._send_json({"ok": False, "error": "\u7f3a\u5c11 job_id / item_id"}, 400)
            manifest = engine.read_manifest(job_id) or {}
            item = _find_item(manifest, item_id)
            if item is None:
                svg = _placeholder_svg("placeholder", "")
                return self._send_file_bytes(svg, "image/svg+xml", cache=False)
            root = engine.job_root(job_id)
            cached = engine.preview_cache_path(item, root=root)
            if cached is None:
                _, cached = engine.resolve_item_cover(dict(item, _author_folder=manifest.get("author_folder"),
                    _direct_archive=manifest.get("direct_archive")), root=root)
            if cached is not None:
                try:
                    ctype = {
                        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                        ".webp": "image/webp", ".avif": "image/avif", ".gif": "image/gif",
                    }.get(cached.suffix.lower(), "application/octet-stream")
                    return self._send_file_bytes(cached.read_bytes(), ctype, cache=True)
                except OSError:
                    pass
            mk = ((item.get("douyin_media") or {}).get("kind")) or item.get("media_kind") or "placeholder"
            svg = _placeholder_svg(mk, item.get("title") or "")
            return self._send_file_bytes(svg, "image/svg+xml", cache=False)

        if path == "/api/refresh_covers":
            # 前端轮询接口：返回 items 当前封面状态（不触发重新抓取）
            job_id = (qs.get("job_id") or [""])[0]
            if not job_id:
                return self._send_json({"ok": False, "error": "\u7f3a\u5c11 job_id"}, 400)
            manifest = engine.read_manifest(job_id)
            if not manifest:
                return self._send_json({"ok": False, "error": "manifest \u4e0d\u5b58\u5728"}, 404)
            items = engine.inventory_items(manifest)
            preview = engine.preview_status(items, root=engine.job_root(job_id))
            return self._send_json({
                "ok": True,
                "job_id": job_id,
                "ready": preview["ready"],
                "pending": preview["pending"],
                "failed": preview["failed"],
                "total": len(items),
                "mode": "local_prefetch",
            })

        if path == "/api/failures":
            # 返回当前冻结清单中仍失败的作品，供前端在进度区下方选择重试。
            job_id = (qs.get("job_id") or [""])[0]
            if not job_id:
                return self._send_json({"ok": False, "error": "缺少 job_id"}, 400)
            try:
                failures = engine.failed_selection_details(job_id)
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"ok": False, "error": str(exc)}, 500)
            return self._send_json({
                "ok": True,
                "job_id": job_id,
                "failures": failures,
                "total": len(failures),
            })

        return self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        with (nullcontext() if urlparse(self.path).path in {"/api/inspect", "/api/inspect-continue"} else MUTATION_LOCK):
            try:
                return self._do_POST()
            except Exception as exc:
                logging.exception("request failed endpoint=%s", urlparse(self.path).path)
                return self._send_json({"ok": False, "error": app_runtime.redact(str(exc)), "code": getattr(exc, "code", "request_failed")}, 400)

    def _do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        data = self._read_body()
        self._receipt = None
        request_id = self.headers.get("X-Request-ID", "")
        if request_id and path in {"/api/inspect", "/api/download", "/api/retry-failures", "/api/history-retry", "/api/history-retry-all"}:
            if not __import__('re').fullmatch(r"[A-Za-z0-9-]{8,80}", request_id):
                raise ValueError("无效请求标识")
            fingerprint = hashlib.sha256(json.dumps([path, str(engine.ARCHIVE_ROOT), data], sort_keys=True).encode()).hexdigest()
            receipt = app_runtime.DATA_DIR / "requests" / (request_id + ".json")
            if receipt.is_file():
                saved = json.loads(receipt.read_text(encoding="utf-8"))
                if saved.get("fingerprint") != fingerprint:
                    return self._send_json({"ok": False, "error": "重复请求标识与参数不一致"}, 409)
                return self._send_json(saved["response"])
            self._receipt, self._fingerprint = receipt, fingerprint

        if path == "/api/resume":
            if RUNNER.busy():
                return self._send_json({"ok": False, "error": "已有下载任务正在进行。"}, 409)
            saved = RUNNER.snapshot()
            if not saved.get("job_id") or not saved.get("ids"):
                return self._send_json({"ok": False, "error": "没有可继续的任务"}, 400)
            if saved.get("status") == "completed":
                return self._send_json({"ok": True, "state": saved})
            if engine.uses_old_destination(saved["job_id"], saved.get("platform")):
                entries = engine.retry_batch_entries(saved["job_id"], saved["ids"])
                if not entries:
                    return self._send_json({"ok": False, "error": "找不到上次的作者清单，请重新盘点。"}, 404)
                RUNNER.start_history(entries, allow_lower=saved.get("allow_lower", False), guest_only=saved.get("guest_only", False))
                return self._send_json({"ok": True, "state": RUNNER.snapshot()})
            retry = saved.get("status") == "completed_with_failures" or saved.get("retry_requested", False)
            prepared = saved.get("status") in {"stalled", "interrupted"} and saved.get("prepared", False)
            RUNNER.start(saved["job_id"], saved["ids"], retry=retry, prepared=prepared,
                         allow_lower=saved.get("allow_lower", False), guest_only=saved.get("guest_only", False))
            return self._send_json({"ok": True, "state": RUNNER.snapshot()})

        if path == "/api/archive_root":
            if RUNNER.busy():
                return self._send_json({"ok": False, "error": "下载中不能切换目录，请等待任务结束。"}, 409)
            # Always serialize a response. Returning the dict directly from a
            # BaseHTTPRequestHandler leaves the socket without HTTP headers,
            # which the browser reports as the unhelpful "Failed to fetch".
            result = self._set_archive_root(data)
            return self._send_json(result, 200 if result.get("ok") else 400)

        if path == "/api/favorite-authors":
            if data.get("action") == "add":
                job_id = str(data.get("job_id") or "")
                favorite_authors.keep(job_id, engine.read_manifest(job_id), data.get("browser"))
            elif data.get("action") == "remove":
                favorite_authors.remove(str(data.get("key") or ""))
            elif data.get("action") == "note":
                favorite_authors.set_note(str(data.get("key") or ""), data.get("note"))
            elif data.get("action") == "pin":
                pinned = data.get("pinned")
                favorite_authors.set_pin(str(data.get("key") or ""), pinned if isinstance(pinned, bool) else None)
            elif data.get("action") == "reorder":
                favorite_authors.reorder(data.get("keys"))
            elif data.get("action") == "check":
                favorite_authors.start(engine.scan_favorite_author, engine.favorite_work_published_at,
                                       data.get("browser"))
            else:
                return self._send_json({"ok": False, "error": "未知的常看作者操作。"}, 400)
            return self._send_json({"ok": True, **favorite_authors.view()})

        if path == "/api/create_folder":
            parent_raw = str(data.get("parent") or "").strip()
            name = str(data.get("name") or "").strip()
            if not parent_raw or not name:
                return self._send_json({"ok": False, "error": "请输入要新建的子文件夹名称。"}, 400)
            if name in {".", ".."} or any(ch in name for ch in '\\/:*?"<>|') or name.endswith((" ", ".")):
                return self._send_json({"ok": False, "error": "文件夹名称包含非法字符。"}, 400)
            parent = Path(parent_raw).expanduser().resolve()
            if not parent.is_dir():
                return self._send_json({"ok": False, "error": "上级目录不存在。"}, 400)
            created = (parent / name).resolve()
            if created.parent != parent:
                return self._send_json({"ok": False, "error": "文件夹名称无效。"}, 400)
            created.mkdir(parents=False, exist_ok=True)
            return self._send_json({"ok": True, "path": str(created)})

        if path == "/api/inspect":
            if RUNNER.busy():
                return self._send_json({"ok": False, "error": "下载中请等待当前任务结束后再盘点。"}, 409)
            url = str(data.get("url") or "").strip()
            if not url:
                return self._send_json({"ok": False, "error": "\u8bf7\u63d0\u4f9b\u4f5c\u8005\u4e3b\u9875\u94fe\u63a5"}, 400)
            if not INVENTORY_LOCK.acquire(blocking=False):
                return self._send_json({"ok": False, "error": "正在盘点，请等待当前盘点完成。"}, 409)
            try:
                browser = str(data.get("browser") or "").strip().lower() or None
                budget = app_runtime.TimeBudget(app_runtime.CHECK_SECONDS)
                options = {"guest_only": True} if data.get("guest_only") is True else {}
                future = app_runtime.submit_operation(budget, engine.inspect_creator, url, browser=browser, **options)
                result = future.result(timeout=budget.remaining())
                if result.get("ok"):
                    try:
                        favorite_authors.record_inventory(result["job_id"], result["platform"], result.get("creator") or {},
                                                          engine.read_manifest, browser)
                    except (OSError, ValueError):
                        logging.exception("could not save favorite author inventory")
                return self._send_json(result)
            except TimeoutError:
                return self._send_json({"ok": False, "error": INVENTORY_TIMEOUT, "code": "inventory_timeout"}, 504)
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"ok": False, "error": app_runtime.redact(str(exc)),
                                        "code": getattr(exc, "code", "inventory_failed")}, 422)
            finally:
                INVENTORY_LOCK.release()

        if path == "/api/download":
            job_id = str(data.get("job_id") or "")
            ids = data.get("ids") or []
            if not job_id or not isinstance(ids, list) or not ids:
                return self._send_json({"ok": False, "error": "\u7f3a\u5c11 job_id \u6216 ids"}, 400)
            manifest = engine.read_manifest(job_id) or {}
            if engine.uses_old_destination(job_id, manifest.get("platform")):
                return self._send_json({"ok": False, "error": "该清单使用旧归档目录，请重新盘点后下载。"}, 409)
            selected_media = data.get("media", {})
            if not isinstance(selected_media, dict):
                raise ValueError("无效的作品媒体选择。")
            if selected_media or (manifest.get("selection") and not RUNNER.busy()):
                if RUNNER.busy():
                    return self._send_json({"ok": False, "error": "已有任务进行中"}, 409)
                job_id = engine.stage_media_batch(job_id, list(dict.fromkeys(map(str, ids))), selected_media)
            # A second click while the same job is already running should
            # attach the page to that job instead of showing a misleading
            # submission failure.  A different job remains blocked, but the
            # caller receives the active job and live state for diagnosis.
            active = RUNNER.snapshot()
            if (active.get("running") or active.get("finished")) and str(active.get("job_id") or "") == job_id and set(active.get("ids", ids)) == set(map(str, ids)):
                return self._send_json({"ok": True, "resumed": True, "state": active})
            started = RUNNER.start(job_id, ids)
            if not started:
                active = RUNNER.snapshot()
                if active.get("running") and str(active.get("job_id") or "") == job_id and set(active.get("ids", ids)) == set(map(str, ids)):
                    return self._send_json({"ok": True, "resumed": True, "state": active})
                return self._send_json({
                    "ok": False,
                    "error": "\u5df2\u6709\u4e0b\u8f7d\u4efb\u52a1\u8fdb\u884c\u4e2d",
                    "active_job_id": active.get("job_id"),
                    "state": active,
                }, 409)
            return self._send_json({"ok": True, "state": RUNNER.snapshot()})

        if path == "/api/retry-failures":
            job_id = str(data.get("job_id") or "")
            ids = data.get("ids") or []
            if not job_id or not isinstance(ids, list) or not ids:
                return self._send_json({"ok": False, "error": "缺少 job_id 或失败作品 ids"}, 400)
            active = RUNNER.snapshot()
            if active.get("running"):
                return self._send_json({
                    "ok": False,
                    "error": "已有下载任务进行中，请等待当前任务完成",
                    "active_job_id": active.get("job_id"),
                    "state": active,
                }, 409)
            manifest = engine.read_manifest(job_id) or {}
            allow_lower = data.get("allow_lower") is True
            options = {"guest_only": True} if data.get("guest_only") is True else {}
            if options and manifest.get("platform") not in {"douyin", "xiaohongshu"}:
                return self._send_json({"ok": False, "error": "此平台未提供免登录重试。"}, 400)
            if allow_lower and manifest.get("platform") not in engine.AUTHOR_PLATFORMS:
                return self._send_json({"ok": False, "error": "当前平台未提供降一级画质下载。"}, 400)
            if engine.uses_old_destination(job_id, manifest.get("platform")):
                entries = engine.retry_batch_entries(job_id, ids)
                if not entries:
                    return self._send_json({"ok": False, "error": "找不到这批作品的作者清单。"}, 404)
                if not RUNNER.start_history(entries, allow_lower=allow_lower, **options):
                    return self._send_json({"ok": False, "error": "已有任务进行中"}, 409)
                return self._send_json({"ok": True, "state": RUNNER.snapshot(), "retry": True})
            if not RUNNER.start(job_id, ids, retry=True, allow_lower=allow_lower, **options):
                return self._send_json({"ok": False, "error": "已有任务进行中"}, 409)
            return self._send_json({"ok": True, "state": RUNNER.snapshot(), "retry": True, "retried": RUNNER.snapshot().get("ids", ids)})

        if path == "/api/history-retry-all":
            entries = download_history.failed_entries()
            if not entries:
                return self._send_json({"ok": False, "error": "暂无失败作品需要重下。"}, 404)
            if not RUNNER.start_history(entries):
                return self._send_json({"ok": False, "error": "请等当前下载完成后再重下。"}, 409)
            return self._send_json({"ok": True, "state": RUNNER.snapshot()})

        if path in {"/api/history-retry", "/api/history-open"}:
            entry = download_history.entry(str(data.get("platform") or ""), str(data.get("work_id") or ""),
                                           failure=path == "/api/history-retry")
            if not entry:
                return self._send_json({"ok": False, "error": "这条下载历史已不存在。"}, 404)
            if path == "/api/history-retry":
                if entry["status"] != "failure":
                    return self._send_json({"ok": False, "error": "这个作品已经下载成功。"}, 409)
                allow_lower = data.get("allow_lower") is True
                options = {"guest_only": True} if data.get("guest_only") is True else {}
                if options and entry["platform"] not in {"douyin", "xiaohongshu"}:
                    return self._send_json({"ok": False, "error": "此平台未提供免登录重试。"}, 400)
                if allow_lower and entry["platform"] not in engine.AUTHOR_PLATFORMS:
                    return self._send_json({"ok": False, "error": "当前平台未提供降一级画质下载。"}, 400)
                if not RUNNER.start_history([entry], allow_lower=allow_lower, **options):
                    return self._send_json({"ok": False, "error": "请等当前下载完成后再重下。"}, 409)
                return self._send_json({"ok": True, "state": RUNNER.snapshot()})
            root, child, _, _ = engine.history_work(entry)
            target = engine.history_local_path(entry, root, child)
            if not target:
                return self._send_json({"ok": False, "error": "找不到这件作品的本地文件或目录。"}, 404)
            if os.name == "nt" and target.is_file():
                explorer = Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"
                subprocess.Popen([str(explorer), "/select,", str(target)])
            else:
                folder = target if target.is_dir() else target.parent
                if os.name == "nt":
                    os.startfile(str(folder))  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
            return self._send_json({"ok": True})

        if path == "/api/import_cookie":
            if RUNNER.busy():
                return self._send_json({"ok": False, "error": "请等待当前下载暂停或结束后再导入 Cookie。"}, 409)
            platform = str(data.get("platform") or "")
            browser = str(data.get("browser") or "")
            if not platform or not browser:
                return self._send_json({"ok": False, "error": "\u7f3a\u5c11 platform \u6216 browser"}, 400)
            try:
                result = engine.import_browser_cookies(platform, browser)
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"ok": False, "error": str(exc)}, 500)
            return self._send_json(result)

        if path == "/api/open_folder":
            # 在系统文件管理器中打开指定路径（绝对路径）。仅作用于已知下载根目录范围内。
            target = (data or {}).get("path")
            platform = data.get("platform")
            if platform is not None and platform not in engine.PLATFORMS:
                return self._send_json({"ok": False, "error": "下载平台无效。"}, 400)
            if not target:
                target = str(engine.get_archive_root(platform) if platform else engine.get_destination_base())
            try:
                p = Path(str(target)).expanduser().resolve()
                job_id = data.get("job_id")
                allowed_roots = [engine.get_destination_base(), *engine.known_archive_roots()]
                if job_id:
                    allowed_roots.insert(0, engine.job_root(job_id))
                elif platform:
                    allowed_roots.insert(0, engine.get_archive_root(platform))
                # 安全：必须在已知下载根目录范围内（防越权）
                if not any(p.is_relative_to(root.resolve()) for root in allowed_roots):
                    return self._send_json({"ok": False, "error": "路径不在下载根目录下"}, 400)
                if not p.is_dir():
                    return self._send_json({"ok": False, "error": "目录不存在"}, 404)
                if os.name == "nt":
                    os.startfile(str(p))  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.run(["open", str(p)], check=False)
                else:
                    subprocess.run(["xdg-open", str(p)], check=False)
                return self._send_json({"ok": True, "path": str(p)})
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"ok": False, "error": str(exc)}, 500)

        if path == "/api/exit":
            request_app_exit()
            return self._send_json({"ok": True, "exiting": True})

        return self._send_json({"ok": False, "error": "not found"}, 404)


WINDOW_TITLE = f"{app_runtime.APP_NAME} V{app_runtime.VERSION}"
_DESKTOP_WINDOW = None
_HTTP_SERVER = None
_FORCE_EXIT = False


def request_app_exit():
    """Gracefully close the desktop window and stop the local server."""
    global _FORCE_EXIT
    _FORCE_EXIT = True

    def _shutdown():
        time.sleep(0.12)
        win = _DESKTOP_WINDOW
        srv = _HTTP_SERVER
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
        time.sleep(0.8)
        os._exit(0)

    threading.Thread(target=_shutdown, name="app-exit", daemon=True).start()


def desktop_lock():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, f"Local\\WorksAppreciationDownloaderDesktop-{app_runtime.VERSION}")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        for _ in range(25):
            existing = user.FindWindowW(None, WINDOW_TITLE)
            if existing:
                user.ShowWindow(existing, 9)
                user.SetForegroundWindow(existing)
                break
            time.sleep(0.2)
        return None
    return lambda: kernel.CloseHandle(handle)


def show_desktop(url):
    global _DESKTOP_WINDOW
    import ctypes
    import webview

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    window = webview.create_window(WINDOW_TITLE, url=url, width=1240, height=850, min_size=(840, 620), text_select=True)
    _DESKTOP_WINDOW = window

    def before_close():
        if _FORCE_EXIT:
            return True
        if RUNNER.busy():
            ctypes.windll.user32.MessageBoxW(None, "下载正在进行，请等待任务完成后关闭窗口。", WINDOW_TITLE, 0x40)
            return False
        return True

    window.events.closing += before_close
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(app_runtime.DATA_DIR / "desktop"))
    finally:
        _DESKTOP_WINDOW = None


def main(port=None, desktop=True):
    global _HTTP_SERVER
    threading.Thread(target=restore_download_history, name="history-import", daemon=True).start()
    from socketserver import ThreadingMixIn
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        # Windows may otherwise let several old one-file EXEs bind the same
        # 127.0.0.1:8000 socket. Requests then land on an arbitrary version,
        # producing the observed initialization and Job mismatches.
        allow_reuse_address = False

        def server_bind(self):
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()
    # A user may already have an older one-file build running on 8000.  With
    # exclusive binding enabled the new build must not silently share that
    # socket (which used to make the browser land on a random old version and
    # show stale jobs or an initialization failure).  Keep 8000 as the
    # preferred address, but make the normal double-click workflow recover by
    # trying the documented 8786 port, then another deterministic port, and
    # finally an ephemeral port if both are occupied.
    requested_port = int(port if port is not None else 0 if desktop else 8000)
    candidates = [requested_port]
    if requested_port == 8000:
        candidates.extend([8786, 8787, 0])
    elif requested_port != 0:
        candidates.append(0)
    server = None
    last_error = None
    for candidate in candidates:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError as exc:
            last_error = exc
            continue
    if server is None:
        raise last_error or OSError("无法绑定本地服务端口")
    _HTTP_SERVER = server
    actual_port = int(server.server_address[1])
    url = f"http://127.0.0.1:{actual_port}/"
    if not desktop:
        print(f"{app_runtime.APP_NAME} V{app_runtime.VERSION} 已启动：{url}")
    app_runtime.atomic_json(app_runtime.DATA_DIR / f"instance-{os.getpid()}.json",
                            {"pid": os.getpid(), "port": actual_port, "url": url, "version": app_runtime.VERSION})
    logging.info("%s version=%s port=%s root=%s", app_runtime.APP_NAME, app_runtime.VERSION, actual_port, engine.ARCHIVE_ROOT)
    try:
        if desktop:
            thread = threading.Thread(target=server.serve_forever, name="desktop-service", daemon=True)
            thread.start()
            try:
                show_desktop(url)
            finally:
                server.shutdown()
                thread.join()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _HTTP_SERVER = None
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="作品鉴赏downloader 本地服务")
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.no_browser:
        main(port=args.port, desktop=False)
    else:
        release = desktop_lock()
        if release:
            try:
                main(port=args.port)
            except Exception as exc:
                import ctypes
                logging.exception("desktop startup failed")
                ctypes.windll.user32.MessageBoxW(None, "无法打开桌面窗口。请确认 Windows 已安装 Microsoft Edge WebView2。\n"
                                                  + app_runtime.redact(str(exc)), WINDOW_TITLE, 0x10)
                raise SystemExit(1) from exc
            finally:
                release()
