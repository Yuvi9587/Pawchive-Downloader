import os
import re
import json
import html
import hashlib
from collections import OrderedDict
import requests
from datetime import datetime
from urllib.parse import urlparse

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QTableView, QHeaderView, QAbstractItemView, QFrame,
    QProgressBar, QMenu, QApplication, QDialog, QMessageBox, QListView,
    QStyledItemDelegate, QStackedWidget, QStyleOptionViewItem, QStyle,
    QScrollArea, QTextBrowser
)
from PySide6.QtCore import (
    Qt, QSize, QThread, Signal, QAbstractTableModel, QModelIndex, QTimer, QUrl,
    QThreadPool, QRunnable, QObject, QRectF, QPoint
)
from PySide6.QtGui import (
    QIcon, QColor, QFont, QDesktopServices, QPainter, QPainterPath,
    QLinearGradient, QPen, QBrush, QPixmap, QFontMetrics
)

from ..ui.assets import get_asset_path, get_asset_html_path
from .creators_tab import (
    CreatorMediaManager, format_short_number, format_relative_time
)
from .media_viewer import PawchiveMediaViewerDialog, extract_media_items_from_post
from .post_detail_view import PostDetailView


SUPPORTED_PLATFORMS = {"patreon", "fanbox", "discord"}


# ─────────────────────────────────────────────────────────────────────────────
#  URL & Media Helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_creator_url(url_str: str):
    """
    Parses a Pawchive / Kemono URL to extract (service, creator_id, post_id).
    Supports formats:
      https://pawchive.pw/patreon/user/12345
      https://pawchive.pw/fanbox/user/12345/post/67890
      https://kemono.su/discord/server/12345
    """
    if not url_str or not isinstance(url_str, str):
        return None, None, None

    url_str = url_str.strip()
    pattern = r'/(patreon|fanbox|discord)/(?:user|server)/([a-zA-Z0-9_\-]+)(?:/post/([a-zA-Z0-9_\-]+))?'
    match = re.search(pattern, url_str, re.IGNORECASE)
    if match:
        svc = match.group(1).lower()
        cid = match.group(2)
        pid = match.group(3) if match.group(3) else None
        return svc, cid, pid
    return None, None, None


def format_published_date(dt_str):
    """Formats an ISO published timestamp to clean date e.g. '8 Nov 2026'."""
    if not dt_str:
        return "Unknown date"
    try:
        clean = str(dt_str).replace("T", " ").split(".")[0]
        dt = datetime.fromisoformat(clean)
        return f"{dt.day} {dt.strftime('%b %Y')}"
    except Exception:
        return str(dt_str)[:10]


def normalize_pawchive_thumbnail_url(path: str, temp_url: str = None) -> str:
    """
    Normalizes archive file path into Pawchive thumbnail CDN URL.
    Rule: Strips leading storage digit from 3-char hex shard prefix (/25f/ -> /5f/).
    """
    if temp_url:
        return temp_url
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path

    trimmed = path.lstrip('/')
    parts = trimmed.split('/', 1)
    first = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if len(first) == 3 and first[0].isdigit() and rest:
        normalized_path = f"{first[1:]}/{rest}"
    else:
        normalized_path = trimmed

    return f"https://img.pawchive.pw/thumbnail/data/{normalized_path}"


def extract_post_thumbnail_url(post: dict) -> str:
    """Finds best image attachment or file path to form a thumbnail preview URL."""
    if not isinstance(post, dict):
        return ""

    # 1. Main file image
    file_info = post.get("file")
    if isinstance(file_info, dict) and file_info.get("path"):
        path = file_info["path"]
        name = file_info.get("name") or path
        ext = os.path.splitext(name)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"):
            return normalize_pawchive_thumbnail_url(path, file_info.get("temp_url"))

    # 2. Attachments
    attachments = post.get("attachments")
    if isinstance(attachments, list):
        for att in attachments:
            if isinstance(att, dict) and att.get("path"):
                path = att["path"]
                name = att.get("name") or path
                ext = os.path.splitext(name)[1].lower()
                if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"):
                    return normalize_pawchive_thumbnail_url(path, att.get("temp_url"))

    # 3. Fallback to main file path even if non-standard
    if isinstance(file_info, dict) and file_info.get("path"):
        return normalize_pawchive_thumbnail_url(file_info["path"], file_info.get("temp_url"))

    # 4. Fallback to any direct media or embedded GIF/image extracted from post
    items = extract_media_items_from_post(post)
    for it in items:
        if it.get("kind") in ("image", "gif") and it.get("thumb_url"):
            return it["thumb_url"]

    return ""


def get_post_media_kinds(post: dict):
    """Returns detected media kinds ('image', 'video', 'audio', 'file') and total file count."""
    items = extract_media_items_from_post(post)
    if not items:
        return [], 0
    kinds = []
    for it in items:
        k = it.get("kind")
        if k == "gif":
            if "image" not in kinds:
                kinds.append("image")
        elif k and k not in kinds:
            kinds.append(k)
    return kinds, len(items)


def get_post_excerpt(post: dict, max_len: int = 150) -> str:
    """Extracts clean text snippet without HTML tags or excess whitespace."""
    content = post.get("substring") or post.get("content") or ""
    if not content:
        return ""
    clean = re.sub(r'<[^>]+>', ' ', content)
    clean = html.unescape(clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    if len(clean) > max_len:
        return clean[:max_len].rstrip() + "…"
    return clean


# ─────────────────────────────────────────────────────────────────────────────
#  Asynchronous Post Thumbnail Manager
# ─────────────────────────────────────────────────────────────────────────────

class _ThumbTaskSignals(QObject):
    loaded = Signal(str, str)  # post_id, disk_path
    failed = Signal(str)       # post_id


class _DownloadThumbTask(QRunnable):
    """Background worker to download a post thumbnail from Pawchive img CDN."""
    def __init__(self, post_id: str, url: str, disk_path: str, signals: _ThumbTaskSignals):
        super().__init__()
        self.post_id = post_id
        self.url = url
        self.disk_path = disk_path
        self.signals = signals

    def run(self):
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
            }
            res = requests.get(self.url, headers=headers, timeout=10)
            if res.status_code == 200 and len(res.content) > 64:
                temp_path = self.disk_path + f".tmp.{os.getpid()}"
                os.makedirs(os.path.dirname(self.disk_path), exist_ok=True)
                with open(temp_path, "wb") as f:
                    f.write(res.content)
                if os.path.exists(self.disk_path):
                    try:
                        os.remove(self.disk_path)
                    except OSError:
                        pass
                os.rename(temp_path, self.disk_path)
                try:
                    self.signals.loaded.emit(self.post_id, self.disk_path)
                except Exception:
                    pass
            else:
                try:
                    self.signals.failed.emit(self.post_id)
                except Exception:
                    pass
        except Exception:
            try:
                self.signals.failed.emit(self.post_id)
            except Exception:
                pass


class PostThumbnailManager(QObject):
    """
    Asynchronous disk & memory cache manager for post thumbnails.
    Caches images in appdata/cache/post_thumbnails/
    """
    thumbnail_loaded = Signal(str)  # post_id

    def __init__(self, base_cache_dir: str, max_concurrent: int = 8, parent=None):
        super().__init__(parent)
        self.cache_dir = os.path.join(base_cache_dir, "post_thumbnails")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._max_capacity = 45             # Bound in-memory cache to visible cards + small buffer
        self._memory_cache = OrderedDict()  # (post_id, w, h) -> QPixmap (LRU order)
        self._in_progress = set()  # post_id
        self._failed_posts = set() # post_id

        self._signals = _ThumbTaskSignals()
        self._signals.loaded.connect(self._on_task_loaded)
        self._signals.failed.connect(self._on_task_failed)

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_concurrent)

    def get_thumbnail(self, post: dict, width: int, height: int) -> QPixmap | None:
        if not post or not isinstance(post, dict):
            return None
        post_id = str(post.get("id") or "")
        if not post_id:
            return None

        cache_key = (post_id, width, height)
        if cache_key in self._memory_cache:
            self._memory_cache.move_to_end(cache_key)
            return self._memory_cache[cache_key]

        url = extract_post_thumbnail_url(post)
        if not url:
            return None

        disk_filename = hashlib.md5(url.encode("utf-8")).hexdigest() + ".jpg"
        disk_path = os.path.join(self.cache_dir, disk_filename)

        if os.path.exists(disk_path):
            pix = QPixmap(disk_path)
            if not pix.isNull():
                scaled_pix = self._crop_and_scale(pix, width, height)
                self._memory_cache[cache_key] = scaled_pix
                self._memory_cache.move_to_end(cache_key)
                while len(self._memory_cache) > self._max_capacity:
                    self._memory_cache.popitem(last=False)
                return scaled_pix
            else:
                try:
                    os.remove(disk_path)
                except OSError:
                    pass

        if post_id in self._failed_posts or post_id in self._in_progress:
            return None

        self._in_progress.add(post_id)
        task = _DownloadThumbTask(post_id, url, disk_path, self._signals)
        self._pool.start(task)
        return None

    def unload_offscreen(self, visible_post_ids: set):
        """
        Evicts in-memory thumbnails for posts that are not currently visible on screen.
        """
        if not visible_post_ids:
            return
        keys_to_remove = [k for k in self._memory_cache.keys() if k[0] not in visible_post_ids]
        for k in keys_to_remove:
            self._memory_cache.pop(k, None)

    def clear_memory_cache(self):
        """Completely flushes all post thumbnails from RAM."""
        self._memory_cache.clear()

    def _crop_and_scale(self, pix: QPixmap, target_w: int, target_h: int) -> QPixmap:
        if pix.isNull() or target_w <= 0 or target_h <= 0:
            return pix
        scaled = pix.scaled(
            target_w, target_h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        )
        x = max(0, (scaled.width() - target_w) // 2)
        y = max(0, (scaled.height() - target_h) // 2)
        return scaled.copy(x, y, target_w, target_h)

    def _on_task_loaded(self, post_id: str, disk_path: str):
        self._in_progress.discard(post_id)
        self.thumbnail_loaded.emit(post_id)

    def _on_task_failed(self, post_id: str):
        self._in_progress.discard(post_id)
        self._failed_posts.add(post_id)


# ─────────────────────────────────────────────────────────────────────────────
#  Post Card Delegate for Grid View
# ─────────────────────────────────────────────────────────────────────────────

class PostCardDelegate(QStyledItemDelegate):
    """
    Renders high quality post cards matching the Pawchive client design:
    - 16:10 preview thumbnail with bottom gradient
    - Overlay badges: attachment icon + count and selection checkmark
    - Platform pill badge (Patreon, Fanbox, Discord) + formatted published date
    - Bold post title (2 lines max, elided)
    - Description excerpt (clean text, 2 lines max)
    - Creator name at the bottom
    """
    def __init__(self, thumb_manager: PostThumbnailManager, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.thumb_manager = thumb_manager
        self.scale = scale

    def sizeHint(self, option, index):
        scale = self.scale
        return QSize(int(238 * scale), int(278 * scale))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        post = index.data(Qt.ItemDataRole.UserRole)
        if not post or not isinstance(post, dict):
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        scale = self.scale
        r = QRectF(option.rect).adjusted(int(4 * scale), int(4 * scale), int(-4 * scale), int(-4 * scale))
        card_w = r.width()
        card_h = r.height()
        card_r = 10.0 * scale

        pid = str(post.get("id") or "")
        model = index.model()
        is_selected = pid in getattr(model, "selected_post_ids", set())
        is_hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # ── 1. Base Card Container ──────────────────────────────────────────
        bg_col = QColor("#181A20")
        if is_selected:
            border_col = QColor("#E5A198")
            border_w = 2.0
        elif is_hovered:
            border_col = QColor("#464B59")
            border_w = 1.2
        else:
            border_col = QColor("#2A2D36")
            border_w = 1.0

        painter.setPen(QPen(border_col, border_w))
        painter.setBrush(QBrush(bg_col))
        painter.drawRoundedRect(r, card_r, card_r)

        # ── 2. Top Thumbnail (16:10 aspect ratio) ───────────────────────────
        thumb_h = int(card_w * 10 / 16)
        thumb_rect = QRectF(r.x(), r.y(), card_w, thumb_h)

        card_clip = QPainterPath()
        card_clip.addRoundedRect(r, card_r, card_r)
        painter.save()
        painter.setClipPath(card_clip)

        thumb_pix = None
        if self.thumb_manager:
            thumb_pix = self.thumb_manager.get_thumbnail(post, int(card_w), thumb_h)

        if thumb_pix and not thumb_pix.isNull():
            painter.drawPixmap(int(r.x()), int(r.y()), thumb_pix)
        else:
            # Fallback sleek dark gradient
            t_grad = QLinearGradient(r.x(), r.y(), r.right(), r.y() + thumb_h)
            t_grad.setColorAt(0.0, QColor("#282B36"))
            t_grad.setColorAt(1.0, QColor("#1C1E26"))
            painter.fillRect(thumb_rect, QBrush(t_grad))

            # Subtle placeholder icon
            kinds, _ = get_post_media_kinds(post)
            svg_file = "assets/uisvg/video.svg" if "video" in kinds else ("assets/Svg/audio.svg" if "audio" in kinds else "assets/uisvg/image.svg")
            p_icon = QIcon(get_asset_path(svg_file)).pixmap(int(36 * scale), int(36 * scale))
            icon_x = int(thumb_rect.x() + (thumb_rect.width() - p_icon.width()) / 2)
            icon_y = int(thumb_rect.y() + (thumb_rect.height() - p_icon.height()) / 2)
            painter.drawPixmap(icon_x, icon_y, p_icon)

        # Bottom gradient overlay on thumbnail
        fade_h = int(48 * scale)
        fade_y = r.y() + thumb_h - fade_h
        f_grad = QLinearGradient(r.x(), fade_y, r.x(), r.y() + thumb_h)
        f_grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        f_grad.setColorAt(1.0, QColor(15, 16, 21, 220))
        painter.fillRect(QRectF(r.x(), fade_y, card_w, fade_h + 1), QBrush(f_grad))

        painter.restore()

        # ── 3. Overlays on Thumbnail ────────────────────────────────────────
        # Bottom-left: Media kind + count badge
        kinds, att_count = get_post_media_kinds(post)
        if att_count > 0 or kinds:
            svg_file = "assets/uisvg/video.svg" if "video" in kinds else ("assets/Svg/audio.svg" if "audio" in kinds else "assets/uisvg/image.svg")
            icon_sz = int(12 * scale)
            b_icon = QIcon(get_asset_path(svg_file)).pixmap(icon_sz, icon_sz)
            badge_text = str(att_count) if att_count > 0 else ""
            badge_font = QFont("Segoe UI", int(7.5 * scale), QFont.Weight.Bold)
            fm = QFontMetrics(badge_font)
            text_w = fm.horizontalAdvance(badge_text) if badge_text else 0
            badge_w = icon_sz + (text_w + int(6 * scale) if badge_text else 0) + int(8 * scale)
            badge_h = int(18 * scale)
            b_rect = QRectF(r.x() + int(8 * scale), r.y() + thumb_h - badge_h - int(7 * scale), badge_w, badge_h)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(0, 0, 0, 190)))
            painter.drawRoundedRect(b_rect, 4 * scale, 4 * scale)

            icon_x = int(b_rect.x() + int(4 * scale))
            icon_y = int(b_rect.y() + (badge_h - icon_sz) / 2)
            painter.drawPixmap(icon_x, icon_y, b_icon)

            if badge_text:
                painter.setPen(QColor("#DFE1E5"))
                painter.setFont(badge_font)
                text_rect = QRectF(icon_x + icon_sz + int(4 * scale), b_rect.y(), text_w + int(4 * scale), badge_h)
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, badge_text)

        # Top-right: Selection indicator
        sz = int(22 * scale)
        chk_rect = QRectF(r.right() - sz - int(8 * scale), r.y() + int(8 * scale), sz, sz)
        if is_selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor("#E5A198")))
            painter.drawRoundedRect(chk_rect, sz / 2, sz / 2)
            chk_sz = int(13 * scale)
            chk_icon = QIcon(get_asset_path("assets/uisvg/check.svg")).pixmap(chk_sz, chk_sz)
            chk_x = int(chk_rect.x() + (sz - chk_sz) / 2)
            chk_y = int(chk_rect.y() + (sz - chk_sz) / 2)
            painter.drawPixmap(chk_x, chk_y, chk_icon)
        elif is_hovered:
            painter.setPen(QPen(QColor(255, 255, 255, 120), 1.2))
            painter.setBrush(QBrush(QColor(0, 0, 0, 100)))
            painter.drawRoundedRect(chk_rect, sz / 2, sz / 2)

        # ── 4. Lower Card Body ──────────────────────────────────────────────
        content_x = r.x() + int(10 * scale)
        content_w = card_w - int(20 * scale)
        curr_y = r.y() + thumb_h + int(8 * scale)

        # Row 1: Platform pill + published date
        svc = (post.get("_creator_service") or post.get("service") or "patreon").lower().strip()
        svc_label = "Patreon"
        pill_bg = QColor(50, 32, 32)
        pill_border = QColor(104, 53, 53)
        pill_text = QColor("#E5A198")

        if svc == "fanbox":
            svc_label = "Fanbox"
            pill_bg = QColor(28, 46, 61)
            pill_border = QColor(43, 88, 125)
            pill_text = QColor("#82B8FF")
        elif svc == "discord":
            svc_label = "Discord"
            pill_bg = QColor(42, 38, 64)
            pill_border = QColor(78, 66, 117)
            pill_text = QColor("#A99AF0")

        pill_font = QFont("Segoe UI", int(7.5 * scale), QFont.Weight.Bold)
        fm_pill = QFontMetrics(pill_font)
        pill_w = fm_pill.horizontalAdvance(svc_label) + int(10 * scale)
        pill_h = int(16 * scale)
        pill_rect = QRectF(content_x, curr_y, pill_w, pill_h)

        painter.setPen(QPen(pill_border, 1.0))
        painter.setBrush(QBrush(pill_bg))
        painter.drawRoundedRect(pill_rect, 4 * scale, 4 * scale)

        painter.setPen(pill_text)
        painter.setFont(pill_font)
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, svc_label)

        # Formatted Date
        date_str = format_published_date(post.get("published") or post.get("added"))
        date_rect = QRectF(pill_rect.right() + int(6 * scale), curr_y, content_w - pill_w - int(6 * scale), pill_h)
        painter.setPen(QColor("#8C929E"))
        painter.setFont(QFont("Segoe UI", int(8 * scale)))
        painter.drawText(date_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, date_str)

        curr_y += pill_h + int(5 * scale)

        # Row 2: Post Title (1-2 lines, bold)
        title = post.get("title") or "Untitled post"
        title_font = QFont("Segoe UI", int(9.5 * scale), QFont.Weight.Bold)
        painter.setFont(title_font)
        painter.setPen(QColor("#F5F6F8"))
        title_h = int(32 * scale)
        title_rect = QRectF(content_x, curr_y, content_w, title_h)
        painter.drawText(title_rect, Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, title)

        curr_y += title_h + int(2 * scale)

        # Row 3: Description Excerpt (clean text, 1-2 lines)
        excerpt = get_post_excerpt(post, max_len=140)
        if excerpt:
            exc_font = QFont("Segoe UI", int(8 * scale))
            painter.setFont(exc_font)
            painter.setPen(QColor("#8C929E"))
            exc_h = int(26 * scale)
            exc_rect = QRectF(content_x, curr_y, content_w, exc_h)
            painter.drawText(exc_rect, Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, excerpt)

        # Row 4: Creator name at the bottom
        cname = post.get("_creator_name") or "Creator"
        c_font = QFont("Segoe UI", int(7.5 * scale))
        painter.setFont(c_font)
        painter.setPen(QColor("#6B7280"))
        c_rect = QRectF(content_x, r.bottom() - int(18 * scale), content_w, int(15 * scale))
        painter.drawText(c_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, cname)

        painter.restore()


