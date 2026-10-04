"""上传区（inbox）：对话窗口上传的文件落地于此，并纳入 Agent 沙箱（可读/可写/可执行）。

安全：解压时做 zip-slip/tar 路径逃逸防护（绝对路径与 .. 条目一律跳过）。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from .config import GNA_HOME

INBOX = GNA_HOME / "inbox"

ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2"}


def is_archive(name: str) -> bool:
    return Path(name).suffix.lower() in ARCHIVE_EXTS or name.lower().endswith((".tar.gz", ".tar.bz2"))


def save_upload(name: str, data: bytes) -> Path:
    """把上传内容写入 inbox（时间戳前缀防覆盖），返回落地路径。"""
    INBOX.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", Path(name).name) or f"upload-{int(time.time())}"
    dest = INBOX / f"{datetime.now().strftime('%H%M%S')}_{safe}"
    dest.write_bytes(data)
    return dest


def _safe_target(dest_dir: Path, entry: str) -> Path | None:
    """解压目标校验：拒绝绝对路径与 .. 逃逸（zip-slip）。"""
    if not entry or entry.startswith(("/", "\\")) or ":" in entry.split("/")[0]:
        return None
    target = (dest_dir / entry).resolve()
    try:
        target.relative_to(dest_dir.resolve())
    except ValueError:
        return None
    return target


def extract_archive(path: Path) -> tuple[list[str], list[str]]:
    """解压到 inbox/<名称>_extracted/，返回 (解压出的相对路径, 跳过的危险条目)。"""
    import tarfile
    import zipfile

    path = Path(path)
    dest_dir = path.parent / f"{path.stem}_extracted"
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    skipped: list[str] = []

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                target = _safe_target(dest_dir, info.filename)
                if target is None:
                    skipped.append(info.filename)
                    continue
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
                extracted.append(target.relative_to(dest_dir).as_posix())
    elif path.suffix.lower() in (".tar", ".tgz") or path.name.lower().endswith((".tar.gz", ".tar.bz2")):
        with tarfile.open(path) as t:
            for m in t.getmembers():
                target = _safe_target(dest_dir, m.name)
                if target is None or (m.issym() or m.islnk()):
                    skipped.append(m.name)
                    continue
                if not m.isfile():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(t.extractfile(m).read())
                extracted.append(target.relative_to(dest_dir).as_posix())
    else:
        raise ValueError(f"不支持的压缩格式：{path.name}")
    return extracted, skipped
