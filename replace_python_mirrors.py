#!/usr/bin/env python3
"""Batch-rewrite public Python package indexes to Aliyun mirror.

This script scans a repository recursively and rewrites only known public
PyPI-style indexes. Private/internal indexes and high-risk constructs such as
extra-index-url, find-links, wheel URLs, and VCS dependencies are preserved.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


ALIYUN_SIMPLE_URL = "https://mirrors.aliyun.com/pypi/simple/"
ALIYUN_UBUNTU_APT_URL = "https://mirrors.aliyun.com/ubuntu/"
ALIYUN_DEBIAN_APT_URL = "https://mirrors.aliyun.com/debian/"
DEFAULT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
}

# Replace only well-known public package index hosts. Unknown hosts are treated
# as potentially private and are left untouched.
KNOWN_PUBLIC_INDEX_HOSTS = {
    "pypi.org",
    "pypi.python.org",
    "test.pypi.org",
    "pypi.tuna.tsinghua.edu.cn",
    "pypi.mirrors.ustc.edu.cn",
    "mirrors.cloud.tencent.com",
    "repo.huaweicloud.com",
    "mirrors.bfsu.edu.cn",
    "mirrors.163.com",
    "pypi.douban.com",
}

IGNORE_LINE_HINTS = (
    "from ",
    " import ",
)

RISKY_MARKERS = {
    "extra-index-url": "contains extra-index-url",
    "--extra-index-url": "contains extra-index-url",
    "find-links": "contains find-links",
    "--find-links": "contains find-links",
    "git+": "contains VCS dependency",
    ".whl": "contains wheel URL",
    ".tar.gz": "contains source archive URL",
    ".zip": "contains archive URL",
}

INDEX_OPTION_PATTERNS = (
    re.compile(
        r"(?P<prefix>(?:^|[\s(])--index-url(?:\s+|=))"
        r"(?P<quote>['\"]?)(?P<url>https?://[^\s'\"\\)]+)(?P=quote)"
    ),
    re.compile(
        r"(?P<prefix>(?:^|[\s(])-i\s+)"
        r"(?P<quote>['\"]?)(?P<url>https?://[^\s'\"\\)]+)(?P=quote)"
    ),
)

ENV_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<prefix>\b(?:export\s+)?(?:PIP_INDEX_URL|UV_INDEX_URL|UV_DEFAULT_INDEX)\b\s*[:=]\s*)"
    r"(?P<quote>['\"]?)(?P<url>https?://[^\s'\",]+)(?P=quote)"
)

CONFIG_INDEX_PATTERN = re.compile(
    r"(?P<prefix>\bindex-url\b\s*[:=]\s*)"
    r"(?P<quote>['\"]?)(?P<url>https?://[^\s'\",]+)(?P=quote)"
)

TOML_URL_PATTERN = re.compile(
    r"(?P<prefix>\burl\b\s*=\s*)(?P<quote>['\"])(?P<url>https?://[^'\"]+)(?P=quote)"
)

SECTION_PATTERN = re.compile(r"^\s*\[(?P<section>[^\]]+)\]\s*$")
DOUBLE_SECTION_PATTERN = re.compile(r"^\s*\[\[(?P<section>[^\]]+)\]\]\s*$")
APT_COMMAND_PATTERN = re.compile(r"\b(?:apt-get|apt)\s+(?:update|install)\b")
UV_COMMAND_PATTERN = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<env>(?:[A-Za-z_][A-Za-z0-9_]*=(?:[^\s\"']+|\"[^\"]*\"|'[^']*')\s+)*)"
    r"(?P<sudo>sudo\s+)?"
    r"(?P<cmd>uvx|uv)\b"
)


@dataclass
class Decision:
    action: str
    reason: str
    new_url: str | None = None


@dataclass
class FileResult:
    path: Path
    replacements: int = 0
    skip_reasons: list[str] | None = None

    def __post_init__(self) -> None:
        if self.skip_reasons is None:
            self.skip_reasons = []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rewrite public pip/uv indexes to the Aliyun PyPI mirror."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Repository root to scan. Defaults to the current directory.",
    )
    parser.add_argument(
        "--mirror-url",
        default=ALIYUN_SIMPLE_URL,
        help=f"Replacement mirror URL. Defaults to {ALIYUN_SIMPLE_URL}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print skipped matches and unchanged candidate files.",
    )
    return parser


def normalize_mirror_url(url: str) -> str:
    return url.rstrip("/") + "/"


def normalize_docker_image_ref(image_ref: str) -> str:
    image = image_ref.strip()
    if " as " in image.lower():
        image = re.split(r"\s+as\s+", image, flags=re.IGNORECASE)[0]
    return image.lower()


def detect_docker_apt_distro(original: str) -> str | None:
    for line in original.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.upper().startswith("FROM "):
            continue
        image_ref = normalize_docker_image_ref(stripped[5:])
        if "ubuntu" in image_ref:
            return "ubuntu"
        if "debian" in image_ref:
            return "debian"
    return None


def build_apt_rewrite_prefix(distro: str) -> str:
    if distro == "ubuntu":
        return (
            "if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then "
            f"sed -i \"s|http://archive.ubuntu.com/ubuntu/|{ALIYUN_UBUNTU_APT_URL}|g; "
            f"s|https://archive.ubuntu.com/ubuntu/|{ALIYUN_UBUNTU_APT_URL}|g; "
            f"s|http://security.ubuntu.com/ubuntu/|{ALIYUN_UBUNTU_APT_URL}|g; "
            f"s|https://security.ubuntu.com/ubuntu/|{ALIYUN_UBUNTU_APT_URL}|g\" "
            "/etc/apt/sources.list.d/ubuntu.sources; fi; "
            "if [ -f /etc/apt/sources.list ]; then "
            f"sed -i \"s|http://archive.ubuntu.com/ubuntu/|{ALIYUN_UBUNTU_APT_URL}|g; "
            f"s|https://archive.ubuntu.com/ubuntu/|{ALIYUN_UBUNTU_APT_URL}|g; "
            f"s|http://security.ubuntu.com/ubuntu/|{ALIYUN_UBUNTU_APT_URL}|g; "
            f"s|https://security.ubuntu.com/ubuntu/|{ALIYUN_UBUNTU_APT_URL}|g\" "
            "/etc/apt/sources.list; fi && "
        )

    if distro == "debian":
        return (
            "if [ -f /etc/apt/sources.list.d/debian.sources ]; then "
            f"sed -i \"s|http://deb.debian.org/debian|{ALIYUN_DEBIAN_APT_URL.rstrip('/')}|g; "
            f"s|https://deb.debian.org/debian|{ALIYUN_DEBIAN_APT_URL.rstrip('/')}|g; "
            "s|http://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g; "
            "s|https://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g\" "
            "/etc/apt/sources.list.d/debian.sources; fi; "
            "if [ -f /etc/apt/sources.list ]; then "
            f"sed -i \"s|http://deb.debian.org/debian|{ALIYUN_DEBIAN_APT_URL.rstrip('/')}|g; "
            f"s|https://deb.debian.org/debian|{ALIYUN_DEBIAN_APT_URL.rstrip('/')}|g; "
            "s|http://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g; "
            "s|https://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g\" "
            "/etc/apt/sources.list; fi && "
        )

    raise ValueError(f"Unsupported distro for apt rewrite: {distro}")


def maybe_rewrite_dockerfile_apt_line(
    line: str,
    distro: str | None,
    original: str,
) -> tuple[str, int]:
    stripped = line.lstrip()
    if distro is None or not stripped.startswith("RUN "):
        return line, 0
    if "mirrors.aliyun.com/ubuntu" in original or "mirrors.aliyun.com/debian" in original:
        return line, 0
    match = APT_COMMAND_PATTERN.search(line)
    if not match:
        return line, 0
    if "mirrors.aliyun.com/" in line:
        return line, 0

    prefix = build_apt_rewrite_prefix(distro)
    updated = f"{line[:match.start()]}{prefix}{line[match.start():]}"
    return updated, 1


def maybe_prefix_uv_command(line: str, mirror_url: str) -> tuple[str, int]:
    stripped = line.lstrip()
    if not stripped or stripped.startswith(("#", "//", ";")):
        return line, 0
    if "UV_INDEX_URL" in line or "UV_DEFAULT_INDEX" in line:
        return line, 0
    if "--index-url" in line or "--index " in line or "--default-index" in line:
        return line, 0

    match = UV_COMMAND_PATTERN.match(line)
    if not match:
        return line, 0

    updated = (
        f"{match.group('indent')}{match.group('env')}{match.group('sudo') or ''}"
        f"UV_DEFAULT_INDEX={mirror_url} {match.group('cmd')}{line[match.end('cmd'):]}"
    )
    return updated, 1


def should_scan(path: Path) -> bool:
    name = path.name
    lower_name = name.lower()

    if name.startswith("Dockerfile"):
        return True
    if lower_name in {"pyproject.toml", "uv.toml", "pip.conf", "pip.ini"}:
        return True
    if lower_name.startswith("requirements") and lower_name.endswith(".txt"):
        return True
    if path.suffix.lower() in {".sh", ".bash", ".zsh", ".yaml", ".yml"}:
        return True
    return False


def iter_candidate_files(root: Path) -> Iterable[Path]:
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in DEFAULT_SKIP_DIRS and not dirname.startswith(".cursor")
        ]
        for filename in filenames:
            path = Path(current_root) / filename
            if should_scan(path):
                yield path


def is_likely_text(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    return b"\x00" not in chunk


def is_index_like_url(parsed_url) -> bool:
    path = (parsed_url.path or "").lower()
    if any(path.endswith(suffix) for suffix in (".whl", ".zip", ".tar.gz", ".tgz")):
        return False
    if "/simple" in path:
        return True
    return path in {"", "/"}


def is_private_or_internal_host(host: str) -> bool:
    host = host.lower()
    if host in {"localhost"}:
        return True
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        octets = [int(part) for part in host.split(".")]
        if octets[0] == 10:
            return True
        if octets[0] == 127:
            return True
        if octets[0] == 192 and octets[1] == 168:
            return True
        if octets[0] == 172 and 16 <= octets[1] <= 31:
            return True
        return False
    return host.endswith(
        (
            ".local",
            ".lan",
            ".corp",
            ".internal",
            ".intranet",
            ".home",
            ".cluster.local",
        )
    )


def decide_replacement(url: str, mirror_url: str) -> Decision:
    parsed = urlparse(url)
    host = parsed.hostname

    if parsed.scheme not in {"http", "https"} or not host:
        return Decision("skip", "not a valid http(s) URL")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return Decision("skip", "contains credentials")
    if is_private_or_internal_host(host):
        return Decision("skip", "private or internal host")

    normalized_mirror = normalize_mirror_url(mirror_url)
    normalized_url = url.rstrip("/") + "/"
    if normalized_url == normalized_mirror:
        return Decision("keep", "already uses Aliyun mirror")

    if host not in KNOWN_PUBLIC_INDEX_HOSTS:
        return Decision("skip", f"unknown host {host}")
    if not is_index_like_url(parsed):
        return Decision("skip", "URL does not look like an index")

    return Decision("replace", f"replace public host {host}", normalized_mirror)


def replace_pattern(
    line: str,
    pattern: re.Pattern[str],
    mirror_url: str,
    result: FileResult,
) -> tuple[str, int]:
    replacements = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal replacements
        url = match.group("url")
        decision = decide_replacement(url, mirror_url)
        if decision.action == "replace":
            replacements += 1
            return f"{match.group('prefix')}{match.group('quote')}{decision.new_url}{match.group('quote')}"
        if decision.action == "skip":
            result.skip_reasons.append(f"{result.path}: {decision.reason} -> {url}")
        return match.group(0)

    updated = pattern.sub(_sub, line)
    return updated, replacements


def has_risky_marker(line: str) -> str | None:
    lowered = line.lower()
    for marker, reason in RISKY_MARKERS.items():
        if marker in lowered:
            return reason
    return None


def process_file(path: Path, mirror_url: str, dry_run: bool) -> FileResult:
    result = FileResult(path=path)
    current_section = ""

    try:
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        original = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        result.skip_reasons.append(f"{path}: failed to read file ({exc})")
        return result

    lines = original.splitlines(keepends=True)
    dockerfile_apt_distro = (
        detect_docker_apt_distro(original) if path.name.startswith("Dockerfile") else None
    )
    updated_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        double_section = DOUBLE_SECTION_PATTERN.match(line)
        single_section = SECTION_PATTERN.match(line) if not double_section else None
        if double_section:
            current_section = double_section.group("section").strip()
        elif single_section:
            current_section = single_section.group("section").strip()

        if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith(";"):
            updated_lines.append(line)
            continue
        if all(hint in line for hint in IGNORE_LINE_HINTS) and "pip" not in line and "uv" not in line:
            updated_lines.append(line)
            continue

        risky_reason = has_risky_marker(line)
        if risky_reason and "http" in line:
            result.skip_reasons.append(f"{path}: {risky_reason} -> {stripped}")
            updated_lines.append(line)
            continue

        updated_line = line

        if path.name.startswith("Dockerfile"):
            updated_line, changed = maybe_rewrite_dockerfile_apt_line(
                updated_line,
                dockerfile_apt_distro,
                original,
            )
            result.replacements += changed

        updated_line, changed = maybe_prefix_uv_command(updated_line, mirror_url)
        result.replacements += changed

        for pattern in INDEX_OPTION_PATTERNS:
            updated_line, changed = replace_pattern(updated_line, pattern, mirror_url, result)
            result.replacements += changed

        updated_line, changed = replace_pattern(updated_line, ENV_ASSIGNMENT_PATTERN, mirror_url, result)
        result.replacements += changed

        updated_line, changed = replace_pattern(updated_line, CONFIG_INDEX_PATTERN, mirror_url, result)
        result.replacements += changed

        if path.name.lower() in {"pyproject.toml", "uv.toml"}:
            if current_section.startswith("tool.uv.index") or current_section.startswith("tool.poetry.source") or current_section.startswith("tool.pdm.source"):
                updated_line, changed = replace_pattern(updated_line, TOML_URL_PATTERN, mirror_url, result)
                result.replacements += changed

        updated_lines.append(updated_line)

    updated = "".join(updated_lines)
    if updated != original and not dry_run:
        path.write_text(updated, encoding="utf-8")
    return result


def print_summary(
    results: list[FileResult],
    dry_run: bool,
    verbose: bool,
) -> int:
    modified = [item for item in results if item.replacements > 0]
    skipped = [item for item in results if item.skip_reasons]
    total_replacements = sum(item.replacements for item in results)

    mode_label = "DRY-RUN" if dry_run else "APPLY"
    print(f"[{mode_label}] scanned {len(results)} candidate files")
    print(f"[{mode_label}] modified {len(modified)} files, {total_replacements} replacement(s)")

    if modified:
        print("\nModified files:")
        for item in modified:
            print(f"  - {item.path}: {item.replacements} replacement(s)")

    if skipped and verbose:
        print("\nSkipped matches:")
        for item in skipped:
            for reason in item.skip_reasons:
                print(f"  - {reason}")

    if verbose:
        unchanged = [item.path for item in results if item.replacements == 0]
        if unchanged:
            print("\nUnchanged candidate files:")
            for path in unchanged:
                print(f"  - {path}")

    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).resolve()

    if not root.exists():
        print(f"Root path does not exist: {root}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"Root path is not a directory: {root}", file=sys.stderr)
        return 2

    mirror_url = normalize_mirror_url(args.mirror_url)
    candidate_files = [path for path in iter_candidate_files(root) if is_likely_text(path)]
    results: list[FileResult] = []

    for path in candidate_files:
        result = process_file(path, mirror_url, dry_run=args.dry_run)
        results.append(result)

    return print_summary(results, dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
