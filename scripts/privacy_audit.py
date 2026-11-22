"""Automated repository privacy, license, and clean-source audit for CI and pre-release checks."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Patterns that must never appear in published source files.
# Built dynamically from fragments so this audit script itself stays clean.
_AI_TERMS = [
    "g" + "pt",
    "co" + "dex",
    "work" + "buddy",
    "deep" + "seek",
    "ge" + "mini",
    "anti" + "gravity",
    "ne" + "on",
    "cla" + "ude",
    "open" + "ai",
    "anth" + "ropic",
]
_PERSONAL_PATTERNS = [
    r"C:\\Users\\hp\b",
    r"C:/Users/hp\b",
    r"F:/作品提取",
    r"F:\\作品提取",
    r"WeChatArchive",
]
_FORBIDDEN_FILES = {
    "favorite-authors.json",
    "settings.json",
    "download-history.sqlite3",
    "x-graphql-ops.json",
    "x-rate-limit.json",
}
_REQUIRED_LICENSES = [
    "LICENSE",
    "THIRD_PARTY_LICENSES.md",
    "licenses/FFmpeg-LICENSE.txt",
    "licenses/Node.js-LICENSE.txt",
    "licenses/yt-dlp-LICENSE.txt",
    "licenses/redbook-LICENSE.txt",
]
_TEXT_EXTS = {
    ".py", ".html", ".js", ".css", ".md", ".txt", ".yml", ".yaml",
    ".spec", ".bat", ".iss", ".json", ".toml", ".cfg", ".ini",
}


def _candidate_files() -> list[Path]:
    try:
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        tracked = [ROOT / line.strip() for line in res.stdout.splitlines() if line.strip()]
        if tracked:
            return [p for p in tracked if p.is_file()]
    except Exception:
        pass
    out = []
    skip_dirs = {".git", "__pycache__", "runtime", "dist", "build", "node_modules"}
    for p in ROOT.rglob("*"):
        if any(part in skip_dirs for part in p.parts):
            continue
        if "vendor" in p.parts and "transparent-core" in p.parts:
            continue
        if p.is_file():
            out.append(p)
    return out


def main() -> int:
    errors: list[str] = []

    for rel in _REQUIRED_LICENSES:
        if not (ROOT / rel).is_file():
            errors.append(f"Missing required license/compliance file: {rel}")

    ai_regex = re.compile("|".join(re.escape(term) for term in _AI_TERMS), re.IGNORECASE)
    personal_regex = re.compile("|".join(_PERSONAL_PATTERNS), re.IGNORECASE)
    cookie_regex = re.compile(r"(SESSDATA|auth_token|SUB)=[A-Za-z0-9._%-]{16,}")

    for path in _candidate_files():
        rel = path.relative_to(ROOT).as_posix()
        if path.name in _FORBIDDEN_FILES or path.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".exe"}:
            errors.append(f"Forbidden runtime/personal file tracked: {rel}")
            continue
        if rel.startswith("licenses/") or rel == "LICENSE":
            continue
        if path.suffix.lower() not in _TEXT_EXTS and path.name not in {".gitignore"}:
            continue
        if rel == "scripts/privacy_audit.py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            errors.append(f"Unreadable file {rel}: {exc}")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if ai_regex.search(line):
                errors.append(f"Forbidden term match in {rel}:{lineno}")
            if personal_regex.search(line):
                errors.append(f"Hardcoded personal path in {rel}:{lineno}: {line.strip()[:80]}")
            if cookie_regex.search(line):
                errors.append(f"Possible leaked cookie/token in {rel}:{lineno}")

    if errors:
        print("PRIVACY/COMPLIANCE AUDIT FAILED:")
        for item in errors:
            print("  -", item)
        return 1
    print("PRIVACY/COMPLIANCE AUDIT PASSED (0 issues).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
