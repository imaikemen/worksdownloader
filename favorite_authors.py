"""Persist followed profiles and compare their works with the last inventory."""

from concurrent.futures import FIRST_COMPLETED, wait
from datetime import datetime, timezone
import json
import logging
import threading

import app_runtime


PLATFORMS = {"douyin", "xiaohongshu", "weibo"}
LOCK = threading.RLock()
checking = False
CHECK_TIMEOUT = "更新检查超时（60 秒）。请重试或更换网络。"
AUTH_ERRORS = {"reauthentication_required", "cookie_import_failed", "platform_cookies_missing",
               "xiaohongshu_browser_invalid", "douyin_empty_response"}


def _path():
    return app_runtime.DATA_DIR / "favorite-authors.json"


def _read():
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _key(platform, creator):
    return f"{platform}:{creator['id']}"


def _timestamp(value):
    if not value:
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdecimal()):
            number = float(value)
            return number / 1000 if number >= 1e12 else number
        date = datetime.fromisoformat(value)
        return date.timestamp() if date.tzinfo else date.replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _summary(baseline_ids, items, read_published=None):
    known = set(baseline_ids)
    new = {str(row["id"]): row for row in items if row.get("id") and str(row["id"]) not in known}
    dates = [_timestamp(item.get("published_at")) for item in new.values()]
    missing = [index for index, value in enumerate(dates) if value is None]
    if read_published and len(missing) <= 2:
        rows = list(new.values())
        for index in missing:
            dates[index] = _timestamp(read_published(rows[index]))
    return {"new_count": len(new), "latest_at": max(dates) if dates and all(dates) else None}


def _profile(platform, creator, previous=None):
    profile = {field: str(creator.get(field) or (previous or {}).get(field) or "")
               for field in ("id", "name", "account", "avatar", "url", "browser")}
    return {"key": _key(platform, creator), "platform": platform, **profile}


def _sort_key(row):
    pinned = bool(row.get("pinned"))
    order = row.get("order")
    has_order = isinstance(order, (int, float)) and not isinstance(order, bool)
    captured = _timestamp(row.get("captured_at")) or 0.0
    return (0 if pinned else 1, 0 if has_order else 1, float(order) if has_order else 0.0, -captured, str(row.get("key") or ""))


def _sorted_rows(records):
    return sorted(records.values(), key=_sort_key)


def _usable_inventory(creator, inventory, platform=None):
    items = (inventory or {}).get("items") or []
    return bool((inventory or {}).get("complete") or (items and (creator or {}).get("access") != "public"))


def _advance(records, key, platform, creator, inventory, job_id):
    previous = records.get(key) or {}
    updated = _profile(platform, creator, previous)
    updated["note"] = previous.get("note", "")
    updated["pinned"] = bool(previous.get("pinned", False))
    if "order" in previous and isinstance(previous.get("order"), (int, float)) and not isinstance(previous.get("order"), bool):
        updated["order"] = previous["order"]
    elif key not in records:
        unpinned_orders = [row["order"] for row in records.values()
                           if not row.get("pinned") and isinstance(row.get("order"), (int, float)) and not isinstance(row.get("order"), bool)]
        if unpinned_orders:
            updated["order"] = min(unpinned_orders) - 1
    current_ids = {str(item["id"]) for item in inventory["items"] if item.get("id")}
    if not inventory.get("complete") and previous.get("baseline_ids"):
        current_ids |= {str(x) for x in previous.get("baseline_ids") if x}
    updated.update(job_id=job_id, captured_at=inventory["captured_at"],
                   baseline_ids=sorted(current_ids),
                   new_count=0, latest_at=None, checked_at=None, error="")
    records[key] = updated
    app_runtime.atomic_json(_path(), records)


def keep(job_id, manifest, browser=None):
    platform = (manifest or {}).get("platform")
    creator = (manifest or {}).get("creator") or {}
    inventory = (manifest or {}).get("inventory") or {}
    if (platform not in PLATFORMS or not creator.get("id") or creator.get("single_work")
            or not creator.get("url") or not _usable_inventory(creator, inventory, platform)):
        raise ValueError("请先成功盘点抖音、小红书或微博的作者主页。")
    key = _key(platform, creator)
    with LOCK:
        records = _read()
        if key not in records:
            _advance(records, key, platform, {**creator, "browser": creator.get("browser") or browser},
                     inventory, job_id)
    return key


def record_inventory(job_id, platform, creator, read_manifest, browser=None):
    if platform not in PLATFORMS or not creator.get("id") or creator.get("single_work"):
        return
    key = _key(platform, creator)
    with LOCK:
        records = _read()
        previous = records.get(key)
        if previous and previous["job_id"] != job_id:
            manifest = read_manifest(job_id) or {}
            inventory = manifest.get("inventory") or {}
            if _usable_inventory(creator, inventory, platform) and inventory.get("captured_at", "") >= previous.get("captured_at", ""):
                _advance(records, key, platform, {**creator, "browser": creator.get("browser") or browser},
                         inventory, job_id)


def remove(key):
    with LOCK:
        records = _read()
        if key in records:
            del records[key]
            app_runtime.atomic_json(_path(), records)


def set_note(key, note):
    if not isinstance(note, str) or len(note) > 120:
        raise ValueError("备注最多 120 个字符。")
    with LOCK:
        records = _read()
        if key not in records:
            raise ValueError("该作者已从常看列表移除。")
        records[key]["note"] = note.strip()
        app_runtime.atomic_json(_path(), records)