# ─────────────────────────────────────────────────────────────────────────────
#  Creator Profile Hero Header Widget
# ─────────────────────────────────────────────────────────────────────────────

class CreatorAvatarWidget(QLabel):
    """Square rounded avatar with dark solid ring border."""
    def __init__(self, size: int = 90, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.scale = scale
        self.sz = int(size * scale)
        self.setFixedSize(self.sz, self.sz)
        self.pix = None
        self.initials = ""

    def set_avatar_pixmap(self, pix: QPixmap | None, initials: str = ""):
        self.pix = pix
        self.initials = initials
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        scale = self.scale
        r = QRectF(1.5 * scale, 1.5 * scale, self.width() - 3.0 * scale, self.height() - 3.0 * scale)
        radius = 12.0 * scale

        clip = QPainterPath()
        clip.addRoundedRect(r, radius, radius)
        painter.save()
        painter.setClipPath(clip)

        if self.pix and not self.pix.isNull():
            painter.drawPixmap(int(r.x()), int(r.y()), self.pix)
        else:
            painter.fillRect(r, QColor("#2E2225"))
            painter.setPen(QColor("#E5A198"))
            font = QFont("Segoe UI", int(17 * scale), QFont.Weight.Bold)
            painter.setFont(font)
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, self.initials)

        painter.restore()

        # Dark thick ring border
        painter.setPen(QPen(QColor("#1A1C23"), 3.5 * scale))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(r, radius, radius)
        painter.end()


