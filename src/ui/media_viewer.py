import os
import re
import json
import hashlib
import requests
from urllib.parse import urlparse, unquote

from PySide6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QStackedWidget, QFrame, QScrollArea, QApplication,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsItem,
    QProgressBar, QSizePolicy, QToolTip, QMessageBox, QComboBox
)
from PySide6.QtCore import (
    Qt, QSize, QUrl, Signal, QThread, QPoint, QRectF, QTimer, QObject, QEvent
)
from PySide6.QtGui import (
    QIcon, QPixmap, QMovie, QPainter, QBrush, QColor, QFont, QPen,
    QKeySequence, QShortcut, QDesktopServices, QPainterPath, QImageReader
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget

from ..ui.assets import get_asset_path, get_asset_html_path

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

try:
    from ..core.stream_resolver import CloudStreamResolver
    CLOUD_STREAM_AVAILABLE = True
except Exception:
    CLOUD_STREAM_AVAILABLE = False
    CloudStreamResolver = None


# ─────────────────────────────────────────────────────────────────────────────
#  URL Resolution & Helpers
# ─────────────────────────────────────────────────────────────────────────────

MEDIA_EXTENSIONS = (
    '.gif', '.png', '.jpg', '.jpeg', '.webp', '.avif', '.bmp', '.svg',
    '.mp4', '.webm', '.mkv', '.mov', '.avi', '.m4v',
    '.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus'
)


def _normalize_kemono_thumb_subpath(subpath: str) -> str:
    trimmed = subpath.lstrip('/')
    parts = trimmed.split('/', 1)
    first = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if len(first) == 3 and first[0].isdigit() and rest:
        normalized_path = f"{first[1:]}/{rest}"
    else:
        normalized_path = trimmed
    return f"https://img.pawchive.pw/thumbnail/data/{normalized_path}"


def normalize_media_url(path: str, temp_url: str = None, server: str = None) -> str:
    """
    Constructs the high-definition direct media streaming/download URL.
    Prefers explicit server endpoint, direct original file paths, or temp_url.
    """
    if server and path:
        clean_path = path.lstrip('/')
        if clean_path.startswith("thumbnail/data/"):
            clean_path = clean_path[len("thumbnail/data/"):]
        elif clean_path.startswith("data/"):
            clean_path = clean_path[len("data/"):]
        return f"{server.rstrip('/')}/data/{clean_path}"

    if temp_url and not ("img.pawchive" in temp_url or "thumbnail" in temp_url):
        return temp_url

    if not path:
        return temp_url or ""

    if path.startswith("http://") or path.startswith("https://"):
        if "img.pawchive.pw/thumbnail/data/" in path:
            return path.replace("img.pawchive.pw/thumbnail/data/", "file.pawchive.pw/data/")
        return path
    if path.startswith("//"):
        return f"https:{path}"
    if re.match(r'^(?:[a-z0-9-]+\.)+[a-z]{2,}/', path, re.IGNORECASE):
        return f"https://{path}"

    clean_path = path.lstrip('/')
    if clean_path.startswith("thumbnail/data/"):
        clean_path = clean_path[len("thumbnail/data/"):]
    elif clean_path.startswith("data/"):
        clean_path = clean_path[len("data/"):]

    return f"https://file.pawchive.pw/data/{clean_path}"


def normalize_thumb_url(path: str, temp_url: str = None) -> str:
    """
    Constructs thumbnail preview URL with Pawchive shard stripping.
    Prefers canonical archive path for CDN thumbnails.
    """
    if path:
        if path.startswith("http://") or path.startswith("https://"):
            if "/thumbnail/data/" in path:
                return path
            if "/data/" in path:
                sub = path[path.find("/data/") + 6:]
                return _normalize_kemono_thumb_subpath(sub)
            return path
        if path.startswith("//"):
            return f"https:{path}"

        clean = path.lstrip('/')
        if clean.startswith("thumbnail/data/"):
            clean = clean[len("thumbnail/data/"):]
        elif clean.startswith("data/"):
            clean = clean[len("data/"):]

        return _normalize_kemono_thumb_subpath(clean)

    if temp_url:
        return temp_url
    return ""


def extract_media_items_from_post(post: dict) -> list:
    """
    Parses a post dictionary and extracts all playable/viewable media items
    (videos, animated GIFs, high-res images, audio, files).
    Extracts from:
      1. Primary file (post['file'])
      2. Attachments list (post['attachments'])
      3. Embedded media links in HTML/text content (post['content'])
      4. Embed payload links (post['embed'])
    """
    if not post or not isinstance(post, dict):
        return []

    items = []
    seen_urls = set()
    seen_paths = set()

    def _add_media_candidate(path: str, name: str = "", temp_url: str = "", server: str = ""):
        if not path and not temp_url:
            return

        raw_target = temp_url or path
        clean_target = raw_target.split("?")[0].split("#")[0]
        ext = os.path.splitext(clean_target)[1].lower()
        if not ext and name:
            ext = os.path.splitext(name)[1].lower()

        url = normalize_media_url(path, temp_url, server)
        thumb_url = normalize_thumb_url(path, temp_url)

        clean_name = name or os.path.basename(clean_target) or f"Media_{len(items)+1}"
        clean_name = unquote(clean_name)

        # Deduplicate
        url_clean = url.split("?")[0].split("#")[0]
        url_key = url_clean.lower().rstrip("/")
        path_key = path.strip("/").lower() if path else ""
        if url_key in seen_urls:
            return
        if path_key and path_key in seen_paths:
            return

        seen_urls.add(url_key)
        if path_key:
            seen_paths.add(path_key)

        # Classify media kind — cloud links are always treated as video
        _is_cloud = (
            CLOUD_STREAM_AVAILABLE
            and CloudStreamResolver is not None
            and CloudStreamResolver.is_cloud_link(url)
        )
        if _is_cloud:
            kind = "video"
            thumb_url = ""  # Cloud links do not have HTTP image thumbnails
        elif ext == ".gif":
            kind = "gif"
        elif ext in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"):
            kind = "video"
        elif ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif", ".svg"):
            kind = "image"
        elif ext in (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus"):
            kind = "audio"
        else:
            kind = "file"

        # Build fallback URLs for streaming worker
        fallbacks = []
        if path:
            cpath = path.lstrip('/')
            if cpath.startswith("thumbnail/data/"):
                cpath = cpath[len("thumbnail/data/"):]
            elif cpath.startswith("data/"):
                cpath = cpath[len("data/"):]

            for srv in [server, "https://file.pawchive.pw", "https://fox.pawchive.pw", "https://n4.pawchive.pw", "https://n2.pawchive.pw", "https://n1.pawchive.pw"]:
                if srv:
                    cand = f"{srv.rstrip('/')}/data/{cpath}"
                    if cand != url and cand not in fallbacks:
                        fallbacks.append(cand)
        if temp_url and temp_url != url and temp_url not in fallbacks:
            fallbacks.append(temp_url)

        items.append({
            "index": len(items),
            "name": clean_name,
            "path": path,
            "url": url,
            "server": server,
            "temp_url": temp_url,
            "fallback_urls": fallbacks,
            "thumb_url": thumb_url,
            "kind": kind,
            "ext": ext or ".bin",
            "post_title": post.get("title") or f"Post #{post.get('id', '')}",
            "creator_name": post.get("_creator_name") or "Creator",
            "is_cloud_link": _is_cloud,
        })

    # 1. Main post file
    main_file = post.get("file")
    if isinstance(main_file, dict) and (main_file.get("path") or main_file.get("url") or main_file.get("temp_url")):
        _add_media_candidate(
            main_file.get("path") or "",
            main_file.get("name") or "",
            main_file.get("temp_url") or main_file.get("url") or "",
            server=main_file.get("server") or ""
        )

    # 2. Attachments
    attachments = post.get("attachments")
    if isinstance(attachments, list):
        for att in attachments:
            if isinstance(att, dict) and (att.get("path") or att.get("url") or att.get("temp_url")):
                _add_media_candidate(
                    att.get("path") or "",
                    att.get("name") or "",
                    att.get("temp_url") or att.get("url") or "",
                    server=att.get("server") or ""
                )

    # 3. Direct media embedded in post content HTML / text & embed JSON
    content_text = post.get("content") or ""
    embed = post.get("embed")
    if isinstance(embed, dict):
        try:
            embed_text = json.dumps(embed)
        except Exception:
            embed_text = str(embed)
    elif isinstance(embed, str):
        embed_text = embed
    else:
        embed_text = ""

    full_text = f"{content_text}\n{embed_text}"
    if full_text.strip():
        # Match HTML tags like <img src="...">, <a href="...">, <source src="...">, <video src="...">
        tag_matches = re.findall(
            r'''(?i)<(?:img|source|video|audio|a)[^>]+(?:src|href)=["']([^"']+)["']''',
            full_text
        )
        # Match raw / standalone media URLs in text
        ext_alt = "|".join(ext.lstrip(".") for ext in MEDIA_EXTENSIONS)
        raw_matches = re.findall(
            rf'''(?i)(?:https?://[^\s"\'<>]+|/?data/[^\s"\'<>]+|(?:[a-z0-9-]+\.)+[a-z]{{2,}}/[^\s"\'<>]+)\.(?:{ext_alt})(?:\?[^\s"\'<>]*)?''',
            full_text
        )

        for candidate in tag_matches + raw_matches:
            cand = candidate.strip().rstrip(".,;:)]}")
            cand_clean = cand.split("?")[0].split("#")[0]
            cand_ext = os.path.splitext(cand_clean)[1].lower()
            if cand_ext in MEDIA_EXTENSIONS:
                _add_media_candidate(path=cand, name="", temp_url="")

        # Also scan for direct cloud video file links (Mega / Google Drive / Dropbox) in content
        # Note: Folders and Google Docs are excluded since they cannot be streamed as a single video
        if CLOUD_STREAM_AVAILABLE and CloudStreamResolver is not None:
            cloud_pattern = re.compile(
                r'https?://(?:'
                r'(?:www\.)?mega\.nz/(?:file/[A-Za-z0-9_-]+(?:#[A-Za-z0-9_-]+)?|#![A-Za-z0-9_-]+![A-Za-z0-9_-]+)'
                r'|drive\.google\.com/(?:file/d/|open\?id=)[A-Za-z0-9_-]+'
                r'|(?:www\.)?dropbox\.com/(?:s|scl/fi)/[^\s"\'<>]+\.(?:mp4|mkv|webm|mov|avi|m4v)(?:\?[^\s"\'<>]*)?'
                r')',
                re.IGNORECASE
            )
            for m in cloud_pattern.finditer(full_text):
                cand = m.group(0).strip().rstrip(".,;:)]}")
                url_clean = cand.split("?")[0].split("#")[0]
                url_key = url_clean.lower().rstrip("/")
                if url_key not in seen_urls:
                    seen_urls.add(url_key)
                    ext_guess = ".mp4"  # all cloud links treated as video
                    clean_name = "Cloud Video"
                    if "mega.nz" in cand.lower():
                        clean_name = "MEGA Video"
                    elif "drive.google.com" in cand.lower():
                        clean_name = "Google Drive Video"
                    elif "dropbox.com" in cand.lower():
                        clean_name = "Dropbox Video"
                    items.append({
                        "index": len(items),
                        "name": clean_name,
                        "path": "",
                        "url": cand,
                        "server": "",
                        "temp_url": "",
                        "fallback_urls": [],
                        "thumb_url": "",
                        "kind": "video",
                        "ext": ext_guess,
                        "post_title": post.get("title") or f"Post #{post.get('id', '')}",
                        "creator_name": post.get("_creator_name") or "Creator",
                        "is_cloud_link": True,
                    })

    return items


def format_time(ms: int) -> str:
    """Formats milliseconds into MM:SS or HH:MM:SS."""
    if ms <= 0:
        return "00:00"
    total_sec = int(ms // 1000)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


# ─────────────────────────────────────────────────────────────────────────────
#  Background Stream Worker for High-Definition Images & GIFs
# ─────────────────────────────────────────────────────────────────────────────

class _MediaStreamWorker(QThread):
    progress = Signal(int)              # 0 - 100
    finished = Signal(bool, str, str)   # success, disk_path, error_msg

    def __init__(self, url: str, disk_path: str, fallback_urls: list = None, parent=None):
        super().__init__(parent)
        self.url = url
        self.disk_path = disk_path
        self.fallback_urls = fallback_urls or []
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def _get_headers_for_url(self, target_url: str) -> dict:
        is_kemono = "kemono" in target_url or "coomer" in target_url
        if is_kemono:
            return {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://kemono.su/" if "kemono" in target_url else "https://coomer.su/",
                "Accept": "*/*"
            }
        return {
            "User-Agent": "PawchiveClient/1.0.5 (Desktop App)",
            "Referer": "https://pawchive.pw/",
            "Accept": "*/*"
        }

    def _download_stream(self, target_url: str, temp_path: str) -> tuple:
        """Downloads a URL with proper headers and curl_cffi fallback on 403."""
        headers = self._get_headers_for_url(target_url)

        # 1. Standard requests attempt
        try:
            res = requests.get(target_url, headers=headers, stream=True, timeout=20)
            if res.status_code == 200:
                total_size = int(res.headers.get("content-length", 0))
                downloaded = 0
                with open(temp_path, "wb") as f:
                    for chunk in res.iter_content(chunk_size=65536):
                        if self._is_cancelled:
                            return False, "cancelled"
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_size > 0:
                                percent = min(99, int(downloaded * 100 / total_size))
                                self.progress.emit(percent)
                return True, ""

            # If 403 Cloudflare protection, retry with curl_cffi impersonating chrome
            if res.status_code == 403 and CURL_CFFI_AVAILABLE:
                try:
                    cffi_res = cffi_requests.get(target_url, headers=headers, stream=True, impersonate="chrome120", timeout=20)
                    if cffi_res.status_code == 200:
                        total_size = int(cffi_res.headers.get("content-length", 0))
                        downloaded = 0
                        with open(temp_path, "wb") as f:
                            for chunk in cffi_res.iter_content(chunk_size=65536):
                                if self._is_cancelled:
                                    return False, "cancelled"
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if total_size > 0:
                                        percent = min(99, int(downloaded * 100 / total_size))
                                        self.progress.emit(percent)
                        return True, ""
                except Exception:
                    pass

            return False, f"HTTP {res.status_code}"
        except Exception as e:
            return False, str(e)

    def run(self):
        if os.path.exists(self.disk_path) and os.path.getsize(self.disk_path) > 128:
            self.progress.emit(100)
            self.finished.emit(True, self.disk_path, "")
            return

        urls_to_try = [self.url] + [u for u in self.fallback_urls if u and u != self.url]
        temp_path = self.disk_path + f".tmp.{os.getpid()}"
        os.makedirs(os.path.dirname(self.disk_path), exist_ok=True)
        last_err = ""

        for u in urls_to_try:
            if self._is_cancelled:
                break
            success, err = self._download_stream(u, temp_path)
            if success:
                if os.path.exists(self.disk_path):
                    try:
                        os.remove(self.disk_path)
                    except OSError:
                        pass
                os.rename(temp_path, self.disk_path)
                self.progress.emit(100)
                self.finished.emit(True, self.disk_path, "")
                return
            last_err = err
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        self.finished.emit(False, "", last_err or "Failed to stream media")


class _BatchHDLoaderWorker(QThread):
    item_loaded = Signal(int, str)       # index, disk_path
    item_failed = Signal(int, str)       # index, error_msg
    progress = Signal(int, int)          # completed_count, total_count
    all_finished = Signal(int, int)      # success_count, total_count

    def __init__(self, tasks: list, parent=None):
        super().__init__(parent)
        self.tasks = tasks
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def _get_headers_for_url(self, target_url: str) -> dict:
        is_kemono = "kemono" in target_url or "coomer" in target_url
        if is_kemono:
            return {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://kemono.su/" if "kemono" in target_url else "https://coomer.su/",
                "Accept": "*/*"
            }
        return {
            "User-Agent": "PawchiveClient/1.0.5 (Desktop App)",
            "Referer": "https://pawchive.pw/",
            "Accept": "*/*"
        }

    def _download_task_file(self, target_url: str, tmp_path: str) -> bool:
        headers = self._get_headers_for_url(target_url)

        # 1. Standard requests attempt
        try:
            resp = requests.get(target_url, headers=headers, stream=True, timeout=25)
            if resp.status_code == 200:
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if self._is_cancelled:
                            return False
                        if chunk:
                            f.write(chunk)
                if not self._is_cancelled and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 128:
                    return True

            # If 403 Cloudflare protection, fallback to curl_cffi
            if resp.status_code in (401, 403, 429) and CURL_CFFI_AVAILABLE and not self._is_cancelled:
                try:
                    cffi_res = cffi_requests.get(target_url, headers=headers, stream=True, impersonate="chrome120", timeout=25)
                    if cffi_res.status_code == 200:
                        with open(tmp_path, "wb") as f:
                            for chunk in cffi_res.iter_content(chunk_size=65536):
                                if self._is_cancelled:
                                    return False
                                if chunk:
                                    f.write(chunk)
                        if not self._is_cancelled and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 128:
                            return True
                except Exception:
                    pass
        except Exception:
            # If standard requests failed with SSL/connection error, try curl_cffi
            if CURL_CFFI_AVAILABLE and not self._is_cancelled:
                try:
                    cffi_res = cffi_requests.get(target_url, headers=headers, stream=True, impersonate="chrome120", timeout=25)
                    if cffi_res.status_code == 200:
                        with open(tmp_path, "wb") as f:
                            for chunk in cffi_res.iter_content(chunk_size=65536):
                                if self._is_cancelled:
                                    return False
                                if chunk:
                                    f.write(chunk)
                        if not self._is_cancelled and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 128:
                            return True
                except Exception:
                    pass

        return False

    def run(self):
        total = len(self.tasks)
        success_count = 0

        for idx_in_batch, task in enumerate(self.tasks):
            if self._is_cancelled:
                break
            idx = task["index"]
            disk_path = task["disk_path"]
            url = task["url"]
            fallback_urls = task.get("fallback_urls") or []

            # Check if file is already cached and valid
            if os.path.exists(disk_path) and os.path.getsize(disk_path) > 128:
                success_count += 1
                self.item_loaded.emit(idx, disk_path)
                self.progress.emit(idx_in_batch + 1, total)
                continue

            urls_to_try = [url] + [u for u in fallback_urls if u and u != url]
            downloaded = False
            tmp_path = disk_path + f".batch_{os.getpid()}_{idx}.tmp"

            for target_url in urls_to_try:
                if self._is_cancelled:
                    break
                try:
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
                    os.makedirs(os.path.dirname(disk_path), exist_ok=True)
                    if self._download_task_file(target_url, tmp_path):
                        if os.path.exists(disk_path):
                            try:
                                os.remove(disk_path)
                            except OSError:
                                pass
                        os.rename(tmp_path, disk_path)
                        downloaded = True
                        break
                except Exception:
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass

            if downloaded:
                success_count += 1
                self.item_loaded.emit(idx, disk_path)
            else:
                self.item_failed.emit(idx, "Download failed")

            self.progress.emit(idx_in_batch + 1, total)

        self.all_finished.emit(success_count, total)


# ─────────────────────────────────────────────────────────────────────────────
#  Video & Media Buffering & Loading Animation Overlay
# ─────────────────────────────────────────────────────────────────────────────

class VideoLoadingOverlay(QWidget):
    """
    A sleek, modern translucent overlay with a smooth circular spinning animation
    and real-time buffering/loading status.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._angle = 0
        self._message = "Loading media..."
        self._timer = QTimer(self)
        self._timer.setInterval(16)  # ~60 FPS smooth rotation
        self._timer.timeout.connect(self._rotate)
        self._is_active = False
        self.hide()

    def start_loading(self, message: str = "Loading media..."):
        self._message = message
        self._is_active = True
        if not self._timer.isActive():
            self._timer.start()
        self.show()
        self.raise_()
        self.update()

    def show_loading(self, message: str = "Loading media..."):
        self.start_loading(message)

    def set_message(self, message: str):
        if self._message != message:
            self._message = message
            self.update()

    def set_status(self, message: str):
        self.set_message(message)

    def stop_loading(self):
        self._is_active = False
        self._timer.stop()
        self.hide()

    def hide_loading(self):
        self.stop_loading()

    def _rotate(self):
        self._angle = (self._angle + 6) % 360
        self.update()

    def paintEvent(self, event):
        if not self._is_active:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        radius = 22.0

        # Draw sleek translucent rounded badge backdrop
        box_w = 170.0
        box_h = 96.0
        bg_rect = QRectF(cx - box_w / 2.0, cy - box_h / 2.0, box_w, box_h)
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1.0))
        painter.setBrush(QBrush(QColor(14, 16, 22, 210)))
        painter.drawRoundedRect(bg_rect, 12.0, 12.0)

        # Draw spinner background track
        track_center_y = cy - 10.0
        track_rect = QRectF(cx - radius, track_center_y - radius, radius * 2.0, radius * 2.0)
        track_pen = QPen(QColor(255, 255, 255, 28), 3.5)
        track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.drawEllipse(track_rect)

        # Draw animated spinning arc with Pawchive accent color (#E5A198)
        spin_pen = QPen(QColor("#E5A198"), 3.5)
        spin_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(spin_pen)

        start_angle_16 = int(-self._angle * 16)
        span_angle_16 = int(-110 * 16)
        painter.drawArc(track_rect, start_angle_16, span_angle_16)

        # Draw status message under the spinner
        font = QFont("Segoe UI", 8, QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor("#DFE1E5"))
        text_rect = QRectF(cx - box_w / 2.0 + 4, cy + 18, box_w - 8, 22)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self._message)

        painter.end()


# ─────────────────────────────────────────────────────────────────────────────
#  Interactive HD Image Zoom & Pan View (Click-to-Load On Demand)
# ─────────────────────────────────────────────────────────────────────────────

class ImageZoomView(QGraphicsView):
    """
    High-Definition image canvas supporting:
    - On-demand click-to-load full resolution
    - Smooth mouse wheel zoom anchored under cursor
    - Grab & drag panning with hand cursor
    - Double-click to load HD or toggle 100% 1:1 original size vs fit-to-window
    - Clockwise rotation (90°)
    - Dynamic responsive viewport resizing
    """
    load_full_res_requested = Signal()

    def __init__(self, parent=None, scale: float = 1.0):
        super().__init__(parent)
        self.ui_scale = scale
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.pixmap_item = QGraphicsPixmapItem()
        self.pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene.addItem(self.pixmap_item)

        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QBrush(QColor("#0E1015")))
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._zoom_factor = 1.0
        self._rotation_angle = 0
        self._pixmap = None
        self._preview_pixmap = None
        self._is_fitted = True
        self._is_full_res = False
        self._is_loading = False

        # Floating overlay button to load full resolution
        self.overlay_btn = QPushButton("  Load Full Resolution  ", self.viewport())
        self.overlay_btn.setObjectName("imageLoadOverlayBtn")
        self.overlay_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overlay_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_in.svg")))
        self.overlay_btn.setIconSize(QSize(int(15 * scale), int(15 * scale)))
        self.overlay_btn.setStyleSheet(
            f"QPushButton#imageLoadOverlayBtn {{ "
            f"background-color: rgba(20, 22, 28, 230); "
            f"color: #FFFFFF; "
            f"border: 2px solid #E5A198; "
            f"border-radius: {int(20 * scale)}px; "
            f"font-size: {int(10 * scale)}pt; "
            f"font-weight: bold; "
            f"padding: {int(7 * scale)}px {int(18 * scale)}px; "
            f"}} "
            f"QPushButton#imageLoadOverlayBtn:hover {{ "
            f"background-color: #E5A198; "
            f"color: #12141A; "
            f"border-color: #FFFFFF; "
            f"}}"
        )
        self.overlay_btn.clicked.connect(self.request_load_full_res)
        self.overlay_btn.hide()

        # Loading / Buffering Spinner Overlay
        self.loading_overlay = VideoLoadingOverlay(self.viewport())
        self.loading_overlay.hide()

    def sizeHint(self):
        return QSize(200, 200)

    def minimumSizeHint(self):
        return QSize(100, 100)

    def set_preview_thumbnail(self, pixmap: QPixmap = None, fit_to_view: bool = True):
        """Displays lightweight preview thumbnail with a centered 'Load Full Resolution' prompt."""
        self._is_full_res = False
        self._is_loading = False
        self.loading_overlay.stop_loading()
        self._preview_pixmap = pixmap

        if pixmap and not pixmap.isNull():
            self._pixmap = pixmap
            self.pixmap_item.setPixmap(pixmap)
            self.scene.setSceneRect(QRectF(pixmap.rect()))
            if fit_to_view:
                self.fit_in_window()
        else:
            self._pixmap = None
            self.pixmap_item.setPixmap(QPixmap())
            self.scene.setSceneRect(QRectF(0, 0, 100, 100))

        self.overlay_btn.hide()

    def set_full_res_pixmap(self, pixmap: QPixmap, fit_to_view: bool = True):
        """Displays native high-definition image, hiding all loading and overlay prompts."""
        self._is_full_res = True
        self._is_loading = False
        self.overlay_btn.hide()
        self.loading_overlay.stop_loading()
        if pixmap and not pixmap.isNull():
            self._pixmap = pixmap
            self.pixmap_item.setPixmap(pixmap)
            self.scene.setSceneRect(QRectF(pixmap.rect()))
            if fit_to_view:
                self.fit_in_window()

    def set_pixmap(self, pixmap: QPixmap, fit_to_view: bool = True):
        """Backwards compatibility alias for set_full_res_pixmap."""
        self.set_full_res_pixmap(pixmap, fit_to_view)

    def show_loading(self, message: str = "Loading Full Resolution Image..."):
        self._is_loading = True
        self.overlay_btn.hide()
        self.loading_overlay.start_loading(message)
        self.loading_overlay.show()
        self.loading_overlay.raise_()
        self._reposition_overlays()

    def set_loading_progress(self, percent: int, message: str = ""):
        if self._is_loading:
            status_text = f"{message or 'Loading HD'} ({percent}%)"
            self.loading_overlay.set_status(status_text)

    def is_full_res(self) -> bool:
        return self._is_full_res

    def request_load_full_res(self):
        if not self._is_full_res and not self._is_loading:
            self.load_full_res_requested.emit()

    def _reposition_overlays(self):
        vp_rect = self.viewport().rect()
        if hasattr(self, "overlay_btn") and not self.overlay_btn.isHidden():
            self.overlay_btn.adjustSize()
            btn_w = self.overlay_btn.width()
            btn_h = self.overlay_btn.height()
            self.overlay_btn.move(
                max(0, (vp_rect.width() - btn_w) // 2),
                max(0, (vp_rect.height() - btn_h) // 2)
            )
        if hasattr(self, "loading_overlay") and not self.loading_overlay.isHidden():
            self.loading_overlay.setGeometry(vp_rect)

    def fit_in_window(self):
        """Fits image in current viewport keeping aspect ratio."""
        self.resetTransform()
        self._zoom_factor = 1.0
        self._rotation_angle = 0
        self._is_fitted = True
        if self._pixmap and not self._pixmap.isNull():
            self.fitInView(self.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_overlays()
        if getattr(self, "_is_fitted", False) and self._pixmap and not self._pixmap.isNull():
            self.fit_in_window()

    def set_actual_size(self):
        """Renders image at 100% 1:1 original native resolution."""
        self.resetTransform()
        self._zoom_factor = 1.0
        self._is_fitted = False
        if self._rotation_angle:
            self.rotate(self._rotation_angle)

    def zoom_in(self):
        self._is_fitted = False
        if self._zoom_factor < 20.0:
            self.scale(1.2, 1.2)
            self._zoom_factor *= 1.2

    def zoom_out(self):
        self._is_fitted = False
        if self._zoom_factor > 0.05:
            self.scale(1 / 1.2, 1 / 1.2)
            self._zoom_factor /= 1.2

    def rotate_cw(self):
        self._rotation_angle = (self._rotation_angle + 90) % 360
        self.rotate(90)

    def wheelEvent(self, event):
        if not self._pixmap or self._pixmap.isNull():
            return
        delta = event.angleDelta().y()
        if delta > 0:
            self.zoom_in()
        elif delta < 0:
            self.zoom_out()

    def mouseDoubleClickEvent(self, event):
        if not self._is_full_res:
            self.request_load_full_res()
            return
        if abs(self._zoom_factor - 1.0) < 0.15 and not getattr(self, "_is_fitted", False):
            self.fit_in_window()
        else:
            self.set_actual_size()


# ─────────────────────────────────────────────────────────────────────────────
#  Hardware-Accelerated Video Player
# ─────────────────────────────────────────────────────────────────────────────

class VideoPlayerWidget(QWidget):
    """
    Integrated video player powered by QtMultimedia and Windows Media Foundation:
    - Streams directly from HTTP/HTTPS with Range 206 seeking
    - Custom seek slider with time display
    - Volume control + Mute toggle
    - Playback speeds (0.5× to 2.0×)
    - Fullscreen and Auto-Loop toggle
    - Real-time loading/buffering animation overlay for direct online streaming
    """
    fullscreen_requested = Signal()

    def __init__(self, parent=None, scale: float = 1.0):
        super().__init__(parent)
        self.scale = scale
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)

        self._url: str = ""
        self._disk_path: str = ""
        self._preview_pixmap: QPixmap | None = None
        self._has_started: bool = False

        self._is_user_scrubbing = False
        self._is_looping = False
        self._speeds = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
        self._speed_idx = 2  # 1.0x

        # Cloud stream resolver state
        self._pending_cloud_url: str | None = None
        self._cloud_resolver = None

        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        scale = self.scale
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Video Output & Overlay Container
        self.video_container = QWidget(self)
        self.video_container.setStyleSheet("background-color: #0E1015;")
        v_layout = QVBoxLayout(self.video_container)
        v_layout.setContentsMargins(0, 0, 0, 0)
        v_layout.setSpacing(0)

        self.video_widget = QVideoWidget(self.video_container)
        self.video_widget.setStyleSheet("background-color: #0E1015;")
        self.video_widget.installEventFilter(self)
        self.player.setVideoOutput(self.video_widget)
        v_layout.addWidget(self.video_widget)

        # Static poster preview label (displays thumbnail until user clicks to play)
        self.poster_label = QLabel(self.video_container)
        self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster_label.setStyleSheet("background-color: #0E1015;")
        self.poster_label.installEventFilter(self)

        # Big Centered "PLAY" Overlay Button
        self.play_overlay_btn = QPushButton("  PLAY  ", self.video_container)
        self.play_overlay_btn.setObjectName("videoPlayOverlayBtn")
        self.play_overlay_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_overlay_btn.setIcon(QIcon(get_asset_path("assets/Svg/play.svg")))
        self.play_overlay_btn.setIconSize(QSize(int(18 * scale), int(18 * scale)))
        self.play_overlay_btn.setStyleSheet(
            f"QPushButton#videoPlayOverlayBtn {{ "
            f"background-color: rgba(20, 22, 28, 225); "
            f"color: #FFFFFF; "
            f"border: 2px solid #E5A198; "
            f"border-radius: {int(22 * scale)}px; "
            f"font-size: {int(11 * scale)}pt; "
            f"font-weight: bold; "
            f"padding: {int(8 * scale)}px {int(24 * scale)}px; "
            f"}} "
            f"QPushButton#videoPlayOverlayBtn:hover {{ "
            f"background-color: #E5A198; "
            f"color: #12141A; "
            f"border-color: #FFFFFF; "
            f"}}"
        )
        self.play_overlay_btn.clicked.connect(self._on_video_area_clicked)

        # Loading & Buffering Animation Overlay
        self.loading_overlay = VideoLoadingOverlay(self.video_container)

        # Cloud stream error label (shown briefly on resolution failure)
        self._cloud_error_label = QLabel(self.video_container)
        self._cloud_error_label.setWordWrap(True)
        self._cloud_error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cloud_error_label.setStyleSheet(
            "QLabel { background-color: rgba(180,40,40,210); color: #FFFFFF; "
            "font-size: 9pt; border-radius: 6px; padding: 6px 12px; }"
        )
        self._cloud_error_label.setVisible(False)
        self._cloud_error_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        root.addWidget(self.video_container, 1)

        # Bottom Control Bar
        self.controls_bar = QFrame(self)
        self.controls_bar.setStyleSheet(
            "QFrame { background-color: #16181F; border-top: 1px solid #2B2E38; }"
            "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; border-radius: 5px; font-size: 8.5pt; font-weight: bold; padding: 4px 8px; }"
            "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
            "QSlider::groove:horizontal { height: 6px; background: #2A2D36; border-radius: 3px; }"
            "QSlider::sub-page:horizontal { background: #E5A198; border-radius: 3px; }"
            "QSlider::handle:horizontal { background: #FFFFFF; width: 14px; margin-top: -4px; margin-bottom: -4px; border-radius: 7px; }"
        )
        ctrl_layout = QVBoxLayout(self.controls_bar)
        ctrl_layout.setContentsMargins(12, 6, 12, 8)
        ctrl_layout.setSpacing(4)

        # Row 1: Seek Slider
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        ctrl_layout.addWidget(self.seek_slider)

        # Row 2: Controls & Buttons
        row2 = QHBoxLayout()
        row2.setSpacing(6)

        # Play / Pause
        self.play_btn = QPushButton("  Play")
        self.play_btn.setIcon(QIcon(get_asset_path("assets/uisvg/play.svg")))
        self.play_btn.setIconSize(QSize(13, 13))
        self.play_btn.setFixedWidth(74)
        self.play_btn.clicked.connect(self.toggle_play)
        row2.addWidget(self.play_btn)

        # Skip -10s
        self.skip_back_btn = QPushButton(" 10s")
        self.skip_back_btn.setIcon(QIcon(get_asset_path("assets/Svg2/back 10Sec.svg")))
        self.skip_back_btn.setIconSize(QSize(15, 15))
        self.skip_back_btn.setFixedWidth(52)
        self.skip_back_btn.setToolTip("Rewind 10 seconds (Left Arrow)")
        self.skip_back_btn.clicked.connect(lambda: self.skip(-10000))
        row2.addWidget(self.skip_back_btn)

        # Skip +10s
        self.skip_fwd_btn = QPushButton("10s ")
        self.skip_fwd_btn.setIcon(QIcon(get_asset_path("assets/Svg2/skip 10Sec.svg")))
        self.skip_fwd_btn.setIconSize(QSize(15, 15))
        self.skip_fwd_btn.setFixedWidth(52)
        self.skip_fwd_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.skip_fwd_btn.setToolTip("Fast-forward 10 seconds (Right Arrow)")
        self.skip_fwd_btn.clicked.connect(lambda: self.skip(10000))
        row2.addWidget(self.skip_fwd_btn)

        # Time label
        self.time_lbl = QLabel("00:00 / 00:00")
        self.time_lbl.setStyleSheet("color: #8C929E; font-size: 8pt; font-weight: bold;")
        self.time_lbl.setMinimumWidth(0)
        row2.addWidget(self.time_lbl)

        row2.addStretch(1)

        # Loop Toggle
        self.loop_btn = QPushButton("  Loop")
        self.loop_btn.setIcon(QIcon(get_asset_path("assets/Svg2/repeat.svg")))
        self.loop_btn.setIconSize(QSize(13, 13))
        self.loop_btn.setFixedWidth(64)
        self.loop_btn.setToolTip("Toggle automatic looping")
        self.loop_btn.clicked.connect(self._toggle_loop)
        row2.addWidget(self.loop_btn)

        # Speed button
        self.speed_btn = QPushButton("1.0×")
        self.speed_btn.setFixedWidth(46)
        self.speed_btn.setToolTip("Change playback speed")
        self.speed_btn.clicked.connect(self._cycle_speed)
        row2.addWidget(self.speed_btn)

        # Volume Mute
        self.mute_btn = QPushButton()
        self.mute_btn.setIcon(QIcon(get_asset_path("assets/Svg/speaker-high.svg")))
        self.mute_btn.setIconSize(QSize(15, 15))
        self.mute_btn.setFixedWidth(30)
        self.mute_btn.clicked.connect(self._toggle_mute)
        row2.addWidget(self.mute_btn)

        # Volume slider
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(100)
        self.vol_slider.setFixedWidth(65)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        row2.addWidget(self.vol_slider)

        # Fullscreen button
        self.fs_btn = QPushButton()
        self.fs_btn.setIcon(QIcon(get_asset_path("assets/Svg/fullscreen.svg")))
        self.fs_btn.setIconSize(QSize(15, 15))
        self.fs_btn.setFixedWidth(30)
        self.fs_btn.setToolTip("Toggle Fullscreen (F11)")
        self.fs_btn.clicked.connect(self.fullscreen_requested.emit)
        row2.addWidget(self.fs_btn)

        ctrl_layout.addLayout(row2)
        root.addWidget(self.controls_bar)

    def eventFilter(self, watched, event):
        if watched in (getattr(self, "video_widget", None), getattr(self, "poster_label", None)):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._on_video_area_clicked()
                return True
        return super().eventFilter(watched, event)

    def _on_video_area_clicked(self):
        if not self._has_started:
            self._start_media()
        else:
            self.toggle_play()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "video_container"):
            w = self.video_container.width()
            h = self.video_container.height()
            if hasattr(self, "poster_label"):
                self.poster_label.setGeometry(0, 0, w, h)
                self._update_poster_display()
            if hasattr(self, "play_overlay_btn"):
                btn_w = self.play_overlay_btn.sizeHint().width() + int(36 * self.scale)
                btn_h = int(46 * self.scale)
                self.play_overlay_btn.resize(btn_w, btn_h)
                self.play_overlay_btn.move((w - btn_w) // 2, (h - btn_h) // 2)
                if not self._has_started:
                    self.play_overlay_btn.raise_()
            if hasattr(self, "loading_overlay"):
                self.loading_overlay.setGeometry(0, 0, w, h)
                self.loading_overlay.raise_()
            if hasattr(self, "_cloud_error_label"):
                lbl = self._cloud_error_label
                lbl.adjustSize()
                lbl.setGeometry(20, h - lbl.height() - 20, w - 40, lbl.height())
                lbl.raise_()

    def _connect_signals(self):
        self.seek_slider.sliderPressed.connect(self._on_slider_pressed)
        self.seek_slider.sliderReleased.connect(self._on_slider_released)
        self.seek_slider.sliderMoved.connect(self._on_slider_moved)

        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.bufferProgressChanged.connect(self._on_buffer_progress_changed)
        self.player.errorOccurred.connect(self._on_media_error)

    def _update_poster_display(self):
        if hasattr(self, "poster_label") and hasattr(self, "video_container"):
            if self._preview_pixmap and not self._preview_pixmap.isNull():
                avail_w = max(50, self.video_container.width() - 20)
                avail_h = max(50, self.video_container.height() - 20)
                scaled = self._preview_pixmap.scaled(
                    avail_w, avail_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
                self.poster_label.setPixmap(scaled)
            else:
                self.poster_label.setPixmap(QPixmap())

    def set_preview_pixmap(self, pixmap: QPixmap):
        if not pixmap or pixmap.isNull():
            return
        self._preview_pixmap = pixmap
        if not self._has_started:
            self._update_poster_display()

    def set_media(self, url: str, disk_path: str = None, preview_pixmap: QPixmap = None):
        """Prepares video player in poster / click-to-play mode without auto-playing."""
        self.stop_and_release()
        self._url = url
        self._disk_path = disk_path or ""
        self._preview_pixmap = preview_pixmap
        self._has_started = False

        self._update_poster_display()
        if hasattr(self, "poster_label"):
            self.poster_label.show()
            self.poster_label.raise_()

        if hasattr(self, "play_overlay_btn"):
            self.play_overlay_btn.show()
            self.play_overlay_btn.raise_()

        if hasattr(self, "video_container"):
            self.video_container.setCursor(Qt.CursorShape.PointingHandCursor)

        self._set_play_btn_state(False)
        self.time_lbl.setText("00:00 / 00:00")
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setValue(0)

    def _start_media(self):
        """Starts video streaming / loading on explicit user click."""
        if self._has_started:
            return
        self._has_started = True
        if hasattr(self, "play_overlay_btn"):
            self.play_overlay_btn.hide()
        if hasattr(self, "video_container"):
            self.video_container.setCursor(Qt.CursorShape.ArrowCursor)

        url = self._url
        disk_path = self._disk_path
        if not url and not disk_path:
            return

        if disk_path and os.path.exists(disk_path) and os.path.getsize(disk_path) > 1024:
            self.loading_overlay.start_loading("Opening video...")
            self.player.setSource(QUrl.fromLocalFile(disk_path))
            self.player.play()
            self._set_play_btn_state(True)

        elif CLOUD_STREAM_AVAILABLE and CloudStreamResolver is not None and CloudStreamResolver.is_cloud_link(url):
            # Cloud link — resolve in background
            self._pending_cloud_url = url
            if "mega.nz" in url.lower():
                msg = "Resolving MEGA link..."
            elif "drive.google.com" in url.lower():
                msg = "Resolving Google Drive link..."
            elif "dropbox.com" in url.lower():
                msg = "Resolving Dropbox link..."
            else:
                msg = "Resolving cloud link..."
            self.loading_overlay.start_loading(msg)
            resolver = CloudStreamResolver(parent=self)
            resolver.resolved.connect(self._on_cloud_stream_resolved)
            resolver.failed.connect(self._on_cloud_stream_failed)
            resolver.progress.connect(self._on_cloud_stream_progress)
            self._cloud_resolver = resolver
            resolver.resolve(url)

        else:
            self.loading_overlay.start_loading("Connecting to stream...")
            self.player.setSource(QUrl(url))
            self.player.play()
            self._set_play_btn_state(True)

    def _on_cloud_stream_resolved(self, original_url: str, stream_url: str):
        """Called when the background resolver has a playable URL."""
        if original_url != self._pending_cloud_url:
            return  # stale result
        self.loading_overlay.start_loading("Connecting to stream...")
        self.player.setSource(QUrl(stream_url))
        self.player.play()
        self._set_play_btn_state(True)

    def _on_cloud_stream_failed(self, original_url: str, error: str):
        """Called when the resolver could not produce a stream URL."""
        if original_url != self._pending_cloud_url:
            return
        self.loading_overlay.stop_loading()
        self._pending_cloud_url = None
        self._has_started = False
        if hasattr(self, "play_overlay_btn"):
            self.play_overlay_btn.show()
            self.play_overlay_btn.raise_()
        if hasattr(self, "poster_label"):
            self.poster_label.show()
            self.poster_label.raise_()
        if hasattr(self, "video_container"):
            self.video_container.setCursor(Qt.CursorShape.PointingHandCursor)
        # Show error in a small label overlay
        if hasattr(self, "_cloud_error_label"):
            self._cloud_error_label.setText(f"⚠ Could not stream: {error[:80]}")
            self._cloud_error_label.setVisible(True)
            QTimer.singleShot(6000, lambda: self._cloud_error_label.setVisible(False))

    def _on_cloud_stream_progress(self, original_url: str, message: str):
        """Called with intermediate status messages (e.g. 'Downloading MEGA…')."""
        if original_url == self._pending_cloud_url:
            self.loading_overlay.set_message(message)

    def _set_play_btn_state(self, playing: bool):
        if playing:
            self.play_btn.setIcon(QIcon(get_asset_path("assets/Svg/pause.svg")))
            self.play_btn.setText("  Pause")
        else:
            self.play_btn.setIcon(QIcon(get_asset_path("assets/uisvg/play.svg")))
            self.play_btn.setText("  Play")

    def _update_mute_btn(self):
        is_muted = self.audio.isMuted() or self.audio.volume() == 0
        icon_path = "assets/Svg/speaker-slash.svg" if is_muted else "assets/Svg/speaker-high.svg"
        self.mute_btn.setIcon(QIcon(get_asset_path(icon_path)))
        self.mute_btn.setText("")

    def toggle_play(self):
        if not self._has_started:
            self._start_media()
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        else:
            self.play()

    def play(self):
        if not self._has_started:
            self._start_media()
            return
        self.player.play()
        self._set_play_btn_state(True)

    def pause(self):
        if not self._has_started:
            return
        self.player.pause()
        self._set_play_btn_state(False)

    def stop(self):
        if hasattr(self, "loading_overlay"):
            self.loading_overlay.stop_loading()
        self.player.stop()
        self._set_play_btn_state(False)

    def is_playing(self) -> bool:
        return self._has_started and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def skip(self, ms: int):
        new_pos = max(0, min(self.player.duration(), self.player.position() + ms))
        self.player.setPosition(new_pos)

    def stop_and_release(self):
        if hasattr(self, "loading_overlay"):
            self.loading_overlay.stop_loading()
        # Stop any active cloud resolver / MEGA proxy
        if hasattr(self, "_cloud_resolver") and self._cloud_resolver:
            self._cloud_resolver.stop()
            self._cloud_resolver = None
        self._pending_cloud_url = None
        self._has_started = False
        self.player.stop()
        self.player.setSource(QUrl())
        self._set_play_btn_state(False)
        if hasattr(self, "poster_label"):
            self.poster_label.show()
            self.poster_label.raise_()
        if hasattr(self, "play_overlay_btn"):
            self.play_overlay_btn.show()
            self.play_overlay_btn.raise_()
        if hasattr(self, "video_container"):
            self.video_container.setCursor(Qt.CursorShape.PointingHandCursor)

    def _on_slider_pressed(self):
        self._is_user_scrubbing = True

    def _on_slider_released(self):
        self._is_user_scrubbing = False
        self.player.setPosition(self.seek_slider.value())

    def _on_slider_moved(self, pos):
        self.time_lbl.setText(f"{format_time(pos)} / {format_time(self.player.duration())}")

    def _on_position_changed(self, pos):
        if pos > 0:
            if hasattr(self, "poster_label") and self.poster_label.isVisible():
                self.poster_label.hide()
            if hasattr(self, "loading_overlay") and self.loading_overlay.isVisible():
                self.loading_overlay.stop_loading()
        if not self._is_user_scrubbing:
            self.seek_slider.setValue(pos)
            self.time_lbl.setText(f"{format_time(pos)} / {format_time(self.player.duration())}")

    def _on_duration_changed(self, duration):
        self.seek_slider.setRange(0, duration)
        self.time_lbl.setText(f"{format_time(self.player.position())} / {format_time(duration)}")

    def _on_playback_state_changed(self, state):
        is_playing = (state == QMediaPlayer.PlaybackState.PlayingState)
        self._set_play_btn_state(is_playing)
        if is_playing and hasattr(self, "poster_label"):
            self.poster_label.hide()

    def _on_media_status_changed(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadingMedia,):
            self.loading_overlay.start_loading("Connecting to stream...")
        elif status in (QMediaPlayer.MediaStatus.BufferingMedia, QMediaPlayer.MediaStatus.StalledMedia):
            self.loading_overlay.start_loading("Buffering video...")
        elif status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.loading_overlay.stop_loading()
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.loading_overlay.stop_loading()
            if self._is_looping:
                self.player.setPosition(0)
                self.player.play()
            else:
                self._set_play_btn_state(False)
        elif status in (QMediaPlayer.MediaStatus.InvalidMedia, QMediaPlayer.MediaStatus.NoMedia):
            self.loading_overlay.stop_loading()

    def _on_buffer_progress_changed(self, progress: float):
        if progress >= 1.0 or self.player.position() > 0:
            if hasattr(self, "loading_overlay") and self.loading_overlay.isVisible():
                self.loading_overlay.stop_loading()
        elif hasattr(self, "loading_overlay") and self.loading_overlay.isVisible():
            pct = int(progress * 100)
            if pct > 0:
                self.loading_overlay.set_message(f"Buffering {pct}%...")

    def _on_media_error(self, error, error_string):
        if hasattr(self, "loading_overlay"):
            self.loading_overlay.stop_loading()

    def toggle_loop(self):
        self._toggle_loop()

    def _toggle_loop(self):
        self._is_looping = not self._is_looping
        self.loop_btn.setStyleSheet(
            "QPushButton { background-color: #352020; color: #E5A198; border: 1px solid #E5A198; font-weight: bold; }"
            if self._is_looping else
            "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; }"
        )

    def cycle_speed(self):
        self._cycle_speed()

    def set_volume(self, val: int):
        self.vol_slider.setValue(max(0, min(val, 100)))

    def _cycle_speed(self):
        self._speed_idx = (self._speed_idx + 1) % len(self._speeds)
        speed = self._speeds[self._speed_idx]
        self.player.setPlaybackRate(speed)
        self.speed_btn.setText(f"{speed}×")

    def _on_volume_changed(self, val):
        self.audio.setVolume(val / 100.0)
        self._update_mute_btn()

    def _toggle_mute(self):
        is_muted = self.audio.isMuted()
        self.audio.setMuted(not is_muted)
        self._update_mute_btn()


# ─────────────────────────────────────────────────────────────────────────────
#  Animated GIF Player (Hardware-Accelerated Aspect Ratio & Frame Controls)
# ─────────────────────────────────────────────────────────────────────────────

class _GifCanvasView(QGraphicsView):
    """
    Smooth, hardware-accelerated canvas for animated GIFs with pan-drag,
    wheel zoom, and click-to-play support with strict aspect ratio preservation.
    """
    clicked = Signal()
    double_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QBrush(QColor("#0E1015")))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._drag_start = None
        self._dragged = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
            self._dragged = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start and (event.pos() - self._drag_start).manhattanLength() > 5:
            self._dragged = True
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._dragged:
                self.clicked.emit()
            self._drag_start = None
            self._dragged = False

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()


class GifPlayerWidget(QWidget):
    """
    Comprehensive Animated GIF Player:
    - Never stretches or distorts portrait or landscape GIFs (100% aspect ratio preserved)
    - Full playback controls: Play/Pause, Step Back/Forward (-1/+1 frame), Restart, Speed selection
    - Interactive viewer controls: Fit, 1:1 Actual Resolution, Zoom In/Out, Rotate 90°
    - Detailed frame status: Frame X/Y, native dimensions, portrait/landscape badge, speed
    - Smooth mouse-wheel zooming and hand drag pan
    """
    play_requested = Signal()
    fullscreen_requested = Signal()

    def __init__(self, parent=None, scale: float = 1.0):
        super().__init__(parent)
        self.scale = scale
        self.ui_scale = scale
        self._url: str = ""
        self._disk_path: str = ""
        self._fallback_urls: list = []
        self._preview_pixmap: QPixmap | None = None
        self._is_loaded: bool = False
        self._is_fitted: bool = True
        self._zoom_factor: float = 1.0
        self._rotation_angle: int = 0
        self._native_size: QSize = QSize(0, 0)

        self._build_ui()

    def _build_ui(self):
        scale = self.scale
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 1. Canvas View & Scene
        self.canvas = _GifCanvasView(self)
        self.scene = QGraphicsScene(self.canvas)
        self.pixmap_item = QGraphicsPixmapItem()
        self.pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene.addItem(self.pixmap_item)
        self.canvas.setScene(self.scene)

        self.canvas.clicked.connect(self._on_container_clicked)
        self.canvas.double_clicked.connect(self._on_double_clicked)

        # Centered "PLAY GIF" Overlay Button
        self.overlay_btn = QPushButton("  GIF  ", self.canvas.viewport())
        self.overlay_btn.setObjectName("gifPlayOverlayBtn")
        self.overlay_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overlay_btn.setIcon(QIcon(get_asset_path("assets/Svg/play.svg")))
        self.overlay_btn.setIconSize(QSize(int(18 * scale), int(18 * scale)))
        self.overlay_btn.setStyleSheet(
            f"QPushButton#gifPlayOverlayBtn {{ "
            f"background-color: rgba(20, 22, 28, 225); "
            f"color: #FFFFFF; "
            f"border: 2px solid #E5A198; "
            f"border-radius: {int(22 * scale)}px; "
            f"font-size: {int(11 * scale)}pt; "
            f"font-weight: bold; "
            f"padding: {int(8 * scale)}px {int(22 * scale)}px; "
            f"}} "
            f"QPushButton#gifPlayOverlayBtn:hover {{ "
            f"background-color: #E5A198; "
            f"color: #12141A; "
            f"border-color: #FFFFFF; "
            f"}}"
        )
        self.overlay_btn.clicked.connect(self._on_container_clicked)

        # Loading / Buffering Spinner Overlay
        self.loading_overlay = VideoLoadingOverlay(self.canvas.viewport())

        root.addWidget(self.canvas, 1)

        # 2. Bottom Control Strip
        controls = QFrame(self)
        controls.setStyleSheet("background-color: #16181F; border-top: 1px solid #2B2E38;")
        ctrl_h = QHBoxLayout(controls)
        ctrl_h.setContentsMargins(int(10 * scale), int(5 * scale), int(10 * scale), int(5 * scale))
        ctrl_h.setSpacing(int(6 * scale))

        btn_style = (
            "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; "
            "border-radius: 4px; font-size: 8.5pt; font-weight: 500; padding: 3px 8px; } "
            "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; } "
            "QPushButton:disabled { background-color: #1A1C22; color: #555A66; border-color: #2A2D36; }"
        )

        # Play / Pause button
        self.play_btn = QPushButton("  Play GIF")
        self.play_btn.setIcon(QIcon(get_asset_path("assets/uisvg/play.svg")))
        self.play_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        self.play_btn.setFixedHeight(int(26 * scale))
        self.play_btn.setStyleSheet(btn_style)
        self.play_btn.clicked.connect(self._toggle_play)
        ctrl_h.addWidget(self.play_btn)

        # Restart button
        self.restart_btn = QPushButton("  Restart")
        self.restart_btn.setIcon(QIcon(get_asset_path("assets/Svg2/repeat.svg")))
        self.restart_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        self.restart_btn.setFixedHeight(int(26 * scale))
        self.restart_btn.setStyleSheet(btn_style)
        self.restart_btn.clicked.connect(self._restart)
        ctrl_h.addWidget(self.restart_btn)

        # Step -1 Frame
        self.step_back_btn = QPushButton("◀|")
        self.step_back_btn.setFixedSize(int(28 * scale), int(26 * scale))
        self.step_back_btn.setToolTip("Step 1 Frame Backward")
        self.step_back_btn.setStyleSheet(btn_style)
        self.step_back_btn.clicked.connect(self.step_back)
        ctrl_h.addWidget(self.step_back_btn)

        # Step +1 Frame
        self.step_fwd_btn = QPushButton("|▶")
        self.step_fwd_btn.setFixedSize(int(28 * scale), int(26 * scale))
        self.step_fwd_btn.setToolTip("Step 1 Frame Forward")
        self.step_fwd_btn.setStyleSheet(btn_style)
        self.step_fwd_btn.clicked.connect(self.step_forward)
        ctrl_h.addWidget(self.step_fwd_btn)

        # Speed Dropdown
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.25x", "0.5x", "0.75x", "1.0x", "1.25x", "1.5x", "2.0x"])
        self.speed_combo.setCurrentText("1.0x")
        self.speed_combo.setFixedHeight(int(26 * scale))
        self.speed_combo.setToolTip("Playback Speed")
        self.speed_combo.setStyleSheet(
            "QComboBox { background: #242730; border: 1px solid #363A45; border-radius: 4px; "
            "color: #DFE1E5; padding: 2px 6px; font-size: 8pt; font-weight: bold; } "
            "QComboBox QAbstractItemView { background: #242730; color: #FFFFFF; selection-background-color: #3B82F6; }"
        )
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        ctrl_h.addWidget(self.speed_combo)

        ctrl_h.addStretch(1)

        # Frame counter & details label
        self.info_lbl = QLabel("Animated GIF • Click to play")
        self.info_lbl.setStyleSheet("color: #8C929E; font-size: 8pt; font-weight: bold;")
        ctrl_h.addWidget(self.info_lbl)

        root.addWidget(controls)

        # Movie controller
        self.movie = QMovie(self)
        self.movie.frameChanged.connect(self._on_frame_changed)

    def _on_container_clicked(self):
        if not self._is_loaded or self.movie.state() == QMovie.MovieState.NotRunning:
            self.play_requested.emit()
        else:
            self._toggle_play()

    def _on_double_clicked(self):
        if not self._is_loaded:
            self.play_requested.emit()
            return
        if self._is_fitted:
            self.set_actual_size()
        else:
            self.fit_in_window()

    def _on_frame_changed(self, frame_no: int):
        pm = self.movie.currentPixmap()
        if not pm.isNull():
            self.pixmap_item.setPixmap(pm)
        self._update_info_label()

    def _on_speed_changed(self, speed_text: str):
        speeds = {"0.25x": 25, "0.5x": 50, "0.75x": 75, "1.0x": 100, "1.25x": 125, "1.5x": 150, "2.0x": 200}
        percent = speeds.get(speed_text, 100)
        self.movie.setSpeed(percent)
        self._update_info_label()

    def step_forward(self):
        if not self._is_loaded or self.movie.state() == QMovie.MovieState.NotRunning:
            if self._disk_path and os.path.exists(self._disk_path) and os.path.getsize(self._disk_path) > 128:
                self.load_gif(self._disk_path)
                self.pause()
            else:
                self.play_requested.emit()
                return
        self.pause()
        cur = self.movie.currentFrameNumber()
        tot = self.movie.frameCount()

        # If already at the last frame and total count is known, wrap back to 0
        if tot > 0 and cur >= tot - 1:
            self.movie.jumpToFrame(0)
        else:
            if not self.movie.jumpToNextFrame():
                self.movie.jumpToFrame(0)

        pm = self.movie.currentPixmap()
        if not pm.isNull():
            self.pixmap_item.setPixmap(pm)
        self._update_info_label()

    def step_back(self):
        if not self._is_loaded or self.movie.state() == QMovie.MovieState.NotRunning:
            if self._disk_path and os.path.exists(self._disk_path) and os.path.getsize(self._disk_path) > 128:
                self.load_gif(self._disk_path)
                self.pause()
            else:
                self.play_requested.emit()
                return
        self.pause()
        cur = self.movie.currentFrameNumber()
        tot = self.movie.frameCount()

        if tot > 0:
            target = (cur - 1) % tot
        else:
            target = max(0, cur - 1)

        if target == cur:
            return

        if target < cur:
            # QMovie cannot jump backwards to non-zero frames directly;
            # must rewind to 0 then advance forward to target frame
            self.movie.jumpToFrame(0)
            if target > 0:
                while self.movie.currentFrameNumber() < target:
                    if not self.movie.jumpToNextFrame():
                        break
        else:
            # Wrap-around from 0 to last frame
            while self.movie.currentFrameNumber() < target:
                if not self.movie.jumpToNextFrame():
                    break

        pm = self.movie.currentPixmap()
        if not pm.isNull():
            self.pixmap_item.setPixmap(pm)
        self._update_info_label()

    def fit_in_window(self):
        """Fits image in current viewport preserving aspect ratio (never stretches)."""
        self.canvas.resetTransform()
        self._zoom_factor = 1.0
        self._rotation_angle = 0
        self._is_fitted = True
        if not self.pixmap_item.pixmap().isNull():
            self.canvas.fitInView(self.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    def set_actual_size(self):
        """Renders image at 100% 1:1 original native resolution."""
        self.canvas.resetTransform()
        self._zoom_factor = 1.0
        self._is_fitted = False
        if self._rotation_angle:
            self.canvas.rotate(self._rotation_angle)

    def zoom_in(self):
        self._is_fitted = False
        if self._zoom_factor < 20.0:
            self.canvas.scale(1.2, 1.2)
            self._zoom_factor *= 1.2

    def zoom_out(self):
        self._is_fitted = False
        if self._zoom_factor > 0.05:
            self.canvas.scale(1 / 1.2, 1 / 1.2)
            self._zoom_factor /= 1.2

    def rotate_cw(self):
        self._rotation_angle = (self._rotation_angle + 90) % 360
        self.canvas.rotate(90)

    def wheelEvent(self, event):
        if not self.pixmap_item.pixmap().isNull():
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            elif delta < 0:
                self.zoom_out()
        else:
            super().wheelEvent(event)

    def get_native_size(self) -> QSize:
        return self._native_size

    def get_current_pixmap(self) -> QPixmap:
        return self.pixmap_item.pixmap()

    def set_media(self, url: str, disk_path: str = "", preview_pixmap: QPixmap = None, fallback_urls: list = None):
        """Prepares the player in static poster / thumbnail mode without auto-downloading full GIF."""
        self.movie.stop()
        self._url = url
        self._disk_path = disk_path
        self._fallback_urls = fallback_urls or []
        self._is_loaded = False
        self._native_size = QSize(0, 0)

        if not preview_pixmap and disk_path and os.path.exists(disk_path) and os.path.getsize(disk_path) > 128:
            try:
                reader = QImageReader(disk_path)
                img = reader.read()
                if not img.isNull():
                    preview_pixmap = QPixmap.fromImage(img)
            except Exception:
                pass

        self._preview_pixmap = preview_pixmap
        self._update_preview_display()

        self.overlay_btn.setVisible(True)
        self.overlay_btn.raise_()
        self.loading_overlay.stop_loading()

        self.play_btn.setEnabled(True)
        self.play_btn.setIcon(QIcon(get_asset_path("assets/uisvg/play.svg")))
        self.play_btn.setText("  Play GIF")
        self._update_info_label()
        self._reposition_overlays()

    def set_preview_pixmap(self, pixmap: QPixmap):
        """Sets or updates the static preview poster while in poster mode."""
        if not pixmap or pixmap.isNull():
            return
        self._preview_pixmap = pixmap
        if not self._is_loaded or self.movie.state() == QMovie.MovieState.NotRunning:
            self._update_preview_display()

    def show_loading(self, message: str = "Loading Animated GIF..."):
        """Displays spinner and status while full GIF media streams."""
        self.overlay_btn.setVisible(False)
        self.loading_overlay.start_loading(message)
        self.info_lbl.setText("Loading Animated GIF...")
        self.play_btn.setEnabled(False)

    def set_loading_progress(self, percent: int):
        self.loading_overlay.set_message(f"Loading GIF ({percent}%)...")

    def load_gif(self, file_path: str):
        """Loads and plays full animated GIF once downloaded."""
        if not os.path.exists(file_path):
            return
        self._disk_path = file_path
        self._is_loaded = True
        self.loading_overlay.stop_loading()
        self.overlay_btn.setVisible(False)
        self.play_btn.setEnabled(True)

        self.movie.stop()
        self.movie.setFileName(file_path)
        self.movie.start()
        self.movie.jumpToFrame(0)

        pm = self.movie.currentPixmap()
        if not pm.isNull() and pm.width() > 0:
            self._native_size = pm.size()
            self.pixmap_item.setPixmap(pm)
            self.scene.setSceneRect(QRectF(pm.rect()))
            self.fit_in_window()

        self.play_btn.setIcon(QIcon(get_asset_path("assets/Svg/pause.svg")))
        self.play_btn.setText("  Pause")
        self._update_info_label()

    def stop(self):
        """Stops movie and resets back to thumbnail poster if available."""
        self.movie.stop()
        self.loading_overlay.stop_loading()
        self.overlay_btn.setVisible(True)
        self.overlay_btn.raise_()
        self.play_btn.setEnabled(True)
        self.play_btn.setIcon(QIcon(get_asset_path("assets/uisvg/play.svg")))
        self.play_btn.setText("  Play GIF")
        if self._preview_pixmap:
            self._update_preview_display()
        else:
            self._update_info_label()

    def play(self):
        if not self._is_loaded or self.movie.state() == QMovie.MovieState.NotRunning:
            self.play_requested.emit()
            return
        self.movie.setPaused(False)
        self.play_btn.setIcon(QIcon(get_asset_path("assets/Svg/pause.svg")))
        self.play_btn.setText("  Pause")
        self._update_info_label()

    def pause(self):
        if self.movie.state() == QMovie.MovieState.Running and not self.movie.state() == QMovie.MovieState.Paused:
            self.movie.setPaused(True)
            self.play_btn.setIcon(QIcon(get_asset_path("assets/uisvg/play.svg")))
            self.play_btn.setText("  Play")
            self._update_info_label()

    def _toggle_play(self):
        if not self._is_loaded or self.movie.state() == QMovie.MovieState.NotRunning:
            self.play_requested.emit()
        elif self.movie.state() == QMovie.MovieState.Running and not self.movie.state() == QMovie.MovieState.Paused:
            self.pause()
        else:
            self.play()

    def _restart(self):
        if not self._is_loaded or self.movie.state() == QMovie.MovieState.NotRunning:
            self.play_requested.emit()
            return
        self.movie.stop()
        self.movie.start()
        self.play_btn.setIcon(QIcon(get_asset_path("assets/Svg/pause.svg")))
        self.play_btn.setText("  Pause")
        self._update_info_label()

    def is_playing(self) -> bool:
        return self._is_loaded and self.movie.state() == QMovie.MovieState.Running and not self.movie.state() == QMovie.MovieState.Paused

    def is_loaded(self) -> bool:
        return self._is_loaded

    def _update_info_label(self):
        if not self._is_loaded or self.movie.state() == QMovie.MovieState.NotRunning:
            if self._native_size.isValid() and self._native_size.width() > 0:
                w, h = self._native_size.width(), self._native_size.height()
                orientation = "Portrait" if h > w else "Landscape"
                self.info_lbl.setText(f"Animated GIF • {w}×{h} ({orientation}) • Click to play")
            else:
                self.info_lbl.setText("Animated GIF • Click to play")
            return

        cur = self.movie.currentFrameNumber() + 1
        tot = self.movie.frameCount()
        frame_str = f"Frame {cur}/{tot}" if tot > 0 else f"Frame {cur}"
        w = self._native_size.width()
        h = self._native_size.height()
        dim_str = f" • {w}×{h}" if w > 0 else ""
        orientation = f" ({'Portrait' if h > w else 'Landscape'})" if (w > 0 and h > 0) else ""
        speed = self.speed_combo.currentText()
        state = "Paused" if self.movie.state() == QMovie.MovieState.Paused else "Playing"
        self.info_lbl.setText(f"{frame_str}{dim_str}{orientation} • {speed} [{state}]")

    def _update_preview_display(self):
        if self._preview_pixmap and not self._preview_pixmap.isNull():
            self._native_size = self._preview_pixmap.size()
            self.pixmap_item.setPixmap(self._preview_pixmap)
            self.scene.setSceneRect(QRectF(self._preview_pixmap.rect()))
            self.fit_in_window()
        else:
            self.pixmap_item.setPixmap(QPixmap())
            self.scene.setSceneRect(QRectF(0, 0, 100, 100))
        self._update_info_label()

    def _reposition_overlays(self):
        if hasattr(self, "overlay_btn") and hasattr(self, "canvas"):
            btn_w = self.overlay_btn.sizeHint().width() + int(36 * self.scale)
            btn_h = int(44 * self.scale)
            self.overlay_btn.resize(btn_w, btn_h)
            cx = (self.canvas.viewport().width() - btn_w) // 2
            cy = (self.canvas.viewport().height() - btn_h) // 2
            self.overlay_btn.move(max(0, cx), max(0, cy))
        if hasattr(self, "loading_overlay") and hasattr(self, "canvas"):
            self.loading_overlay.setGeometry(0, 0, self.canvas.viewport().width(), self.canvas.viewport().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_overlays()
        if getattr(self, "_is_fitted", False):
            self.fit_in_window()


# ─────────────────────────────────────────────────────────────────────────────
#  Master Universal Media Viewer Dialog
# ─────────────────────────────────────────────────────────────────────────────

class PawchiveMediaViewerDialog(QDialog):
    """
    In-App Universal Media Player & HD Viewer Dialog matching the Pawchive client:
    - Streams MP4/WebM videos directly with seek, speeds, volume, loop
    - Displays full-resolution HD/4K images with smooth zoom, pan, rotation
    - Plays animated GIFs with pause, restart, and loop
    - Carousel strip with thumbnails of all post attachments
    - Floating left/right navigation arrows + keyboard shortcuts
    """
    def __init__(self, post: dict, initial_index: int = 0, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.post = post or {}
        self.scale = scale
        self.parent_app = getattr(parent, "parent_app", parent)

        self.media_items = extract_media_items_from_post(self.post)
        self.current_index = max(0, min(initial_index, len(self.media_items) - 1)) if self.media_items else 0

        # Cache directory
        base_dir = getattr(self.parent_app, "app_base_dir", ".")
        self.cache_dir = os.path.join(base_dir, "cache", "media_cache")
        os.makedirs(self.cache_dir, exist_ok=True)

        self.stream_worker = None
        self.batch_hd_worker = None
        self._is_fullscreen = False
        self._loaded_full_res_indices = set()

        self.setWindowTitle(f"Media Viewer — {self.post.get('title', 'Pawchive')}")
        self.resize(int(1050 * scale), int(720 * scale))
        self.setStyleSheet("background-color: #12141A; color: #FFFFFF;")

        self._build_ui()
        self._load_current_media()

    def _build_ui(self):
        scale = self.scale
        root_v = QVBoxLayout(self)
        root_v.setContentsMargins(0, 0, 0, 0)
        root_v.setSpacing(0)

        # ── 1. Top Header Bar ─────────────────────────────────────────────────
        top_bar = QFrame(self)
        top_bar.setFixedHeight(int(46 * scale))
        top_bar.setStyleSheet("background-color: #161820; border-bottom: 1px solid #2B2E38;")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(int(12 * scale), 0, int(12 * scale), 0)
        top_layout.setSpacing(int(8 * scale))

        # Title & Creator
        title_text = self.post.get("title") or "Untitled Post"
        cname = self.post.get("_creator_name") or "Creator"
        self.title_lbl = QLabel(f"<b>{title_text}</b> &nbsp;<span style='color: #8C929E;'>by {cname}</span>")
        self.title_lbl.setStyleSheet(f"font-size: {int(9.5 * scale)}pt; color: #FFFFFF;")
        top_layout.addWidget(self.title_lbl, 1)

        # Counter badge
        self.counter_lbl = QLabel("1 / 1")
        self.counter_lbl.setStyleSheet(
            "background-color: #272A34; color: #E5A198; border: 1px solid #3E4352; "
            f"border-radius: {int(4 * scale)}px; padding: {int(2 * scale)}px {int(8 * scale)}px; font-weight: bold; font-size: {int(8 * scale)}pt;"
        )
        top_layout.addWidget(self.counter_lbl)

        # Zoom Controls (visible for images)
        self.zoom_box = QFrame()
        zb_layout = QHBoxLayout(self.zoom_box)
        zb_layout.setContentsMargins(0, 0, 0, 0)
        zb_layout.setSpacing(int(4 * scale))

        self.dialog_load_hd_btn = QPushButton(" Load HD")
        self.dialog_load_hd_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_in.svg")))
        self.dialog_load_hd_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        self.dialog_load_hd_btn.setFixedHeight(int(26 * scale))
        self.dialog_load_hd_btn.setToolTip("Download & load full native resolution image")
        self.dialog_load_hd_btn.setStyleSheet(
            "QPushButton { background-color: #2F2424; color: #E5A198; border: 1px solid #5A3535; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 2px 8px; }"
            "QPushButton:hover { background-color: #E5A198; color: #161820; }"
        )
        self.dialog_load_hd_btn.clicked.connect(self._on_dialog_image_load_hd_requested)
        zb_layout.addWidget(self.dialog_load_hd_btn)

        self.dialog_load_all_hd_btn = QPushButton(" Load All HD")
        self.dialog_load_all_hd_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_in.svg")))
        self.dialog_load_all_hd_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        self.dialog_load_all_hd_btn.setFixedHeight(int(26 * scale))
        self.dialog_load_all_hd_btn.setToolTip("Pre-load and cache all images in this post in full native HD")
        self.dialog_load_all_hd_btn.setStyleSheet(
            "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 2px 8px; }"
            "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
        )
        self.dialog_load_all_hd_btn.clicked.connect(self._on_dialog_load_all_hd_clicked)
        zb_layout.addWidget(self.dialog_load_all_hd_btn)

        self.zoom_out_btn = QPushButton()
        self.zoom_out_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_out.svg")))
        self.zoom_out_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.zoom_out_btn.setFixedSize(int(26 * scale), int(26 * scale))
        self.zoom_out_btn.setToolTip("Zoom Out (-)")
        self.zoom_out_btn.clicked.connect(self._zoom_out_active)
        zb_layout.addWidget(self.zoom_out_btn)

        self.fit_btn = QPushButton("Fit")
        self.fit_btn.setFixedHeight(int(26 * scale))
        self.fit_btn.setToolTip("Fit in window (0)")
        self.fit_btn.clicked.connect(self._fit_active)
        zb_layout.addWidget(self.fit_btn)

        self.actual_btn = QPushButton("1:1")
        self.actual_btn.setFixedHeight(int(26 * scale))
        self.actual_btn.setToolTip("100% Original Size")
        self.actual_btn.clicked.connect(self._actual_size_active)
        zb_layout.addWidget(self.actual_btn)

        self.zoom_in_btn = QPushButton()
        self.zoom_in_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_in.svg")))
        self.zoom_in_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.zoom_in_btn.setFixedSize(int(26 * scale), int(26 * scale))
        self.zoom_in_btn.setToolTip("Zoom In (+)")
        self.zoom_in_btn.clicked.connect(self._zoom_in_active)
        zb_layout.addWidget(self.zoom_in_btn)

        self.rotate_btn = QPushButton()
        self.rotate_btn.setIcon(QIcon(get_asset_path("assets/Svg2/repeat.svg")))
        self.rotate_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.rotate_btn.setFixedSize(int(26 * scale), int(26 * scale))
        self.rotate_btn.setToolTip("Rotate 90° (R)")
        self.rotate_btn.clicked.connect(self._rotate_active)
        zb_layout.addWidget(self.rotate_btn)

        top_layout.addWidget(self.zoom_box)

        # Action Buttons
        self.download_file_btn = QPushButton("  Download File")
        self.download_file_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.download_file_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.download_file_btn.setFixedHeight(int(26 * scale))
        self.download_file_btn.setToolTip("Queue this specific media file for download in Downloader tab")
        self.download_file_btn.clicked.connect(self._download_current_file)
        top_layout.addWidget(self.download_file_btn)

        self.copy_url_btn = QPushButton("  Copy Link")
        self.copy_url_btn.setIcon(QIcon(get_asset_path("assets/Svg/link.svg")))
        self.copy_url_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.copy_url_btn.setFixedHeight(int(26 * scale))
        self.copy_url_btn.clicked.connect(self._copy_current_url)
        top_layout.addWidget(self.copy_url_btn)

        self.browser_btn = QPushButton("  Browser")
        self.browser_btn.setIcon(QIcon(get_asset_path("assets/Svg/eye_open.svg")))
        self.browser_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.browser_btn.setFixedHeight(int(26 * scale))
        self.browser_btn.clicked.connect(self._open_in_browser)
        top_layout.addWidget(self.browser_btn)

        self.fs_toggle_btn = QPushButton()
        self.fs_toggle_btn.setIcon(QIcon(get_asset_path("assets/Svg/fullscreen.svg")))
        self.fs_toggle_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.fs_toggle_btn.setFixedSize(int(26 * scale), int(26 * scale))
        self.fs_toggle_btn.setToolTip("Toggle Fullscreen (F11)")
        self.fs_toggle_btn.clicked.connect(self.toggle_fullscreen)
        top_layout.addWidget(self.fs_toggle_btn)

        self.close_btn = QPushButton()
        self.close_btn.setIcon(QIcon(get_asset_path("assets/uisvg/x_circle.svg")))
        self.close_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.close_btn.setFixedSize(int(26 * scale), int(26 * scale))
        self.close_btn.setStyleSheet("QPushButton { font-weight: bold; } QPushButton:hover { background-color: #8C2E2E; color: #FFFFFF; }")
        self.close_btn.clicked.connect(self.close)
        top_layout.addWidget(self.close_btn)

        root_v.addWidget(top_bar)

        # ── 2. Center Content Area (Stack + Overlay Arrows) ───────────────────
        content_container = QFrame(self)
        content_container.setStyleSheet("background-color: #0E1015;")
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.media_stack = QStackedWidget(content_container)

        # View 0: Video Player
        self.video_player = VideoPlayerWidget(self.media_stack)
        self.video_player.fullscreen_requested.connect(self.toggle_fullscreen)
        self.media_stack.addWidget(self.video_player)

        # View 1: HD Image Zoom View
        self.image_view = ImageZoomView(self.media_stack, scale=self.scale)
        self.image_view.load_full_res_requested.connect(self._on_dialog_image_load_hd_requested)
        self.media_stack.addWidget(self.image_view)

        # View 2: Animated GIF Player
        self.gif_player = GifPlayerWidget(self.media_stack, scale=self.scale)
        self.gif_player.play_requested.connect(self._on_dialog_gif_play_requested)
        self.gif_player.fullscreen_requested.connect(self.toggle_fullscreen)
        self.media_stack.addWidget(self.gif_player)

        # View 3: Unsupported / Text File Fallback
        self.unsupported_widget = QWidget(self.media_stack)
        un_layout = QVBoxLayout(self.unsupported_widget)
        un_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.unsupported_icon = QLabel()
        self.unsupported_icon.setPixmap(QIcon(get_asset_path("assets/uisvg/attach.svg")).pixmap(int(48 * scale), int(48 * scale)))
        self.unsupported_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.unsupported_lbl = QLabel("This file cannot be previewed directly.\nClick 'Download File' to save it.")
        self.unsupported_lbl.setStyleSheet("color: #8C929E; font-size: 11pt; text-align: center;")
        self.unsupported_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        un_layout.addWidget(self.unsupported_icon)
        un_layout.addWidget(self.unsupported_lbl)
        self.media_stack.addWidget(self.unsupported_widget)

        content_layout.addWidget(self.media_stack, 1)

        # Floating Loading Progress Pill (overlaid at bottom of content)
        self.progress_pill = QLabel(content_container)
        self.progress_pill.setStyleSheet(
            "background-color: rgba(26, 28, 35, 230); color: #E5A198; border: 1px solid #4F5565; "
            f"border-radius: {int(6 * scale)}px; padding: {int(4 * scale)}px {int(12 * scale)}px; font-weight: bold; font-size: {int(8.5 * scale)}pt;"
        )
        self.progress_pill.setVisible(False)

        # Floating Navigation Arrows
        self.prev_arrow_btn = QPushButton(content_container)
        self.prev_arrow_btn.setIcon(QIcon(get_asset_path("assets/Svg/previous.svg")))
        self.prev_arrow_btn.setIconSize(QSize(int(20 * scale), int(20 * scale)))
        self.prev_arrow_btn.setFixedSize(int(42 * scale), int(56 * scale))
        self.prev_arrow_btn.setStyleSheet(
            "QPushButton { background-color: rgba(22, 24, 30, 180); color: #DFE1E5; border: 1px solid rgba(62, 67, 80, 160); "
            f"border-radius: {int(6 * scale)}px; font-size: {int(14 * scale)}pt; font-weight: bold; }}"
            "QPushButton:hover { background-color: rgba(229, 161, 152, 220); color: #161820; border-color: #E5A198; }"
        )
        self.prev_arrow_btn.clicked.connect(self.prev_item)

        self.next_arrow_btn = QPushButton(content_container)
        self.next_arrow_btn.setIcon(QIcon(get_asset_path("assets/Svg/next.svg")))
        self.next_arrow_btn.setIconSize(QSize(int(20 * scale), int(20 * scale)))
        self.next_arrow_btn.setFixedSize(int(42 * scale), int(56 * scale))
        self.next_arrow_btn.setStyleSheet(
            "QPushButton { background-color: rgba(22, 24, 30, 180); color: #DFE1E5; border: 1px solid rgba(62, 67, 80, 160); "
            f"border-radius: {int(6 * scale)}px; font-size: {int(14 * scale)}pt; font-weight: bold; }}"
            "QPushButton:hover { background-color: rgba(229, 161, 152, 220); color: #161820; border-color: #E5A198; }"
        )
        self.next_arrow_btn.clicked.connect(self.next_item)

        root_v.addWidget(content_container, 1)

        # ── 3. Bottom Carousel Strip (When post has multiple attachments) ────
        self.carousel_frame = QFrame(self)
        self.carousel_frame.setFixedHeight(int(74 * scale))
        self.carousel_frame.setStyleSheet("background-color: #14161D; border-top: 1px solid #282B34;")
        c_layout = QHBoxLayout(self.carousel_frame)
        c_layout.setContentsMargins(int(10 * scale), int(6 * scale), int(10 * scale), int(6 * scale))
        c_layout.setSpacing(int(8 * scale))

        self.scroll_area = QScrollArea(self.carousel_frame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self.thumb_strip_widget = QWidget()
        self.thumb_strip_widget.setStyleSheet("background: transparent;")
        self.thumb_strip_layout = QHBoxLayout(self.thumb_strip_widget)
        self.thumb_strip_layout.setContentsMargins(0, 0, 0, 0)
        self.thumb_strip_layout.setSpacing(int(8 * scale))
        self.thumb_strip_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.scroll_area.setWidget(self.thumb_strip_widget)
        c_layout.addWidget(self.scroll_area)

        root_v.addWidget(self.carousel_frame)

        self._populate_carousel()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Position floating arrows
        h = self.height()
        btn_y = (h - self.carousel_frame.height() - self.prev_arrow_btn.height()) // 2
        self.prev_arrow_btn.move(int(12 * self.scale), btn_y)
        self.next_arrow_btn.move(self.width() - self.next_arrow_btn.width() - int(12 * self.scale), btn_y)

        # Position progress pill
        pw = int(220 * self.scale)
        self.progress_pill.setGeometry(
            (self.width() - pw) // 2,
            h - self.carousel_frame.height() - int(50 * self.scale),
            pw, int(28 * self.scale)
        )

    def _populate_carousel(self):
        # Reset thumb buttons list immediately
        self.thumb_buttons = []

        # Clear previous items
        while self.thumb_strip_layout.count():
            item = self.thumb_strip_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        count = len(self.media_items)
        if count <= 1:
            self.carousel_frame.setVisible(False)
            self.prev_arrow_btn.setVisible(False)
            self.next_arrow_btn.setVisible(False)
            return

        self.carousel_frame.setVisible(True)
        self.prev_arrow_btn.setVisible(True)
        self.next_arrow_btn.setVisible(True)

        sz = int(58 * self.scale)

        for idx, item in enumerate(self.media_items):
            btn = QPushButton(self.thumb_strip_widget)
            btn.setFixedSize(sz, sz)
            btn.setToolTip(f"#{idx + 1}: {item['name']}")

            kind = item["kind"]
            svg_file = "assets/uisvg/video.svg" if kind in ("video", "gif") else "assets/uisvg/image.svg"
            btn.setIcon(QIcon(get_asset_path(svg_file)))
            btn.setIconSize(QSize(int(22 * self.scale), int(22 * self.scale)))
            btn.setText("")
            btn.setStyleSheet(
                "QPushButton { background-color: #20232B; color: #DFE1E5; border: 1.5px solid #333742; border-radius: 6px; }"
                "QPushButton:hover { background-color: #2A2E38; border-color: #E5A198; }"
            )
            btn.clicked.connect(lambda _, i=idx: self.switch_to_index(i))
            self.thumb_strip_layout.addWidget(btn)
            self.thumb_buttons.append(btn)

    def _load_current_media(self):
        if not self.media_items:
            self.media_stack.setCurrentIndex(3)
            return

        item = self.media_items[self.current_index]
        count = len(self.media_items)
        kind = item["kind"]

        # Update counter & badges
        kind_label = "Video" if kind == "video" else ("GIF" if kind == "gif" else "Image")
        svg_file = "assets/uisvg/video.svg" if kind in ("video", "gif") else "assets/uisvg/image.svg"
        self.counter_lbl.setText(f"<img src='{get_asset_html_path(svg_file)}' width='13' height='13'> &nbsp;<b>{kind_label}</b> &nbsp;{self.current_index + 1} / {count}")
        self.zoom_box.setVisible(kind in ("image", "gif"))

        # Update active carousel thumbnail border
        if getattr(self, "thumb_buttons", None) and len(self.thumb_buttons) > self.current_index:
            for idx, btn in enumerate(self.thumb_buttons):
                try:
                    if idx == self.current_index:
                        btn.setStyleSheet(
                            "QPushButton { background-color: #352020; color: #E5A198; border: 2px solid #E5A198; border-radius: 6px; font-size: 13pt; }"
                        )
                    else:
                        btn.setStyleSheet(
                            "QPushButton { background-color: #20232B; color: #DFE1E5; border: 1.5px solid #333742; border-radius: 6px; font-size: 13pt; }"
                            "QPushButton:hover { background-color: #2A2E38; border-color: #E5A198; }"
                        )
                except Exception:
                    pass

        # Stop previous playback
        self.video_player.stop_and_release()
        self.gif_player.stop()

        url = item["url"]
        ext = item.get("ext", ".bin")
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        disk_filename = f"{url_hash}{ext}"
        disk_path = os.path.join(self.cache_dir, disk_filename)

        if kind == "video":
            self.media_stack.setCurrentIndex(0)
            self.progress_pill.setVisible(False)

            preview_pix = None
            thumb_url = item.get("thumb_url")
            if thumb_url:
                t_hash = hashlib.md5(thumb_url.encode("utf-8")).hexdigest() + ".jpg"
                t_path = os.path.join(self.cache_dir, "..", "thumbs_cache", t_hash)
                if os.path.exists(t_path):
                    p = QPixmap(t_path)
                    if not p.isNull():
                        preview_pix = p

            self.video_player.set_media(url, disk_path, preview_pixmap=preview_pix)

        elif kind == "image":
            self.media_stack.setCurrentIndex(1)
            self.progress_pill.setVisible(False)

            # Check if full-resolution file is cached on disk
            if os.path.exists(disk_path) and os.path.getsize(disk_path) > 128:
                pix = QPixmap(disk_path)
                if not pix.isNull():
                    self._loaded_full_res_indices.add(self.current_index)
                    self.image_view.set_full_res_pixmap(pix, fit_to_view=True)
                    self._update_dialog_hd_btn_state(True)
                    sz_mb = os.path.getsize(disk_path) / (1024 * 1024)
                    self.title_lbl.setText(
                        f"<b>{item['name']}</b> &nbsp;"
                        f"<span style='color: #8C929E;'>({pix.width()}×{pix.height()} • {sz_mb:.1f} MB)</span>"
                    )
                    return

            self._update_dialog_hd_btn_state(False)
            preview_pix = None
            thumb_url = item.get("thumb_url")
            if thumb_url:
                t_hash = hashlib.md5(thumb_url.encode("utf-8")).hexdigest() + ".jpg"
                t_path = os.path.join(self.cache_dir, "..", "thumbs_cache", t_hash)
                if os.path.exists(t_path):
                    p = QPixmap(t_path)
                    if not p.isNull():
                        preview_pix = p

            self.image_view.set_preview_thumbnail(preview_pix, fit_to_view=True)

        elif kind == "gif":
            self.media_stack.setCurrentIndex(2)
            self.progress_pill.setVisible(False)

            preview_pix = None
            thumb_url = item.get("thumb_url")
            if thumb_url:
                t_hash = hashlib.md5(thumb_url.encode("utf-8")).hexdigest() + ".jpg"
                t_path = os.path.join(self.cache_dir, "..", "thumbs_cache", t_hash)
                if os.path.exists(t_path):
                    p = QPixmap(t_path)
                    if not p.isNull():
                        preview_pix = p

            if not preview_pix and os.path.exists(disk_path) and os.path.getsize(disk_path) > 128:
                try:
                    reader = QImageReader(disk_path)
                    img = reader.read()
                    if not img.isNull():
                        preview_pix = QPixmap.fromImage(img)
                except Exception:
                    pass

            self.gif_player.set_media(
                url=url,
                disk_path=disk_path,
                preview_pixmap=preview_pix,
                fallback_urls=item.get("fallback_urls", [])
            )

        else:
            self.media_stack.setCurrentIndex(3)
            self.progress_pill.setVisible(False)

    def _update_dialog_hd_btn_state(self, is_loaded: bool):
        curr_item = self.media_items[self.current_index] if (0 <= self.current_index < len(self.media_items)) else None
        is_img = bool(curr_item and curr_item.get("kind") == "image")

        if hasattr(self, "dialog_load_hd_btn"):
            if is_loaded or not is_img:
                self.dialog_load_hd_btn.setVisible(False)
            else:
                self.dialog_load_hd_btn.setVisible(True)
                self.dialog_load_hd_btn.setText(" Load HD")
                self.dialog_load_hd_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_in.svg")))
                self.dialog_load_hd_btn.setStyleSheet(
                    "QPushButton { background-color: #2F2424; color: #E5A198; border: 1px solid #5A3535; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 2px 8px; }"
                    "QPushButton:hover { background-color: #E5A198; color: #161820; }"
                )

        if hasattr(self, "dialog_load_all_hd_btn") and not (self.batch_hd_worker and self.batch_hd_worker.isRunning()):
            image_count = sum(1 for item in (self.media_items or []) if item.get("kind") == "image")
            unloaded_count = sum(
                1 for idx, it in enumerate(self.media_items)
                if it.get("kind") == "image" and idx not in self._loaded_full_res_indices
            )
            if unloaded_count > 0 and image_count > 1:
                self.dialog_load_all_hd_btn.setVisible(True)
                self.dialog_load_all_hd_btn.setText(f" Load All HD ({unloaded_count})")
            else:
                self.dialog_load_all_hd_btn.setVisible(False)

    def _on_dialog_load_all_hd_clicked(self):
        if not self.media_items:
            return
        if self.batch_hd_worker and self.batch_hd_worker.isRunning():
            return

        tasks = []
        for idx, item in enumerate(self.media_items):
            if item.get("kind") == "image":
                url = item["url"]
                ext = item.get("ext", ".bin")
                url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
                disk_filename = f"{url_hash}{ext}"
                disk_path = os.path.join(self.cache_dir, disk_filename)
                tasks.append({
                    "index": idx,
                    "url": url,
                    "disk_path": disk_path,
                    "fallback_urls": item.get("fallback_urls") or []
                })

        if not tasks:
            return

        self.dialog_load_all_hd_btn.setText(" Downloading HD (0%)...")
        self.dialog_load_all_hd_btn.setEnabled(False)
        self.progress_pill.setText("Downloading All Images in HD...")
        self.progress_pill.setVisible(True)

        self.batch_hd_worker = _BatchHDLoaderWorker(tasks, parent=self)
        self.batch_hd_worker.item_loaded.connect(self._on_dialog_batch_hd_item_loaded)
        self.batch_hd_worker.progress.connect(self._on_dialog_batch_hd_progress)
        self.batch_hd_worker.all_finished.connect(self._on_dialog_batch_hd_finished)
        self.batch_hd_worker.start()

    def _on_dialog_batch_hd_item_loaded(self, index: int, disk_path: str):
        self._loaded_full_res_indices.add(index)
        if self.current_index == index and hasattr(self, "image_view"):
            pix = QPixmap(disk_path)
            if not pix.isNull():
                self.image_view.set_full_res_pixmap(pix, fit_to_view=False)
                self._update_dialog_hd_btn_state(True)
                sz_mb = os.path.getsize(disk_path) / (1024 * 1024)
                if index < len(self.media_items):
                    self.title_lbl.setText(
                        f"<b>{self.media_items[index]['name']}</b> &nbsp;"
                        f"<span style='color: #8C929E;'>({pix.width()}×{pix.height()} • {sz_mb:.1f} MB)</span>"
                    )

    def _on_dialog_batch_hd_progress(self, completed: int, total: int):
        pct = int((completed / max(1, total)) * 100)
        self.dialog_load_all_hd_btn.setText(f" Downloading HD ({completed}/{total})...")
        self.progress_pill.setText(f"Downloading HD Images ({completed}/{total} - {pct}%)...")

    def _on_dialog_batch_hd_finished(self, success_count: int, total: int):
        self.dialog_load_all_hd_btn.setEnabled(True)
        image_count = sum(1 for item in (self.media_items or []) if item.get("kind") == "image")
        if len(self._loaded_full_res_indices) >= image_count:
            self.dialog_load_all_hd_btn.setVisible(False)
        else:
            self.dialog_load_all_hd_btn.setVisible(True)
            self.dialog_load_all_hd_btn.setText(f" Load All HD ({image_count - len(self._loaded_full_res_indices)})")
        self.progress_pill.setText(f"Batch finished: {success_count}/{total} images loaded in HD!")
        QTimer.singleShot(2500, lambda: self.progress_pill.setVisible(False))

        # Refresh currently viewed image in full HD if its file was downloaded
        if 0 <= self.current_index < len(self.media_items):
            curr_item = self.media_items[self.current_index]
            if curr_item.get("kind") == "image":
                url = curr_item["url"]
                ext = curr_item.get("ext", ".bin")
                url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
                dpath = os.path.join(self.cache_dir, f"{url_hash}{ext}")
                if os.path.exists(dpath) and os.path.getsize(dpath) > 128:
                    pix = QPixmap(dpath)
                    if not pix.isNull():
                        self._loaded_full_res_indices.add(self.current_index)
                        self.image_view.set_full_res_pixmap(pix, fit_to_view=True)
                        self._update_dialog_hd_btn_state(True)
                        sz_mb = os.path.getsize(dpath) / (1024 * 1024)
                        self.title_lbl.setText(
                            f"<b>{curr_item['name']}</b> &nbsp;"
                            f"<span style='color: #8C929E;'>({pix.width()}×{pix.height()} • {sz_mb:.1f} MB)</span>"
                        )

    def _on_dialog_image_load_hd_requested(self):
        if not self.media_items or self.current_index < 0 or self.current_index >= len(self.media_items):
            return
        item = self.media_items[self.current_index]
        if item.get("kind") != "image":
            return
        if self.image_view.is_full_res():
            return

        url = item["url"]
        ext = item.get("ext", ".bin")
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        disk_filename = f"{url_hash}{ext}"
        disk_path = os.path.join(self.cache_dir, disk_filename)

        if os.path.exists(disk_path) and os.path.getsize(disk_path) > 128:
            pix = QPixmap(disk_path)
            if not pix.isNull():
                self._loaded_full_res_indices.add(self.current_index)
                self.image_view.set_full_res_pixmap(pix, fit_to_view=True)
                self._update_dialog_hd_btn_state(True)
                sz_mb = os.path.getsize(disk_path) / (1024 * 1024)
                self.title_lbl.setText(
                    f"<b>{item['name']}</b> &nbsp;"
                    f"<span style='color: #8C929E;'>({pix.width()}×{pix.height()} • {sz_mb:.1f} MB)</span>"
                )
                self.progress_pill.setVisible(False)
                return

        self.image_view.show_loading("Loading Full Resolution Image...")
        self.progress_pill.setText("Loading HD Image (0%)...")
        self.progress_pill.setVisible(True)

        if self.stream_worker and self.stream_worker.isRunning():
            self.stream_worker.cancel()
            self.stream_worker.wait(500)

        fallbacks = item.get("fallback_urls", [])
        self.stream_worker = _MediaStreamWorker(url, disk_path, fallback_urls=fallbacks, parent=self)
        self.stream_worker.progress.connect(self._on_stream_progress)
        self.stream_worker.progress.connect(lambda pct: self.image_view.set_loading_progress(pct))
        self.stream_worker.finished.connect(self._on_image_download_finished)
        self.stream_worker.start()

    def _on_dialog_gif_play_requested(self):
        if not self.media_items or self.current_index < 0 or self.current_index >= len(self.media_items):
            return
        item = self.media_items[self.current_index]
        if item.get("kind") != "gif":
            return

        url = item["url"]
        ext = item.get("ext", ".gif")
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        disk_filename = f"{url_hash}{ext}"
        disk_path = os.path.join(self.cache_dir, disk_filename)

        if os.path.exists(disk_path) and os.path.getsize(disk_path) > 128:
            self.gif_player.load_gif(disk_path)
            self.progress_pill.setVisible(False)
            return

        self.gif_player.show_loading("Loading Animated GIF (0%)...")
        self.progress_pill.setText("Loading Animated GIF (0%)...")
        self.progress_pill.setVisible(True)

        if self.stream_worker and self.stream_worker.isRunning():
            self.stream_worker.cancel()
            self.stream_worker.wait(500)

        fallbacks = item.get("fallback_urls", [])
        self.stream_worker = _MediaStreamWorker(url, disk_path, fallback_urls=fallbacks, parent=self)
        self.stream_worker.progress.connect(self._on_stream_progress)
        self.stream_worker.finished.connect(self._on_gif_download_finished)
        self.stream_worker.start()

    def _on_stream_progress(self, percent: int):
        self.progress_pill.setText(f"Loading HD Media ({percent}%)...")
        if hasattr(self, "gif_player"):
            self.gif_player.set_loading_progress(percent)
        if hasattr(self, "image_view"):
            self.image_view.set_loading_progress(percent)

    def _on_image_download_finished(self, success: bool, disk_path: str, err: str):
        self.progress_pill.setVisible(False)
        if success and os.path.exists(disk_path):
            pix = QPixmap(disk_path)
            if not pix.isNull():
                self._loaded_full_res_indices.add(self.current_index)
                self.image_view.set_full_res_pixmap(pix, fit_to_view=True)
                self._update_dialog_hd_btn_state(True)
                sz_mb = os.path.getsize(disk_path) / (1024 * 1024)
                self.title_lbl.setText(
                    f"<b>{self.media_items[self.current_index]['name']}</b> &nbsp;"
                    f"<span style='color: #8C929E;'>({pix.width()}×{pix.height()} • {sz_mb:.1f} MB)</span>"
                )
                return
        prev_pix = getattr(self.image_view, "_preview_pixmap", None)
        self.image_view.set_preview_thumbnail(prev_pix, fit_to_view=True)
        if hasattr(self.image_view, "overlay_btn"):
            self.image_view.overlay_btn.setText("  Failed to Load HD (Click to Retry)  ")
        self._update_dialog_hd_btn_state(False)

    def _on_gif_download_finished(self, success: bool, disk_path: str, err: str):
        self.progress_pill.setVisible(False)
        if success and os.path.exists(disk_path):
            self.gif_player.load_gif(disk_path)
        else:
            self.gif_player.stop()

    def switch_to_index(self, index: int):
        if 0 <= index < len(self.media_items):
            if self.current_index == index:
                kind = self.media_items[index].get("kind")
                if kind == "gif" and hasattr(self, "gif_player") and not self.gif_player.is_playing():
                    self._on_dialog_gif_play_requested()
                    return
                elif kind == "video" and hasattr(self, "video_player") and not self.video_player.is_playing():
                    self.video_player.play()
                    return
                elif kind == "image" and hasattr(self, "image_view") and not self.image_view.is_full_res():
                    self._on_dialog_image_load_hd_requested()
                    return
            self.current_index = index
            self._load_current_media()

    def next_item(self):
        if self.media_items:
            self.switch_to_index((self.current_index + 1) % len(self.media_items))

    def prev_item(self):
        if self.media_items:
            self.switch_to_index((self.current_index - 1) % len(self.media_items))

    def toggle_fullscreen(self):
        self._is_fullscreen = not self._is_fullscreen
        if self._is_fullscreen:
            self.showFullScreen()
            self.fs_toggle_btn.setIcon(QIcon(get_asset_path("assets/Svg/minimize.svg")))
            self.fs_toggle_btn.setText("")
        else:
            self.showNormal()
            self.fs_toggle_btn.setIcon(QIcon(get_asset_path("assets/Svg/fullscreen.svg")))
            self.fs_toggle_btn.setText("")

    def _copy_current_url(self):
        if not self.media_items:
            return
        url = self.media_items[self.current_index]["url"]
        QApplication.clipboard().setText(url)
        self.progress_pill.setText("Copied link to clipboard!")
        self.progress_pill.setVisible(True)
        QTimer.singleShot(1500, lambda: self.progress_pill.setVisible(False))

    def _open_in_browser(self):
        if not self.media_items:
            return
        url = self.media_items[self.current_index]["url"]
        QDesktopServices.openUrl(QUrl(url))

    def _download_current_file(self):
        if not self.media_items:
            return
        item = self.media_items[self.current_index]
        url = item["url"]
        cname = item.get("creator_name") or getattr(self, "creator_name", None) or (self.post.get("_creator_name") if hasattr(self, "post") else "") or "Creator"
        name = item.get("name") or "media_file"

        if hasattr(self.parent_app, "favorite_download_queue"):
            queue_item = {
                "url": url,
                "name": f"[{cname}] {name}",
                "filename": name,
                "name_for_folder": cname,
                "creator_name": cname,
                "type": "direct_file",
                "force_artist_folder": True,
                "post_title": getattr(self, "post_title", "") or (self.post.get("title") if hasattr(self, "post") else "") or "",
                "post_id": str(getattr(self, "post_id", "") or (self.post.get("id") if hasattr(self, "post") else "") or ""),
                "fallback_urls": item.get("fallback_urls") or []
            }
            self.parent_app.favorite_download_queue.append(queue_item)
            if hasattr(self.parent_app, "log_signal"):
                self.parent_app.log_signal.emit(f"Queued '{name}' for download.")
            if hasattr(self.parent_app, "_process_next_favorite_download"):
                if not getattr(self.parent_app, "is_processing_favorites_queue", False):
                    self.parent_app._process_next_favorite_download()
            self.progress_pill.setText("Sent file to Downloader queue!")
            self.progress_pill.setVisible(True)
            QTimer.singleShot(2000, lambda: self.progress_pill.setVisible(False))
            if hasattr(self, "download_file_btn"):
                self.download_file_btn.setText("  ✓ Queued!")
                QTimer.singleShot(1500, lambda: self.download_file_btn.setText("  Download File"))
        else:
            QDesktopServices.openUrl(QUrl(url))

    def _zoom_in_active(self):
        if self.media_stack.currentIndex() == 2:
            self.gif_player.zoom_in()
        else:
            self.image_view.zoom_in()

    def _zoom_out_active(self):
        if self.media_stack.currentIndex() == 2:
            self.gif_player.zoom_out()
        else:
            self.image_view.zoom_out()

    def _fit_active(self):
        if self.media_stack.currentIndex() == 2:
            self.gif_player.fit_in_window()
        else:
            self.image_view.fit_in_window()

    def _actual_size_active(self):
        if self.media_stack.currentIndex() == 2:
            self.gif_player.set_actual_size()
        else:
            self.image_view.set_actual_size()

    def _rotate_active(self):
        if self.media_stack.currentIndex() == 2:
            self.gif_player.rotate_cw()
        else:
            self.image_view.rotate_cw()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._is_fullscreen:
                self.toggle_fullscreen()
            else:
                self.close()
        elif key == Qt.Key.Key_F11 or key == Qt.Key.Key_F:
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Left or key == Qt.Key.Key_A or key == Qt.Key.Key_PageUp:
            if self.media_stack.currentIndex() == 0 and event.modifiers() == Qt.KeyboardModifier.NoModifier:
                # In video, left arrow rewinds 10s
                self.video_player.skip(-10000)
            else:
                self.prev_item()
        elif key == Qt.Key.Key_Right or key == Qt.Key.Key_D or key == Qt.Key.Key_PageDown:
            if self.media_stack.currentIndex() == 0 and event.modifiers() == Qt.KeyboardModifier.NoModifier:
                # In video, right arrow fast-forwards 10s
                self.video_player.skip(10000)
            else:
                self.next_item()
        elif key == Qt.Key.Key_Space:
            if self.media_stack.currentIndex() == 0:
                self.video_player.toggle_play()
            elif self.media_stack.currentIndex() == 2:
                self.gif_player._toggle_play()
        elif key == Qt.Key.Key_Plus or key == Qt.Key.Key_Equal:
            self._zoom_in_active()
        elif key == Qt.Key.Key_Minus:
            self._zoom_out_active()
        elif key == Qt.Key.Key_0:
            self._fit_active()
        elif key == Qt.Key.Key_R:
            self._rotate_active()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        # Stop background worker and players cleanly
        if self.batch_hd_worker and self.batch_hd_worker.isRunning():
            self.batch_hd_worker.cancel()
            self.batch_hd_worker.wait(500)
        if self.stream_worker and self.stream_worker.isRunning():
            self.stream_worker.cancel()
            self.stream_worker.wait(500)
        self.video_player.stop_and_release()
        self.gif_player.stop()
        super().closeEvent(event)
