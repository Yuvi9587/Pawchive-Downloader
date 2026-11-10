import os
import re
import html
import json
import hashlib
import requests
from datetime import datetime
from urllib.parse import urlparse, unquote

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QTextBrowser, QStackedWidget, QProgressBar,
    QSizePolicy, QApplication, QMessageBox, QGridLayout
)
from PySide6.QtCore import (
    Qt, QSize, Signal, QThread, QUrl, QTimer, QRectF, QThreadPool,
    QRunnable, QObject
)
from PySide6.QtGui import (
    QIcon, QColor, QFont, QDesktopServices, QPixmap, QPainter,
    QBrush, QPen, QImageReader
)

from ..ui.assets import get_asset_path, get_asset_html_path
from .creators_tab import format_relative_time, CreatorMediaManager
from .media_viewer import (
    VideoPlayerWidget, ImageZoomView, GifPlayerWidget,
    extract_media_items_from_post, normalize_media_url, normalize_thumb_url,
    _MediaStreamWorker, PawchiveMediaViewerDialog, _BatchHDLoaderWorker
)


# ─────────────────────────────────────────────────────────────────────────────
#  Full Post Detail & Media Asynchronous Fetchers
# ─────────────────────────────────────────────────────────────────────────────

class _PostDetailFetchWorker(QThread):
    """
    Fetches full post detail asynchronously for a given post from Pawchive API.
    Endpoint: https://pawchive.pw/api/v1/{service}/{url_type}/{creator_id}/post/{post_id}
    Caches results to cache/media_cache/post_{service}_{creator_id}_{post_id}.json
    """
    post_loaded = Signal(dict)
    post_failed = Signal(str)

    def __init__(self, service: str, creator_id: str, post_id: str, cache_dir: str, parent=None):
        super().__init__(parent)
        self.service = (service or "patreon").lower().strip()
        self.creator_id = str(creator_id or "").strip()
        self.post_id = str(post_id or "").strip()
        self.cache_dir = cache_dir
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        if not self.creator_id or not self.post_id:
            return

        cache_file = os.path.join(
            self.cache_dir, f"post_{self.service}_{self.creator_id}_{self.post_id}.json"
        )

        # 1. Local disk cache
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                    if isinstance(cached_data, dict) and not self._cancelled:
                        self.post_loaded.emit(cached_data)
                        return
            except Exception:
                pass

        if self._cancelled:
            return

        # 2. Network fetch
        url_type = "server" if self.service == "discord" else "user"
        api_url = f"https://pawchive.pw/api/v1/{self.service}/{url_type}/{self.creator_id}/post/{self.post_id}"

        headers = {
            "User-Agent": "PawchiveClient/1.0.5 (Desktop App)",
            "Accept": "application/json",
            "Referer": "https://pawchive.pw/"
        }

        try:
            resp = requests.get(api_url, headers=headers, timeout=12)
            if self._cancelled:
                return

            if resp.status_code == 200:
                data = resp.json()
                post_obj = None
                if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
                    post_obj = data[0]
                elif isinstance(data, dict):
                    if "post" in data and isinstance(data["post"], dict):
                        post_obj = data["post"]
                        if "attachments" in data and not post_obj.get("attachments"):
                            post_obj["attachments"] = data["attachments"]
                    else:
                        post_obj = data

                if post_obj and not self._cancelled:
                    try:
                        os.makedirs(self.cache_dir, exist_ok=True)
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump(post_obj, f, ensure_ascii=False)
                    except Exception:
                        pass
                    self.post_loaded.emit(post_obj)
            else:
                if not self._cancelled:
                    self.post_failed.emit(f"HTTP {resp.status_code}")
        except Exception as e:
            if not self._cancelled:
                self.post_failed.emit(str(e))


class _ThumbTaskSignals(QObject):
    loaded = Signal(int, str, str)  # index, thumb_url, disk_path


class _ThumbDownloadTask(QRunnable):
    def __init__(self, index: int, url: str, disk_path: str, generation: int, get_current_gen, signals: _ThumbTaskSignals):
        super().__init__()
        self.index = index
        self.url = url
        self.disk_path = disk_path
        self.generation = generation
        self.get_current_gen = get_current_gen
        self.signals = signals
        self.setAutoDelete(True)

    def run(self):
        try:
            # Check if post changed before starting
            if self.generation != self.get_current_gen():
                return

            os.makedirs(os.path.dirname(self.disk_path), exist_ok=True)
            if not os.path.exists(self.disk_path):
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://pawchive.pw/"
                }
                r = requests.get(self.url, headers=headers, timeout=10)
                if self.generation != self.get_current_gen():
                    return
                if r.status_code == 200 and r.content:
                    # Offload thumbnail decoding and resizing to background thread using PIL
                    try:
                        from PIL import Image
                        import io
                        im = Image.open(io.BytesIO(r.content))
                        im.thumbnail((180, 180), Image.Resampling.LANCZOS)
                        if im.mode in ("RGBA", "P"):
                            im = im.convert("RGB")
                        im.save(self.disk_path, format="JPEG", quality=85)
                    except Exception:
                        with open(self.disk_path, "wb") as f:
                            f.write(r.content)

            if self.generation != self.get_current_gen():
                return

            if os.path.exists(self.disk_path):
                self.signals.loaded.emit(self.index, self.url, self.disk_path)
        except Exception:
            pass



class _NoHorizontalScrollArea(QScrollArea):
    """
    QScrollArea that guarantees strictly zero horizontal scrolling under any circumstance
    (including child focus changes, ensureWidgetVisible calls, and responsive resize events).
    """
    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(0, dy)
        if self.horizontalScrollBar().value() != 0:
            self.horizontalScrollBar().setValue(0)

    def ensureWidgetVisible(self, childWidget, xMargin=50, yMargin=50):
        # Strictly ensure vertical visibility without shifting the viewport horizontally
        super().ensureWidgetVisible(childWidget, 0, yMargin)
        if self.horizontalScrollBar().value() != 0:
            self.horizontalScrollBar().setValue(0)


class _ResponsiveStackedWidget(QStackedWidget):
    """
    QStackedWidget that reports sizeHint and minimumSizeHint based ONLY on
    the CURRENT active page, preventing hidden large pages (like the video player)
    from artificially expanding the minimum width when viewing an image or GIF.
    """
    def minimumSizeHint(self):
        return QSize(100, 100)

    def sizeHint(self):
        curr = self.currentWidget()
        if curr:
            sh = curr.sizeHint()
            return QSize(min(sh.width(), 800), sh.height())
        return QSize(400, 400)


class _CompactCounterLabel(QLabel):
    """QLabel that reports a small minimumSizeHint width to prevent expanding horizontal layouts."""
    def minimumSizeHint(self):
        return QSize(40, super().minimumSizeHint().height())