def set_pin(key, pinned=None):
    with LOCK:
        records = _read()
        if key not in records:
            raise ValueError("该作者已从常看列表移除。")
        ordered = _sorted_rows(records)
        target = records[key]
        new_state = (not bool(target.get("pinned"))) if pinned is None else bool(pinned)
        if bool(target.get("pinned")) == new_state:
            return
        pinned_rows = [row for row in ordered if row["key"] != key and row.get("pinned")]
        unpinned_rows = [row for row in ordered if row["key"] != key and not row.get("pinned")]
        target["pinned"] = new_state
        if new_state:
            pinned_rows.append(target)
        else:
            unpinned_rows.insert(0, target)
        for index, row in enumerate((*pinned_rows, *unpinned_rows)):
            row["order"] = index
        app_runtime.atomic_json(_path(), records)


def reorder(keys):
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise ValueError("排序列表格式无效。")
    with LOCK:
        records = _read()
        if not records:
            return
        ordered = _sorted_rows(records)
        seen = set()
        requested = []
        for k in keys:
            if k in records and k not in seen:
                requested.append(records[k])
                seen.add(k)
        remaining = [row for row in ordered if row["key"] not in seen]
        combined = [*requested, *remaining]
        pinned_rows = [row for row in combined if row.get("pinned")]
        unpinned_rows = [row for row in combined if not row.get("pinned")]
        for index, row in enumerate((*pinned_rows, *unpinned_rows)):
            row["order"] = index
        app_runtime.atomic_json(_path(), records)


def get_avatar_info(key):
    with LOCK:
        row = _read().get(str(key or ""))
        if not row:
            return None
        return str(row.get("platform") or ""), str(row.get("avatar") or "")


def view():
    with LOCK:
        values = _sorted_rows(_read())
        fields = ("key", "platform", "id", "note", "name", "account", "avatar", "url", "new_count",
                  "latest_at", "checked_at", "error")
        return {
            "authors": [
                {**{field: row.get(field, "") for field in fields}, "pinned": bool(row.get("pinned", False))}
                for row in values
            ],
            "checking": checking,
        }


def start(scan, read_published, browser=None):
    global checking
    with LOCK:
        if checking:
            return
        records = list(_read().values())
        if not records:
            return
        checking = True
        budget = app_runtime.TimeBudget(app_runtime.CHECK_SECONDS)
        threading.Thread(target=_check_all, args=(records, scan, read_published, browser, budget), daemon=True).start()


def _scan_one(previous, scan, read_published, browser, budget):
    token = app_runtime.favorite_baseline.set(previous.get("baseline_ids", ()))
    try:
        return _scan_profile(previous, scan, read_published, browser, budget)
    finally:
        app_runtime.favorite_baseline.reset(token)


def _scan_profile(previous, scan, read_published, browser, budget):
    selected_browser = previous.get("browser") or browser or "firefox"
    try:
        try:
            creator, items = scan(previous["platform"], previous["url"], selected_browser)
        except Exception as exc:
            if getattr(exc, "code", None) not in AUTH_ERRORS or not browser or browser == selected_browser:
                raise
            selected_browser = browser
            creator, items = scan(previous["platform"], previous["url"], selected_browser)
        app_runtime.operation_timeout()
        if not creator.get("id") or _key(previous["platform"], creator) != previous["key"]:
            return {"error": "作者身份已变化，请重新盘点作者主页。"}

        def read_date(item):
            seconds = min(4, budget.remaining() - 1)
            if not read_published or seconds <= 0:
                return None
            try:
                return app_runtime.TimeBudget(seconds).call(read_published, previous["platform"], item, selected_browser)
            except TimeoutError:
                return None

        if creator.get("name") == creator["id"]:
            creator = {**creator, "name": previous.get("name")}
        creator = {**creator, "browser": selected_browser}
        return {**_profile(previous["platform"], creator, previous),
                **_summary(previous["baseline_ids"], items, read_date), "error": ""}
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        logging.warning("favorite author check failed platform=%s code=%s", previous["platform"], code)
        if not budget.remaining() or isinstance(exc, TimeoutError) or code in {"network_timeout", "xiaohongshu_detail_timeout"}:
            return {"error": CHECK_TIMEOUT}
        if code in AUTH_ERRORS:
            return {"error": "无法读取登录态。请确认所选浏览器能打开作者主页，再重新检查。"}
        return {"error": "更新检查失败。请确认浏览器能打开作者主页，或更换网络后重试。"}


def _commit_checks(results):
    if not results:
        return
    with LOCK:
        current = _read()
        changed = False
        for previous, result in results:
            entry = current.get(previous["key"])
            if entry and entry["job_id"] == previous["job_id"]:
                entry.update(result, checked_at=datetime.now(timezone.utc).isoformat())
                changed = True
        if changed:
            app_runtime.atomic_json(_path(), current)


def _check_all(records, scan, read_published, browser, budget=None):
    global checking
    budget = budget or app_runtime.TimeBudget(app_runtime.CHECK_SECONDS)
    waiting = iter(records)
    pending = {}

    def submit_next():
        previous = next(waiting, None)
        if previous:
            future = app_runtime.submit_operation(budget, _scan_one, previous, scan, read_published, browser, budget)
            pending[future] = previous

    try:
        for _ in range(4):
            submit_next()
        while pending:
            done, _ = wait(pending, timeout=budget.remaining(), return_when=FIRST_COMPLETED)
            if not done:
                break
            checked = []
            for future in done:
                previous = pending.pop(future)
                try:
                    result = future.result()
                except TimeoutError:
                    result = {"error": CHECK_TIMEOUT}
                checked.append((previous, result))
                if budget.remaining():
                    submit_next()
            _commit_checks(checked)
        _commit_checks([(previous, {"error": CHECK_TIMEOUT}) for previous in (*pending.values(), *waiting)])
    finally:
        for future in pending:
            future.cancel()
        with LOCK:
            checking = False
