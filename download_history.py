"""Persistent per-work download results, independent of the download folder."""

from contextlib import closing
import json
import hashlib
import logging
from pathlib import Path
import sqlite3

import app_runtime
from quality_verification import verified_work

TERMINAL_STATES = {"completed", "failed", "unavailable"}
PLATFORMS = {"douyin", "xiaohongshu", "weibo"}
FAILURES = """
    SELECT h.*, j.archive_root, j.child_job, j.batch_job FROM history h
    LEFT JOIN history_jobs j USING (platform, work_id) WHERE h.status='failure'
    UNION ALL
    SELECT * FROM history_failed_attempts
"""


def connect():
    database = sqlite3.connect(app_runtime.DATA_DIR / "download-history.sqlite3", timeout=8)
    database.row_factory = sqlite3.Row
    with database:
        database.execute("""
            CREATE TABLE IF NOT EXISTS history (
                platform TEXT NOT NULL, work_id TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT NOT NULL,
                folder TEXT NOT NULL, quality TEXT NOT NULL,
                PRIMARY KEY (platform, work_id)
            )
        """)
        database.execute("CREATE INDEX IF NOT EXISTS history_status_time ON history (status, updated_at DESC)")
        database.execute("CREATE TABLE IF NOT EXISTS imported_roots (path TEXT PRIMARY KEY)")
        database.execute("""
            CREATE TABLE IF NOT EXISTS history_jobs (
                platform TEXT NOT NULL, work_id TEXT NOT NULL, archive_root TEXT NOT NULL,
                child_job TEXT NOT NULL, batch_job TEXT NOT NULL, PRIMARY KEY (platform, work_id)
            )
        """)
        database.execute("CREATE TABLE IF NOT EXISTS history_jobs_roots (path TEXT PRIMARY KEY)")
        database.execute("""
            CREATE TABLE IF NOT EXISTS work_authors (
                platform TEXT NOT NULL, work_id TEXT NOT NULL, author_id TEXT NOT NULL,
                PRIMARY KEY (platform, work_id)
            )
        """)
        database.execute("CREATE INDEX IF NOT EXISTS work_authors_author ON work_authors(platform, author_id)")
        database.execute("""
            CREATE TABLE IF NOT EXISTS history_failed_attempts (
                platform TEXT NOT NULL, work_id TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT NOT NULL,
                folder TEXT NOT NULL, quality TEXT NOT NULL, archive_root TEXT NOT NULL,
                child_job TEXT NOT NULL, batch_job TEXT NOT NULL, PRIMARY KEY (platform, work_id)
            )
        """)
        database.execute("""
            CREATE TABLE IF NOT EXISTS author_folders (
                platform TEXT NOT NULL, author_id TEXT NOT NULL, archive_root TEXT NOT NULL, folder TEXT NOT NULL,
                PRIMARY KEY (platform, author_id, archive_root), UNIQUE(archive_root, folder)
            )
        """)
        database.execute("CREATE TABLE IF NOT EXISTS imported_author_roots (path TEXT PRIMARY KEY)")
    return database


def from_manifest(root, child, *, status=None, child_job=None, batch_job="", work_id=None):
    platform = child.get("platform")
    work_id = str(work_id or child.get("submission_id") or child.get("content_id") or "")
    terminal = status or child.get("status")
    if platform not in PLATFORMS or not work_id or terminal not in TERMINAL_STATES:
        return None
    status = "success" if terminal == "completed" else "failure"
    output = child.get("output_dir")
    folder = ""
    if status == "success" and output:
        candidate = (root / str(output)).resolve()
        if candidate.is_relative_to(root.resolve()):
            folder = str(candidate)
    error = (child.get("error") or child.get("last_retry_error") or {}).get("message") or "下载失败"
    quality = child.get("image_quality") or ""
    video = child.get("video_quality") or {}
    photos = [record for record in child.get("outputs") or [] if isinstance(record, dict) and record.get("role") == "image"]
    dimensions = video or (photos[0] if len(photos) == 1 and quality != "api_fallback" else {})
    if dimensions.get("width") and dimensions.get("height"):
        quality = f"{dimensions['width']}×{dimensions['height']}"
        if video.get("bitrate"):
            quality += f" · {video['bitrate'] / 1000000:.1f} Mbps"
    if child.get("media_ids") and not child.get("media_selection_complete"):
        quality = " · ".join(filter(None, ("部分下载", quality)))
    if child.get("non_highest"):
        quality = " · ".join(filter(None, ("非最高画质", quality if quality != "api_fallback" else "")))
    if status == "success" and any(photo.get("source_quality") in {"full_size_render", "display"}
                                   and not photo.get("non_highest") for photo in photos) and child.get("image_quality") != "api_fallback":
        label = quality if quality != "full_size_render" else ""
        quality = " · ".join(filter(None, (label, "原尺寸衍生图")))
    if status == "success" and verified_work(child):
        quality = " · ".join(filter(None, (quality, "原画质")))
    if child.get("douyin_clean_revision"):
        quality = " · ".join(filter(None, (quality, "无水印来源")))
    media = [record for record in child.get("outputs") or []
             if record.get("role") in {"image", "video", "live_motion", "live_photo_motion"}]
    if media and all(record.get("retained_existing") for record in media):
        quality = " · ".join(filter(None, (quality, "保留原文件")))
    return (
        platform, work_id, str(child.get("title") or "作品 " + work_id),
        status, str(child.get("completed_at") if status == "success" and child.get("completed_at")
                    else child.get("updated_at") or child.get("created_at") or ""),
        "" if status == "success" else app_runtime.redact(str(error))[:300],
        folder, quality if status == "success" else "",
        str(root.resolve()), str(child_job or child.get("job_id") or ""), batch_job,
    )