class _SidebarMediaCard(QFrame):
    """
    Slim horizontal card widget for the right-hand attachments sidebar.
    Allows quick scrolling and one-click media switching without scrolling the main page.
    """
    play_clicked = Signal(int)       # media index
    download_clicked = Signal(dict)  # media item dict

    def __init__(self, item: dict, index: int, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.item = item
        self.index = index
        self.scale = scale
        self.is_active = False

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(int(74 * scale))
        self._build_ui()
        self._update_style()

    def _build_ui(self):
        scale = self.scale
        card_h = QHBoxLayout(self)
        card_h.setContentsMargins(int(6 * scale), int(6 * scale), int(8 * scale), int(6 * scale))
        card_h.setSpacing(int(8 * scale))

        # 1. Left Thumbnail Preview with Badges
        self.thumb_container = QFrame(self)
        self.thumb_container.setFixedSize(int(88 * scale), int(60 * scale))
        self.thumb_container.setStyleSheet("background-color: #0E1015; border-radius: 5px;")
        t_layout = QVBoxLayout(self.thumb_container)
        t_layout.setContentsMargins(0, 0, 0, 0)
        t_layout.setSpacing(0)

        # Thumbnail Label
        self.thumb_lbl = QLabel(self.thumb_container)
        self.thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_lbl.setStyleSheet("background: transparent; border-radius: 5px;")

        kind = self.item.get("kind", "file")
        badge_text = "GIF" if kind == "gif" else ("Video" if kind == "video" else ("Image" if kind == "image" else "File"))
        svg_file = "assets/uisvg/video.svg" if kind in ("video", "gif") else ("assets/uisvg/image.svg" if kind == "image" else "assets/uisvg/attach.svg")
        self.thumb_lbl.setPixmap(QIcon(get_asset_path(svg_file)).pixmap(int(22 * scale), int(22 * scale)))
        t_layout.addWidget(self.thumb_lbl)

        # Overlay Top-Left Index Badge
        self.idx_badge = QLabel(f"#{self.index + 1}", self.thumb_container)
        self.idx_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.idx_badge.setStyleSheet(
            "background-color: rgba(18, 20, 26, 0.85); color: #DFE1E5; font-size: 6.5pt; "
            "font-weight: bold; padding: 1px 4px; border-radius: 3px; border: 1px solid #333742;"
        )
        self.idx_badge.adjustSize()
        self.idx_badge.move(int(4 * scale), int(4 * scale))
        self.idx_badge.raise_()

        # Overlay Bottom-Right Kind Badge
        self.kind_badge = QLabel(badge_text, self.thumb_container)
        self.kind_badge.setStyleSheet(
            "background-color: rgba(47, 36, 36, 0.9); color: #E5A198; font-size: 6.5pt; "
            "font-weight: bold; padding: 1px 4px; border-radius: 3px; border: 1px solid #5A3535;"
        )
        self.kind_badge.adjustSize()
        self.kind_badge.move(
            int(88 * scale) - self.kind_badge.width() - int(4 * scale),
            int(60 * scale) - self.kind_badge.height() - int(4 * scale)
        )
        self.kind_badge.raise_()

        card_h.addWidget(self.thumb_container)

        # 2. Middle Info (Filename, Subtitle, Now Playing badge)
        info_v = QVBoxLayout()
        info_v.setContentsMargins(0, int(2 * scale), 0, int(2 * scale))
        info_v.setSpacing(int(2 * scale))

        self.raw_fname = self.item.get("name") or f"Item {self.index + 1}"
        self.name_lbl = QLabel(self.raw_fname, self)
        self.name_lbl.setMinimumWidth(0)
        self.name_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.name_lbl.setStyleSheet("color: #DFE1E5; font-size: 8pt; font-weight: bold; background: transparent; border: none;")
        self.setToolTip(self.raw_fname)
        info_v.addWidget(self.name_lbl)

        ext = self.item.get("ext", "").upper().lstrip(".")
        sub_desc = f"{ext} {kind.capitalize()}" if ext else kind.capitalize()
        self.sub_lbl = QLabel(sub_desc, self)
        self.sub_lbl.setMinimumWidth(0)
        self.sub_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sub_lbl.setStyleSheet("color: #7A8190; font-size: 7.5pt; background: transparent; border: none;")
        info_v.addWidget(self.sub_lbl)

        self.now_playing_lbl = QLabel(f"<img src='{get_asset_html_path('assets/uisvg/play.svg')}' width='8' height='8'> &nbsp;NOW PLAYING", self)
        self.now_playing_lbl.setMinimumWidth(0)
        self.now_playing_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.now_playing_lbl.setStyleSheet("color: #E5A198; font-size: 7pt; font-weight: bold; background: transparent; border: none;")
        self.now_playing_lbl.setVisible(False)
        info_v.addWidget(self.now_playing_lbl)

        info_v.addStretch(1)
        card_h.addLayout(info_v, 1)

        # 3. Right Quick Download Button
        self.dl_btn = QPushButton(self)
        self.dl_btn.setIcon(QIcon(get_asset_path("assets/uisvg/save.svg")))
        self.dl_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.dl_btn.setFixedSize(int(24 * scale), int(24 * scale))
        self.dl_btn.setToolTip("Queue this file for download")
        self.dl_btn.setStyleSheet(
            "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; "
            "border-radius: 4px; font-size: 8pt; }"
            "QPushButton:hover { background-color: #2F2424; color: #E5A198; border-color: #E5A198; }"
        )
        self.dl_btn.clicked.connect(lambda: self.download_clicked.emit(self.item))
        card_h.addWidget(self.dl_btn)

    def minimumSizeHint(self):
        return QSize(int(140 * self.scale), int(74 * self.scale))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        fm = self.name_lbl.fontMetrics()
        w = self.name_lbl.width()
        if w > 20 and self.raw_fname:
            self.name_lbl.setText(fm.elidedText(self.raw_fname, Qt.TextElideMode.ElideRight, max(w, 20)))

    def set_thumbnail_pixmap(self, pix: QPixmap):
        if not pix or pix.isNull():
            return
        w = max(int(88 * self.scale), self.thumb_container.width())
        h = max(int(60 * self.scale), self.thumb_container.height())
        scaled = pix.scaled(
            w, h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        )
        x = max(0, (scaled.width() - w) // 2)
        y = max(0, (scaled.height() - h) // 2)
        cropped = scaled.copy(x, y, w, h)
        self.thumb_lbl.setPixmap(cropped)
        self.thumb_lbl.setText("")
        self.idx_badge.adjustSize()
        self.idx_badge.raise_()
        self.kind_badge.raise_()

    def set_active(self, active: bool):
        if self.is_active != active:
            self.is_active = active
            self._update_style()

    def _update_style(self):
        if self.is_active:
            self.setStyleSheet(
                "QFrame { background-color: #261B1B; border: 1.5px solid #E5A198; border-radius: 8px; }"
            )
            self.name_lbl.setStyleSheet("color: #E5A198; font-size: 8pt; font-weight: bold; background: transparent; border: none;")
            self.now_playing_lbl.setVisible(True)
        else:
            self.setStyleSheet(
                "QFrame { background-color: #1A1D26; border: 1px solid #282B35; border-radius: 8px; }"
                "QFrame:hover { border-color: #4A5060; background-color: #20232E; }"
            )
            self.name_lbl.setStyleSheet("color: #DFE1E5; font-size: 8pt; font-weight: bold; background: transparent; border: none;")
            self.now_playing_lbl.setVisible(False)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.play_clicked.emit(self.index)
        super().mousePressEvent(event)


# ─────────────────────────────────────────────────────────────────────────────
#  Comments Fetch Worker
# ─────────────────────────────────────────────────────────────────────────────

class _CommentsFetchWorker(QThread):
    """
    Fetches comments asynchronously for a given post from Pawchive API.
    Endpoint: https://pawchive.pw/api/v1/{service}/user/{creator_id}/post/{post_id}/comments
    Caches results to cache/comments/{service}_{creator_id}_{post_id}.json
    """
    comments_loaded = Signal(list)
    comments_failed = Signal(str)

    def __init__(self, service: str, creator_id: str, post_id: str, cache_dir: str, parent=None):
        super().__init__(parent)
        self.service = (service or "patreon").lower().strip()
        self.creator_id = str(creator_id or "").strip()
        self.post_id = str(post_id or "").strip()
        self.cache_dir = cache_dir
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        if not self.creator_id or not self.post_id:
            try:
                self.comments_loaded.emit([])
            except Exception:
                pass
            return

        cache_file = os.path.join(
            self.cache_dir, f"{self.service}_{self.creator_id}_{self.post_id}.json"
        )

        # 1. Check local disk cache
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        if not self._cancelled:
                            self.comments_loaded.emit(data)
                            return
            except Exception:
                pass

        if self._cancelled:
            return

        # 2. Fetch from network
        url_type = "server" if self.service == "discord" else "user"
        api_url = f"https://pawchive.pw/api/v1/{self.service}/{url_type}/{self.creator_id}/post/{self.post_id}/comments"

        headers = {
            "User-Agent": "PawchiveClient/1.0.5 (Desktop App)",
            "Accept": "application/json",
            "Referer": "https://pawchive.pw/"
        }

        try:
            resp = requests.get(api_url, headers=headers, timeout=12)
            if self._cancelled:
                return

            if resp.status_code == 200:
                comments = resp.json()
                if isinstance(comments, list):
                    # Save cache
                    try:
                        os.makedirs(self.cache_dir, exist_ok=True)
                        with open(cache_file, "w", encoding="utf-8") as f:
                            json.dump(comments, f, ensure_ascii=False)
                    except Exception:
                        pass

                    if not self._cancelled:
                        self.comments_loaded.emit(comments)
                        return
                else:
                    if not self._cancelled:
                        self.comments_loaded.emit([])
                        return
            elif resp.status_code == 404:
                if not self._cancelled:
                    self.comments_loaded.emit([])
                    return
            else:
                if not self._cancelled:
                    self.comments_failed.emit(f"HTTP {resp.status_code}")
                    return

        except Exception as e:
            if not self._cancelled:
                # If cached version exists on failure, return it, else empty
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, "r", encoding="utf-8") as f:
                            self.comments_loaded.emit(json.load(f))
                            return
                    except Exception:
                        pass
                self.comments_failed.emit(str(e))


# ─────────────────────────────────────────────────────────────────────────────
#  Comment Card Widget
# ─────────────────────────────────────────────────────────────────────────────

def format_comment_time(pub) -> str:
    """Format an ISO timestamp or epoch from a comment into a readable relative string like '4d ago'."""
    if not pub:
        return ""
    try:
        if isinstance(pub, (int, float)):
            epoch = float(pub)
        elif isinstance(pub, str):
            clean = pub.strip().replace("Z", "").split("+")[0]
            if "." in clean:
                clean = clean.split(".")[0]
            if "T" in clean:
                dt = datetime.fromisoformat(clean)
            elif "-" in clean and " " in clean:
                dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
            elif "-" in clean:
                dt = datetime.strptime(clean, "%Y-%m-%d")
            else:
                dt = None
                epoch = float(clean)
            if dt is not None:
                epoch = dt.timestamp()
        else:
            return ""

        now = datetime.now().timestamp()
        delta = int(now - epoch)
        if delta < 0 or delta < 60:
            return "just now"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        if delta < 86400 * 30:
            return f"{delta // 86400}d ago"
        if delta < 86400 * 365:
            return f"{delta // (86400 * 30)}mo ago"
        return f"{delta // (86400 * 365)}y ago"
    except Exception:
        return str(pub)[:10] if len(str(pub)) >= 10 else str(pub)


class CommentCardWidget(QFrame):
    """Clean dark card representing a single comment or threaded reply."""
    def __init__(self, comment: dict, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.scale = scale
        self.comment = comment

        is_reply = bool(comment.get("parent_id"))

        self.setObjectName("commentCard")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setStyleSheet(
            f"QFrame#commentCard {{ background-color: {'#1D2028' if is_reply else '#161820'}; "
            f"border: 1px solid {'#353B4A' if is_reply else '#272A34'}; "
            f"border-radius: 8px; {'border-left: 3px solid #E5A198; margin-left: 18px;' if is_reply else ''} }}"
        )

        layout = QVBoxLayout(self)
        pad_x = int(12 * scale)
        pad_y = int(8 * scale)
        layout.setContentsMargins(pad_x, pad_y, pad_x, pad_y)
        layout.setSpacing(int(4 * scale))

        # Header Row: Avatar icon + Name + Time + Reply indicator
        header_row = QHBoxLayout()
        header_row.setSpacing(int(8 * scale))

        user_icon = QLabel()
        user_icon.setPixmap(QIcon(get_asset_path("assets/Svg/user.svg")).pixmap(int(14 * scale), int(14 * scale)))
        user_icon.setStyleSheet("border: none; background: transparent;")
        header_row.addWidget(user_icon)

        c_name = comment.get("commenter_name") or comment.get("commenter") or "Anonymous"
        name_lbl = QLabel(f"<b>{html.escape(c_name)}</b>")
        name_lbl.setStyleSheet("color: #FFFFFF; font-size: 8.5pt; border: none; background: transparent;")
        header_row.addWidget(name_lbl)

        # Published date
        pub = comment.get("published")
        time_str = format_comment_time(pub) if pub else ""
        if time_str:
            time_lbl = QLabel(f"•  {time_str}")
            time_lbl.setStyleSheet("color: #8C929E; font-size: 8pt; border: none; background: transparent;")
            header_row.addWidget(time_lbl)

        if is_reply:
            reply_badge = QLabel("Reply")
            reply_badge.setStyleSheet(
                "background-color: #2F2424; color: #E5A198; font-size: 7.5pt; "
                "font-weight: bold; padding: 1px 6px; border-radius: 3px; border: 1px solid #5A3535;"
            )
            header_row.addWidget(reply_badge)

        header_row.addStretch(1)

        # Revision indicator
        revs = comment.get("revisions") or []
        if len(revs) > 1:
            rev_lbl = QLabel(f"{len(revs)} edits")
            rev_lbl.setStyleSheet("color: #6C7280; font-size: 7.5pt; border: none; background: transparent;")
            header_row.addWidget(rev_lbl)

        layout.addLayout(header_row)

        # Content text (Word-wrapped QLabel: auto-fits exact text height with zero wasted space)
        raw_content = comment.get("content") or ""
        escaped = html.escape(raw_content)
        url_pattern = re.compile(r'(https?://[^\s<>"]+)')
        linkified = url_pattern.sub(r'<a href="\1" style="color: #E5A198; text-decoration: underline;">\1</a>', escaped)
        clean_content = linkified.replace("\n", "<br>")

        self.content_lbl = QLabel(clean_content, self)
        self.content_lbl.setWordWrap(True)
        self.content_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        self.content_lbl.setOpenExternalLinks(True)
        self.content_lbl.setStyleSheet(
            "color: #DFE1E5; font-size: 8.5pt; line-height: 1.45; font-family: Segoe UI, sans-serif; "
            "border: none; background: transparent; padding: 0px;"
        )
        layout.addWidget(self.content_lbl)


# ─────────────────────────────────────────────────────────────────────────────
#  Full In-App Post Detail View
# ─────────────────────────────────────────────────────────────────────────────

class PostDetailView(QWidget):
    """
    Dedicated in-app Post Detail Page matching the official Pawchive Client design:
    - Sticky top navigation bar: Back to Posts, Prev / Next post buttons, Download post, Browser link
    - Post Header: Service pill badge, published/edited date, big bold title, creator avatar & name
    - Full written description / HTML content
    - Embedded In-App Media:
      - Video: Direct HTTP Range video player with seek slider, speed, volume, and fullscreen
      - Images / GIFs: Interactive gallery with HD zoom/pan view and carousel strip
      - File attachments: Downloadable files list
    - Comments section: Asynchronously fetched threaded comments
    """
    back_to_posts_clicked = Signal()
    open_post_requested = Signal(dict)

    def __init__(self, parent_app=None, media_manager=None, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.parent_app = parent_app
        self.media_manager = media_manager
        self.scale = scale
        self.post = {}
        self.all_posts = []
        self.current_post_index = -1
        self.media_items = []
        self.current_media_index = 0
        self.thumb_buttons = []
        self.sidebar_cards = []
        self.is_sidebar_visible = True

        # Background stream, comment, and post detail workers
        self.comments_worker = None
        self.stream_worker = None
        self.post_worker = None
        self.batch_hd_worker = None
        self._loaded_full_res_indices = set()

        # Thumbnail task signals for carousel & gallery cards
        self._thumb_generation = 0
        self.thumb_signals = _ThumbTaskSignals(parent=self)
        self.thumb_signals.loaded.connect(self._on_item_thumb_loaded)

        if self.media_manager:
            try:
                self.media_manager.media_loaded.connect(self._on_avatar_loaded)
            except Exception:
                pass

        # Cache directories
        base_dir = getattr(parent_app, "app_base_dir", ".")
        self.cache_dir = os.path.join(base_dir, "cache", "media_cache")
        self.comments_cache_dir = os.path.join(base_dir, "cache", "comments")
        self.thumbs_cache_dir = os.path.join(base_dir, "cache", "post_thumbnails")
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.comments_cache_dir, exist_ok=True)
        os.makedirs(self.thumbs_cache_dir, exist_ok=True)

        self._build_ui()

    def _build_ui(self):
        scale = self.scale
        root_v = QVBoxLayout(self)
        root_v.setContentsMargins(0, 0, 0, 0)
        root_v.setSpacing(0)

        # ── 1. Sticky Top Navigation Bar ─────────────────────────────────────
        top_bar = QFrame(self)
        top_bar.setFixedHeight(int(46 * scale))
        top_bar.setStyleSheet(
            "QFrame { background-color: #161820; border-bottom: 1px solid #2B2E38; }"
            "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; "
            "border-radius: 5px; font-size: 8.5pt; font-weight: bold; padding: 4px 10px; }"
            "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
        )
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(int(14 * scale), 0, int(14 * scale), 0)
        top_layout.setSpacing(int(8 * scale))

        btn_h = int(28 * scale)

        # Back button
        self.back_btn = QPushButton("  Back to Posts")
        self.back_btn.setIcon(QIcon(get_asset_path("assets/Svg/previous.svg")))
        self.back_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        self.back_btn.setFixedHeight(btn_h)
        self.back_btn.setStyleSheet(
            "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; "
            "border-radius: 5px; padding: 4px 12px; font-weight: bold; font-size: 8.5pt; }"
            "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
        )
        self.back_btn.clicked.connect(self._on_back_clicked)
        top_layout.addWidget(self.back_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        # Post Cycling: Prev & Next buttons
        self.prev_post_btn = QPushButton("  Prev Post")
        self.prev_post_btn.setIcon(QIcon(get_asset_path("assets/Svg/previous.svg")))
        self.prev_post_btn.setIconSize(QSize(int(11 * scale), int(11 * scale)))
        self.prev_post_btn.setFixedHeight(btn_h)
        self.prev_post_btn.clicked.connect(self._on_prev_post_clicked)
        top_layout.addWidget(self.prev_post_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.next_post_btn = QPushButton("Next Post  ")
        self.next_post_btn.setIcon(QIcon(get_asset_path("assets/Svg/next.svg")))
        self.next_post_btn.setIconSize(QSize(int(11 * scale), int(11 * scale)))
        self.next_post_btn.setFixedHeight(btn_h)
        self.next_post_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.next_post_btn.clicked.connect(self._on_next_post_clicked)
        top_layout.addWidget(self.next_post_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        top_layout.addStretch(1)

        # Status indicator
        self.nav_status_lbl = QLabel("")
        self.nav_status_lbl.setStyleSheet("color: #E5A198; font-size: 8.5pt; font-weight: bold;")
        top_layout.addWidget(self.nav_status_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        # Quick Actions
        self.copy_link_btn = QPushButton("  Copy URL")
        self.copy_link_btn.setIcon(QIcon(get_asset_path("assets/Svg/link.svg")))
        self.copy_link_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.copy_link_btn.setFixedHeight(btn_h)
        self.copy_link_btn.clicked.connect(self._copy_post_url)
        top_layout.addWidget(self.copy_link_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.open_browser_btn = QPushButton("  Open in Browser")
        self.open_browser_btn.setIcon(QIcon(get_asset_path("assets/Svg/eye_open.svg")))
        self.open_browser_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.open_browser_btn.setFixedHeight(btn_h)
        self.open_browser_btn.clicked.connect(self._open_in_browser)
        top_layout.addWidget(self.open_browser_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.send_to_dl_btn = QPushButton("  Send to Downloader")
        self.send_to_dl_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.send_to_dl_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.send_to_dl_btn.setFixedHeight(btn_h)
        self.send_to_dl_btn.setStyleSheet(
            "QPushButton { background-color: #E5A198; color: #161820; border: none; font-weight: bold; border-radius: 5px; padding: 4px 12px; font-size: 8.5pt; }"
            "QPushButton:hover { background-color: #F0B3AB; }"
        )
        self.send_to_dl_btn.clicked.connect(self._send_to_downloader)
        top_layout.addWidget(self.send_to_dl_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        # Toggle right-hand sidebar button
        self.toggle_sidebar_btn = QPushButton("  Attachments")
        self.toggle_sidebar_btn.setIcon(QIcon(get_asset_path("assets/Svg/link.svg")))
        self.toggle_sidebar_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.toggle_sidebar_btn.setFixedHeight(btn_h)
        self.toggle_sidebar_btn.setToolTip("Toggle right-hand attachments & playlist sidebar")
        self.toggle_sidebar_btn.setStyleSheet(
            "QPushButton { background-color: #352020; color: #E5A198; border: 1px solid #E5A198; "
            "border-radius: 5px; font-size: 8.5pt; font-weight: bold; padding: 4px 10px; }"
            "QPushButton:hover { background-color: #422626; }"
        )
        self.toggle_sidebar_btn.clicked.connect(self._toggle_sidebar)
        top_layout.addWidget(self.toggle_sidebar_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        root_v.addWidget(top_bar)

        # ── 2. Scrollable Body ───────────────────────────────────────────────
        self.scroll_area = _NoHorizontalScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet(
            "QScrollArea { background-color: #12141A; border: none; }"
            "QScrollBar:vertical { background: #161820; width: 10px; margin: 0; }"
            "QScrollBar::handle:vertical { background: #2F333E; border-radius: 5px; min-height: 24px; }"
            "QScrollBar::handle:vertical:hover { background: #E5A198; }"
            "QScrollBar:horizontal { background: #161820; height: 8px; margin: 0; }"
            "QScrollBar::handle:horizontal { background: #2F333E; border-radius: 4px; min-width: 24px; }"
            "QScrollBar::handle:horizontal:hover { background: #E5A198; }"
        )

        self.scroll_content = QWidget()
        self.scroll_content.setStyleSheet("background-color: #12141A;")
        self.content_layout = QVBoxLayout(self.scroll_content)
        self.content_layout.setContentsMargins(
            int(20 * scale), int(12 * scale), int(20 * scale), int(20 * scale)
        )
        self.content_layout.setSpacing(int(12 * scale))

        # ── A. Post Title & Header Card ──────────────────────────────────────
        self.header_card = QFrame(self.scroll_content)
        self.header_card.setObjectName("headerCard")
        self.header_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.header_card.setStyleSheet(
            "QFrame#headerCard { background-color: #181A22; border: 1px solid #2B2E38; border-radius: 8px; }"
        )
        h_layout = QVBoxLayout(self.header_card)
        h_layout.setContentsMargins(int(16 * scale), int(12 * scale), int(16 * scale), int(12 * scale))
        h_layout.setSpacing(int(6 * scale))

        # Badges row: Platform Pill + Date + Edited
        meta_row = QHBoxLayout()
        meta_row.setSpacing(int(10 * scale))

        self.platform_pill = QLabel("Patreon")
        self.platform_pill.setStyleSheet(
            "background-color: #352222; color: #E5A198; border: 1px solid #663B3B; "
            "border-radius: 4px; padding: 2px 8px; font-size: 7.5pt; font-weight: bold;"
        )
        meta_row.addWidget(self.platform_pill)

        self.date_lbl = QLabel(f"<img src='{get_asset_html_path('assets/uisvg/clock.svg')}' width='11' height='11'> &nbsp;Published Date")
        self.date_lbl.setStyleSheet("color: #8C929E; font-size: 8pt; border: none; background: transparent;")
        meta_row.addWidget(self.date_lbl)

        meta_row.addStretch(1)
        h_layout.addLayout(meta_row)

        # Big Post Title
        self.title_lbl = QLabel("Post Title")
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.title_lbl.setStyleSheet("font-size: 16pt; font-weight: bold; color: #FFFFFF; line-height: 1.3; border: none; background: transparent;")
        h_layout.addWidget(self.title_lbl)

        # Creator Info Bar
        creator_row = QHBoxLayout()
        creator_row.setSpacing(int(8 * scale))

        self.creator_avatar_lbl = QLabel()
        sz = int(28 * scale)
        self.creator_avatar_lbl.setFixedSize(sz, sz)
        self.creator_avatar_lbl.setStyleSheet("background-color: #272A34; border-radius: 14px; border: none;")
        creator_row.addWidget(self.creator_avatar_lbl)

        self.creator_name_lbl = QLabel("Creator Name")
        self.creator_name_lbl.setStyleSheet("font-size: 9.5pt; font-weight: bold; color: #E5A198; border: none; background: transparent;")
        creator_row.addWidget(self.creator_name_lbl)

        creator_row.addStretch(1)
        h_layout.addLayout(creator_row)

        self.content_layout.addWidget(self.header_card)

        # ── B. Post Written Description ──────────────────────────────────────
        self.desc_card = QFrame(self.scroll_content)
        self.desc_card.setObjectName("descCard")
        self.desc_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.desc_card.setStyleSheet(
            "QFrame#descCard { background-color: #181A22; border: 1px solid #2B2E38; border-radius: 8px; }"
        )
        d_layout = QVBoxLayout(self.desc_card)
        d_layout.setContentsMargins(int(16 * scale), int(12 * scale), int(16 * scale), int(12 * scale))
        d_layout.setSpacing(int(6 * scale))

        desc_sec_lbl = QLabel("DESCRIPTION")
        desc_sec_lbl.setStyleSheet("font-size: 8pt; font-weight: bold; color: #8C929E; letter-spacing: 1px; border: none; background: transparent;")
        d_layout.addWidget(desc_sec_lbl)

        self.desc_browser = QTextBrowser(self.desc_card)
        self.desc_browser.setOpenExternalLinks(True)
        self.desc_browser.setStyleSheet(
            "QTextBrowser { background: transparent; border: none; color: #DFE1E5; font-size: 9.5pt; line-height: 1.5; font-family: Segoe UI, sans-serif; padding: 0px; }"
        )
        self.desc_browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.desc_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.desc_browser.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        d_layout.addWidget(self.desc_browser)

        self.content_layout.addWidget(self.desc_card)

        # ── C. Embedded In-App Media Player & Gallery ─────────────────────────
        self.media_card = QFrame(self.scroll_content)
        self.media_card.setObjectName("mediaCard")
        self.media_card.setStyleSheet(
            "QFrame#mediaCard { background-color: #181A22; border: 1px solid #2B2E38; border-radius: 8px; }"
        )
        self.media_card_layout = QVBoxLayout(self.media_card)
        self.media_card_layout.setContentsMargins(int(16 * scale), int(12 * scale), int(16 * scale), int(12 * scale))
        self.media_card_layout.setSpacing(int(8 * scale))

        # Media Header
        m_head_row = QHBoxLayout()
        self.media_sec_lbl = QLabel("MEDIA & ATTACHMENTS")
        self.media_sec_lbl.setStyleSheet("font-size: 8pt; font-weight: bold; color: #8C929E; letter-spacing: 1px; border: none; background: transparent;")
        self.media_sec_lbl.setMinimumWidth(0)
        self.media_sec_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        m_head_row.addWidget(self.media_sec_lbl)

        m_head_row.addStretch(1)

        # Image Zoom / Pan controls (visible when viewing images)
        self.zoom_ctrl_frame = QFrame(self.media_card)
        self.zoom_ctrl_frame.setStyleSheet(
            "QFrame { background: transparent; }"
            "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; "
            "border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 2px 6px; }"
            "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
        )
        z_layout = QHBoxLayout(self.zoom_ctrl_frame)
        z_layout.setContentsMargins(0, 0, 0, 0)
        z_layout.setSpacing(int(4 * scale))

        z_in_btn = QPushButton()
        z_in_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_in.svg")))
        z_in_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        z_in_btn.setFixedSize(int(26 * scale), int(26 * scale))
        z_in_btn.setToolTip("Zoom In (+)")
        z_in_btn.clicked.connect(self._zoom_in)
        z_layout.addWidget(z_in_btn)

        z_out_btn = QPushButton()
        z_out_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_out.svg")))
        z_out_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        z_out_btn.setFixedSize(int(26 * scale), int(26 * scale))
        z_out_btn.setToolTip("Zoom Out (-)")
        z_out_btn.clicked.connect(self._zoom_out)
        z_layout.addWidget(z_out_btn)

        z_fit_btn = QPushButton("Fit")
        z_fit_btn.setFixedSize(int(36 * scale), int(26 * scale))
        z_fit_btn.setToolTip("Fit in view (0)")
        z_fit_btn.clicked.connect(self._zoom_fit)
        z_layout.addWidget(z_fit_btn)

        z_100_btn = QPushButton("1:1")
        z_100_btn.setFixedSize(int(36 * scale), int(26 * scale))
        z_100_btn.setToolTip("Actual 100% native resolution (Double-click)")
        z_100_btn.clicked.connect(self._zoom_actual)
        z_layout.addWidget(z_100_btn)

        self.z_expand_btn = QPushButton(" Full View")
        self.z_expand_btn.setIcon(QIcon(get_asset_path("assets/uisvg/cursor_apply.svg")))
        self.z_expand_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        self.z_expand_btn.setFixedSize(int(78 * scale), int(26 * scale))
        self.z_expand_btn.setToolTip("Expand viewer to show full artwork height in page (no letterboxing)")
        self.z_expand_btn.clicked.connect(self._toggle_expanded_view)
        z_layout.addWidget(self.z_expand_btn)

        self.z_fullscreen_btn = QPushButton(" Fullscreen")
        self.z_fullscreen_btn.setIcon(QIcon(get_asset_path("assets/Svg/fullscreen.svg")))
        self.z_fullscreen_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.z_fullscreen_btn.setFixedSize(int(82 * scale), int(26 * scale))
        self.z_fullscreen_btn.setStyleSheet(
            "QPushButton { background-color: #2F2424; color: #E5A198; border: 1px solid #5A3535; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 2px 6px; }"
            "QPushButton:hover { background-color: #E5A198; color: #161820; }"
        )
        self.z_fullscreen_btn.setToolTip("Open full-screen HD Lightbox (F)")
        self.z_fullscreen_btn.clicked.connect(self._open_fullscreen_viewer)
        z_layout.addWidget(self.z_fullscreen_btn)

        z_rot_btn = QPushButton(" Rotate")
        z_rot_btn.setIcon(QIcon(get_asset_path("assets/Svg2/repeat.svg")))
        z_rot_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        z_rot_btn.setFixedSize(int(66 * scale), int(26 * scale))
        z_rot_btn.setToolTip("Rotate 90° Clockwise (R)")
        z_rot_btn.clicked.connect(self._zoom_rotate)
        z_layout.addWidget(z_rot_btn)

        m_head_row.addWidget(self.zoom_ctrl_frame)
        self.media_card_layout.addLayout(m_head_row)

        # Carousel / Thumbnails row for switching attachments
        self.carousel_scroll = QScrollArea(self.media_card)
        self.carousel_scroll.setFixedHeight(int(66 * scale))
        self.carousel_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.carousel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.carousel_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self.carousel_widget = QWidget()
        self.carousel_widget.setStyleSheet("background: transparent;")
        self.carousel_layout = QHBoxLayout(self.carousel_widget)
        self.carousel_layout.setContentsMargins(0, 0, 0, 0)
        self.carousel_layout.setSpacing(int(8 * scale))
        self.carousel_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.carousel_scroll.setWidget(self.carousel_widget)
        self.media_card_layout.addWidget(self.carousel_scroll)

        # Embedded Viewers Stack:
        # Index 0: VideoPlayerWidget
        # Index 1: ImageZoomView
        # Index 2: GifPlayerWidget
        # Index 3: Placeholder (Empty / Generic File)
        self.embedded_viewer_stack = _ResponsiveStackedWidget(self.media_card)
        self.embedded_viewer_stack.setMinimumHeight(int(580 * scale))

        self.video_player = VideoPlayerWidget(parent=self.embedded_viewer_stack, scale=self.scale)
        self.video_player.fullscreen_requested.connect(self._open_fullscreen_viewer)
        self.image_view = ImageZoomView(parent=self.embedded_viewer_stack, scale=self.scale)
        self.image_view.load_full_res_requested.connect(self._on_image_load_hd_requested)
        self.gif_player = GifPlayerWidget(parent=self.embedded_viewer_stack, scale=self.scale)
        self.gif_player.play_requested.connect(self._on_gif_play_requested)
        self.gif_player.fullscreen_requested.connect(self._open_fullscreen_viewer)

        self.empty_viewer = QFrame(self.embedded_viewer_stack)
        self.empty_viewer.setStyleSheet("background-color: #0E1015; border-radius: 8px;")
        ev_layout = QVBoxLayout(self.empty_viewer)
        self.empty_viewer_lbl = QLabel("No media attachments to display")
        self.empty_viewer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_viewer_lbl.setStyleSheet("color: #6C7280; font-size: 10pt;")
        ev_layout.addWidget(self.empty_viewer_lbl)

        self.embedded_viewer_stack.addWidget(self.video_player)   # 0
        self.embedded_viewer_stack.addWidget(self.image_view)      # 1
        self.embedded_viewer_stack.addWidget(self.gif_player)       # 2
        self.embedded_viewer_stack.addWidget(self.empty_viewer)    # 3

        self.media_card_layout.addWidget(self.embedded_viewer_stack)

        # Loading progress pill for HD media
        self.media_progress_bar = QProgressBar(self.media_card)
        self.media_progress_bar.setFixedHeight(int(4 * scale))
        self.media_progress_bar.setTextVisible(False)
        self.media_progress_bar.setStyleSheet(
            "QProgressBar { background-color: #272A34; border: none; border-radius: 2px; }"
            "QProgressBar::chunk { background-color: #E5A198; border-radius: 2px; }"
        )
        self.media_progress_bar.setVisible(False)
        self.media_card_layout.addWidget(self.media_progress_bar)

        # Media navigation row (Prev, Counter, Next, Download Current)
        self.media_nav_frame = QFrame(self.media_card)
        self.media_nav_frame.setStyleSheet(
            "QFrame { background-color: #12141A; border: 1px solid #282B35; border-radius: 6px; padding: 2px 4px; }"
            "QPushButton { background-color: #20232B; color: #DFE1E5; border: 1px solid #333742; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 4px 10px; }"
            "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
            "QPushButton:disabled { color: #555A66; border-color: #252830; background-color: #161820; }"
        )
        nav_h = QHBoxLayout(self.media_nav_frame)
        nav_h.setContentsMargins(int(6 * scale), int(4 * scale), int(6 * scale), int(4 * scale))
        nav_h.setSpacing(int(8 * scale))

        self.media_prev_btn = QPushButton("  Prev")
        self.media_prev_btn.setIcon(QIcon(get_asset_path("assets/Svg/previous.svg")))
        self.media_prev_btn.setIconSize(QSize(int(11 * scale), int(11 * scale)))
        self.media_prev_btn.setToolTip("Previous media item (A / Left Arrow)")
        self.media_prev_btn.clicked.connect(self._on_prev_media_clicked)
        nav_h.addWidget(self.media_prev_btn)

        self.media_next_btn = QPushButton("Next  ")
        self.media_next_btn.setIcon(QIcon(get_asset_path("assets/Svg/next.svg")))
        self.media_next_btn.setIconSize(QSize(int(11 * scale), int(11 * scale)))
        self.media_next_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.media_next_btn.setToolTip("Next media item (D / Right Arrow)")
        self.media_next_btn.clicked.connect(self._on_next_media_clicked)
        nav_h.addWidget(self.media_next_btn)

        # "Load Full Resolution" button placed next to Prev/Next
        self.nav_load_hd_btn = QPushButton("  Load Full Resolution")
        self.nav_load_hd_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_in.svg")))
        self.nav_load_hd_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        self.nav_load_hd_btn.setFixedHeight(int(26 * scale))
        self.nav_load_hd_btn.setToolTip("Download & view current image at native HD resolution")
        self.nav_load_hd_btn.setStyleSheet(
            "QPushButton { background-color: #2F2424; color: #E5A198; border: 1px solid #5A3535; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 4px 10px; }"
            "QPushButton:hover { background-color: #E5A198; color: #161820; }"
        )
        self.nav_load_hd_btn.clicked.connect(self._on_image_load_hd_requested)
        nav_h.addWidget(self.nav_load_hd_btn)

        # "Load All in HD" button
        self.nav_load_all_hd_btn = QPushButton("  Load All HD")
        self.nav_load_all_hd_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_in.svg")))
        self.nav_load_all_hd_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        self.nav_load_all_hd_btn.setFixedHeight(int(26 * scale))
        self.nav_load_all_hd_btn.setToolTip("Pre-load and cache all images in this post in full native HD")
        self.nav_load_all_hd_btn.setStyleSheet(
            "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 4px 10px; }"
            "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
        )
        self.nav_load_all_hd_btn.clicked.connect(self._on_load_all_hd_clicked)
        nav_h.addWidget(self.nav_load_all_hd_btn)

        self.media_counter_lbl = _CompactCounterLabel("")
        self.media_counter_lbl.setStyleSheet("color: #DFE1E5; font-size: 8.5pt; font-weight: bold; background: transparent; border: none;")
        self.media_counter_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.media_counter_lbl.setMinimumWidth(0)
        self.media_counter_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        nav_h.addWidget(self.media_counter_lbl, 1)

        self.media_dl_curr_btn = QPushButton("  Download File")
        self.media_dl_curr_btn.setIcon(QIcon(get_asset_path("assets/uisvg/save.svg")))
        self.media_dl_curr_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.media_dl_curr_btn.setStyleSheet(
            "QPushButton { background-color: #2F2424; color: #E5A198; border: 1px solid #5A3535; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 4px 10px; }"
            "QPushButton:hover { background-color: #E5A198; color: #161820; }"
        )
        self.media_dl_curr_btn.clicked.connect(self._download_current_media_item)
        nav_h.addWidget(self.media_dl_curr_btn)

        self.media_card_layout.addWidget(self.media_nav_frame)

        # Non-media / direct file attachments list
        self.attachments_frame = QFrame(self.media_card)
        self.attachments_frame.setStyleSheet("background: transparent;")
        self.attachments_layout = QVBoxLayout(self.attachments_frame)
        self.attachments_layout.setContentsMargins(0, int(6 * scale), 0, 0)
        self.attachments_layout.setSpacing(int(6 * scale))
        self.media_card_layout.addWidget(self.attachments_frame)

        self.content_layout.addWidget(self.media_card)

        # ── D. Comments Section ──────────────────────────────────────────────
        self.comments_card = QFrame(self.scroll_content)
        self.comments_card.setObjectName("commentsCard")
        self.comments_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.comments_card.setStyleSheet(
            "QFrame#commentsCard { background-color: #181A22; border: 1px solid #2B2E38; border-radius: 8px; }"
        )
        self.comments_layout = QVBoxLayout(self.comments_card)
        self.comments_layout.setContentsMargins(int(16 * scale), int(12 * scale), int(16 * scale), int(14 * scale))
        self.comments_layout.setSpacing(int(8 * scale))

        # Comments Header
        c_head_row = QHBoxLayout()
        self.comments_header_lbl = QLabel(f"<img src='{get_asset_html_path('assets/uisvg/log.svg')}' width='12' height='12'> &nbsp;COMMENTS")
        self.comments_header_lbl.setStyleSheet("font-size: 8pt; font-weight: bold; color: #8C929E; letter-spacing: 1px; border: none; background: transparent;")
        c_head_row.addWidget(self.comments_header_lbl)

        c_head_row.addStretch(1)

        self.refresh_comments_btn = QPushButton("  Refresh")
        self.refresh_comments_btn.setIcon(QIcon(get_asset_path("assets/uisvg/refresh.svg")))
        self.refresh_comments_btn.setIconSize(QSize(int(11 * scale), int(11 * scale)))
        self.refresh_comments_btn.setFixedHeight(int(24 * scale))
        self.refresh_comments_btn.setStyleSheet(
            "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; "
            "border-radius: 4px; font-size: 7.5pt; padding: 2px 8px; }"
            "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
        )
        self.refresh_comments_btn.clicked.connect(self._fetch_comments)
        c_head_row.addWidget(self.refresh_comments_btn)

        self.comments_layout.addLayout(c_head_row)

        # Status / Placeholder label for comments
        self.comments_status_lbl = QLabel("Loading comments...")
        self.comments_status_lbl.setStyleSheet("color: #8C929E; font-size: 9pt; padding: 10px 0;")
        self.comments_status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.comments_status_lbl.setWordWrap(True)
        self.comments_status_lbl.setMinimumWidth(0)
        self.comments_layout.addWidget(self.comments_status_lbl)

        # Container for loaded comment cards
        self.comments_container = QWidget()
        self.comments_container.setStyleSheet("background: transparent;")
        self.comments_list_layout = QVBoxLayout(self.comments_container)
        self.comments_list_layout.setContentsMargins(0, 0, 0, 0)
        self.comments_list_layout.setSpacing(int(6 * scale))
        self.comments_layout.addWidget(self.comments_container)

        self.content_layout.addWidget(self.comments_card)

        # Final stretch
        self.content_layout.addStretch(1)

        self.scroll_area.setWidget(self.scroll_content)

        # ── 3. Split Body Layout: Main Content (Left) + Attachments Sidebar (Right) ─
        self.body_container = QWidget(self)
        self.body_container.setStyleSheet("background-color: #12141A;")
        body_layout = QHBoxLayout(self.body_container)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        body_layout.addWidget(self.scroll_area, 1)

        # ── Right Vertical Slim Sidebar (Attachments & Playlist) ───────────
        self.sidebar_frame = QFrame(self.body_container)
        self.sidebar_frame.setObjectName("sidebarFrame")
        self.sidebar_frame.setFixedWidth(int(330 * scale))
        self.sidebar_frame.setStyleSheet(
            "QFrame#sidebarFrame { background-color: #15171F; border-left: 1px solid #272A34; border-top: none; border-right: none; border-bottom: none; }"
        )
        sb_layout = QVBoxLayout(self.sidebar_frame)
        sb_layout.setContentsMargins(0, 0, 0, 0)
        sb_layout.setSpacing(0)

        # Sidebar Header
        sb_header = QFrame(self.sidebar_frame)
        sb_header.setFixedHeight(int(46 * scale))
        sb_header.setStyleSheet(
            "QFrame { background-color: #181A24; border-bottom: 1px solid #272A34; border-left: none; }"
        )
        sb_h_layout = QHBoxLayout(sb_header)
        sb_h_layout.setContentsMargins(int(14 * scale), 0, int(12 * scale), 0)
        sb_h_layout.setSpacing(int(8 * scale))

        self.sb_title_lbl = QLabel("ATTACHMENTS")
        self.sb_title_lbl.setStyleSheet(
            "font-size: 8.5pt; font-weight: bold; color: #DFE1E5; letter-spacing: 0.5px; border: none;"
        )
        sb_h_layout.addWidget(self.sb_title_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        badge_h = int(18 * scale)
        self.sb_count_badge = QLabel("0")
        self.sb_count_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sb_count_badge.setFixedHeight(badge_h)
        self.sb_count_badge.setStyleSheet(
            f"background-color: #2F2424; color: #E5A198; font-size: 7.5pt; font-weight: bold; "
            f"padding: 0 {int(6 * scale)}px; border-radius: {badge_h // 2}px; border: 1px solid #5A3535;"
        )
        sb_h_layout.addWidget(self.sb_count_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        sb_h_layout.addStretch(1)

        self.toggle_sb_btn = QPushButton()
        self.toggle_sb_btn.setIcon(QIcon(get_asset_path("assets/uisvg/x_circle.svg")))
        self.toggle_sb_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.toggle_sb_btn.setFixedSize(int(24 * scale), int(24 * scale))
        self.toggle_sb_btn.setToolTip("Hide sidebar")
        self.toggle_sb_btn.setStyleSheet(
            "QPushButton { background: transparent; color: #8C929E; border: none; font-size: 9pt; font-weight: bold; }"
            "QPushButton:hover { color: #E5A198; }"
        )
        self.toggle_sb_btn.clicked.connect(self._toggle_sidebar)
        sb_h_layout.addWidget(self.toggle_sb_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        sb_layout.addWidget(sb_header)

        # Sidebar Scroll Area
        self.sidebar_scroll = _NoHorizontalScrollArea(self.sidebar_frame)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setStyleSheet(
            "QScrollArea { background-color: transparent; border: none; }"
            "QScrollBar:vertical { background: #15171F; width: 6px; margin: 0; }"
            "QScrollBar::handle:vertical { background: #2F333E; border-radius: 3px; min-height: 20px; }"
            "QScrollBar::handle:vertical:hover { background: #E5A198; }"
        )

        self.sidebar_content = QWidget()
        self.sidebar_content.setStyleSheet("background: transparent;")
        self.sidebar_content.setMaximumWidth(int(330 * scale))
        self.sidebar_items_layout = QVBoxLayout(self.sidebar_content)
        self.sidebar_items_layout.setContentsMargins(int(10 * scale), int(10 * scale), int(10 * scale), int(14 * scale))
        self.sidebar_items_layout.setSpacing(int(8 * scale))
        self.sidebar_items_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.sidebar_scroll.setWidget(self.sidebar_content)
        sb_layout.addWidget(self.sidebar_scroll)

        body_layout.addWidget(self.sidebar_frame, 0)

        root_v.addWidget(self.body_container, 1)

    # ─────────────────────────────────────────────────────────────────────────
    #  Loading Post Data
    # ─────────────────────────────────────────────────────────────────────────

    def load_post(self, post: dict, all_posts: list = None):
        """Populates the post detail view with post content, embedded media, and comments."""
        self._thumb_generation += 1
        self._loaded_full_res_indices = set()
        if hasattr(self, "batch_hd_worker") and self.batch_hd_worker and self.batch_hd_worker.isRunning():
            self.batch_hd_worker.cancel()
            self.batch_hd_worker.wait(300)
        self.batch_hd_worker = None
        self.post = post or {}
        self.all_posts = all_posts or []

        # Find index in post list to handle prev/next navigation
        pid = str(self.post.get("id") or "")
        self.current_post_index = -1
        for idx, p in enumerate(self.all_posts):
            if str(p.get("id") or "") == pid:
                self.current_post_index = idx
                break

        self.prev_post_btn.setEnabled(self.current_post_index > 0)
        self.next_post_btn.setEnabled(
            0 <= self.current_post_index < len(self.all_posts) - 1
        )

        # 1. Title & Header
        title = self.post.get("title") or f"Post #{pid}"
        self.title_lbl.setText(title)

        # Platform Pill
        svc = (
            self.post.get("_creator_service")
            or self.post.get("service")
            or "patreon"
        ).lower().strip()
        svc_names = {"patreon": "Patreon", "fanbox": "Pixiv Fanbox", "discord": "Discord"}
        self.platform_pill.setText(svc_names.get(svc, svc.capitalize()))

        # Published date
        pub = self.post.get("published") or self.post.get("added")
        if pub:
            clean_date = str(pub).replace("T", " ").split(".")[0]
            try:
                dt = datetime.fromisoformat(clean_date)
                date_text = f"<img src='{get_asset_html_path('assets/uisvg/clock.svg')}' width='11' height='11'> &nbsp;{dt.day} {dt.strftime('%b %Y')}"
            except Exception:
                date_text = f"<img src='{get_asset_html_path('assets/uisvg/clock.svg')}' width='11' height='11'> &nbsp;{clean_date[:10]}"
        else:
            date_text = f"<img src='{get_asset_html_path('assets/uisvg/clock.svg')}' width='11' height='11'> &nbsp;Unknown Date"
        self.date_lbl.setText(date_text)

        # Creator Info
        cname = self.post.get("_creator_name") or "Creator"
        self.creator_name_lbl.setText(cname)
        cid = str(self.post.get("_creator_id") or self.post.get("user") or "").strip()
        self._update_avatar(svc, cid)

        # 2. Description
        content = (self.post.get("content") or "").strip()
        if content:
            self.desc_card.setVisible(True)
            self.desc_browser.setHtml(
                f"<div style='color: #DFE1E5; font-size: 9.5pt; line-height: 1.5; font-family: Segoe UI, sans-serif;'>"
                f"{content}"
                f"</div>"
            )
            self._update_desc_height()
            QTimer.singleShot(20, self._update_desc_height)
        else:
            self.desc_card.setVisible(False)

        # 3. Extract and load media
        self.media_items = extract_media_items_from_post(self.post)
        self.current_media_index = 0

        # Clear generic attachments list
        while self.attachments_layout.count():
            item = self.attachments_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self.media_items:
            self.media_card.setVisible(False)
            self.sidebar_frame.setVisible(False)
            self.toggle_sidebar_btn.setVisible(False)
            self.video_player.stop_and_release()
            self.gif_player.stop()
        else:
            self.media_card.setVisible(True)
            self.sidebar_frame.setVisible(self.is_sidebar_visible)
            self.toggle_sidebar_btn.setVisible(True)
            count = len(self.media_items)
            self.media_sec_lbl.setText(
                f"MEDIA & ATTACHMENTS ({count} {'ITEM' if count == 1 else 'ITEMS'})"
            )
            self._populate_carousel()
            self._populate_sidebar()
            self._display_media_item(0)

            # Populate non-media or generic file downloads
            for att in self.media_items:
                if att["kind"] in ("file", "audio"):
                    self._add_attachment_download_row(att)

        # 4. Comments
        self._fetch_comments()

        # 5. Asynchronously fetch full post detail (handles Kemono/Pawchive truncated attachments)
        if self.post_worker and self.post_worker.isRunning():
            self.post_worker.cancel()
            self.post_worker.wait(300)

        cid = str(self.post.get("_creator_id") or self.post.get("user") or "").strip()
        if svc and cid and pid:
            self.post_worker = _PostDetailFetchWorker(svc, cid, pid, self.cache_dir, self)
            self.post_worker.post_loaded.connect(self._on_full_post_loaded)
            self.post_worker.start()

        # Scroll to top
        self.scroll_area.verticalScrollBar().setValue(0)

    def _update_avatar(self, svc: str, cid: str):
        sz = int(28 * self.scale)
        if self.media_manager and cid:
            pix = self.media_manager.get_avatar(svc, cid, sz, radius=sz // 2)
            if pix and not pix.isNull():
                self.creator_avatar_lbl.setPixmap(pix)
                self.creator_avatar_lbl.setText("")
                return
        user_pix = QIcon(get_asset_path("assets/Svg/user.svg")).pixmap(sz, sz)
        self.creator_avatar_lbl.setPixmap(user_pix)
        self.creator_avatar_lbl.setText("")
        self.creator_avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def _on_avatar_loaded(self, kind: str, service: str, creator_id: str):
        if kind == "avatar" and self.post:
            svc = (self.post.get("_creator_service") or self.post.get("service") or "").lower().strip()
            cid = str(self.post.get("_creator_id") or self.post.get("user") or "").strip()
            if svc == service.lower().strip() and cid == creator_id.strip():
                self._update_avatar(svc, cid)

    def _add_attachment_download_row(self, att: dict):
        scale = self.scale
        card = QFrame(self.attachments_frame)
        card.setStyleSheet(
            "QFrame { background-color: #20232B; border: 1px solid #2B2E38; border-radius: 6px; padding: 4px; }"
        )
        row = QHBoxLayout(card)
        row.setContentsMargins(int(10 * scale), int(6 * scale), int(10 * scale), int(6 * scale))
        row.setSpacing(int(8 * scale))

        svg_file = "assets/Svg/audio.svg" if att.get("kind") == "audio" else "assets/uisvg/attach.svg"
        icon_pix = QIcon(get_asset_path(svg_file)).pixmap(int(15 * scale), int(15 * scale))
        icon_lbl = QLabel()
        icon_lbl.setPixmap(icon_pix)
        row.addWidget(icon_lbl)

        name_lbl = QLabel(att["name"])
        name_lbl.setStyleSheet("color: #FFFFFF; font-size: 8.5pt; font-weight: bold;")
        row.addWidget(name_lbl, 1)

        dl_btn = QPushButton("  Download")
        dl_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        dl_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        dl_btn.setStyleSheet(
            "QPushButton { background-color: #2A2E38; color: #DFE1E5; border: 1px solid #383D48; border-radius: 4px; font-size: 8pt; padding: 3px 8px; }"
            "QPushButton:hover { background-color: #E5A198; color: #12141A; border-color: #E5A198; font-weight: bold; }"
        )
        dl_btn.clicked.connect(lambda _, a=att: self._download_single_file(a))
        row.addWidget(dl_btn)

        self.attachments_layout.addWidget(card)

    def _download_single_file(self, att: dict):
        if not att:
            return
        url = att["url"]
        name = att.get("name") or "download_file"
        cname = self.post.get("_creator_name") or "Creator"

        if hasattr(self.parent_app, "favorite_download_queue"):
            queue_item = {
                "url": url,
                "name": f"[{cname}] {name}",
                "filename": name,
                "name_for_folder": cname,
                "creator_name": cname,
                "type": "direct_file",
                "force_artist_folder": True,
                "post_title": self.post.get("title") or "",
                "post_id": str(self.post.get("id") or ""),
                "fallback_urls": att.get("fallback_urls") or []
            }
            self.parent_app.favorite_download_queue.append(queue_item)
            if hasattr(self.parent_app, "log_signal"):
                self.parent_app.log_signal.emit(f"Queued file '{name}' for download.")
            if hasattr(self.parent_app, "_process_next_favorite_download"):
                if not getattr(self.parent_app, "is_processing_favorites_queue", False):
                    self.parent_app._process_next_favorite_download()
            self.nav_status_lbl.setText(f"Queued '{name}' for download!")
            QTimer.singleShot(2000, lambda: self.nav_status_lbl.setText(""))
        else:
            QDesktopServices.openUrl(QUrl(url))

    # ─────────────────────────────────────────────────────────────────────────
    #  Media Carousel & Viewers
    # ─────────────────────────────────────────────────────────────────────────

    def _populate_carousel(self):
        # Reset thumb buttons list immediately
        self.thumb_buttons = []

        # Clear existing buttons
        while self.carousel_layout.count():
            item = self.carousel_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        count = len(self.media_items)
        if count <= 1 or self.is_sidebar_visible:
            self.carousel_scroll.setVisible(False)
            return

        self.carousel_scroll.setVisible(True)
        sz = int(52 * self.scale)

        for idx, item in enumerate(self.media_items):
            btn = QPushButton(self.carousel_widget)
            btn.setFixedSize(sz, sz)
            btn.setToolTip(f"#{idx + 1}: {item['name']}")

            kind = item["kind"]
            svg_file = "assets/uisvg/video.svg" if kind in ("video", "gif") else "assets/uisvg/image.svg"
            btn.setIcon(QIcon(get_asset_path(svg_file)))
            btn.setIconSize(QSize(int(20 * self.scale), int(20 * self.scale)))
            btn.setText("")
            btn.setStyleSheet(
                "QPushButton { background-color: #20232B; color: #DFE1E5; border: 1.5px solid #333742; border-radius: 6px; }"
                "QPushButton:hover { background-color: #2A2E38; border-color: #E5A198; }"
            )

            # If thumb cached, set as button icon immediately (skip cloud links and raw videos)
            is_cloud = item.get("is_cloud_link", False)
            thumb_url = item.get("thumb_url")
            if not thumb_url and item.get("kind") == "image" and not is_cloud:
                thumb_url = item.get("url")

            if thumb_url and not is_cloud:
                t_hash = hashlib.md5(thumb_url.encode("utf-8")).hexdigest() + ".jpg"
                t_path = os.path.join(self.thumbs_cache_dir, t_hash)
                if os.path.exists(t_path):
                    p = QPixmap(t_path)
                    if not p.isNull():
                        btn.setIcon(QIcon(p))
                        btn.setIconSize(QSize(sz - 8, sz - 8))
                        btn.setText("")

            btn.clicked.connect(lambda _, i=idx: self._on_carousel_item_clicked(i))
            self.carousel_layout.addWidget(btn)
            self.thumb_buttons.append(btn)

    def _on_carousel_item_clicked(self, index: int):
        if self.current_media_index == index:
            if 0 <= index < len(self.media_items):
                kind = self.media_items[index].get("kind")
                if kind == "gif" and hasattr(self, "gif_player") and not self.gif_player.is_playing():
                    self._on_gif_play_requested()
                    return
                elif kind == "video" and hasattr(self, "video_player") and not self.video_player.is_playing():
                    self.video_player.play()
                    return
                elif kind == "image" and hasattr(self, "image_view") and not self.image_view.is_full_res():
                    self._on_image_load_hd_requested()
                    return
        self._display_media_item(index)

    def _toggle_sidebar(self):
        self.is_sidebar_visible = not self.is_sidebar_visible
        self.sidebar_frame.setVisible(self.is_sidebar_visible)
        if hasattr(self, "carousel_scroll"):
            self.carousel_scroll.setVisible(not self.is_sidebar_visible and len(self.media_items) > 1)
        if self.is_sidebar_visible:
            self.toggle_sidebar_btn.setStyleSheet(
                "QPushButton { background-color: #352020; color: #E5A198; border: 1px solid #E5A198; "
                "border-radius: 5px; font-size: 8.5pt; font-weight: bold; padding: 4px 10px; }"
                "QPushButton:hover { background-color: #422626; }"
            )
        else:
            self.toggle_sidebar_btn.setStyleSheet(
                "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; "
                "border-radius: 5px; font-size: 8.5pt; font-weight: bold; padding: 4px 10px; }"
                "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
            )
        if hasattr(self, "scroll_area"):
            self.scroll_area.horizontalScrollBar().setValue(0)
        if getattr(self, "is_expanded_view", False) and hasattr(self, "image_view"):
            pix = getattr(self.image_view, "_pixmap", None)
            if pix and not pix.isNull():
                self._update_expanded_height(pix)

    def _populate_sidebar(self):
        # Clear existing cards
        self.sidebar_cards = []
        while self.sidebar_items_layout.count():
            item = self.sidebar_items_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        count = len(self.media_items)
        if count == 0:
            self.sidebar_frame.setVisible(False)
            self.toggle_sidebar_btn.setVisible(False)
            return

        self.sidebar_frame.setVisible(self.is_sidebar_visible)
        self.toggle_sidebar_btn.setVisible(True)
        self.toggle_sidebar_btn.setText(f"  Attachments ({count})")
        self.sb_count_badge.setText(str(count))

        for idx, item in enumerate(self.media_items):
            card = _SidebarMediaCard(item, idx, scale=self.scale, parent=self.sidebar_content)
            card.play_clicked.connect(self._on_sidebar_card_clicked)
            card.download_clicked.connect(self._download_single_file)

            self.sidebar_items_layout.addWidget(card)
            self.sidebar_cards.append(card)

            # Check thumb (only fetch thumbnails for images; never download external drive or video files as thumbnails)
            is_cloud = item.get("is_cloud_link", False)
            kind = item.get("kind", "file")
            thumb_url = item.get("thumb_url")
            if not thumb_url and kind == "image" and not is_cloud:
                thumb_url = item.get("url")

            if thumb_url and not is_cloud:
                t_hash = hashlib.md5(thumb_url.encode("utf-8")).hexdigest() + ".jpg"
                t_path = os.path.join(self.thumbs_cache_dir, t_hash)
                if os.path.exists(t_path):
                    card.set_thumbnail_pixmap(QPixmap(t_path))
                else:
                    task = _ThumbDownloadTask(
                        idx, thumb_url, t_path,
                        self._thumb_generation,
                        lambda: self._thumb_generation,
                        self.thumb_signals
                    )
                    QThreadPool.globalInstance().start(task)

        if 0 <= self.current_media_index < len(self.sidebar_cards):
            self.sidebar_cards[self.current_media_index].set_active(True)

    def _on_item_thumb_loaded(self, index: int, thumb_url: str, disk_path: str):
        if not os.path.exists(disk_path):
            return
        pix = QPixmap(disk_path)
        if pix.isNull():
            return

        # Direct O(1) index update to avoid iterating and lagging the GUI thread
        cards = getattr(self, "sidebar_cards", [])
        if 0 <= index < len(cards):
            cards[index].set_thumbnail_pixmap(pix)

        buttons = getattr(self, "thumb_buttons", [])
        sz = int(52 * self.scale)
        if 0 <= index < len(buttons):
            btn = buttons[index]
            try:
                icon_pix = pix.scaled(
                    sz - 8, sz - 8,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation
                )
                btn.setIcon(QIcon(icon_pix))
                btn.setIconSize(QSize(sz - 8, sz - 8))
                btn.setText("")
            except Exception:
                pass

        if index == self.current_media_index and 0 <= index < len(self.media_items):
            kind = self.media_items[index].get("kind")
            if kind == "gif" and hasattr(self, "gif_player"):
                self.gif_player.set_preview_pixmap(pix)
            elif kind == "video" and hasattr(self, "video_player"):
                self.video_player.set_preview_pixmap(pix)
            elif kind == "image" and hasattr(self, "image_view") and not self.image_view.is_full_res():
                self.image_view.set_preview_thumbnail(pix, fit_to_view=True)
                if getattr(self, "is_expanded_view", False):
                    self._update_expanded_height(pix)

    def _on_sidebar_card_clicked(self, index: int):
        if self.current_media_index == index:
            if 0 <= index < len(self.media_items):
                kind = self.media_items[index].get("kind")
                if kind == "gif" and hasattr(self, "gif_player") and not self.gif_player.is_playing():
                    self._on_gif_play_requested()
                    return
                elif kind == "video" and hasattr(self, "video_player") and not self.video_player.is_playing():
                    self.video_player.play()
                    return
                elif kind == "image" and hasattr(self, "image_view") and not self.image_view.is_full_res():
                    self._on_image_load_hd_requested()
                    return
        self._display_media_item(index)

    def _on_prev_media_clicked(self):
        if self.media_items and self.current_media_index > 0:
            self._display_media_item(self.current_media_index - 1)

    def _on_next_media_clicked(self):
        if self.media_items and self.current_media_index < len(self.media_items) - 1:
            self._display_media_item(self.current_media_index + 1)

    def _download_current_media_item(self):
        if self.media_items and 0 <= self.current_media_index < len(self.media_items):
            self._download_single_file(self.media_items[self.current_media_index])
            if hasattr(self, "media_dl_curr_btn"):
                self.media_dl_curr_btn.setText("  ✓ Queued!")
                QTimer.singleShot(1500, lambda: self.media_dl_curr_btn.setText("  Download File"))

    def _on_full_post_loaded(self, full_post: dict):
        if not full_post or not self.post:
            return
        if str(full_post.get("id")) != str(self.post.get("id")):
            return

        new_attachments = full_post.get("attachments") or []
        old_attachments = self.post.get("attachments") or []
        new_content = full_post.get("content") or ""
        old_content = self.post.get("content") or ""

        changed = (len(new_attachments) != len(old_attachments)) or (len(new_content) != len(old_content))

        for k, v in full_post.items():
            if v or k not in self.post:
                self.post[k] = v

        if changed:
            if len(new_content) != len(old_content):
                c = new_content.strip()
                if c:
                    self.desc_card.setVisible(True)
                    self.desc_browser.setHtml(
                        f"<div style='color: #DFE1E5; font-size: 9.5pt; line-height: 1.5; font-family: Segoe UI, sans-serif;'>"
                        f"{c}"
                        f"</div>"
                    )
                    self._update_desc_height()
                    QTimer.singleShot(20, self._update_desc_height)
                else:
                    self.desc_card.setVisible(False)

            updated_items = extract_media_items_from_post(self.post)
            if len(updated_items) != len(self.media_items):
                self.media_items = updated_items
                count = len(self.media_items)
                self.media_card.setVisible(True)
                self.sidebar_frame.setVisible(self.is_sidebar_visible)
                self.toggle_sidebar_btn.setVisible(True)
                self.media_sec_lbl.setText(
                    f"MEDIA & ATTACHMENTS ({count} {'ITEM' if count == 1 else 'ITEMS'})"
                )
                self._populate_carousel()
                self._populate_sidebar()
                if self.current_media_index >= count:
                    self._display_media_item(0)
                else:
                    self._display_media_item(self.current_media_index)

    def _display_media_item(self, index: int):
        if not self.media_items or index < 0 or index >= len(self.media_items):
            self.embedded_viewer_stack.setCurrentIndex(3)
            self.media_counter_lbl.setText("")
            return

        self.current_media_index = index
        item = self.media_items[index]
        kind = item["kind"]

        # Highlight active carousel button
        if getattr(self, "thumb_buttons", None):
            for idx, btn in enumerate(self.thumb_buttons):
                try:
                    if idx == index:
                        btn.setStyleSheet(
                            "QPushButton { background-color: #352020; color: #E5A198; border: 2px solid #E5A198; border-radius: 6px; font-size: 11pt; }"
                        )
                    else:
                        btn.setStyleSheet(
                            "QPushButton { background-color: #20232B; color: #DFE1E5; border: 1.5px solid #333742; border-radius: 6px; font-size: 11pt; }"
                            "QPushButton:hover { background-color: #2A2E38; border-color: #E5A198; }"
                        )
                except Exception:
                    pass

        # Highlight active sidebar card & ensure visible
        if getattr(self, "sidebar_cards", None):
            for idx, card in enumerate(self.sidebar_cards):
                card.set_active(idx == index)
            if 0 <= index < len(self.sidebar_cards):
                try:
                    self.sidebar_scroll.ensureWidgetVisible(self.sidebar_cards[index])
                except Exception:
                    pass

        # Update navigation bar counter & buttons
        kind_label = "GIF" if kind == "gif" else ("Video" if kind == "video" else "Image")
        svg_file = "assets/uisvg/video.svg" if kind in ("video", "gif") else "assets/uisvg/image.svg"
        self.media_counter_lbl.setText(
            f"<img src='{get_asset_html_path(svg_file)}' width='13' height='13'> &nbsp;<b>{kind_label}</b>  {index + 1} of {len(self.media_items)}   ·   {item['name']}"
        )
        has_multiple = len(self.media_items) > 1
        self.media_prev_btn.setVisible(has_multiple)
        self.media_next_btn.setVisible(has_multiple)
        self.media_prev_btn.setEnabled(index > 0)
        self.media_next_btn.setEnabled(index < len(self.media_items) - 1)

        image_count = sum(1 for m in self.media_items if m.get("kind") == "image")
        self.nav_load_hd_btn.setVisible(kind == "image")
        self.nav_load_all_hd_btn.setVisible(image_count > 1)

        # Stop previous playback
        self.video_player.stop_and_release()
        self.gif_player.stop()

        self.zoom_ctrl_frame.setVisible(kind == "image")

        url = item["url"]
        ext = item.get("ext", ".bin")
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        disk_filename = f"{url_hash}{ext}"
        disk_path = os.path.join(self.cache_dir, disk_filename)

        # Check if preview thumbnail is cached to display immediately
        preview_pix = None
        thumb_url = item.get("thumb_url")
        if thumb_url:
            t_hash = hashlib.md5(thumb_url.encode("utf-8")).hexdigest() + ".jpg"
            t_path = os.path.join(self.thumbs_cache_dir, t_hash)
            if os.path.exists(t_path):
                p = QPixmap(t_path)
                if not p.isNull():
                    preview_pix = p

        if kind == "video":
            self.embedded_viewer_stack.setCurrentIndex(0)
            self.media_progress_bar.setVisible(False)
            self.video_player.set_media(url, disk_path, preview_pixmap=preview_pix)

        elif kind == "image":
            self.embedded_viewer_stack.setCurrentIndex(1)

            # If full-resolution image file exists on disk and is valid, ALWAYS load it in HD!
            if os.path.exists(disk_path) and os.path.getsize(disk_path) > 128:
                pix = QPixmap(disk_path)
                if not pix.isNull():
                    self._loaded_full_res_indices.add(index)
                    self.image_view.set_full_res_pixmap(pix, fit_to_view=True)
                    self.media_progress_bar.setVisible(False)
                    self._update_hd_btn_state(True)
                    if getattr(self, "is_expanded_view", False):
                        self._update_expanded_height(pix)
                    return

            self._update_hd_btn_state(False)
            self.media_progress_bar.setVisible(False)
            self.image_view.set_preview_thumbnail(preview_pix, fit_to_view=True)
            if getattr(self, "is_expanded_view", False):
                self._update_expanded_height(preview_pix)

        elif kind == "gif":
            self.embedded_viewer_stack.setCurrentIndex(2)
            self.media_progress_bar.setVisible(False)

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
            self.embedded_viewer_stack.setCurrentIndex(3)
            self.media_progress_bar.setVisible(False)
            self._update_hd_btn_state(False)

    def _update_hd_btn_state(self, is_loaded: bool):
        curr_item = self.media_items[self.current_media_index] if (0 <= self.current_media_index < len(self.media_items)) else None
        is_img = bool(curr_item and curr_item.get("kind") == "image")

        if hasattr(self, "nav_load_hd_btn"):
            if is_loaded or not is_img:
                # Hide the button completely once the image is in full HD (or if viewing video/file)
                self.nav_load_hd_btn.setVisible(False)
            else:
                self.nav_load_hd_btn.setVisible(True)
                self.nav_load_hd_btn.setText("  Load Full Resolution")
                self.nav_load_hd_btn.setIcon(QIcon(get_asset_path("assets/uisvg/zoom_in.svg")))
                self.nav_load_hd_btn.setStyleSheet(
                    "QPushButton { background-color: #2F2424; color: #E5A198; border: 1px solid #5A3535; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 4px 10px; }"
                    "QPushButton:hover { background-color: #E5A198; color: #161820; }"
                )

        if hasattr(self, "nav_load_all_hd_btn"):
            image_count = sum(1 for it in self.media_items if it.get("kind") == "image")
            unloaded_count = sum(
                1 for idx, it in enumerate(self.media_items)
                if it.get("kind") == "image" and idx not in self._loaded_full_res_indices
            )
            is_batch_running = bool(hasattr(self, "batch_hd_worker") and self.batch_hd_worker and self.batch_hd_worker.isRunning())
            if is_batch_running:
                self.nav_load_all_hd_btn.setVisible(True)
            elif unloaded_count > 0 and image_count > 1:
                self.nav_load_all_hd_btn.setVisible(True)
                self.nav_load_all_hd_btn.setText(f"  Load All HD ({unloaded_count})")
                self.nav_load_all_hd_btn.setStyleSheet(
                    "QPushButton { background-color: #242730; color: #DFE1E5; border: 1px solid #363A45; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 4px 10px; }"
                    "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
                )
            else:
                # All images are loaded in HD, or only 1 image in post. Hide the batch button!
                self.nav_load_all_hd_btn.setVisible(False)

    def _on_image_load_hd_requested(self):
        if not self.media_items or self.current_media_index < 0 or self.current_media_index >= len(self.media_items):
            return
        item = self.media_items[self.current_media_index]
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
                self._loaded_full_res_indices.add(self.current_media_index)
                self.image_view.set_full_res_pixmap(pix, fit_to_view=True)
                self._update_hd_btn_state(True)
                self.media_progress_bar.setVisible(False)
                if getattr(self, "is_expanded_view", False):
                    self._update_expanded_height(pix)
                return

        self.image_view.show_loading("Loading Full Resolution Image...")
        self.media_progress_bar.setValue(0)
        self.media_progress_bar.setVisible(True)

        if self.stream_worker and self.stream_worker.isRunning():
            self.stream_worker.cancel()
            self.stream_worker.wait(500)

        fallbacks = item.get("fallback_urls", [])
        self.stream_worker = _MediaStreamWorker(url, disk_path, fallback_urls=fallbacks, parent=self)
        self.stream_worker.progress.connect(self._on_stream_progress)
        self.stream_worker.progress.connect(lambda pct: self.image_view.set_loading_progress(pct))
        self.stream_worker.finished.connect(self._on_image_download_finished)
        self.stream_worker.start()

    def _on_gif_play_requested(self):
        if not self.media_items or self.current_media_index < 0 or self.current_media_index >= len(self.media_items):
            return
        item = self.media_items[self.current_media_index]
        if item.get("kind") != "gif":
            return

        url = item["url"]
        ext = item.get("ext", ".gif")
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        disk_filename = f"{url_hash}{ext}"
        disk_path = os.path.join(self.cache_dir, disk_filename)

        if os.path.exists(disk_path) and os.path.getsize(disk_path) > 128:
            self.gif_player.load_gif(disk_path)
            self.media_progress_bar.setVisible(False)
            return

        self.gif_player.show_loading("Loading Animated GIF (0%)...")
        self.media_progress_bar.setValue(0)
        self.media_progress_bar.setVisible(True)

        if self.stream_worker and self.stream_worker.isRunning():
            self.stream_worker.cancel()
            self.stream_worker.wait(500)

        fallbacks = item.get("fallback_urls", [])
        self.stream_worker = _MediaStreamWorker(url, disk_path, fallback_urls=fallbacks, parent=self)
        self.stream_worker.progress.connect(self._on_stream_progress)
        self.stream_worker.finished.connect(self._on_gif_download_finished)
        self.stream_worker.start()

    def _on_stream_progress(self, percent: int):
        self.media_progress_bar.setValue(percent)
        if hasattr(self, "gif_player"):
            self.gif_player.set_loading_progress(percent)
        if hasattr(self, "image_view"):
            self.image_view.set_loading_progress(percent)

    def _on_image_download_finished(self, success: bool, disk_path: str, err: str):
        self.media_progress_bar.setVisible(False)
        if success and os.path.exists(disk_path):
            pix = QPixmap(disk_path)
            if not pix.isNull():
                self._loaded_full_res_indices.add(self.current_media_index)
                self.image_view.set_full_res_pixmap(pix, fit_to_view=True)
                self._update_hd_btn_state(True)
                image_count = sum(1 for it in self.media_items if it.get("kind") == "image")
                if hasattr(self, "nav_load_all_hd_btn") and len(self._loaded_full_res_indices) >= image_count:
                    self.nav_load_all_hd_btn.setText("  ✓ All HD Loaded")
                    self.nav_load_all_hd_btn.setStyleSheet(
                        "QPushButton { background-color: #1E2B22; color: #A8E5BA; border: 1px solid #2F5A3B; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 4px 10px; }"
                    )
                if getattr(self, "is_expanded_view", False):
                    self._update_expanded_height(pix)
                return
        prev_pix = getattr(self.image_view, "_preview_pixmap", None)
        self.image_view.set_preview_thumbnail(prev_pix, fit_to_view=True)
        if hasattr(self.image_view, "overlay_btn"):
            self.image_view.overlay_btn.setText("  Failed to Load HD (Click to Retry)  ")
        self._update_hd_btn_state(False)

    def _on_load_all_hd_clicked(self):
        """Downloads and caches all images in the post in full native resolution."""
        if not self.media_items:
            return

        image_tasks = []
        for idx, item in enumerate(self.media_items):
            if item.get("kind") != "image":
                continue
            url = item["url"]
            ext = item.get("ext", ".bin")
            url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
            disk_filename = f"{url_hash}{ext}"
            disk_path = os.path.join(self.cache_dir, disk_filename)

            if os.path.exists(disk_path) and os.path.getsize(disk_path) > 128:
                try:
                    reader = QImageReader(disk_path)
                    if reader.size().isValid():
                        self._loaded_full_res_indices.add(idx)
                        if idx == self.current_media_index:
                            pix = QPixmap(disk_path)
                            if not pix.isNull():
                                self.image_view.set_full_res_pixmap(pix, fit_to_view=not getattr(self, "is_expanded_view", False))
                                self._update_hd_btn_state(True)
                        continue
                except Exception:
                    pass

            image_tasks.append({
                "index": idx,
                "url": url,
                "disk_path": disk_path,
                "fallback_urls": item.get("fallback_urls") or []
            })

        image_count = sum(1 for it in self.media_items if it.get("kind") == "image")
        if not image_tasks:
            self.nav_load_all_hd_btn.setText("  ✓ All HD Loaded")
            self.nav_load_all_hd_btn.setStyleSheet(
                "QPushButton { background-color: #1E2B22; color: #A8E5BA; border: 1px solid #2F5A3B; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 4px 10px; }"
            )
            return

        if self.batch_hd_worker and self.batch_hd_worker.isRunning():
            return

        self.nav_load_all_hd_btn.setEnabled(False)
        self.nav_load_all_hd_btn.setText(f"  Loading HD (0/{len(image_tasks)})...")
        self.media_progress_bar.setValue(0)
        self.media_progress_bar.setVisible(True)

        self.batch_hd_worker = _BatchHDLoaderWorker(image_tasks, parent=self)
        self.batch_hd_worker.item_loaded.connect(self._on_batch_hd_item_loaded)
        self.batch_hd_worker.progress.connect(self._on_batch_hd_progress)
        self.batch_hd_worker.all_finished.connect(self._on_batch_hd_finished)
        self.batch_hd_worker.start()

    def _on_batch_hd_item_loaded(self, idx: int, disk_path: str):
        self._loaded_full_res_indices.add(idx)
        if idx == self.current_media_index:
            pix = QPixmap(disk_path)
            if not pix.isNull():
                self.image_view.set_full_res_pixmap(pix, fit_to_view=True)
                self._update_hd_btn_state(True)
                if getattr(self, "is_expanded_view", False):
                    self._update_expanded_height(pix)

    def _on_batch_hd_progress(self, completed: int, total: int):
        self.nav_load_all_hd_btn.setText(f"  Loading HD ({completed}/{total})...")
        pct = int((completed / total) * 100) if total > 0 else 0
        self.media_progress_bar.setValue(pct)

    def _on_batch_hd_finished(self, success_count: int, total: int):
        self.media_progress_bar.setVisible(False)
        self.nav_load_all_hd_btn.setEnabled(True)

        # Guarantee that currently viewed image reloads in HD if its file exists
        if 0 <= self.current_media_index < len(self.media_items):
            curr_item = self.media_items[self.current_media_index]
            if curr_item.get("kind") == "image":
                url = curr_item["url"]
                ext = curr_item.get("ext", ".bin")
                url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
                dpath = os.path.join(self.cache_dir, f"{url_hash}{ext}")
                if os.path.exists(dpath) and os.path.getsize(dpath) > 128:
                    pix = QPixmap(dpath)
                    if not pix.isNull():
                        self._loaded_full_res_indices.add(self.current_media_index)
                        self.image_view.set_full_res_pixmap(pix, fit_to_view=not getattr(self, "is_expanded_view", False))
                        if getattr(self, "is_expanded_view", False):
                            self._update_expanded_height(pix)

        self._update_hd_btn_state(self.image_view.is_full_res())

    def _on_gif_download_finished(self, success: bool, disk_path: str, err: str):
        self.media_progress_bar.setVisible(False)
        if success and os.path.exists(disk_path):
            self.gif_player.load_gif(disk_path)
        else:
            self.gif_player.stop()

    def _toggle_expanded_view(self):
        self.is_expanded_view = not getattr(self, "is_expanded_view", False)
        if self.is_expanded_view:
            self.z_expand_btn.setText(" Fit Box")
            self.z_expand_btn.setStyleSheet(
                "QPushButton { background-color: #352020; color: #E5A198; border: 1px solid #E5A198; border-radius: 4px; font-size: 8pt; font-weight: bold; padding: 2px 6px; }"
            )
            pix = getattr(self.image_view, "_pixmap", None)
            self._update_expanded_height(pix)
        else:
            self.z_expand_btn.setText(" Full View")
            self.z_expand_btn.setStyleSheet("")
            base_h = int(580 * self.scale)
            self.embedded_viewer_stack.setMinimumHeight(base_h)
            self.embedded_viewer_stack.setMaximumHeight(16777215)
            self.embedded_viewer_stack.setFixedHeight(base_h)
            self.image_view.fit_in_window()
        if hasattr(self, "scroll_area"):
            self.scroll_area.horizontalScrollBar().setValue(0)

    def _update_expanded_height(self, pixmap: QPixmap = None):
        if not getattr(self, "is_expanded_view", False):
            return
        base_h = int(580 * self.scale)
        if hasattr(self, "scroll_area"):
            self.scroll_area.horizontalScrollBar().setValue(0)
            viewport_w = self.scroll_area.viewport().width()
            avail_w = max(300, viewport_w - int(72 * self.scale))
        else:
            avail_w = max(300, self.embedded_viewer_stack.width() - 20)

        if pixmap and not pixmap.isNull():
            w = pixmap.width()
            h = pixmap.height()
            if w > 0:
                calc_h = int(avail_w * (h / w))
                target_h = max(base_h, min(calc_h, int(1800 * self.scale)))
                self.embedded_viewer_stack.setFixedHeight(target_h)
                QTimer.singleShot(60, self.image_view.fit_in_window)
                return
        self.embedded_viewer_stack.setFixedHeight(int(850 * self.scale))
        QTimer.singleShot(60, self.image_view.fit_in_window)

    def _open_fullscreen_viewer(self):
        if not self.post:
            return
        if hasattr(self, "video_player"):
            try:
                self.video_player.pause()
            except Exception:
                pass
        if hasattr(self, "gif_player"):
            try:
                self.gif_player.pause()
            except Exception:
                pass
        dlg = PawchiveMediaViewerDialog(
            post=self.post,
            initial_index=self.current_media_index,
            scale=self.scale,
            parent=self
        )
        dlg.showMaximized()
        dlg.exec()

    def _zoom_in(self):
        self.image_view.zoom_in()

    def _zoom_out(self):
        self.image_view.zoom_out()

    def _zoom_fit(self):
        if hasattr(self, "scroll_area"):
            self.scroll_area.horizontalScrollBar().setValue(0)
        self.image_view.fit_in_window()

    def _zoom_actual(self):
        self.image_view.set_actual_size()

    def _zoom_rotate(self):
        self.image_view.rotate_cw()

    # ─────────────────────────────────────────────────────────────────────────
    #  Comments
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_comments(self):
        """Fetches comments asynchronously for the active post."""
        if not self.post:
            return

        # Clear existing comments
        while self.comments_list_layout.count():
            item = self.comments_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.comments_status_lbl.setText("Loading comments...")
        self.comments_status_lbl.setVisible(True)
        self.comments_header_lbl.setText(f"<img src='{get_asset_html_path('assets/uisvg/log.svg')}' width='12' height='12'> &nbsp;COMMENTS")

        svc = (
            self.post.get("_creator_service")
            or self.post.get("service")
            or "patreon"
        ).lower().strip()
        cid = str(self.post.get("_creator_id") or self.post.get("user") or "").strip()
        pid = str(self.post.get("id") or "").strip()

        if self.comments_worker and self.comments_worker.isRunning():
            self.comments_worker.cancel()
            self.comments_worker.wait(500)

        self.comments_worker = _CommentsFetchWorker(
            svc, cid, pid, self.comments_cache_dir, self
        )
        self.comments_worker.comments_loaded.connect(self._on_comments_loaded)
        self.comments_worker.comments_failed.connect(self._on_comments_failed)
        self.comments_worker.start()

    def _on_comments_loaded(self, comments: list):
        if not comments:
            self.comments_status_lbl.setText("No comments on this post.")
            self.comments_status_lbl.setVisible(True)
            self.comments_header_lbl.setText(f"<img src='{get_asset_html_path('assets/uisvg/log.svg')}' width='12' height='12'> &nbsp;COMMENTS (0)")
            return

        self.comments_status_lbl.setVisible(False)
        count = len(comments)
        self.comments_header_lbl.setText(f"<img src='{get_asset_html_path('assets/uisvg/log.svg')}' width='12' height='12'> &nbsp;COMMENTS ({count})")

        # Organize comments with replies threaded
        top_level = []
        replies_by_parent = {}
        for c in comments:
            parent_id = c.get("parent_id")
            if parent_id:
                replies_by_parent.setdefault(parent_id, []).append(c)
            else:
                top_level.append(c)

        for top in top_level:
            card = CommentCardWidget(top, scale=self.scale, parent=self.comments_container)
            self.comments_list_layout.addWidget(card)

            # Insert replies beneath this parent
            cid = str(top.get("id") or "")
            for rep in replies_by_parent.get(cid, []):
                rep_card = CommentCardWidget(rep, scale=self.scale, parent=self.comments_container)
                self.comments_list_layout.addWidget(rep_card)

        # Handle any orphaned replies
        seen_parents = {str(t.get("id") or "") for t in top_level}
        for pid, reps in replies_by_parent.items():
            if pid not in seen_parents:
                for orphan in reps:
                    card = CommentCardWidget(orphan, scale=self.scale, parent=self.comments_container)
                    self.comments_list_layout.addWidget(card)

    def _on_comments_failed(self, err_msg: str):
        short_err = (err_msg[:100] + "...") if len(err_msg) > 100 else err_msg
        self.comments_status_lbl.setText(f"Comments could not be loaded ({short_err})")
        self.comments_status_lbl.setVisible(True)

    # ─────────────────────────────────────────────────────────────────────────
    #  Actions & Navigation
    # ─────────────────────────────────────────────────────────────────────────

    def unload_media(self):
        """Releases all media players and frees loaded images from RAM."""
        self._thumb_generation += 1
        if hasattr(self, "batch_hd_worker") and self.batch_hd_worker and self.batch_hd_worker.isRunning():
            try:
                self.batch_hd_worker.cancel()
                self.batch_hd_worker.wait(300)
            except Exception:
                pass
            self.batch_hd_worker = None
        if getattr(self, "stream_worker", None) and self.stream_worker.isRunning():
            try:
                self.stream_worker.cancel()
            except Exception:
                pass
        if hasattr(self, "video_player"):
            try:
                self.video_player.stop_and_release()
            except Exception:
                pass
        if hasattr(self, "gif_player"):
            try:
                self.gif_player.stop()
            except Exception:
                pass
        if hasattr(self, "image_view"):
            try:
                self.image_view.set_pixmap(QPixmap())
            except Exception:
                pass

    def _on_back_clicked(self):
        # Stop media and release loaded image pixmap before switching away
        self.unload_media()
        self.back_to_posts_clicked.emit()

    def closeEvent(self, event):
        self.unload_media()
        super().closeEvent(event)

    def _on_prev_post_clicked(self):
        if 0 < self.current_post_index < len(self.all_posts):
            prev_p = self.all_posts[self.current_post_index - 1]
            self.load_post(prev_p, self.all_posts)

    def _on_next_post_clicked(self):
        if 0 <= self.current_post_index < len(self.all_posts) - 1:
            next_p = self.all_posts[self.current_post_index + 1]
            self.load_post(next_p, self.all_posts)

    def _get_direct_post_url(self):
        if not self.post:
            return ""
        svc = (
            self.post.get("_creator_service")
            or self.post.get("service")
            or "patreon"
        ).lower().strip()
        cid = str(self.post.get("_creator_id") or self.post.get("user") or "").strip()
        pid = str(self.post.get("id") or "").strip()
        url_type = "server" if svc == "discord" else "user"
        return f"https://pawchive.pw/{svc}/{url_type}/{cid}/post/{pid}"

    def _copy_post_url(self):
        url = self._get_direct_post_url()
        if url:
            QApplication.clipboard().setText(url)
            self.nav_status_lbl.setText("Copied post URL to clipboard!")
            QTimer.singleShot(2000, lambda: self.nav_status_lbl.setText(""))

    def _open_in_browser(self):
        url = self._get_direct_post_url()
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _send_to_downloader(self):
        url = self._get_direct_post_url()
        if not url:
            return

        cname = (self.post.get("_creator_name") or "").strip()
        cid = str(self.post.get("_creator_id") or self.post.get("user") or "").strip()
        csvc = (self.post.get("_creator_service") or self.post.get("service") or "").lower().strip()

        if hasattr(self.parent_app, 'creator_name_cache') and cname and cid:
            if csvc:
                self.parent_app.creator_name_cache[(csvc, cid)] = cname
            self.parent_app.creator_name_cache[cid] = cname
        if cname and hasattr(self.parent_app, 'last_known_creator_name'):
            self.parent_app.last_known_creator_name = cname
        if hasattr(self.parent_app, 'link_input') and self.parent_app.link_input:
            self.parent_app.link_input.setText(url)

        if hasattr(self.parent_app, 'switch_tab'):
            self.parent_app.switch_tab(0)

        if hasattr(self.parent_app, 'log_signal'):
            title = self.post.get("title") or f"Post #{self.post.get('id')}"
            self.parent_app.log_signal.emit(f"Selected post '{title}' by {cname} ({url})")

        self.nav_status_lbl.setText("Sent post to Downloader tab!")
        QTimer.singleShot(2000, lambda: self.nav_status_lbl.setText(""))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_desc_height()
        if hasattr(self, "scroll_area"):
            self.scroll_area.horizontalScrollBar().setValue(0)
        if getattr(self, "is_expanded_view", False) and hasattr(self, "image_view"):
            pix = getattr(self.image_view, "_pixmap", None)
            if pix and not pix.isNull():
                self._update_expanded_height(pix)

    def _update_desc_height(self):
        if not hasattr(self, "desc_browser") or not hasattr(self, "desc_card"):
            return
        if not self.desc_card.isVisible():
            return
        w = self.desc_card.width()
        margins = self.desc_card.layout().contentsMargins()
        avail_w = max(100, w - margins.left() - margins.right() - int(10 * self.scale))
        self.desc_browser.document().setTextWidth(avail_w)
        doc_h = int(self.desc_browser.document().size().height())
        self.desc_browser.setFixedHeight(max(int(24 * self.scale), doc_h + int(6 * self.scale)))
        self.desc_card.adjustSize()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape or key == Qt.Key.Key_Back:
            self._on_back_clicked()
        elif key == Qt.Key.Key_Left and event.modifiers() == Qt.KeyboardModifier.AltModifier:
            self._on_back_clicked()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_A):
            if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._on_prev_media_clicked()
            else:
                super().keyPressEvent(event)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_D):
            if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._on_next_media_clicked()
            else:
                super().keyPressEvent(event)
        elif key == Qt.Key.Key_0:
            self._zoom_fit()
        elif key == Qt.Key.Key_F:
            self._open_fullscreen_viewer()
        elif key == Qt.Key.Key_R:
            self._zoom_rotate()
        else:
            super().keyPressEvent(event)

    def close(self):
        if self.post_worker and self.post_worker.isRunning():
            self.post_worker.cancel()
            self.post_worker.wait(300)
        if self.comments_worker and self.comments_worker.isRunning():
            self.comments_worker.cancel()
            self.comments_worker.wait(500)
        if self.stream_worker and self.stream_worker.isRunning():
            self.stream_worker.cancel()
            self.stream_worker.wait(500)
        self.video_player.stop_and_release()
        self.gif_player.stop()
        super().close()
