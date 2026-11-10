import os
import time
import shutil
from PySide6.QtCore import QThread, Signal
from ..utils.file_utils import get_app_base_path

CACHE_SUBDIRS = [
    "media_cache",
    "post_thumbnails",
    "comments",
    "avatars",
    "banners"
]


def get_base_cache_dir(app_base_dir: str = None) -> str:
    """Returns the absolute path to the main application cache directory."""
    if not app_base_dir:
        app_base_dir = get_app_base_path()
    return os.path.join(app_base_dir, "cache")


def format_size_str(bytes_count: int) -> str:
    """Formats raw bytes into a human-readable string (KB, MB, GB)."""
    if bytes_count < 1024:
        return f"{bytes_count} B"
    elif bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f} KB"
    elif bytes_count < 1024 * 1024 * 1024:
        return f"{bytes_count / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_count / (1024 * 1024 * 1024):.2f} GB"


def get_cache_size_info(app_base_dir: str = None) -> dict:
    """
    Scans the cache directories and returns a dictionary with total bytes,
    file counts, and subfolder breakdown.
    """
    base_dir = get_base_cache_dir(app_base_dir)
    total_bytes = 0
    total_files = 0
    breakdown = {}

    for sub in CACHE_SUBDIRS:
        sub_path = os.path.join(base_dir, sub)
        sub_bytes = 0
        sub_files = 0
        if os.path.isdir(sub_path):
            try:
                for root, _, files in os.walk(sub_path):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            sz = os.path.getsize(fp)
                            sub_bytes += sz
                            sub_files += 1
                        except OSError:
                            pass
            except OSError:
                pass
        breakdown[sub] = {"bytes": sub_bytes, "files": sub_files}
        total_bytes += sub_bytes
        total_files += sub_files

    return {
        "total_bytes": total_bytes,
        "total_files": total_files,
        "formatted": format_size_str(total_bytes),
        "breakdown": breakdown
    }


def clear_cache(app_base_dir: str = None, scopes: list = None) -> tuple:
    """
    Deletes files inside the specified cache subdirectories.
    Returns (bytes_freed: int, files_deleted: int).
    """
    base_dir = get_base_cache_dir(app_base_dir)
    target_subs = scopes if scopes else CACHE_SUBDIRS
    bytes_freed = 0
    files_deleted = 0

    for sub in target_subs:
        sub_path = os.path.join(base_dir, sub)
        if not os.path.isdir(sub_path):
            continue
        try:
            for root, _, files in os.walk(sub_path):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(fp)
                        os.remove(fp)
                        bytes_freed += sz
                        files_deleted += 1
                    except OSError:
                        pass
        except OSError:
            pass

    return bytes_freed, files_deleted


def prune_cache_if_needed(
    app_base_dir: str = None,
    max_size_mb: int = 1024,
    target_ratio: float = 0.75
) -> tuple:
    """
    Least-Recently-Used (LRU) pruning:
    If total cache size exceeds max_size_mb, removes oldest accessed/modified
    files until size drops down to target_ratio * max_size_mb.
    Prioritizes media_cache first, then thumbnails.
    If max_size_mb <= 0 (unlimited), pruning is skipped.

    Returns (bytes_freed: int, files_deleted: int).
    """
    if max_size_mb <= 0:
        return 0, 0

    max_bytes = max_size_mb * 1024 * 1024
    target_bytes = int(max_bytes * target_ratio)

    cache_info = get_cache_size_info(app_base_dir)
    total_bytes = cache_info["total_bytes"]

    if total_bytes <= max_bytes:
        return 0, 0

    base_dir = get_base_cache_dir(app_base_dir)
    bytes_to_free = total_bytes - target_bytes

    # Collect files prioritizing heavy media_cache first, then post_thumbnails, etc.
    priority_order = ["media_cache", "post_thumbnails", "comments", "banners", "avatars"]
    file_list = []

    for sub in priority_order:
        sub_path = os.path.join(base_dir, sub)
        if not os.path.isdir(sub_path):
            continue
        try:
            for root, _, files in os.walk(sub_path):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        st = os.stat(fp)
                        # Last access or modification time
                        atime = getattr(st, 'st_atime', st.st_mtime)
                        mtime = st.st_mtime
                        ref_time = min(atime, mtime)
                        file_list.append((ref_time, st.st_size, fp))
                    except OSError:
                        pass
        except OSError:
            pass

    # Sort files: oldest first
    file_list.sort(key=lambda x: x[0])

    freed_bytes = 0
    deleted_count = 0

    for _, size, fp in file_list:
        if freed_bytes >= bytes_to_free:
            break
        try:
            os.remove(fp)
            freed_bytes += size
            deleted_count += 1
        except OSError:
            pass

    return freed_bytes, deleted_count


class CachePruneWorker(QThread):
    """
    Background worker thread to calculate, prune, or clear disk cache
    without blocking the user interface.
    """
    finished = Signal(int, int)  # (bytes_freed, files_deleted)
    info_ready = Signal(dict)    # cache_size_info dict

    def __init__(self, action: str = "prune", app_base_dir: str = None, max_size_mb: int = 1024, parent=None):
        super().__init__(parent)
        self.action = action  # "prune", "clear", or "info"
        self.app_base_dir = app_base_dir
        self.max_size_mb = max_size_mb

    def run(self):
        try:
            if self.action == "info":
                info = get_cache_size_info(self.app_base_dir)
                self.info_ready.emit(info)
            elif self.action == "clear":
                freed, count = clear_cache(self.app_base_dir)
                self.finished.emit(freed, count)
            elif self.action == "prune":
                freed, count = prune_cache_if_needed(self.app_base_dir, self.max_size_mb)
                self.finished.emit(freed, count)
        except Exception:
            self.finished.emit(0, 0)