def save(rows, *, author=None, track_attempts=False):
    if not rows:
        return
    with closing(connect()) as database, database:
        database.executemany("""
            INSERT INTO history (platform, work_id, title, status, updated_at, error, folder, quality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (platform, work_id) DO UPDATE SET
                title=excluded.title, status=excluded.status, updated_at=excluded.updated_at,
                error=excluded.error, folder=excluded.folder, quality=excluded.quality
            WHERE (excluded.status = 'success' AND history.status = 'failure')
                OR (excluded.status = history.status AND excluded.updated_at >= history.updated_at
                    AND (excluded.status != 'success' OR
                         ((instr(excluded.quality, '部分下载') = 0 OR instr(history.quality, '部分下载') > 0)
                         AND (instr(excluded.quality, '非最高画质') = 0 OR instr(history.quality, '非最高画质') > 0
                              OR (instr(excluded.quality, '无水印来源') > 0 AND instr(history.quality, '无水印来源') = 0)
                              OR instr(history.quality, '部分下载') > 0))))
        """, (row[:8] for row in rows))
        database.executemany("""
            INSERT INTO history_jobs (platform, work_id, archive_root, child_job, batch_job)
            SELECT ?, ?, ?, ?, ? WHERE EXISTS (
                SELECT 1 FROM history WHERE platform=? AND work_id=? AND status=? AND updated_at=? AND quality=?
            )
            ON CONFLICT (platform, work_id) DO UPDATE SET
                archive_root=excluded.archive_root, child_job=excluded.child_job,
                batch_job=CASE WHEN excluded.batch_job != '' OR excluded.child_job != history_jobs.child_job
                               THEN excluded.batch_job ELSE history_jobs.batch_job END
        """, ((*row[:2], *row[8:], *row[:2], row[3], row[4], row[7])
              for row in rows if len(row) > 8 and (row[9] or row[10])))
        if author:
            platform, author_id = author
            link_works(database, platform, author_id, ({"id": row[1]} for row in rows if row[0] == platform))
        if track_attempts:
            # Keep a usable existing download and its latest unresolved failure independently.
            database.executemany("""
                INSERT INTO history_failed_attempts
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM history WHERE platform=? AND work_id=?
                              AND status='success' AND updated_at<=?)
                ON CONFLICT (platform, work_id) DO UPDATE SET
                    title=excluded.title, updated_at=excluded.updated_at, error=excluded.error,
                    archive_root=excluded.archive_root, child_job=excluded.child_job, batch_job=excluded.batch_job
                WHERE excluded.updated_at >= history_failed_attempts.updated_at
            """, ((*row, *row[:2], row[4]) for row in rows if row[3] == "failure" and len(row) == 11))
        database.executemany("""
            DELETE FROM history_failed_attempts WHERE platform=? AND work_id=? AND updated_at<=?
        """, ((*row[:2], row[4]) for row in rows if row[3] == "success"))


def entry(platform, work_id, *, failure=False):
    with closing(connect()) as database:
        if failure:
            row = database.execute(f"SELECT * FROM ({FAILURES}) WHERE platform=? AND work_id=?",
                                   (platform, work_id)).fetchone()
            return dict(row) if row else None
        row = database.execute("""
            SELECT h.*, j.archive_root, j.child_job, j.batch_job FROM history h
            LEFT JOIN history_jobs j USING (platform, work_id) WHERE h.platform=? AND h.work_id=?
        """, (platform, work_id)).fetchone()
    return dict(row) if row else None


def quality_labels(platform):
    with closing(connect()) as database:
        return {row["work_id"]: row["quality"] for row in database.execute("""
            SELECT work_id, quality FROM history
            WHERE platform=? AND status='success'
        """, (platform,))}

def failed_entries():
    with closing(connect()) as database:
        return [dict(row) for row in database.execute(f"""
            SELECT * FROM ({FAILURES}) ORDER BY updated_at DESC, platform, work_id
        """)]


