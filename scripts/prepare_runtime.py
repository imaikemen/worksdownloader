"""Prepare third-party runtime tools (FFmpeg, Node.js, @lucasygu/redbook) for reproducible builds.

Downloads official upstream release archives on Windows (used by both GitHub Actions CI
and local builds) so large third-party binaries never need to be committed into Git history.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
NODE_VERSION = os.environ.get("NODE_VERSION", "22.14.0")
FFMPEG_ZIP_URL = os.environ.get(
    "FFMPEG_ZIP_URL",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
)
REDBOOK_PKG = os.environ.get("REDBOOK_PKG", "@lucasygu/redbook")


def _download(url: str, dest: Path) -> None:
    print(f"Downloading {url} -> {dest.name}")
    req = urllib.request.Request(url, headers={"User-Agent": "WorksAppreciationDownloader-CI/1.0"})
    with urllib.request.urlopen(req, timeout=180) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh)


def ensure_ffmpeg(tmp: Path) -> None:
    ffmpeg = RUNTIME / "ffmpeg.exe"
    ffprobe = RUNTIME / "ffprobe.exe"
    if ffmpeg.is_file() and ffprobe.is_file():
        print("FFmpeg already present in runtime/.")
        return
    archive = tmp / "ffmpeg.zip"
    _download(FFMPEG_ZIP_URL, archive)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            name = Path(member).name.lower()
            if name in {"ffmpeg.exe", "ffprobe.exe"}:
                target = RUNTIME / name
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    if not (ffmpeg.is_file() and ffprobe.is_file()):
        raise RuntimeError("Failed to extract ffmpeg.exe / ffprobe.exe")
    print("Prepared runtime/ffmpeg.exe and runtime/ffprobe.exe")


def ensure_node(tmp: Path) -> None:
    node_exe = RUNTIME / "node.exe"
    if node_exe.is_file():
        print("Node.js already present in runtime/.")
        return
    url = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip"
    archive = tmp / "node.zip"
    _download(url, archive)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            if member.endswith("/node.exe"):
                with zf.open(member) as src, node_exe.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                break
    if not node_exe.is_file():
        raise RuntimeError("Failed to extract node.exe")
    print("Prepared runtime/node.exe")


def _patch_redbook_cli(cli_path: Path) -> None:
    """Apply graceful --max-duration stop and faster default pagination delay to redbook cli.js."""
    if not cli_path.is_file():
        return
    text = cli_path.read_text(encoding="utf-8")
    changed = False
    if "--max-duration" not in text and ".option('--delay <ms>'" in text:
        text = text.replace(
            ".option('--delay <ms>', 'Delay between pages in ms', '3000')",
            ".option('--delay <ms>', 'Delay between pages in ms', '450')\n  .option('--max-duration <ms>', 'Max duration in ms before stopping pagination', '0')",
        )
        changed = True
    if changed:
        cli_path.write_text(text, encoding="utf-8")
        print(f"Patched {cli_path}")


def ensure_redbook() -> None:
    rb_dir = RUNTIME / "redbook"
    cli_js = rb_dir / "node_modules" / "@lucasygu" / "redbook" / "dist" / "cli.js"
    if cli_js.is_file():
        print("@lucasygu/redbook already present in runtime/redbook/.")
        return
    rb_dir.mkdir(parents=True, exist_ok=True)
    pkg_json = rb_dir / "package.json"
    if not pkg_json.is_file():
        pkg_json.write_text(json.dumps({"name": "redbook-runtime", "private": True}, indent=2), encoding="utf-8")
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("npm is required on PATH to install @lucasygu/redbook")
    subprocess.run(
        [npm, "install", "--omit=dev", "--no-audit", "--no-fund", REDBOOK_PKG],
        cwd=str(rb_dir),
        check=True,
    )
    _patch_redbook_cli(cli_js)
    print("Prepared runtime/redbook")


def main() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ensure_ffmpeg(tmp)
        ensure_node(tmp)
    ensure_redbook()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
