"""Use completed media and recorded source evidence for the original-quality badge."""
from contextlib import closing
from functools import lru_cache
import json
from pathlib import Path

MEDIA_ROLES = {"video", "image", "live_motion", "live_photo_motion", "audio"}
QUALITY_BLOCKERS = {"api_fallback", "preview_fallback", "full_size_render"}


def media_outputs(record):
    return [row for row in record.get("outputs", ()) if row["role"] in MEDIA_ROLES]


def same_dimensions(left, right):
    return all(left) and all(right) and sorted(left) == sorted(right)


def verified_video(record):
    if "source_match" in record:
        return record["source_match"]
    return bool(record.get("original") and record.get("width") and record.get("height")
                and not record.get("non_highest"))


def verified_work(child):
    if (child.get("status") != "completed" or child.get("non_highest")
            or (child.get("media_ids") and not child.get("media_selection_complete"))
            or child.get("image_quality") in QUALITY_BLOCKERS):
        return False
    outputs = media_outputs(child)
    if not outputs:
        return False
    video = child.get("video_quality") or {}
    references = iter(
        (row.get("width") or 0, row.get("height") or 0)
        for row in (child.get("douyin_media") or {}).get("items", ())
        if row.get("has_still") or row.get("image_urls")
    )
    for row in outputs:
        if row.get("non_highest"):
            return False
        role = row["role"]
        if role == "image":
            if "source_match" in row:
                verified = row["source_match"]
            elif child.get("platform") == "xiaohongshu":
                verified = row.get("source_quality") == "cdn_object" and row.get("width") and row.get("height")
            else:
                verified = same_dimensions((row.get("width") or 0, row.get("height") or 0),
                                           next(references, (0, 0)))
        else:
            verified = verified_video(video if role == "video" and "source_match" not in row else row)
        if not verified:
            return False
    return True


@lru_cache(maxsize=2048)
def _read_record(path, modified):
    return json.loads(path.read_text(encoding="utf-8"))


def _same_size(path, size):
    try:
        return bool(size) and path.stat().st_size == size
    except FileNotFoundError:
        return False


def add_legacy_badges(items, platform, connect, job_id_pattern):
    pending = {str(item["id"]): item for item in items if item.get("_downloaded")
               and not any(label in item.get("archive_quality", "") for label in ("原画质", "非最高画质", "部分下载"))}
    if not pending:
        return
    with closing(connect()) as database:
        sources = database.execute(
            """SELECT work_id, archive_root, child_job FROM history_jobs
               JOIN history USING (platform, work_id) WHERE platform=? AND status='success'""",
            (platform,),
        ).fetchall()
    for work_id, root_name, child_job in sources:
        if work_id not in pending or not root_name or not job_id_pattern.fullmatch(child_job):
            continue
        root = Path(root_name)
        manifest = root / "jobs" / child_job / "manifest.json"
        try:
            child = _read_record(manifest, manifest.stat().st_mtime_ns)
        except (FileNotFoundError, ValueError):
            continue
        if verified_work(child) and all(
            _same_size(root / output["path"], output["bytes"]) for output in media_outputs(child)
        ):
            item = pending[work_id]
            item["archive_quality"] = " · ".join(filter(None, (item.get("archive_quality"), "原画质")))

