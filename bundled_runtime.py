"""Expand bundled media tools after the desktop opens."""
import json
import lzma
import os
import sys
import threading
from concurrent.futures import Future

_runtime = None
_layout = {}
_jobs = {}
_lock = threading.Lock()


def configure(runtime):
    global _runtime, _layout
    _runtime = runtime
    if not runtime.is_dir():
        return
    layout = runtime / "payload-map.json"
    if layout.is_file():
        _layout = json.loads(layout.read_text(encoding="utf-8"))
    for name in ("ffmpeg", "ffprobe", "node"):
        candidate = runtime / (name + ".exe")
        if _layout or candidate.is_file():
            os.environ.setdefault("WECHAT_" + name.upper(), str(candidate))
    os.environ["PATH"] = str(runtime) + os.pathsep + os.environ.get("PATH", "")
    cli = runtime / "redbook/node_modules/@lucasygu/redbook/dist/cli.js"
    if cli.is_file():
        os.environ.setdefault("WECHAT_REDBOOK_CLI", str(cli))


class ArchiveSlice:
    def __init__(self, archive, offset, size):
        self.archive = archive
        self.remaining = size
        archive.seek(offset)

    def read(self, amount=-1):
        if amount < 0:
            amount = self.remaining
        data = self.archive.read(min(self.remaining, amount))
        self.remaining -= len(data)
        return data


def _expand(group):
    offset, size, files = _layout[group]
    with open(sys.executable, "rb") as archive:
        with lzma.LZMAFile(ArchiveSlice(archive, offset, size)) as unpacked:
            for name, remaining in files:
                with (_runtime / name).open("wb") as output:
                    while remaining:
                        chunk = unpacked.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise EOFError(f"组件数据不完整：{name}")
                        output.write(chunk)
                        remaining -= len(chunk)
            if unpacked.read(1):
                raise ValueError("组件包长度不符。")


def _schedule(group):
    with _lock:
        if group not in _jobs:
            job = _jobs[group] = Future()

            def expand():
                try:
                    _expand(group)
                except (OSError, EOFError, ValueError, lzma.LZMAError) as exc:
                    job.set_exception(exc)
                else:
                    job.set_result(None)

            threading.Thread(target=expand, name="unpack-" + group, daemon=True).start()
        return _jobs[group]


def ensure(group):
    if _layout:
        from app_runtime import operation_timeout
        _schedule(group).result(timeout=operation_timeout())


def warm():
    jobs = [_schedule(group) for group in _layout]
    for job in jobs:
        if job.done():
            job.result()
    return all(job.done() for job in jobs)

