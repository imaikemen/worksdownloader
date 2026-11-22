"""Stable desktop paths, atomic settings and redacted diagnostics."""
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from contextvars import ContextVar
from pathlib import Path

import bundled_runtime

APP_NAME = '作品鉴赏downloader'
VERSION = '1.0.1'
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('WORKS_ASSISTANT_DATA') or Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'WorksAppreciationDownloader')
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / 'settings.json'
CHECK_SECONDS = 60
DOWNLOAD_IDLE_SECONDS = 60
_budget = ContextVar('operation_budget', default=None)
favorite_baseline = ContextVar('favorite_baseline', default=())
_operation_slots = threading.BoundedSemaphore(12)


class OperationTimeout(TimeoutError):
    code = 'operation_timeout'


class TimeBudget:
    def __init__(self, seconds):
        self.seconds = seconds
        self.expires = time.monotonic() + seconds
        self.lock = threading.Lock()

    def remaining(self):
        with self.lock:
            return max(0, self.expires - time.monotonic())

    def progress(self):
        with self.lock:
            now = time.monotonic()
            if now < self.expires:
                self.expires = now + self.seconds

    def call(self, function, *args, **kwargs):
        token = _budget.set(self)
        try:
            operation_timeout()
            return function(*args, **kwargs)
        finally:
            _budget.reset(token)


def operation_timeout(ceiling=None):
    budget = _budget.get()
    if budget is None:
        return ceiling
    remaining = budget.remaining()
    if not remaining:
        raise OperationTimeout('操作超时。')
    return min(remaining, ceiling) if ceiling is not None else remaining


def current_budget():
    return _budget.get()


def submit_operation(budget, function, *args, **kwargs):
    result = Future()
    if not _operation_slots.acquire(blocking=False):
        result.set_exception(OperationTimeout('上一次的网络请求尚未结束，请稍后重试。'))
        return result

    def execute():
        try:
            if result.set_running_or_notify_cancel():
                try:
                    result.set_result(budget.call(function, *args, **kwargs))
                except BaseException as exc:
                    result.set_exception(exc)
        finally:
            _operation_slots.release()

    threading.Thread(target=execute, daemon=True).start()
    return result

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def read_settings():
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}

def redact(text):
    text = re.sub(r'https?://[^\s<>\"\']+', lambda m: m[0].split('?')[0] + ('?[REDACTED]' if '?' in m[0] else ''), str(text))
    return re.sub(r'(?i)((?:cookie|authorization|web_session|a1|x-s|x-t)\s*[=:]\s*)[^\r\n]+', r'\1[REDACTED]', text)

class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def configure_dependencies():
    bundled = Path(__file__).resolve().parent / 'runtime'
    bundled_runtime.configure(bundled if bundled.is_dir() else APP_DIR / 'runtime')


configure_dependencies()