class CreatorProfileHeroWidget(QFrame):
    """
    Wide hero header matching the Pawchive client profile page:
    - Banner background with bottom gradient fade
    - Overlapping square avatar with dark border ring
    - Large bold creator name, service pill, handle/ID, archive favorites count
    - Right-side actions: Favorite button, Send to Downloader, Updated relative time
    - Sub-tabs: Posts (active with warm accent underline), About & links, Announcements
    """
    favorite_clicked = Signal()
    send_to_downloader_clicked = Signal()
    about_clicked = Signal()

    def __init__(self, media_manager: CreatorMediaManager, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.media_manager = media_manager
        self.scale = scale
        self.creator = None
        self.total_creators_count = 1
        self.is_favorite = False

        self.setObjectName("CreatorProfileHero")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._build_ui()

    def _build_ui(self):
        scale = self.scale
        self.banner_height = int(140 * scale)
        self.setFixedHeight(int(220 * scale))

        root_v = QVBoxLayout(self)
        root_v.setContentsMargins(int(16 * scale), 0, int(16 * scale), int(8 * scale))
        root_v.setSpacing(0)

        # Top spacer pushing the content down so avatar overlaps banner
        top_spacer = int(75 * scale)
        root_v.addSpacing(top_spacer)

        # Profile Row: Avatar + Info + Right Actions
        profile_row = QHBoxLayout()
        profile_row.setSpacing(int(14 * scale))
        profile_row.setAlignment(Qt.AlignmentFlag.AlignBottom)

        # 1. Avatar
        self.avatar_widget = CreatorAvatarWidget(size=86, scale=scale, parent=self)
        profile_row.addWidget(self.avatar_widget)

        # 2. Info Column (Name, Badges)
        info_col = QVBoxLayout()
        info_col.setSpacing(int(3 * scale))
        info_col.setAlignment(Qt.AlignmentFlag.AlignBottom)

        self.name_label = QLabel("Creator Name")
        self.name_label.setStyleSheet(
            f"font-size: {int(16 * scale)}pt; font-weight: bold; color: #FFFFFF;"
        )
        info_col.addWidget(self.name_label)

        badges_h = QHBoxLayout()
        badges_h.setSpacing(int(8 * scale))
        badges_h.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.service_badge = QLabel("Patreon")
        self.service_badge.setStyleSheet(
            "background-color: #352020; color: #E5A198; border: 1px solid #683535; "
            f"border-radius: {int(4 * scale)}px; padding: {int(2 * scale)}px {int(8 * scale)}px; font-weight: bold; font-size: {int(8 * scale)}pt;"
        )
        badges_h.addWidget(self.service_badge)

        self.handle_label = QLabel("@creator")
        self.handle_label.setStyleSheet(f"color: #8C929E; font-size: {int(8.5 * scale)}pt;")
        badges_h.addWidget(self.handle_label)

        self.favorites_label = QLabel(f"<img src='{get_asset_html_path('assets/Svg/star.svg')}' width='11' height='11'> &nbsp;0 archive favourites")
        self.favorites_label.setStyleSheet(f"color: #8C929E; font-size: {int(8.5 * scale)}pt;")
        badges_h.addWidget(self.favorites_label)

        badges_h.addStretch(1)
        info_col.addLayout(badges_h)
        profile_row.addLayout(info_col, 1)

        # 3. Action Buttons on Right
        actions_v = QVBoxLayout()
        actions_v.setSpacing(int(4 * scale))
        actions_v.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        actions_h = QHBoxLayout()
        actions_h.setSpacing(int(8 * scale))

        self.fav_btn = QPushButton("  Favorite")
        self.fav_btn.setIcon(QIcon(get_asset_path("assets/Svg/star.svg")))
        self.fav_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.fav_btn.setFixedHeight(int(28 * scale))
        self.fav_btn.setStyleSheet(
            "QPushButton { background-color: #272A32; color: #DFE1E5; border: 1px solid #383D48; border-radius: 6px; padding: 0 12px; font-weight: bold; font-size: 8.5pt; }"
            "QPushButton:hover { background-color: #323640; border-color: #E5A198; color: #E5A198; }"
        )
        self.fav_btn.clicked.connect(self._toggle_favorite)
        actions_h.addWidget(self.fav_btn)

        self.send_to_dl_btn = QPushButton("  Send to Downloader")
        self.send_to_dl_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.send_to_dl_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.send_to_dl_btn.setFixedHeight(int(28 * scale))
        self.send_to_dl_btn.setStyleSheet(
            "QPushButton { background-color: #272A32; color: #DFE1E5; border: 1px solid #383D48; border-radius: 6px; padding: 0 12px; font-weight: bold; font-size: 8.5pt; }"
            "QPushButton:hover { background-color: #323640; border-color: #E5A198; color: #E5A198; }"
        )
        self.send_to_dl_btn.clicked.connect(self.send_to_downloader_clicked.emit)
        actions_h.addWidget(self.send_to_dl_btn)

        actions_v.addLayout(actions_h)

        self.updated_label = QLabel("Updated recently")
        self.updated_label.setStyleSheet(f"color: #8C929E; font-size: {int(8 * scale)}pt;")
        self.updated_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        actions_v.addWidget(self.updated_label)

        profile_row.addLayout(actions_v)
        root_v.addLayout(profile_row)

        root_v.addSpacing(int(10 * scale))

        # 4. Sub-tabs bar at bottom of hero
        tabs_h = QHBoxLayout()
        tabs_h.setSpacing(int(14 * scale))
        tabs_h.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.tab_posts = QLabel("Posts")
        self.tab_posts.setStyleSheet(
            "color: #FFFFFF; font-weight: bold; font-size: 9.5pt; "
            "border-bottom: 2px solid #E5A198; padding-bottom: 4px;"
        )
        tabs_h.addWidget(self.tab_posts)

        self.tab_about_btn = QPushButton("About & links")
        self.tab_about_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #8C929E; font-size: 9pt; padding-bottom: 4px; }"
            "QPushButton:hover { color: #FFFFFF; }"
        )
        self.tab_about_btn.clicked.connect(self.about_clicked.emit)
        tabs_h.addWidget(self.tab_about_btn)

        self.tab_announcements_btn = QPushButton("Announcements")
        self.tab_announcements_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #8C929E; font-size: 9pt; padding-bottom: 4px; }"
            "QPushButton:hover { color: #FFFFFF; }"
        )
        self.tab_announcements_btn.clicked.connect(self.about_clicked.emit)
        tabs_h.addWidget(self.tab_announcements_btn)

        tabs_h.addStretch(1)
        root_v.addLayout(tabs_h)

    def set_creator(self, creator: dict, total_count: int = 1):
        self.creator = creator
        self.total_creators_count = total_count

        if not creator:
            self.name_label.setText("No creator selected")
            self.service_badge.setText("Patreon")
            self.handle_label.setText("")
            self.favorites_label.setText("")
            self.updated_label.setText("")
            self.avatar_widget.set_avatar_pixmap(None, "")
            self.update()
            return

        name = creator.get("name") or "Creator"
        if total_count > 1:
            self.name_label.setText(f"{name} (+{total_count - 1} more)")
        else:
            self.name_label.setText(name)

        svc = (creator.get("service") or "patreon").lower().strip()
        svc_label = svc.capitalize()
        if svc == "fanbox":
            svc_label = "Pixiv Fanbox"
            self.service_badge.setStyleSheet(
                "background-color: #1C2E3D; color: #82B8FF; border: 1px solid #2B587D; "
                f"border-radius: {int(4 * self.scale)}px; padding: {int(2 * self.scale)}px {int(8 * self.scale)}px; font-weight: bold; font-size: {int(8 * self.scale)}pt;"
            )
        elif svc == "discord":
            svc_label = "Discord"
            self.service_badge.setStyleSheet(
                "background-color: #2A2640; color: #A99AF0; border: 1px solid #4E4275; "
                f"border-radius: {int(4 * self.scale)}px; padding: {int(2 * self.scale)}px {int(8 * self.scale)}px; font-weight: bold; font-size: {int(8 * self.scale)}pt;"
            )
        else:
            svc_label = "Patreon"
            self.service_badge.setStyleSheet(
                "background-color: #352020; color: #E5A198; border: 1px solid #683535; "
                f"border-radius: {int(4 * self.scale)}px; padding: {int(2 * self.scale)}px {int(8 * self.scale)}px; font-weight: bold; font-size: {int(8 * self.scale)}pt;"
            )
        self.service_badge.setText(svc_label)

        cid = str(creator.get("id") or "")
        public_id = creator.get("public_id")
        self.handle_label.setText(f"@{public_id}" if public_id else f"ID: {cid}")

        fav = creator.get("favorited", 0)
        fav_str = format_short_number(fav)
        self.favorites_label.setText(f"<img src='{get_asset_html_path('assets/Svg/star.svg')}' width='11' height='11'> &nbsp;{fav_str} archive favourites")

        upd = creator.get("updated")
        if upd:
            rel = format_relative_time(upd)
            self.updated_label.setText(f"<img src='{get_asset_html_path('assets/uisvg/clock.svg')}' width='11' height='11'> &nbsp;Updated {rel}")
        else:
            self.updated_label.setText("")

        # Fetch Avatar
        initials = "".join([part[0].upper() for part in name.split() if part][:2]) or name[:2].upper()
        avatar_pix = None
        if self.media_manager and cid:
            avatar_pix = self.media_manager.get_avatar(svc, cid, self.avatar_widget.sz, radius=12)
        self.avatar_widget.set_avatar_pixmap(avatar_pix, initials)

        fav_mgr = getattr(self, "fav_manager", None) or getattr(getattr(self.parent(), "parent_app", None), "favorites_manager", None)
        if fav_mgr and svc and cid:
            self.is_favorite = fav_mgr.is_favorite(svc, cid)
        self._update_favorite_button()

        self.update()

    def _toggle_favorite(self):
        fav_mgr = getattr(self, "fav_manager", None) or getattr(getattr(self.parent(), "parent_app", None), "favorites_manager", None)
        if fav_mgr and self.creator:
            self.is_favorite = fav_mgr.toggle(self.creator)
        else:
            self.is_favorite = not self.is_favorite
        self._update_favorite_button()
        self.favorite_clicked.emit()

    def _update_favorite_button(self):
        icon_path = "assets/Svg/star.svg" if self.is_favorite else "assets/uisvg/heart.svg"
        self.fav_btn.setIcon(QIcon(get_asset_path(icon_path)))
        self.fav_btn.setIconSize(QSize(int(13 * self.scale), int(13 * self.scale)))
        if self.is_favorite:
            self.fav_btn.setText("  Favorited")
            self.fav_btn.setStyleSheet(
                "QPushButton { background-color: #352618; color: #F59E0B; border: 1px solid #F59E0B; border-radius: 6px; padding: 0 12px; font-weight: bold; font-size: 8.5pt; }"
            )
        else:
            self.fav_btn.setText("  Favorite")
            self.fav_btn.setStyleSheet(
                "QPushButton { background-color: #272A32; color: #DFE1E5; border: 1px solid #383D48; border-radius: 6px; padding: 0 12px; font-weight: bold; font-size: 8.5pt; }"
                "QPushButton:hover { background-color: #323640; border-color: #E5A198; color: #E5A198; }"
            )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        scale = self.scale
        r = QRectF(0, 0, self.width(), self.height())
        card_r = 12.0 * scale

        # Base Card Container
        painter.setPen(QPen(QColor("#2C2F38"), 1.0))
        painter.setBrush(QBrush(QColor("#1A1C23")))
        painter.drawRoundedRect(r, card_r, card_r)

        # Top Banner
        banner_h = self.banner_height
        banner_rect = QRectF(r.x(), r.y(), r.width(), banner_h)

        clip = QPainterPath()
        clip.addRoundedRect(r, card_r, card_r)
        painter.save()
        painter.setClipPath(clip)

        banner_pix = None
        if self.creator and self.media_manager:
            svc = (self.creator.get("service") or "patreon").lower().strip()
            cid = str(self.creator.get("id") or "").strip()
            if cid:
                banner_pix = self.media_manager.get_banner(svc, cid, int(r.width()), banner_h)

        if banner_pix and not banner_pix.isNull():
            painter.drawPixmap(int(r.x()), int(r.y()), banner_pix)
        else:
            # Subtle dark gradient banner
            b_grad = QLinearGradient(r.x(), r.y(), r.right(), banner_h)
            b_grad.setColorAt(0.0, QColor("#362528"))
            b_grad.setColorAt(0.5, QColor("#28222A"))
            b_grad.setColorAt(1.0, QColor("#1A1C23"))
            painter.fillRect(banner_rect, QBrush(b_grad))

        # Bottom gradient fade (transparent at top -> card background at bottom)
        fade_h = int(60 * scale)
        fade_y = banner_h - fade_h
        f_grad = QLinearGradient(r.x(), fade_y, r.x(), banner_h)
        f_grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        f_grad.setColorAt(1.0, QColor(26, 28, 35, 255))
        painter.fillRect(QRectF(r.x(), fade_y, r.width(), fade_h + 1), QBrush(f_grad))

        painter.restore()
        painter.end()


# ─────────────────────────────────────────────────────────────────────────────
#  Creator About & Links Modal Dialog
# ─────────────────────────────────────────────────────────────────────────────

class CreatorAboutDialog(QDialog):
    """Clean modal dialog displaying creator description, platform ID and external links."""
    def __init__(self, creator: dict, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.scale = scale
        self.creator = creator or {}
        name = self.creator.get("name") or "Creator"
        self.setWindowTitle(f"About {name} — Pawchive")
        self.resize(int(480 * scale), int(340 * scale))
        self._build_ui()

    def _build_ui(self):
        scale = self.scale
        layout = QVBoxLayout(self)
        layout.setContentsMargins(int(16 * scale), int(16 * scale), int(16 * scale), int(14 * scale))
        layout.setSpacing(int(10 * scale))

        name = self.creator.get("name") or "Creator"
        svc = (self.creator.get("service") or "patreon").capitalize()
        cid = str(self.creator.get("id") or "")
        url_type = "server" if svc.lower() == "discord" else "user"
        pawchive_url = f"https://pawchive.pw/{svc.lower()}/{url_type}/{cid}"

        title_lbl = QLabel(f"<b>{name}</b>")
        title_lbl.setStyleSheet(f"font-size: {int(14 * scale)}pt; color: #FFFFFF;")
        layout.addWidget(title_lbl)

        txt_view = QTextBrowser()
        txt_view.setOpenExternalLinks(True)
        txt_view.setStyleSheet("background-color: #20232A; border: 1px solid #333742; border-radius: 6px; padding: 8px; color: #DFE1E5;")
        fav = self.creator.get("favorited", 0)
        fav_str = format_short_number(fav)
        html_content = f"""
        <p><b>Platform:</b> {svc}</p>
        <p><b>Creator ID:</b> <code>{cid}</code></p>
        <p><b>Archive Favorites:</b> <img src='{get_asset_html_path("assets/Svg/star.svg")}' width='12' height='12'> {fav_str}</p>
        <p><b>Pawchive Archive URL:</b><br><a style='color: #E5A198;' href='{pawchive_url}'>{pawchive_url}</a></p>
        """
        txt_view.setHtml(html_content)
        layout.addWidget(txt_view, 1)

        btn_box = QHBoxLayout()
        btn_box.setSpacing(int(8 * scale))

        cp_btn = QPushButton("Copy Archive URL")
        cp_btn.clicked.connect(lambda: QApplication.clipboard().setText(pawchive_url))
        btn_box.addWidget(cp_btn)

        op_btn = QPushButton("Open in Browser")
        op_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(pawchive_url)))
        btn_box.addWidget(op_btn)

        btn_box.addStretch(1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_box.addWidget(close_btn)

        layout.addLayout(btn_box)


# ─────────────────────────────────────────────────────────────────────────────
#  Background Worker Thread: Fetch Posts from Multiple Creators
# ─────────────────────────────────────────────────────────────────────────────

class FetchMultipleCreatorPostsThread(QThread):
    """
    Background worker that fetches posts for one or multiple creators from Pawchive API.
    Supports full-catalog keyword searching with auto-pagination and graceful cancellation.
    Endpoint: https://pawchive.pw/api/v1/{service}/{url_type}/{creator_id}?q={query}&o={offset}
    """
    finished_signal = Signal(bool, str, list)   # success, message, merged_posts
    status_signal = Signal(str)
    progress_signal = Signal(int, int)          # current_idx, total_creators

    def __init__(self, creators: list, offset: int = 0, query: str = "", fetch_all: bool = False, max_pages: int = 15):
        super().__init__()
        self.creators = list(creators) if isinstance(creators, list) else [creators]
        self.offset = offset
        self.query = (query or "").strip()
        self.fetch_all = fetch_all or bool(self.query)
        self.max_pages = max_pages
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        if not self.creators:
            self.finished_signal.emit(False, "No creators specified to fetch.", [])
            return

        all_posts = []
        errors = []
        total = len(self.creators)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json"
        }

        try:
            import cloudscraper
            scraper = cloudscraper.create_scraper()
        except Exception:
            import requests
            scraper = requests

        for idx, creator in enumerate(self.creators, 1):
            if self._is_cancelled:
                break

            if not isinstance(creator, dict):
                continue

            svc = (creator.get("service") or "patreon").lower().strip()
            cid = str(creator.get("id") or "").strip()
            cname = creator.get("name") or cid or "Creator"

            if not cid:
                continue

            url_type = "server" if svc == "discord" else "user"
            api_url = f"https://pawchive.pw/api/v1/{svc}/{url_type}/{cid}"

            # When searching with a query and fetch_all is enabled, paginate through
            # all matching pages on Pawchive so user gets complete catalog results
            if self.query and self.fetch_all:
                page_offset = 0
                page_count = 0
                while not self._is_cancelled and page_count < self.max_pages:
                    params = {"q": self.query}
                    if page_offset > 0:
                        params["o"] = str(page_offset)

                    if total > 1:
                        self.status_signal.emit(
                            f"Searching '{cname}' for '{self.query}' (found {len(all_posts)} posts)..."
                        )
                    else:
                        self.status_signal.emit(
                            f"Searching all posts for '{self.query}' (found {len(all_posts)})..."
                        )

                    try:
                        res = scraper.get(api_url, headers=headers, params=params, timeout=20)
                        if res.status_code == 200:
                            data = res.json()
                            if isinstance(data, list):
                                for post in data:
                                    if isinstance(post, dict):
                                        post["_creator_name"] = cname
                                        post["_creator_service"] = svc
                                        post["_creator_id"] = cid
                                        all_posts.append(post)
                                if len(data) < 50:
                                    break
                                page_offset += 50
                                page_count += 1
                            else:
                                break
                        elif res.status_code == 404:
                            errors.append(f"{cname}: Creator not found (404)")
                            break
                        else:
                            break
                    except Exception as e:
                        errors.append(f"{cname}: {str(e)[:50]}")
                        break
            else:
                params = {}
                if self.query:
                    params["q"] = self.query
                if self.offset > 0:
                    params["o"] = str(self.offset)

                if total > 1:
                    self.status_signal.emit(f"Loading posts for '{cname}' ({idx}/{total})...")
                else:
                    self.status_signal.emit(f"Connecting to Pawchive API for '{cname}' (offset: {self.offset})...")
                self.progress_signal.emit(idx, total)

                try:
                    res = scraper.get(api_url, headers=headers, params=params, timeout=25)
                    if res.status_code == 200:
                        data = res.json()
                        if isinstance(data, list):
                            for post in data:
                                if isinstance(post, dict):
                                    post["_creator_name"] = cname
                                    post["_creator_service"] = svc
                                    post["_creator_id"] = cid
                                    all_posts.append(post)
                        else:
                            errors.append(f"{cname}: Unexpected response format")
                    elif res.status_code == 404:
                        errors.append(f"{cname}: Creator not found (404)")
                    else:
                        errors.append(f"{cname}: HTTP {res.status_code}")
                except Exception as e:
                    errors.append(f"{cname}: {str(e)[:50]}")

        if self._is_cancelled:
            self.finished_signal.emit(False, "Search cancelled.", [])
            return

        # Sort combined posts chronologically by published / added date descending
        def _get_sort_key(p):
            pub = p.get("published") or p.get("added") or ""
            return str(pub)

        all_posts.sort(key=_get_sort_key, reverse=True)

        if all_posts or not errors:
            if self.query:
                msg = f"Found {len(all_posts)} post(s) matching '{self.query}' across {total} creator(s)"
            elif total > 1:
                msg = f"Loaded {len(all_posts)} posts across {total} creator(s)"
            else:
                cname = self.creators[0].get("name", "Creator") if self.creators else "Creator"
                msg = f"Loaded {len(all_posts)} post(s) for '{cname}'"
            if errors:
                msg += f" ({len(errors)} creator(s) reported issues)"
            self.finished_signal.emit(True, msg, all_posts)
        else:
            self.finished_signal.emit(False, "; ".join(errors[:3]), [])