def page(success_offset=0, failure_offset=0, limit=24, *, author=None):
    result = {}
    with closing(connect()) as database, database:
        for status, offset in (("success", success_offset), ("failure", failure_offset)):
            source = "history" if status == "success" else f"({FAILURES})"
            filters = "h.status=?"
            arguments = (status,)
            if author:
                filters += """
                    AND EXISTS (SELECT 1 FROM work_authors a
                      WHERE a.platform=h.platform AND a.work_id=h.work_id
                        AND a.platform=? AND a.author_id=?)
                """
                arguments += tuple(author)
            count = database.execute(f"SELECT COUNT(*) FROM {source} h WHERE " + filters, arguments).fetchone()[0]
            rows = database.execute(f"SELECT platform, work_id, title, status, updated_at, error, folder, quality FROM {source} h WHERE " + filters +
                                    " ORDER BY updated_at DESC, platform, work_id LIMIT ? OFFSET ?",
                                    (*arguments, limit, offset)).fetchall()
            result[status] = {"total": count, "items": [dict(row) for row in rows], "more": offset + limit < count}
    return result


def link_works(database, platform, author_id, items):
    database.executemany("""
        INSERT OR IGNORE INTO work_authors VALUES (?, ?, ?)
    """, ((platform, str(item["id"]), author_id) for item in items if isinstance(item, dict) and item.get("id")))


def bind_author(root, platform, creator, items, proposed_folder):
    author_id = str(creator["id"])
    root_key = str(Path(root).resolve())
    with closing(connect()) as database, database:
        database.execute("BEGIN IMMEDIATE")
        link_works(database, platform, author_id, items)
        current = database.execute("""
            SELECT folder FROM author_folders WHERE platform=? AND author_id=? AND archive_root=?
        """, (platform, author_id, root_key)).fetchone()
        if current:
            return current["folder"]
        folder = proposed_folder
        digest = hashlib.sha256(f"{platform}:{author_id}".encode()).hexdigest()[:12]
        claimed = database.execute("SELECT folder FROM author_folders WHERE archive_root=?", (root_key,)).fetchall()
        if folder.casefold() in {row["folder"].casefold() for row in claimed}:
            folder += "--" + digest
        database.execute("INSERT INTO author_folders VALUES (?, ?, ?, ?)", (platform, author_id, root_key, folder))
        return folder


def known_roots():
    """Return every archive root ever recorded in history, author folders, or imports."""
    try:
        with closing(connect()) as database:
            rows = database.execute("""
                SELECT archive_root AS path FROM history_jobs WHERE archive_root != ''
                UNION
                SELECT archive_root AS path FROM author_folders WHERE archive_root != ''
                UNION
                SELECT path FROM imported_roots WHERE path != ''
                UNION
                SELECT path FROM history_jobs_roots WHERE path != ''
            """).fetchall()
            return [Path(row["path"]).resolve() for row in rows if row["path"]]
    except (OSError, sqlite3.Error):
        return []


def import_author_links(roots):
    for root in dict.fromkeys(Path(root).resolve() for root in roots):
        if not (root / "jobs").is_dir():
            continue
        try:
            with closing(connect()) as database, database:
                if database.execute("SELECT 1 FROM imported_author_roots WHERE path=?", (str(root),)).fetchone():
                    continue
                for path in (root / "jobs").glob("batch-*/manifest.json"):
                    try:
                        manifest = json.loads(path.read_text(encoding="utf-8"))
                        creator = manifest.get("creator") or {}
                        platform = manifest.get("platform")
                        key = str(creator.get("id") or "")
                        if key and platform in PLATFORMS and not creator.get("single_work"):
                            link_works(database, platform, key, (manifest.get("inventory") or {}).get("items") or [])
                    except (OSError, ValueError, TypeError, AttributeError):
                        continue
                database.execute("INSERT OR IGNORE INTO imported_author_roots VALUES (?)", (str(root),))
        except (OSError, sqlite3.Error):
            logging.exception("author history import failed")


def import_archives(roots):
    """Import terminal results and locate their original work and batch manifests."""
    with closing(connect()) as database:
        prior_roots = [row[0] for row in database.execute("SELECT path FROM imported_roots")]
    for root in dict.fromkeys(Path(root).resolve() for root in [*roots, *prior_roots]):
        try:
            if not (root / "jobs").is_dir():
                continue
            with closing(connect()) as database:
                if database.execute("SELECT 1 FROM history_jobs_roots WHERE path=?", (str(root),)).fetchone():
                    continue
            batches = {}
            for path in sorted((root / "jobs").glob("batch-*/manifest.json")):
                try:
                    batch = json.loads(path.read_text(encoding="utf-8"))
                    for item in (batch.get("inventory") or {}).get("items") or []:
                        if item.get("child_job_id"):
                            batches[item["child_job_id"]] = path.parent.name
                except (OSError, ValueError, AttributeError):
                    continue
            rows = []
            for path in (root / "jobs").glob("content-*/manifest.json"):
                try:
                    child = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(child, dict):
                        continue
                    job_id = path.parent.name
                    row = from_manifest(root, child, child_job=job_id, batch_job=batches.get(job_id, ""))
                    if row:
                        rows.append(row)
                except (OSError, ValueError, TypeError):
                    continue
            save(rows)
            with closing(connect()) as database, database:
                database.execute("INSERT OR IGNORE INTO imported_roots VALUES (?)", (str(root),))
                database.execute("INSERT OR IGNORE INTO history_jobs_roots VALUES (?)", (str(root),))
        except (OSError, sqlite3.Error):
            logging.exception("history import failed")