class FetchCreatorPostsThread(FetchMultipleCreatorPostsThread):
    """Backwards-compatible wrapper for single-creator fetching."""
    def __init__(self, service: str, creator_id: str, offset: int = 0):
        super().__init__([{"service": service, "id": creator_id}], offset=offset)


# ─────────────────────────────────────────────────────────────────────────────
#  Posts Table Model
# ─────────────────────────────────────────────────────────────────────────────

class PostsTableModel(QAbstractTableModel):
    """
    Virtualised table model for displaying creator posts with checkbox selection.
    Columns:
      0: Selection Checkbox
      1: Creator
      2: Post Title
      3: Published Date
      4: Files / Attachments
      5: Post ID
    """
    HEADERS = ["Select", "Creator", "Post Title", "Published Date", "Media / Attachments", "Post ID"]

    def __init__(self, posts=None, on_selection_changed=None):
        super().__init__()
        self._all_posts = posts or []
        self._filtered_posts = list(self._all_posts)
        self.selected_post_ids = set()
        self.on_selection_changed = on_selection_changed

    def set_posts(self, posts, append=False):
        self.beginResetModel()
        if append:
            existing_ids = {p.get("id") for p in self._all_posts if p.get("id")}
            new_items = [p for p in posts if p.get("id") not in existing_ids]
            self._all_posts.extend(new_items)
        else:
            self._all_posts = list(posts)
            self.selected_post_ids.clear()
        self._filtered_posts = list(self._all_posts)
        self.endResetModel()
        if self.on_selection_changed:
            self.on_selection_changed()

    def filter_posts(self, query=""):
        self.beginResetModel()
        query = query.strip().lower()
        if not query:
            self._filtered_posts = list(self._all_posts)
        else:
            self._filtered_posts = [
                p for p in self._all_posts
                if query in (p.get("title") or "").lower()
                or query in str(p.get("id") or "").lower()
                or query in str(p.get("_creator_name") or "").lower()
                or query in str(p.get("_creator_service") or "").lower()
                or query in (p.get("content") or "").lower()
            ]
        self.endResetModel()

    def select_all(self, select=True):
        self.beginResetModel()
        if select:
            for p in self._filtered_posts:
                pid = str(p.get("id"))
                if pid:
                    self.selected_post_ids.add(pid)
        else:
            for p in self._filtered_posts:
                pid = str(p.get("id"))
                self.selected_post_ids.discard(pid)
        self.endResetModel()
        if self.on_selection_changed:
            self.on_selection_changed()

    def invert_selection(self):
        self.beginResetModel()
        for p in self._filtered_posts:
            pid = str(p.get("id"))
            if not pid:
                continue
            if pid in self.selected_post_ids:
                self.selected_post_ids.discard(pid)
            else:
                self.selected_post_ids.add(pid)
        self.endResetModel()
        if self.on_selection_changed:
            self.on_selection_changed()

    def get_selected_posts(self):
        """Returns list of full post dicts that are currently checked."""
        id_set = self.selected_post_ids
        return [p for p in self._all_posts if str(p.get("id")) in id_set]

    def get_post_at(self, row):
        if 0 <= row < len(self._filtered_posts):
            return self._filtered_posts[row]
        return None

    def rowCount(self, parent=QModelIndex()):
        return len(self._filtered_posts)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()
        post = self._filtered_posts[row]
        pid = str(post.get("id") or "")

        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return Qt.CheckState.Checked if pid in self.selected_post_ids else Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return ""
            elif col == 1:
                # Creator name & platform
                cname = post.get("_creator_name") or "Unknown"
                svc = (post.get("_creator_service") or "").capitalize()
                return f"{cname} [{svc}]" if svc else cname
            elif col == 2:
                title = post.get("title")
                if not title:
                    content = post.get("content") or ""
                    clean_content = re.sub(r'<[^>]+>', '', content).strip()
                    title = clean_content[:80] if clean_content else f"Post #{pid}"
                return title.strip()
            elif col == 3:
                pub = post.get("published") or post.get("added") or ""
                if pub:
                    try:
                        clean_pub = pub.replace("T", " ").split(".")[0]
                        return clean_pub[:16]
                    except Exception:
                        return str(pub)[:16]
                return "Unknown date"
            elif col == 4:
                items = post.get("_cached_media_items")
                if items is None:
                    items = extract_media_items_from_post(post)
                    post["_cached_media_items"] = items

                if not items:
                    return "Text / Content only"

                count = len(items)
                gif_count = sum(1 for it in items if it.get("kind") == "gif")
                vid_count = sum(1 for it in items if it.get("kind") == "video")
                img_count = sum(1 for it in items if it.get("kind") == "image")
                other_count = count - (gif_count + vid_count + img_count)

                badges = []
                if gif_count:
                    badges.append(f"{gif_count} GIF{'s' if gif_count > 1 else ''}")
                if vid_count:
                    badges.append(f"{vid_count} Video{'s' if vid_count > 1 else ''}")
                if img_count:
                    badges.append(f"{img_count} Image{'s' if img_count > 1 else ''}")
                if other_count:
                    badges.append(f"{other_count} File{'s' if other_count > 1 else ''}")

                if badges:
                    return f"{count} item{'s' if count != 1 else ''} ({' · '.join(badges)})"
                return f"{count} item{'s' if count != 1 else ''}"
            elif col == 5:
                return pid

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (0, 3, 5):
                return Qt.AlignmentFlag.AlignCenter
            return Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 1:
                return QColor("#F5B0A8")  # Creator name in soft warm coral
            elif col == 2:
                return QColor("#DFE1E5")  # Post title
            elif col == 3:
                return QColor("#8C929E")  # Published date
            elif col == 4:
                return QColor("#E5A198")  # Media count
            elif col == 5:
                return QColor("#6B7280")  # Post ID

        if role == Qt.ItemDataRole.ToolTipRole:
            if col == 1:
                cname = post.get("_creator_name") or "Unknown"
                svc = (post.get("_creator_service") or "").capitalize()
                cid = post.get("_creator_id") or ""
                return f"Creator: {cname}\nPlatform: {svc}\nCreator ID: {cid}"
            elif col == 2:
                return post.get("title") or ""

        if role == Qt.ItemDataRole.UserRole:
            return post

        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or index.column() != 0:
            return False

        if role == Qt.ItemDataRole.CheckStateRole:
            post = self._filtered_posts[index.row()]
            pid = str(post.get("id") or "")
            if pid:
                if value == Qt.CheckState.Checked or value == 2:
                    self.selected_post_ids.add(pid)
                else:
                    self.selected_post_ids.discard(pid)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
                if self.on_selection_changed:
                    self.on_selection_changed()
                return True
        return False

    def flags(self, index):
        default_flags = super().flags(index)
        if index.column() == 0:
            return default_flags | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
        return default_flags | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


# ─────────────────────────────────────────────────────────────────────────────
#  Creator Quick-Picker Modal Dialog
# ─────────────────────────────────────────────────────────────────────────────

class CreatorPickerDialog(QDialog):
    """
    Searchable dialog to quickly pick one or multiple creators from the local creators database.
    Maintains persistent multi-selection across searches so you can search and accumulate creators.
    """
    def __init__(self, creators, scale=1.0, existing_creators=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Creator(s) — Pawchive")
        self.resize(int(680 * scale), int(560 * scale))
        self.scale = scale
        self.all_creators = creators or []
        self.filtered_creators = list(self.all_creators)

        # Persistent dictionary mapping (service, id) -> creator dict across searches
        self.chosen_creators_map = {}
        if existing_creators:
            for c in existing_creators:
                if isinstance(c, dict):
                    k = ((c.get("service") or "").lower().strip(), str(c.get("id") or "").strip())
                    self.chosen_creators_map[k] = c

        self.selected_creators = []
        self.selected_creator = None

        self._build_ui()
        self._update_chosen_tray_ui()

    def _build_ui(self):
        scale = self.scale
        layout = QVBoxLayout(self)
        layout.setContentsMargins(int(14 * scale), int(14 * scale), int(14 * scale), int(12 * scale))
        layout.setSpacing(int(8 * scale))

        # Title & Subtitle
        title_box = QVBoxLayout()
        title_box.setSpacing(int(2 * scale))
        title = QLabel("Select Creator(s) to Browse Posts")
        title.setStyleSheet(f"font-size: {int(11 * scale)}pt; font-weight: bold; color: #EBB3AC;")
        sub = QLabel("Tip: Search and click any creator to add them. You can search multiple creators one by one — selections stay accumulated!")
        sub.setStyleSheet("font-size: 8pt; color: #8A8D93;")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        layout.addLayout(title_box)

        # Search & Filter row
        search_row = QHBoxLayout()
        search_row.setSpacing(int(6 * scale))

        self.platform_combo = QComboBox()
        self.platform_combo.addItems(["All Platforms", "Patreon", "Fanbox", "Discord"])
        self.platform_combo.currentTextChanged.connect(self._apply_filter)
        search_row.addWidget(self.platform_combo)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by creator name or ID...")
        self.search_input.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search_input, 1)

        self.select_all_btn = QPushButton("Select All Visible")
        self.select_all_btn.clicked.connect(self._select_all_filtered)
        search_row.addWidget(self.select_all_btn)

        self.clear_sel_btn = QPushButton("Clear All Selection")
        self.clear_sel_btn.clicked.connect(self._clear_all_chosen)
        search_row.addWidget(self.clear_sel_btn)

        layout.addLayout(search_row)

        # Persistent Selected Creators Tray
        self.selected_tray_label = QLabel("No creators selected yet. Click or select creators below to add them.")
        self.selected_tray_label.setStyleSheet(
            "color: #E5A198; font-size: 8.5pt; font-weight: normal; background-color: #2D2528; "
            "border: 1px solid #483A3E; border-radius: 4px; padding: 5px 8px;"
        )
        self.selected_tray_label.setWordWrap(True)
        layout.addWidget(self.selected_tray_label)

        # Creators Table
        from ..ui.creators_tab import CreatorsTableModel
        self.model = CreatorsTableModel(self.filtered_creators[:300])
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self._on_accept)
        self.table.selectionModel().selectionChanged.connect(self._on_picker_selection_changed)

        th = self.table.horizontalHeader()
        th.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        th.setStretchLastSection(False)
        th.setCascadingSectionResizes(False)
        th.resizeSection(0, int(90 * scale))
        th.resizeSection(1, int(260 * scale))
        th.resizeSection(2, int(100 * scale))
        th.resizeSection(3, int(110 * scale))
        th.resizeSection(4, int(110 * scale))
        layout.addWidget(self.table, 1)

        # Bottom buttons
        btn_row = QHBoxLayout()
        self.info_lbl = QLabel(f"Showing {min(300, len(self.filtered_creators))} of {len(self.all_creators):,} creators")
        self.info_lbl.setStyleSheet("color: #8C929E; font-size: 8pt;")
        btn_row.addWidget(self.info_lbl, 1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self.select_btn = QPushButton("Select Creator(s)")
        self.select_btn.setObjectName("PrimaryButton")
        self.select_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(self.select_btn)

        layout.addLayout(btn_row)

    def _select_all_filtered(self):
        sm = self.table.selectionModel()
        if sm:
            self.table.selectAll()
            for idx in sm.selectedRows():
                c = self.model.get_creator(idx.row())
                if c:
                    k = ((c.get("service") or "").lower().strip(), str(c.get("id") or "").strip())
                    self.chosen_creators_map[k] = c
            self._update_chosen_tray_ui()

    def _clear_all_chosen(self):
        self.chosen_creators_map.clear()
        self.table.clearSelection()
        self._update_chosen_tray_ui()

    def _on_picker_selection_changed(self):
        sm = self.table.selectionModel()
        if sm:
            for idx in sm.selectedRows():
                c = self.model.get_creator(idx.row())
                if c:
                    k = ((c.get("service") or "").lower().strip(), str(c.get("id") or "").strip())
                    self.chosen_creators_map[k] = c
        self._update_chosen_tray_ui()

    def _update_chosen_tray_ui(self):
        count = len(self.chosen_creators_map)
        if count == 0:
            self.selected_tray_label.setText("No creators selected yet. Search and select creators below to add them.")
            self.select_btn.setText("Select Creator(s)")
        elif count == 1:
            c = next(iter(self.chosen_creators_map.values()))
            name = c.get("name") or "Creator"
            svc = (c.get("service") or "").capitalize()
            self.selected_tray_label.setText(f"Accumulated ({count}): <b>{name}</b> [{svc}]")
            self.select_btn.setText("Select 1 Creator")
        else:
            names = [
                f"<b>{c.get('name') or 'Creator'}</b> [{c.get('service', '').capitalize()}]"
                for c in list(self.chosen_creators_map.values())[:4]
            ]
            more = f" +{count - 4} more" if count > 4 else ""
            summary = " &nbsp;•&nbsp; ".join(names) + more
            self.selected_tray_label.setText(f"Accumulated (<b>{count} creators</b>): {summary}")
            self.select_btn.setText(f"Select {count} Creators")

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_header_initialized", False):
            th = self.table.horizontalHeader()
            vw = self.table.viewport().width()
            if vw > int(450 * self.scale):
                fixed_sum = int(90 * self.scale) + int(100 * self.scale) + int(110 * self.scale) + int(110 * self.scale)
                th.resizeSection(1, max(int(200 * self.scale), vw - fixed_sum - 2))
            self._header_initialized = True

    def _apply_filter(self):
        query = self.search_input.text().strip().lower()
        pf = self.platform_combo.currentText().lower()

        results = self.all_creators
        if pf != "all platforms":
            results = [c for c in results if (c.get("service") or "").lower() == pf]

        if query:
            results = [
                c for c in results
                if query in (c.get("name") or "").lower() or query in str(c.get("id") or "").lower()
            ]

        self.filtered_creators = results
        self.model.set_creators(self.filtered_creators[:300])
        self.info_lbl.setText(f"Showing {min(300, len(self.filtered_creators))} of {len(self.filtered_creators):,} matching")

        # Automatically re-select any visible creators that were previously accumulated
        sm = self.table.selectionModel()
        if sm:
            from PySide6.QtCore import QItemSelectionModel
            for row, c in enumerate(self.filtered_creators[:300]):
                k = ((c.get("service") or "").lower().strip(), str(c.get("id") or "").strip())
                if k in self.chosen_creators_map:
                    idx = self.model.index(row, 0)
                    sm.select(idx, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)

    def _on_accept(self):
        if self.chosen_creators_map:
            self.selected_creators = list(self.chosen_creators_map.values())
            self.selected_creator = self.selected_creators[0]
            self.accept()
            return

        sm = self.table.selectionModel()
        if sm and sm.hasSelection():
            selected_rows = [idx.row() for idx in sm.selectedRows()]
            self.selected_creators = [
                self.model.get_creator(r) for r in selected_rows
                if self.model.get_creator(r) is not None
            ]
            if self.selected_creators:
                self.selected_creator = self.selected_creators[0]
                self.accept()
                return

        QMessageBox.information(self, "No Selection", "Please click or select at least one creator row.")


# ─────────────────────────────────────────────────────────────────────────────
#  Main Posts Tab Widget
# ─────────────────────────────────────────────────────────────────────────────

class PostsTabWidget(QWidget):
    """
    Creator Profile & Posts page matching the modern Pawchive client:
    - Back button returning directly to Creators directory
    - Hero Profile Card with banner background, fade gradient, overlapping avatar with ring border,
      creator name, service pill, handle/public_id, and archive favorites count
    - Section header: POSTS with live keyword search and full-catalog searching
    - Dual view support: Responsive Card Grid (16:10 preview thumbnails, media count, selection checkmark)
      and Classic Table View
    - Full multi-post downloading, batch creator downloading, and instant downloader queueing
    """
    def __init__(self, parent_app, scale=1.0):
        super().__init__()
        self.parent_app = parent_app
        self.scale = scale

        # State
        self.current_creators = []      # [{"id": ..., "name": ..., "service": ...}, ...]
        self.current_creator = None     # first creator for convenience/backward compat
        self.current_offset = 0
        self.all_creators_cache = []
        self.fetch_thread = None

        # Post search & caching state
        self.current_search_query = ""
        self._initial_posts = []

        # Media & Thumbnail Managers
        cache_dir = os.path.join(getattr(self.parent_app, 'app_base_dir', '.'), "cache")
        self.media_manager = CreatorMediaManager(base_cache_dir=cache_dir, max_concurrent=6, parent=self)
        self.media_manager.media_loaded.connect(self._on_creator_media_loaded)

        self.thumb_manager = PostThumbnailManager(base_cache_dir=cache_dir, max_concurrent=8, parent=self)
        self.thumb_manager.thumbnail_loaded.connect(self._on_thumbnail_loaded)

        # Debounced viewport prune timer (unloads off-screen post thumbnails on scroll pause)
        self._scroll_prune_timer = QTimer(self)
        self._scroll_prune_timer.setSingleShot(True)
        self._scroll_prune_timer.setInterval(120)
        self._scroll_prune_timer.timeout.connect(self._prune_offscreen_thumbnails)

        self._build_ui()
        self._load_local_creators_cache()

    def _build_ui(self):
        scale = self.scale
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Page Stack: Page 0 = Browse / Cards / Table, Page 1 = Full In-App Post Detail ──
        self.page_stack = QStackedWidget(self)

        # Page 0: Posts Browse View
        self.browse_page = QWidget(self.page_stack)
        browse_layout = QVBoxLayout(self.browse_page)
        browse_layout.setContentsMargins(
            int(14 * scale), int(10 * scale), int(14 * scale), int(12 * scale)
        )
        browse_layout.setSpacing(int(8 * scale))

        # Page 1: Dedicated In-App Post Detail View
        self.detail_view = PostDetailView(
            parent_app=self.parent_app,
            media_manager=self.media_manager,
            scale=scale,
            parent=self.page_stack
        )
        self.detail_view.back_to_posts_clicked.connect(self._close_post_detail)

        self.page_stack.addWidget(self.browse_page)  # Index 0
        self.page_stack.addWidget(self.detail_view)  # Index 1
        self.page_stack.setCurrentIndex(0)
        root_layout.addWidget(self.page_stack)

        # ── 1. Top Navigation Bar ─────────────────────────────────────────────
        nav_bar = QHBoxLayout()
        nav_bar.setSpacing(int(10 * scale))

        # Back to Creators button
        self.back_to_creators_btn = QPushButton("  Back to Creators")
        self.back_to_creators_btn.setIcon(QIcon(get_asset_path("assets/Svg/previous.svg")))
        self.back_to_creators_btn.setIconSize(QSize(int(12 * scale), int(12 * scale)))
        self.back_to_creators_btn.setFixedHeight(int(28 * scale))
        self.back_to_creators_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #9AA0AC; border: 1px solid #363A45; "
            "border-radius: 6px; padding: 0 12px; font-weight: bold; font-size: 8.5pt; }"
            "QPushButton:hover { background-color: #2D313A; color: #FFFFFF; border-color: #4F5565; }"
        )
        self.back_to_creators_btn.setToolTip("Return to Creators directory")
        self.back_to_creators_btn.clicked.connect(self._on_back_to_creators_clicked)
        nav_bar.addWidget(self.back_to_creators_btn)

        self.status_label = QLabel("No creator selected")
        self.status_label.setStyleSheet("font-size: 8.5pt; color: #E5A198; font-weight: bold;")
        nav_bar.addWidget(self.status_label)

        nav_bar.addStretch(1)

        # Toggle search / creator input card
        self.toggle_selector_btn = QPushButton("  Change Creator")
        self.toggle_selector_btn.setIcon(QIcon(get_asset_path("assets/Svg/search.svg")))
        self.toggle_selector_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.toggle_selector_btn.setFixedHeight(int(26 * scale))
        self.toggle_selector_btn.setStyleSheet(
            "QPushButton { background-color: #252830; color: #DFE1E5; border: 1px solid #383D48; "
            "border-radius: 5px; padding: 0 10px; font-size: 8pt; }"
            "QPushButton:hover { background-color: #2F333E; color: #FFFFFF; border-color: #E5A198; }"
        )
        self.toggle_selector_btn.setVisible(False)
        self.toggle_selector_btn.clicked.connect(self._toggle_selector_card)
        nav_bar.addWidget(self.toggle_selector_btn)

        # Choose Creator Button
        self.pick_btn = QPushButton("  Choose Creator(s)...")
        self.pick_btn.setIcon(QIcon(get_asset_path("assets/Svg/search.svg")))
        self.pick_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.pick_btn.setFixedHeight(int(26 * scale))
        self.pick_btn.setToolTip("Browse and pick multiple creators from 98,000+ cached database")
        self.pick_btn.clicked.connect(self._open_creator_picker)
        nav_bar.addWidget(self.pick_btn)

        browse_layout.addLayout(nav_bar)

        # ── 2. Creator Profile Hero Header ────────────────────────────────────
        self.hero_widget = CreatorProfileHeroWidget(self.media_manager, scale=scale, parent=self)
        self.hero_widget.send_to_downloader_clicked.connect(self._send_creator_to_downloader)
        self.hero_widget.about_clicked.connect(self._open_creator_about_dialog)
        self.hero_widget.setVisible(False)
        browse_layout.addWidget(self.hero_widget)

        # ── 3. Creator Selection & URL Input Card ─────────────────────────────
        self.selector_card = QFrame()
        self.selector_card.setObjectName("SectionCard")
        self.selector_card.setStyleSheet(
            "background-color: #2A2D34; border: 1px solid #3E4350; border-radius: 8px;"
        )
        sel_layout = QVBoxLayout(self.selector_card)
        sel_layout.setContentsMargins(
            int(12 * scale), int(10 * scale), int(12 * scale), int(10 * scale)
        )
        sel_layout.setSpacing(int(6 * scale))

        row1 = QHBoxLayout()
        row1.setSpacing(int(6 * scale))

        self.platform_combo = QComboBox()
        self.platform_combo.addItems(["All Platforms", "Patreon", "Fanbox", "Discord"])
        self.platform_combo.setToolTip("Platform / Service filter for creators")
        row1.addWidget(self.platform_combo)

        self.creator_input = QLineEdit()
        self.creator_input.setPlaceholderText(
            "Enter creator name(s), ID(s), or URLs (comma-separated)..."
        )
        self.creator_input.returnPressed.connect(self._on_load_posts_clicked)
        row1.addWidget(self.creator_input, 1)

        self.load_btn = QPushButton("  Load Posts")
        self.load_btn.setObjectName("PrimaryButton")
        self.load_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.load_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.load_btn.setMinimumHeight(int(28 * scale))
        self.load_btn.setToolTip("Fetch posts for all active creators from Pawchive API")
        self.load_btn.clicked.connect(self._on_load_posts_clicked)
        row1.addWidget(self.load_btn)

        sel_layout.addLayout(row1)

        active_row = QHBoxLayout()
        active_row.setSpacing(int(6 * scale))
        self.active_creator_label = QLabel("Tip: Click 'Choose Creator(s)...' or paste creator links (comma-separated) to load posts.")
        self.active_creator_label.setStyleSheet("color: #9AA0AC; font-size: 8pt; font-weight: normal;")
        active_row.addWidget(self.active_creator_label, 1)

        self.clear_creators_btn = QPushButton("Clear")
        self.clear_creators_btn.setFixedHeight(int(22 * scale))
        self.clear_creators_btn.setVisible(False)
        self.clear_creators_btn.setToolTip("Reset loaded creators and clear posts")
        self.clear_creators_btn.clicked.connect(self._clear_creators)
        active_row.addWidget(self.clear_creators_btn)

        sel_layout.addLayout(active_row)
        browse_layout.addWidget(self.selector_card)

        # ── 4. Posts Section Header ───────────────────────────────────────────
        posts_hdr_row = QHBoxLayout()
        posts_hdr_row.setContentsMargins(int(2 * scale), int(2 * scale), int(2 * scale), 0)
        self.posts_header_lbl = QLabel("POSTS")
        self.posts_header_lbl.setStyleSheet(
            "color: #E5A198; font-size: 8.5pt; font-weight: bold; letter-spacing: 2px;"
        )
        posts_hdr_row.addWidget(self.posts_header_lbl)
        posts_hdr_row.addStretch(1)
        browse_layout.addLayout(posts_hdr_row)

        # ── 5. Post Filter & Selection Toolbar ─────────────────────────────────
        toolbar_card = QFrame()
        toolbar_card.setStyleSheet("background-color: #252830; border: 1px solid #363A45; border-radius: 6px;")
        tool_layout = QHBoxLayout(toolbar_card)
        tool_layout.setContentsMargins(int(8 * scale), int(6 * scale), int(8 * scale), int(6 * scale))
        tool_layout.setSpacing(int(6 * scale))

        # Search within posts
        self.post_search_input = QLineEdit()
        self.post_search_input.setPlaceholderText("Search this creator's posts (e.g. art, pack, comic)...")
        self.post_search_input.setClearButtonEnabled(True)
        tool_layout.addWidget(self.post_search_input, 1)

        self.search_btn = QPushButton("  Search")
        self.search_btn.setIcon(QIcon(get_asset_path("assets/Svg/search.svg")))
        self.search_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.search_btn.setFixedHeight(int(26 * scale))
        self.search_btn.setToolTip("Search all creator posts on Pawchive for this keyword")
        self.search_btn.clicked.connect(self._execute_post_search)
        tool_layout.addWidget(self.search_btn)

        # Smooth search debounce timer
        self.search_debounce_timer = QTimer(self)
        self.search_debounce_timer.setSingleShot(True)
        self.search_debounce_timer.setInterval(450)
        self.search_debounce_timer.timeout.connect(self._execute_post_search)
        self.post_search_input.textChanged.connect(self._on_search_text_changed)
        self.post_search_input.returnPressed.connect(self._execute_post_search)

        # Select All / None buttons
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.setFixedHeight(int(26 * scale))
        self.select_all_btn.clicked.connect(lambda: self.model.select_all(True))
        tool_layout.addWidget(self.select_all_btn)

        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.setFixedHeight(int(26 * scale))
        self.deselect_all_btn.clicked.connect(lambda: self.model.select_all(False))
        tool_layout.addWidget(self.deselect_all_btn)

        self.invert_btn = QPushButton("Invert")
        self.invert_btn.setFixedHeight(int(26 * scale))
        self.invert_btn.clicked.connect(lambda: self.model.invert_selection())
        tool_layout.addWidget(self.invert_btn)

        # Pagination controls
        self.prev_page_btn = QPushButton("Previous 50")
        self.prev_page_btn.setFixedHeight(int(26 * scale))
        self.prev_page_btn.setEnabled(False)
        self.prev_page_btn.clicked.connect(self._load_prev_page)
        tool_layout.addWidget(self.prev_page_btn)

        self.page_info_lbl = QLabel("Offset: 0")
        self.page_info_lbl.setStyleSheet("color: #E5A198; font-weight: bold; font-size: 8pt;")
        tool_layout.addWidget(self.page_info_lbl)

        self.next_page_btn = QPushButton("Next 50")
        self.next_page_btn.setFixedHeight(int(26 * scale))
        self.next_page_btn.setEnabled(False)
        self.next_page_btn.clicked.connect(self._load_next_page)
        tool_layout.addWidget(self.next_page_btn)

        self.load_more_btn = QPushButton("Load More (+50)")
        self.load_more_btn.setFixedHeight(int(26 * scale))
        self.load_more_btn.setEnabled(False)
        self.load_more_btn.setToolTip("Appends the next 50 posts without clearing currently loaded ones")
        self.load_more_btn.clicked.connect(self._load_more_posts)
        tool_layout.addWidget(self.load_more_btn)

        # View Mode Toggle: Grid View / Table View
        self.view_toggle_btn = QPushButton("  Table View")
        self.view_toggle_btn.setIcon(QIcon(get_asset_path("assets/Svg/doc.svg")))
        self.view_toggle_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.view_toggle_btn.setFixedHeight(int(26 * scale))
        self.view_toggle_btn.setToolTip("Toggle between Responsive Post Card Grid and Classic Table View")
        self.view_toggle_btn.clicked.connect(self._toggle_view_mode)
        tool_layout.addWidget(self.view_toggle_btn)

        browse_layout.addWidget(toolbar_card)

        # ── 6. Posts View Stack (Card Grid vs Table View) ─────────────────────
        self.model = PostsTableModel([], on_selection_changed=self._update_selection_counter)

        self.view_stack = QStackedWidget()

        # View 0: Post Card Grid (Default)
        self.grid_view = QListView()
        self.grid_view.setViewMode(QListView.ViewMode.IconMode)
        self.grid_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid_view.setMovement(QListView.Movement.Static)
        self.grid_view.setSpacing(int(12 * scale))
        self.grid_view.setMouseTracking(True)
        self.grid_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid_view.setStyleSheet(
            "QListView { background-color: #1A1C23; border: 1px solid #2B2E38; border-radius: 8px; padding: 6px; }"
            "QListView::item { background: transparent; border: none; }"
        )
        self.grid_delegate = PostCardDelegate(self.thumb_manager, scale=scale, parent=self.grid_view)
        self.grid_view.setItemDelegate(self.grid_delegate)
        self.grid_view.setModel(self.model)
        self.grid_view.clicked.connect(self._on_grid_card_clicked)
        self.grid_view.doubleClicked.connect(self._on_table_double_clicked)
        self.grid_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid_view.customContextMenuRequested.connect(self._show_grid_context_menu)
        self.view_stack.addWidget(self.grid_view)

        # View 1: Classic Table View
        self.table_view = QTableView()
        self.table_view.setModel(self.model)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setShowGrid(True)
        self.table_view.verticalHeader().setVisible(False)
        self.table_view.doubleClicked.connect(self._on_table_double_clicked)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._show_context_menu)

        th = self.table_view.horizontalHeader()
        th.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        th.setStretchLastSection(False)
        th.setCascadingSectionResizes(False)
        th.resizeSection(0, int(50 * scale))
        th.resizeSection(1, int(150 * scale))
        th.resizeSection(2, int(300 * scale))
        th.resizeSection(3, int(135 * scale))
        th.resizeSection(4, int(160 * scale))
        th.resizeSection(5, int(100 * scale))
        self.view_stack.addWidget(self.table_view)

        # Dynamic viewport memory pruning on scroll
        self.grid_view.verticalScrollBar().valueChanged.connect(self._on_view_scrolled)
        self.table_view.verticalScrollBar().valueChanged.connect(self._on_view_scrolled)

        # Default to Card Grid View (Index 0)
        self.view_stack.setCurrentIndex(0)
        browse_layout.addWidget(self.view_stack, 1)

        # ── 7. Bottom Action Bar ──────────────────────────────────────────────
        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(int(8 * scale))

        self.selection_counter_lbl = QLabel("0 posts loaded | 0 selected")
        self.selection_counter_lbl.setStyleSheet("color: #8C929E; font-size: 8.5pt;")
        bottom_bar.addWidget(self.selection_counter_lbl, 1)

        self.play_media_btn = QPushButton("  View Post & Media")
        self.play_media_btn.setIcon(QIcon(get_asset_path("assets/Svg/resume.svg")))
        self.play_media_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.play_media_btn.setEnabled(False)
        self.play_media_btn.setToolTip("Open full post description, comments, and in-app media player")
        self.play_media_btn.clicked.connect(self._open_post_detail_for_focused)
        bottom_bar.addWidget(self.play_media_btn)

        self.copy_post_url_btn = QPushButton("  Copy Post URL")
        self.copy_post_url_btn.setIcon(QIcon(get_asset_path("assets/Svg/link.svg")))
        self.copy_post_url_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.copy_post_url_btn.setEnabled(False)
        self.copy_post_url_btn.clicked.connect(self._copy_focused_post_url)
        bottom_bar.addWidget(self.copy_post_url_btn)

        self.browser_post_btn = QPushButton("  Open in Browser")
        self.browser_post_btn.setIcon(QIcon(get_asset_path("assets/Svg/eye_open.svg")))
        self.browser_post_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.browser_post_btn.setEnabled(False)
        self.browser_post_btn.clicked.connect(self._open_focused_post_in_browser)
        bottom_bar.addWidget(self.browser_post_btn)

        self.send_to_dl_btn = QPushButton("  Send to Downloader")
        self.send_to_dl_btn.setObjectName("PrimaryButton")
        self.send_to_dl_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.send_to_dl_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.send_to_dl_btn.setMinimumHeight(int(32 * scale))
        self.send_to_dl_btn.setEnabled(False)
        self.send_to_dl_btn.setToolTip("Sends the selected post or creator link to the main Downloader tab")
        self.send_to_dl_btn.clicked.connect(self._on_send_to_downloader_clicked)
        bottom_bar.addWidget(self.send_to_dl_btn)

        browse_layout.addLayout(bottom_bar)

        # Connect row selection changes to enable focused buttons
        self.table_view.selectionModel().selectionChanged.connect(self._on_table_row_selection_changed)
        if self.grid_view.selectionModel():
            self.grid_view.selectionModel().selectionChanged.connect(self._on_table_row_selection_changed)

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_header_initialized", False):
            th = self.table_view.horizontalHeader()
            vw = self.table_view.viewport().width()
            if vw > int(650 * self.scale):
                fixed_sum = int(50 * self.scale) + int(150 * self.scale) + int(135 * self.scale) + int(160 * self.scale) + int(100 * self.scale)
                title_w = max(int(240 * self.scale), vw - fixed_sum - 2)
                th.resizeSection(2, title_w)
            self._header_initialized = True
        self._scroll_prune_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scroll_prune_timer.start()

    def _on_view_scrolled(self):
        """Debounce viewport checking during rapid scrolling."""
        self._scroll_prune_timer.start()

    def _prune_offscreen_thumbnails(self):
        """Unloads post thumbnails that have scrolled outside the visible viewport."""
        if hasattr(self, 'thumb_manager') and self.thumb_manager:
            visible_ids = self.get_visible_post_ids()
            if visible_ids:
                self.thumb_manager.unload_offscreen(visible_ids)

    def get_visible_post_ids(self) -> set:
        """Determines the set of post IDs visible in the viewport plus a small buffer."""
        visible_ids = set()
        current_mode = self.view_stack.currentIndex()
        view = self.grid_view if current_mode == 0 else self.table_view
        model = self.model

        if not model or model.rowCount() == 0:
            return visible_ids

        total = model.rowCount()
        vp = view.viewport()
        vp_w, vp_h = vp.width(), vp.height()
        if vp_w <= 0 or vp_h <= 0:
            return visible_ids

        top_row = -1
        for y in (15, 45, 90, 150):
            for x in (25, 120, 250, 450):
                idx = view.indexAt(QPoint(x, y))
                if idx.isValid():
                    top_row = idx.row()
                    break
            if top_row != -1:
                break

        bot_row = -1
        for y in (vp_h - 15, vp_h - 45, vp_h - 90, vp_h - 150):
            for x in (vp_w - 25, vp_w // 2, 25):
                idx = view.indexAt(QPoint(x, y))
                if idx.isValid():
                    bot_row = idx.row()
                    break
            if bot_row != -1:
                break

        if top_row == -1:
            top_row = 0
        if bot_row == -1:
            bot_row = min(top_row + 20, total - 1)

        start_row = max(0, min(top_row, bot_row) - 6)
        end_row = min(total - 1, max(top_row, bot_row) + 6)

        for r in range(start_row, end_row + 1):
            p = model.get_post_at(r)
            if p and isinstance(p, dict):
                pid = str(p.get("id") or "").strip()
                if pid:
                    visible_ids.add(pid)

        return visible_ids

    def on_tab_deactivated(self):
        """Called when user navigates away from the Posts tab: unloads memory caches immediately."""
        self._scroll_prune_timer.stop()
        if hasattr(self, 'thumb_manager') and self.thumb_manager:
            self.thumb_manager.clear_memory_cache()
        if hasattr(self, 'media_manager') and self.media_manager:
            self.media_manager.clear_memory_cache()

    def on_tab_activated(self):
        """Called when user navigates back to Posts tab: re-renders visible viewport from disk cache."""
        if hasattr(self, "page_stack") and self.page_stack.currentIndex() == 0:
            if hasattr(self, "view_stack"):
                if self.view_stack.currentIndex() == 0:
                    self.grid_view.viewport().update()
                else:
                    self.table_view.viewport().update()

    # ─────────────────────────────────────────────────────────────────────────
    #  Creator Cache & Picker
    # ─────────────────────────────────────────────────────────────────────────

    def _load_local_creators_cache(self):
        """Loads data/creators.json asynchronously for the creator picker."""
        creators_path = os.path.join(
            getattr(self.parent_app, 'app_base_dir', '.'), "data", "creators.json"
        )
        if not os.path.exists(creators_path):
            from ..utils.file_utils import get_app_base_path
            bundled_creators = os.path.join(get_app_base_path(), "data", "creators.json")
            if os.path.exists(bundled_creators):
                creators_path = bundled_creators
        if os.path.exists(creators_path):
            try:
                with open(creators_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    if data and isinstance(data[0], list):
                        data = data[0]
                    self.all_creators_cache = [
                        c for c in data
                        if isinstance(c, dict) and (c.get("service") or "").lower().strip() in SUPPORTED_PLATFORMS
                    ]
            except Exception:
                pass

    def _open_creator_picker(self):
        """Opens the modal dialog to search & select one or multiple creators from the local creators database."""
        if not self.all_creators_cache:
            self._load_local_creators_cache()

        dialog = CreatorPickerDialog(
            self.all_creators_cache,
            scale=self.scale,
            existing_creators=self.current_creators,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_creators:
            self.load_creators(dialog.selected_creators)

    # ─────────────────────────────────────────────────────────────────────────
    #  Creator Loading & Multiple-Creator Management
    # ─────────────────────────────────────────────────────────────────────────

    def _get_creator_url(self, creator: dict) -> str:
        if not creator:
            return ""
        svc = (creator.get("service") or "patreon").lower().strip()
        cid = str(creator.get("id") or "").strip()
        url_type = "server" if svc == "discord" else "user"
        return f"https://pawchive.pw/{svc}/{url_type}/{cid}"

    def _update_active_creators_ui(self):
        count = len(self.current_creators)
        if count == 0:
            self.active_creator_label.setText("Tip: Click 'Choose Creator(s)...' or paste creator links (comma-separated) to load posts.")
            self.clear_creators_btn.setVisible(False)
            self.status_label.setText("No creator selected")
            if hasattr(self, 'send_to_dl_btn'):
                has_sel = self._get_focused_post() is not None
                self.send_to_dl_btn.setEnabled(has_sel)
        elif count == 1:
            c = self.current_creators[0]
            name = c.get("name") or "Unknown"
            svc = (c.get("service") or "patreon").capitalize()
            cid = c.get("id") or ""
            self.active_creator_label.setText(
                f"Active Creator: <b>{name}</b> &nbsp;|&nbsp; Platform: <b>{svc}</b> &nbsp;|&nbsp; Creator ID: <code>{cid}</code>"
            )
            self.clear_creators_btn.setVisible(True)
            if hasattr(self, 'send_to_dl_btn'):
                self.send_to_dl_btn.setEnabled(True)
        else:
            names = [f"<b>{c.get('name') or 'Unknown'}</b> [{(c.get('service') or '').capitalize()}]" for c in self.current_creators[:4]]
            more = f" +{count - 4} more" if count > 4 else ""
            summary = ", ".join(names) + more
            self.active_creator_label.setText(
                f"Active (<b>{count} creators</b>): {summary}"
            )
            self.clear_creators_btn.setVisible(True)
            if hasattr(self, 'send_to_dl_btn'):
                self.send_to_dl_btn.setEnabled(True)

    def _clear_creators(self):
        """Clears all loaded creators and empties the posts table."""
        self.current_creators = []
        self.current_creator = None
        self.current_search_query = ""
        self._initial_posts = []
        self.creator_input.clear()
        self.post_search_input.clear()
        self.model.set_posts([])
        self.hero_widget.set_creator(None, 0)
        self.hero_widget.setVisible(False)
        self.selector_card.setVisible(True)
        self.toggle_selector_btn.setVisible(False)
        self._update_active_creators_ui()
        self._update_selection_counter()
        self.status_label.setText("No creators selected")
        self.page_info_lbl.setText("Offset: 0")
        self.prev_page_btn.setEnabled(False)
        self.next_page_btn.setEnabled(False)
        self.load_more_btn.setEnabled(False)

    def load_creators(self, creators: list):
        """
        Loads multiple creators, displays them, and starts fetching posts from Pawchive.
        """
        if not creators:
            return

        # Return to posts browse list if a post detail view is currently open
        if hasattr(self, "page_stack") and self.page_stack.currentIndex() != 0:
            self._close_post_detail()

        self.current_creators = list(creators)
        self.current_creator = creators[0]
        self.current_search_query = ""
        self._initial_posts = []
        self.post_search_input.clear()

        # Update hero widget
        self.hero_widget.set_creator(self.current_creator, total_count=len(creators))
        self.hero_widget.setVisible(True)
        self.selector_card.setVisible(False)
        self.toggle_selector_btn.setVisible(True)
        self.toggle_selector_btn.setText("  Change Creator")

        cname = self.current_creator.get("name") or "Creator"
        self.status_label.setText(f"Viewing {cname}'s Posts")

        # Update input text with creator names
        names = [c.get("name") or str(c.get("id")) for c in creators]
        self.creator_input.setText(", ".join(names))

        # Update platform dropdown if all creators share the same platform
        services = {(c.get("service") or "").lower() for c in creators}
        if len(services) == 1:
            svc = next(iter(services))
            for i in range(self.platform_combo.count()):
                if self.platform_combo.itemText(i).lower() == svc:
                    self.platform_combo.setCurrentIndex(i)
                    break
        else:
            for i in range(self.platform_combo.count()):
                if self.platform_combo.itemText(i).lower() == "all platforms":
                    self.platform_combo.setCurrentIndex(i)
                    break

        self._update_active_creators_ui()
        self.current_offset = 0
        self._fetch_all_posts(offset=0, append=False)

    def _on_back_to_creators_clicked(self):
        """Switches back to Creators tab directory."""
        if hasattr(self.parent_app, 'switch_tab'):
            self.parent_app.switch_tab("creators")

    def _toggle_selector_card(self):
        """Toggles visibility of the creator picker/search card."""
        is_vis = self.selector_card.isVisible()
        self.selector_card.setVisible(not is_vis)
        self.toggle_selector_btn.setText("  Hide Search" if not is_vis else "  Change Creator")

    def _open_creator_about_dialog(self):
        """Displays creator bio & external links dialog."""
        if not self.current_creator:
            return
        dlg = CreatorAboutDialog(self.current_creator, scale=self.scale, parent=self)
        dlg.exec()

    def _toggle_view_mode(self):
        """Toggles between Responsive Post Card Grid and Classic Table View."""
        if self.view_stack.currentIndex() == 0:
            self.view_stack.setCurrentIndex(1)
            self.view_toggle_btn.setText("  Grid View")
            self.view_toggle_btn.setIcon(QIcon(get_asset_path("assets/Svg/folder.svg")))
        else:
            self.view_stack.setCurrentIndex(0)
            self.view_toggle_btn.setText("  Table View")
            self.view_toggle_btn.setIcon(QIcon(get_asset_path("assets/Svg/doc.svg")))
            self.grid_view.viewport().update()
        self._scroll_prune_timer.start()

    def _on_grid_card_clicked(self, index: QModelIndex):
        """Clicking on a post card toggles checkmark selection."""
        if not index.isValid():
            return
        row = index.row()
        self._toggle_post_check(row)
        sm = self.table_view.selectionModel()
        if sm:
            from PySide6.QtCore import QItemSelectionModel
            idx = self.model.index(row, 0)
            sm.setCurrentIndex(idx, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        self._update_selection_counter()

    def _on_thumbnail_loaded(self, post_id: str):
        """Triggered asynchronously when a post thumbnail finishes downloading."""
        if hasattr(self, 'grid_view'):
            self.grid_view.viewport().update()

    def _on_creator_media_loaded(self, kind: str, service: str, creator_id: str):
        """Triggered asynchronously when a creator avatar or banner finishes downloading."""
        if hasattr(self, 'hero_widget') and self.current_creator:
            svc = (self.current_creator.get("service") or "").lower().strip()
            cid = str(self.current_creator.get("id") or "").strip()
            if svc == service.lower().strip() and cid == creator_id.strip():
                self.hero_widget.set_creator(self.current_creator, len(self.current_creators))

    def load_creator(self, creator: dict):
        """Backwards-compatible convenience method to load a single creator."""
        if creator and isinstance(creator, dict):
            self.load_creators([creator])

    def _resolve_creators_from_input(self, text: str) -> list:
        """
        Intelligently parses comma-separated inputs of URLs, IDs, and creator names.
        """
        if not self.all_creators_cache:
            self._load_local_creators_cache()

        default_service = self.platform_combo.currentText().lower()
        if default_service == "all platforms":
            default_service = "patreon"

        tokens = [t.strip() for t in re.split(r'[,;\n]+', text) if t.strip()]
        creators = []
        seen_keys = set()

        # Build quick lookup indexes from local cache
        cache_by_id = {}
        cache_by_name = {}
        for c in self.all_creators_cache:
            cid = str(c.get("id") or "").strip()
            cname = (c.get("name") or "").strip().lower()
            if cid:
                cache_by_id[cid] = c
            if cname:
                cache_by_name[cname] = c

        for tok in tokens:
            resolved = None

            # Case 1: URL format (pawchive.pw, kemono, or path with slashes)
            if "pawchive.pw" in tok or "kemono" in tok or "/" in tok:
                clean_tok = tok.split("?")[0].strip()
                parts = [p for p in clean_tok.split("/") if p and p not in ("http:", "https:")]
                svc = default_service
                cid = None
                for idx, p in enumerate(parts):
                    if p.lower() in SUPPORTED_PLATFORMS:
                        svc = p.lower()
                        if idx + 2 < len(parts) and parts[idx + 1] in ("user", "server"):
                            cid = parts[idx + 2]
                        elif idx + 1 < len(parts):
                            cid = parts[idx + 1]
                        break

                if not cid and len(parts) >= 2:
                    cid = parts[-1]

                if cid:
                    if cid in cache_by_id:
                        resolved = dict(cache_by_id[cid])
                        resolved["service"] = svc
                    else:
                        resolved = {"id": cid, "service": svc, "name": f"{svc.capitalize()}_{cid}"}

            # Case 2: Exact ID match in cache
            elif tok in cache_by_id:
                resolved = dict(cache_by_id[tok])

            # Case 3: Exact name match in cache
            elif tok.lower() in cache_by_name:
                resolved = dict(cache_by_name[tok.lower()])

            # Case 4: Pure digits (treat as ID)
            elif tok.isdigit():
                resolved = {"id": tok, "service": default_service, "name": f"Creator {tok}"}

            # Case 5: Partial name lookup in cache
            else:
                tok_lower = tok.lower()
                matches = [c for c in self.all_creators_cache if tok_lower in (c.get("name") or "").lower()]
                if matches:
                    resolved = dict(matches[0])
                else:
                    resolved = {"id": tok, "service": default_service, "name": tok}

            if resolved:
                key = (resolved.get("service", "").lower(), str(resolved.get("id", "")))
                if key not in seen_keys:
                    seen_keys.add(key)
                    creators.append(resolved)

        return creators

    def _on_load_posts_clicked(self):
        """Called when user clicks 'Load Posts' or presses Enter in the input."""
        text = self.creator_input.text().strip()
        if not text:
            if self.current_creators:
                self.current_offset = 0
                self._fetch_all_posts(offset=0, append=False)
                return
            QMessageBox.information(
                self, "Input Required",
                "Please enter creator name(s), ID(s), or paste creator URL(s), or click 'Choose Creator(s)...'"
            )
            return

        creators = self._resolve_creators_from_input(text)
        if not creators:
            QMessageBox.warning(
                self, "Creator Not Found",
                "Could not find or resolve any creator from the provided input."
            )
            return

        self.load_creators(creators)

    # ─────────────────────────────────────────────────────────────────────────
    #  API Fetching Logic (Multi-Creator & Single Creator)
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_all_posts(self, offset: int = 0, append: bool = False):
        if not self.current_creators:
            return

        if self.fetch_thread and self.fetch_thread.isRunning():
            return

        count = len(self.current_creators)
        if count > 1:
            self.status_label.setText(f"Loading posts for {count} creators (offset {offset})...")
        else:
            cname = self.current_creators[0].get("name", "Creator")
            self.status_label.setText(f"Loading posts for '{cname}' (offset {offset})...")

        self.load_btn.setEnabled(False)
        self.prev_page_btn.setEnabled(False)
        self.next_page_btn.setEnabled(False)
        self.load_more_btn.setEnabled(False)

        self.fetch_thread = FetchMultipleCreatorPostsThread(
            self.current_creators,
            offset=offset,
            query="",
            fetch_all=False
        )
        self.fetch_thread.status_signal.connect(self._on_fetch_status)
        self.fetch_thread.finished_signal.connect(
            lambda ok, msg, posts: self._on_fetch_finished(ok, msg, posts, append, offset)
        )
        self.fetch_thread.start()

    def _fetch_posts(self, service: str, creator_id: str, offset: int = 0, append: bool = False):
        """Compatibility wrapper for single creator fetch."""
        self.load_creators([{"service": service, "id": creator_id}])

    def _on_fetch_status(self, msg: str):
        self.status_label.setText(msg)

    def _on_fetch_finished(self, success: bool, message: str, posts: list, append: bool, offset: int):
        self.load_btn.setEnabled(True)

        if success:
            self.current_offset = offset
            self.model.set_posts(posts, append=append)
            if not append and offset == 0 and not self.current_search_query:
                self._initial_posts = list(posts)

            count = len(self.model._all_posts)
            c_count = len(self.current_creators)
            c_desc = f"from {c_count} creators" if c_count > 1 else ""
            self.status_label.setText(f"Loaded {count} post(s) {c_desc}".strip())
            self.page_info_lbl.setText(f"Offset: {offset} (Page {offset // 50 + 1})")

            has_posts = len(posts) > 0
            self.prev_page_btn.setEnabled(offset >= 50)
            self.next_page_btn.setEnabled(has_posts)
            self.load_more_btn.setEnabled(has_posts)

            if hasattr(self.parent_app, 'log_signal'):
                summary = f"across {c_count} creators" if c_count > 1 else f"for '{self.current_creators[0].get('name', 'Creator')}'"
                self.parent_app.log_signal.emit(
                    f"Posts tab: Loaded {len(posts)} posts {summary} (offset {offset})"
                )
        else:
            self.status_label.setText(message)
            if not append:
                self.model.set_posts([])
            QMessageBox.warning(self, "Load Posts Failed", message)

        self._update_selection_counter()

    def _load_prev_page(self):
        if not self.current_creators:
            return
        new_offset = max(0, self.current_offset - 50)
        self._fetch_all_posts(offset=new_offset, append=False)

    def _load_next_page(self):
        if not self.current_creators:
            return
        new_offset = self.current_offset + 50
        self._fetch_all_posts(offset=new_offset, append=False)

    def _load_more_posts(self):
        if not self.current_creators:
            return
        new_offset = self.current_offset + 50
        self._fetch_all_posts(offset=new_offset, append=True)

    # ─────────────────────────────────────────────────────────────────────────
    #  Full Catalog Search Logic with Smooth Debounce & Zero Lag
    # ─────────────────────────────────────────────────────────────────────────

    def _on_search_text_changed(self, text: str):
        query = text.strip()
        if not query:
            self.search_debounce_timer.stop()
            if self.current_search_query:
                self.current_search_query = ""
                if self.fetch_thread and self.fetch_thread.isRunning():
                    self.fetch_thread.cancel()

                # Instant restore of initial posts with zero latency
                if self._initial_posts:
                    self.model.set_posts(self._initial_posts)
                    count = len(self._initial_posts)
                    c_count = len(self.current_creators)
                    c_desc = f"from {c_count} creators" if c_count > 1 else ""
                    self.status_label.setText(f"Loaded {count} post(s) {c_desc}".strip())
                    self.page_info_lbl.setText(f"Offset: {self.current_offset} (Page {self.current_offset // 50 + 1})")
                    self.prev_page_btn.setEnabled(self.current_offset >= 50)
                    self.next_page_btn.setEnabled(count >= 50)
                    self.load_more_btn.setEnabled(count >= 50)
                    self._update_selection_counter()
                else:
                    self._fetch_all_posts(offset=0, append=False)
            return

        # User is actively typing: cancel any ongoing background search to eliminate lag
        if self.fetch_thread and self.fetch_thread.isRunning():
            self.fetch_thread.cancel()
        self.search_debounce_timer.start()

    def _execute_post_search(self):
        self.search_debounce_timer.stop()
        query = self.post_search_input.text().strip()

        if not self.current_creators:
            return

        if query == self.current_search_query:
            return

        if not query:
            self._on_search_text_changed("")
            return

        self.current_search_query = query

        if self.fetch_thread and self.fetch_thread.isRunning():
            self.fetch_thread.cancel()

        self.status_label.setText(f"Searching full catalog for '{query}'...")
        self.page_info_lbl.setText(f"Query: \"{query}\"")
        self.load_btn.setEnabled(False)
        self.prev_page_btn.setEnabled(False)
        self.next_page_btn.setEnabled(False)
        self.load_more_btn.setEnabled(False)

        # Launch background search thread with fetch_all=True
        self.fetch_thread = FetchMultipleCreatorPostsThread(
            self.current_creators,
            offset=0,
            query=query,
            fetch_all=True
        )
        self.fetch_thread.status_signal.connect(self._on_fetch_status)
        self.fetch_thread.finished_signal.connect(
            lambda ok, msg, posts: self._on_search_finished(ok, msg, posts, query)
        )
        self.fetch_thread.start()

    def _on_search_finished(self, success: bool, message: str, posts: list, query: str):
        self.load_btn.setEnabled(True)

        # Discard stale callback if user already typed a new query
        if self.current_search_query != query:
            return

        if success:
            self.model.set_posts(posts, append=False)
            count = len(posts)
            c_count = len(self.current_creators)
            if count == 0:
                self.status_label.setText(f"No posts found matching '{query}'")
            else:
                self.status_label.setText(f"Found {count} post(s) matching '{query}' across archive")

            self.page_info_lbl.setText(f"Matches: {count}")
            self.prev_page_btn.setEnabled(False)
            self.next_page_btn.setEnabled(False)
            self.load_more_btn.setEnabled(False)

            if hasattr(self.parent_app, 'log_signal'):
                summary = f"across {c_count} creators" if c_count > 1 else f"for '{self.current_creators[0].get('name', 'Creator')}'"
                self.parent_app.log_signal.emit(
                    f"Posts tab: Full-catalog search found {count} post(s) for '{query}' {summary}"
                )
        else:
            self.status_label.setText(message)
            QMessageBox.warning(self, "Search Failed", message)

        self._update_selection_counter()

    # ─────────────────────────────────────────────────────────────────────────
    #  Selection & Actions
    # ─────────────────────────────────────────────────────────────────────────

    def _update_selection_counter(self):
        total = len(self.model._all_posts)
        filtered = len(self.model._filtered_posts)
        selected = len(self.model.selected_post_ids)
        c_count = len(self.current_creators)

        c_info = f" across {c_count} creator{'s' if c_count != 1 else ''}" if c_count > 1 else ""

        if self.current_search_query:
            self.selection_counter_lbl.setText(
                f"<b>{total}</b> post(s) matching \"{self.current_search_query}\"{c_info} | <b>{selected}</b> selected"
            )
        elif filtered != total:
            self.selection_counter_lbl.setText(
                f"Showing {filtered} of {total} posts{c_info} | <b>{selected}</b> selected"
            )
        else:
            self.selection_counter_lbl.setText(
                f"{total} posts loaded{c_info} | <b>{selected}</b> selected"
            )

        if hasattr(self, 'send_to_dl_btn'):
            focused = self._get_focused_post()
            self.send_to_dl_btn.setEnabled(selected > 0 or focused is not None or c_count > 0)

        self._on_table_row_selection_changed()

    def _on_table_row_selection_changed(self):
        focused = self._get_focused_post()
        has_sel = focused is not None
        if hasattr(self, 'play_media_btn'):
            self.play_media_btn.setEnabled(has_sel)
        if hasattr(self, 'copy_post_url_btn'):
            self.copy_post_url_btn.setEnabled(has_sel)
        if hasattr(self, 'browser_post_btn'):
            self.browser_post_btn.setEnabled(has_sel)
        if hasattr(self, 'send_to_dl_btn'):
            selected = len(getattr(self.model, 'selected_post_ids', []))
            c_count = len(getattr(self, 'current_creators', []))
            self.send_to_dl_btn.setEnabled(has_sel or selected > 0 or c_count > 0)

    def _get_focused_post(self):
        if hasattr(self, 'view_stack') and self.view_stack.currentIndex() == 0:
            sm = self.grid_view.selectionModel()
            if sm and sm.hasSelection():
                indexes = sm.selectedIndexes()
                if indexes:
                    return self.model.get_post_at(indexes[0].row())
        else:
            sm = self.table_view.selectionModel()
            if sm and sm.hasSelection():
                rows = sm.selectedRows()
                if rows:
                    return self.model.get_post_at(rows[0].row())
                indexes = sm.selectedIndexes()
                if indexes:
                    return self.model.get_post_at(indexes[0].row())

        # Fallback to checking the alternate view
        for v in (getattr(self, 'table_view', None), getattr(self, 'grid_view', None)):
            if v and v.selectionModel() and v.selectionModel().hasSelection():
                idxes = v.selectionModel().selectedIndexes()
                if idxes:
                    return self.model.get_post_at(idxes[0].row())

        selected = self.model.get_selected_posts()
        return selected[0] if selected else None

    def _show_grid_context_menu(self, pos):
        index = self.grid_view.indexAt(pos)
        if not index.isValid():
            return
        self._show_post_context_menu_at(index, self.grid_view.viewport().mapToGlobal(pos))

    def _show_context_menu(self, pos):
        index = self.table_view.indexAt(pos)
        if not index.isValid():
            return
        self._show_post_context_menu_at(index, self.table_view.viewport().mapToGlobal(pos))

    def _show_post_context_menu_at(self, index: QModelIndex, global_pos):
        post = self.model.get_post_at(index.row())
        if not post:
            return

        menu = QMenu(self)

        media_items = extract_media_items_from_post(post)
        if media_items:
            count = len(media_items)
            play_label = f"View Post & Media ({count} {'item' if count == 1 else 'items'})"
        else:
            play_label = "View Post & Media"

        play_act = menu.addAction(QIcon(get_asset_path("assets/uisvg/play.svg")), play_label)
        play_act.triggered.connect(lambda: self._open_post_detail(post))
        menu.addSeparator()

        send_act = menu.addAction(QIcon(get_asset_path("assets/Svg/download.svg")), "Send to Downloader")
        send_act.triggered.connect(self._on_send_to_downloader_clicked)

        if self.current_creators:
            c_label = f"Send {len(self.current_creators)} Creators to Downloader" if len(self.current_creators) > 1 else "Send Creator to Downloader"
            send_c_act = menu.addAction(QIcon(get_asset_path("assets/Svg/download.svg")), c_label)
            send_c_act.triggered.connect(self._send_creator_to_downloader)

        menu.addSeparator()

        copy_act = menu.addAction(QIcon(get_asset_path("assets/Svg/copy.svg")), "Copy Post URL")
        copy_act.triggered.connect(self._copy_focused_post_url)

        browser_act = menu.addAction(QIcon(get_asset_path("assets/Svg/eye_open.svg")), "Open in Browser (pawchive.pw)")
        browser_act.triggered.connect(self._open_focused_post_in_browser)

        toggle_check_act = menu.addAction("Toggle Checkmark Selection")
        toggle_check_act.triggered.connect(
            lambda: self._toggle_post_check(index.row())
        )

        menu.exec(global_pos)

    def _open_post_detail(self, post: dict):
        """Transitions into the full dedicated in-app Post Detail view."""
        if not post:
            return
        if hasattr(self, 'thumb_manager') and self.thumb_manager:
            self.thumb_manager.clear_memory_cache()
        self.detail_view.load_post(post, all_posts=self.model._filtered_posts)
        self.page_stack.setCurrentIndex(1)

    def _open_post_detail_for_focused(self):
        """Opens in-app Post Detail view for the focused post."""
        post = self._get_focused_post()
        if post:
            self._open_post_detail(post)

    def _close_post_detail(self):
        """Returns from Post Detail view back to Posts browse view."""
        if hasattr(self, "detail_view"):
            try:
                self.detail_view.close()
            except Exception:
                pass
            if hasattr(self.detail_view, "unload_media"):
                self.detail_view.unload_media()
            else:
                if hasattr(self.detail_view, "video_player"):
                    self.detail_view.video_player.stop_and_release()
                if hasattr(self.detail_view, "gif_player"):
                    self.detail_view.gif_player.stop()
                if hasattr(self.detail_view, "image_view"):
                    self.detail_view.image_view.set_pixmap(QPixmap())
        if hasattr(self, "page_stack"):
            self.page_stack.setCurrentIndex(0)
            if hasattr(self, "view_stack"):
                if self.view_stack.currentIndex() == 0:
                    self.grid_view.viewport().update()
                else:
                    self.table_view.viewport().update()

    # Aliases
    def _open_media_viewer_for_post(self, post: dict, initial_index: int = 0):
        self._open_post_detail(post)

    def _open_media_viewer_for_focused(self):
        self._open_post_detail_for_focused()

    def _get_post_direct_url(self, post: dict):
        if not post:
            return ""
        svc = (
            post.get("_creator_service")
            or post.get("service")
            or (self.current_creator.get("service") if self.current_creator else "patreon")
        ).lower().strip()
        cid = str(
            post.get("_creator_id")
            or post.get("user")
            or (self.current_creator.get("id") if self.current_creator else "")
        ).strip()
        pid = str(post.get("id") or "").strip()
        url_type = "server" if svc == "discord" else "user"
        return f"https://pawchive.pw/{svc}/{url_type}/{cid}/post/{pid}"

    def _copy_focused_post_url(self):
        post = self._get_focused_post()
        if not post:
            return
        url = self._get_post_direct_url(post)
        QApplication.clipboard().setText(url)
        self.status_label.setText("Copied post URL to clipboard")

    def _open_focused_post_in_browser(self):
        post = self._get_focused_post()
        if not post:
            return
        url = self._get_post_direct_url(post)
        QDesktopServices.openUrl(QUrl(url))

    def _send_focused_post_to_downloader(self):
        post = self._get_focused_post()
        if not post:
            return
        url = self._get_post_direct_url(post)
        cname = (post.get("_creator_name") or "").strip()
        cid = str(post.get("_creator_id") or post.get("user") or "").strip()
        csvc = (post.get("_creator_service") or post.get("service") or "").lower().strip()
        if hasattr(self.parent_app, 'creator_name_cache') and cname and cid:
            if csvc:
                self.parent_app.creator_name_cache[(csvc, cid)] = cname
            self.parent_app.creator_name_cache[cid] = cname
        if cname:
            self.parent_app.last_known_creator_name = cname
        if hasattr(self.parent_app, 'link_input') and self.parent_app.link_input:
            self.parent_app.link_input.setText(url)

        if hasattr(self.parent_app, 'switch_tab'):
            self.parent_app.switch_tab(0)

        if hasattr(self.parent_app, 'log_signal'):
            title = post.get("title") or f"Post {post.get('id')}"
            cname = post.get("_creator_name") or "Creator"
            self.parent_app.log_signal.emit(f"Selected post '{title}' by {cname} ({url})")

    def _on_table_double_clicked(self, index: QModelIndex):
        """Double clicking a post row or card opens the full in-app Post Detail view."""
        if not index.isValid():
            return
        post = self.model.get_post_at(index.row())
        if post:
            self._open_post_detail(post)

    def _toggle_post_check(self, row: int):
        post = self.model.get_post_at(row)
        if not post:
            return
        idx = self.model.index(row, 0)
        curr = self.model.data(idx, Qt.ItemDataRole.CheckStateRole)
        new_val = Qt.CheckState.Unchecked if curr == Qt.CheckState.Checked else Qt.CheckState.Checked
        self.model.setData(idx, new_val, Qt.ItemDataRole.CheckStateRole)

    # ─────────────────────────────────────────────────────────────────────────
    #  Send to Downloader (Posts and Creators)
    # ─────────────────────────────────────────────────────────────────────────

    def _on_send_to_downloader_clicked(self):
        """Sends the selected posts, focused post, or loaded creator to the Downloader tab."""
        selected_posts = self.model.get_selected_posts()
        if len(selected_posts) > 1:
            if hasattr(self.parent_app, 'favorite_download_queue'):
                self.parent_app.favorite_download_queue.clear()
                creator_names = set()
                for p in selected_posts:
                    url = self._get_post_direct_url(p)
                    title = p.get("title") or f"Post_{p.get('id')}"
                    cname = (
                        p.get("_creator_name")
                        or (self.current_creator.get("name") if self.current_creator else "Creator")
                        or "Unknown Creator"
                    )
                    creator_names.add(cname)
                    queue_item = {
                        'url': url,
                        'name': f"[{cname}] {title}",
                        'name_for_folder': cname,
                        'type': 'post',
                        'force_artist_folder': True
                    }
                    self.parent_app.favorite_download_queue.append(queue_item)

                if hasattr(self.parent_app, 'link_input') and self.parent_app.link_input:
                    summary_text = f"{len(selected_posts)} selected posts from {', '.join(creator_names)}"
                    self.parent_app.link_input.setText(summary_text)
                    self.parent_app.last_link_input_text_for_queue_sync = summary_text

                if hasattr(self.parent_app, 'log_signal'):
                    self.parent_app.log_signal.emit(
                        f"Queued {len(selected_posts)} post(s) for download. Click 'Start Download' in Downloader tab."
                    )

                if hasattr(self.parent_app, 'switch_tab'):
                    self.parent_app.switch_tab(0)
                return

        focused = self._get_focused_post()
        if focused:
            self._send_focused_post_to_downloader()
            return

        if self.current_creators:
            self._send_creator_to_downloader()

    def _send_creator_to_downloader(self):
        """Sends currently loaded creator(s) to the Downloader tab."""
        if not self.current_creators:
            return

        if len(self.current_creators) == 1:
            c = self.current_creators[0]
            url = self._get_creator_url(c)
            cname = (c.get("name") or "").strip()
            cid = str(c.get("id") or "").strip()
            csvc = (c.get("service") or "").lower().strip()
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
                name = c.get("name") or "Creator"
                self.parent_app.log_signal.emit(f"Selected creator '{name}' ({url})")
        else:
            creators = self.current_creators
            if hasattr(self.parent_app, 'favorite_download_queue'):
                self.parent_app.favorite_download_queue.clear()
                for c in creators:
                    url = self._get_creator_url(c)
                    cname = c.get("name") or "Unknown Creator"
                    queue_item = {
                        'url': url,
                        'name': cname,
                        'name_for_folder': cname,
                        'type': 'creator_popup_selection',
                        'force_artist_folder': True
                    }
                    self.parent_app.favorite_download_queue.append(queue_item)

                if hasattr(self.parent_app, 'link_input') and self.parent_app.link_input:
                    names_text = ", ".join([c.get("name") or "Creator" for c in creators])
                    self.parent_app.link_input.setText(names_text)
                    self.parent_app.last_link_input_text_for_queue_sync = names_text

                if hasattr(self.parent_app, 'log_signal'):
                    self.parent_app.log_signal.emit(
                        f"Queued {len(creators)} creators for download. Click 'Start Download' in Downloader tab."
                    )

                if hasattr(self.parent_app, 'switch_tab'):
                    self.parent_app.switch_tab(0)

    # Legacy delegates
    def _download_selected_posts(self):
        self._on_send_to_downloader_clicked()

    def _download_entire_creators(self):
        self._send_creator_to_downloader()

