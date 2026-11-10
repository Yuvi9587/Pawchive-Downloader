import os
import json
from collections import OrderedDict
import requests
from datetime import datetime
from urllib.parse import urlparse

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QTableView, QHeaderView, QAbstractItemView, QFrame,
    QProgressBar, QMenu, QApplication, QListView, QStyledItemDelegate,
    QStyle, QStyleOptionViewItem, QStackedWidget
)
from PySide6.QtCore import (
    Qt, QSize, QThread, Signal, QTimer, QAbstractTableModel,
    QAbstractListModel, QModelIndex, QRectF, QUrl, QObject,
    QRunnable, QThreadPool, QEvent, QPoint
)
from PySide6.QtGui import (
    QIcon, QDesktopServices, QAction, QPainter, QColor, QFont,
    QPen, QBrush, QFontMetrics, QPainterPath, QPixmap, QLinearGradient
)

from ..ui.assets import get_asset_path


# ─────────────────────────────────────────────────────────────────────────────
#  Pawchive Supported Platforms
# ─────────────────────────────────────────────────────────────────────────────
SUPPORTED_PLATFORMS = {"patreon", "fanbox", "discord"}


# ─────────────────────────────────────────────────────────────────────────────
#  Background Worker Threads
# ─────────────────────────────────────────────────────────────────────────────

class FetchCreatorsApiThread(QThread):
    """
    Background worker that fetches the latest creators list from Pawchive API (/creators)
    and saves it to local data/creators.json.
    """
    finished_signal = Signal(bool, str, list)
    status_signal = Signal(str)

    def __init__(self, api_url="https://pawchive.pw/api/v1/creators", save_path=None):
        super().__init__()
        self.api_url = api_url
        self.save_path = save_path

    def run(self):
        try:
            self.status_signal.emit("Connecting to Pawchive API (/creators)...")
            import cloudscraper
            scraper = cloudscraper.create_scraper()
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json"
            }
            res = scraper.get(self.api_url, headers=headers, timeout=60)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list):
                    # Filter strictly to Pawchive supported platforms only
                    filtered_data = [
                        c for c in data
                        if isinstance(c, dict) and (c.get("service") or "").lower().strip() in SUPPORTED_PLATFORMS
                    ]
                    if self.save_path:
                        os.makedirs(os.path.dirname(self.save_path), exist_ok=True)
                        with open(self.save_path, "w", encoding="utf-8") as f:
                            json.dump(filtered_data, f)
                    self.finished_signal.emit(
                        True,
                        f"Successfully synced {len(filtered_data):,} creators from API!",
                        filtered_data
                    )
                else:
                    self.finished_signal.emit(
                        False, "Unexpected API format (expected JSON list).", []
                    )
            else:
                self.finished_signal.emit(
                    False, f"API returned status HTTP {res.status_code}", []
                )
        except Exception as e:
            self.finished_signal.emit(False, f"Sync error: {str(e)}", [])


class LoadLocalCreatorsThread(QThread):
    """Loads cached creators from data/creators.json in background without blocking UI."""
    loaded_signal = Signal(list)

    def __init__(self, file_path):
        super().__init__()
        self.file_path = file_path

    def run(self):
        if not self.file_path or not os.path.exists(self.file_path):
            self.loaded_signal.emit([])
            return

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                if data and isinstance(data[0], list):
                    data = data[0]
                filtered_data = [
                    c for c in data
                    if isinstance(c, dict) and (c.get("service") or "").lower().strip() in SUPPORTED_PLATFORMS
                ]
                self.loaded_signal.emit(filtered_data)
            else:
                self.loaded_signal.emit([])
        except Exception:
            self.loaded_signal.emit([])


# ─────────────────────────────────────────────────────────────────────────────
#  Creator Media (Avatars & Banners) Caching & Asynchronous Loader
# ─────────────────────────────────────────────────────────────────────────────

def format_relative_time(epoch: int | float | None) -> str:
    """Formats a unix timestamp into relative time like 'Updated 19d ago' or 'Updated 10h ago'."""
    if not epoch:
        return "Updated —"
    try:
        now = datetime.now().timestamp()
        delta = int(now - epoch)
        if delta < 60:
            return "Updated just now"
        if delta < 3600:
            return f"Updated {delta // 60}m ago"
        if delta < 86400:
            return f"Updated {delta // 3600}h ago"
        if delta < 86400 * 30:
            return f"Updated {delta // 86400}d ago"
        if delta < 86400 * 365:
            return f"Updated {delta // (86400 * 30)}mo ago"
        return f"Updated {delta // (86400 * 365)}y ago"
    except Exception:
        return "Updated —"


def format_short_number(val: int | float | None) -> str:
    """Formats large numbers into compact strings like '52k' or '1.2m'."""
    if not val:
        return "0"
    try:
        val = int(val)
        if val < 1000:
            return str(val)
        if val < 1_000_000:
            scaled = val / 1000
            return f"{scaled:.1f}k" if val < 10_000 else f"{int(scaled)}k"
        return f"{val / 1_000_000:.1f}m"
    except Exception:
        return str(val)


class _MediaTaskSignals(QObject):
    loaded = Signal(str, str, str, str)  # kind, service, creator_id, disk_path
    failed = Signal(str, str, str)       # kind, service, creator_id


class _DownloadMediaTask(QRunnable):
    """Background worker to download creator avatar or banner from Pawchive."""
    def __init__(self, kind: str, service: str, creator_id: str, url: str, disk_path: str, signals: _MediaTaskSignals):
        super().__init__()
        self.kind = kind
        self.service = service
        self.creator_id = creator_id
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
            res = requests.get(self.url, headers=headers, timeout=8)
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
                self.signals.loaded.emit(self.kind, self.service, self.creator_id, self.disk_path)
            else:
                self.signals.failed.emit(self.kind, self.service, self.creator_id)
        except Exception:
            try:
                self.signals.failed.emit(self.kind, self.service, self.creator_id)
            except Exception:
                pass


class CreatorMediaManager(QObject):
    """
    Asynchronous disk & memory caching manager for Pawchive creator avatars and banners.
    Avatars: https://pawchive.pw/icons/{service}/{creator_id}
    Banners: https://pawchive.pw/banners/{service}/{creator_id}
    """
    avatar_loaded = Signal(str, str)
    media_loaded = Signal(str, str, str)  # kind, service, creator_id

    def __init__(self, base_cache_dir: str, max_concurrent: int = 6, parent=None):
        super().__init__(parent)
        self.avatars_cache_dir = os.path.join(base_cache_dir, "avatars")
        self.banners_cache_dir = os.path.join(base_cache_dir, "banners")
        os.makedirs(self.avatars_cache_dir, exist_ok=True)
        os.makedirs(self.banners_cache_dir, exist_ok=True)

        self._max_capacity = 80          # Bound in-memory cache to 30 creators (30 banners + 30 avatars + buffer)
        self._memory_cache = OrderedDict() # key -> QPixmap (LRU order)
        self._in_progress = set()        # (kind, service, creator_id)
        self._failed_keys = set()        # (kind, service, creator_id)

        self._signals = _MediaTaskSignals()
        self._signals.loaded.connect(self._on_task_loaded)
        self._signals.failed.connect(self._on_task_failed)

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_concurrent)

    def get_avatar(self, service: str, creator_id: str, size: int, radius: int = 8) -> QPixmap | None:
        if not service or not creator_id or size <= 0:
            return None

        svc = service.lower().strip()
        cid = str(creator_id).strip()
        key = ("avatar", svc, cid, size)

        # 1. Fast in-memory check (LRU promotion)
        if key in self._memory_cache:
            self._memory_cache.move_to_end(key)
            return self._memory_cache[key]

        # 2. Check disk cache
        disk_path = os.path.join(self.avatars_cache_dir, f"{svc}_{cid}.png")
        if os.path.exists(disk_path):
            pix = QPixmap(disk_path)
            if not pix.isNull():
                rounded = self._make_rounded_pixmap(pix, size, radius)
                self._memory_cache[key] = rounded
                self._memory_cache.move_to_end(key)
                while len(self._memory_cache) > self._max_capacity:
                    self._memory_cache.popitem(last=False)
                return rounded
            else:
                try:
                    os.remove(disk_path)
                except OSError:
                    pass

        # 3. Check failed / in progress
        cache_key = ("avatar", svc, cid)
        if cache_key in self._failed_keys or cache_key in self._in_progress:
            return None

        # 4. Enqueue background download
        self._in_progress.add(cache_key)
        avatar_url = f"https://pawchive.pw/icons/{svc}/{cid}"
        task = _DownloadMediaTask("avatar", svc, cid, avatar_url, disk_path, self._signals)
        self._pool.start(task)
        return None

    def get_banner(self, service: str, creator_id: str, width: int, height: int) -> QPixmap | None:
        if not service or not creator_id or width <= 0 or height <= 0:
            return None

        svc = service.lower().strip()
        cid = str(creator_id).strip()
        key = ("banner", svc, cid, width, height)

        # 1. Fast in-memory check (LRU promotion)
        if key in self._memory_cache:
            self._memory_cache.move_to_end(key)
            return self._memory_cache[key]

        # 2. Check disk cache
        disk_path = os.path.join(self.banners_cache_dir, f"{svc}_{cid}.png")
        if os.path.exists(disk_path):
            pix = QPixmap(disk_path)
            if not pix.isNull():
                scaled = pix.scaled(
                    width, height,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation
                )
                x = max(0, (scaled.width() - width) // 2)
                y = max(0, (scaled.height() - height) // 2)
                cropped = scaled.copy(x, y, width, height)
                self._memory_cache[key] = cropped
                self._memory_cache.move_to_end(key)
                while len(self._memory_cache) > self._max_capacity:
                    self._memory_cache.popitem(last=False)
                return cropped
            else:
                try:
                    os.remove(disk_path)
                except OSError:
                    pass

        # 3. Check failed / in progress
        cache_key = ("banner", svc, cid)
        if cache_key in self._failed_keys or cache_key in self._in_progress:
            return None

        # 4. Enqueue background download
        self._in_progress.add(cache_key)
        banner_url = f"https://pawchive.pw/banners/{svc}/{cid}"
        task = _DownloadMediaTask("banner", svc, cid, banner_url, disk_path, self._signals)
        self._pool.start(task)
        return None

    def unload_offscreen(self, visible_creator_keys: set):
        """
        Evicts in-memory pixmaps for creators not currently in visible_creator_keys.
        visible_creator_keys is a set of (service, creator_id) tuples.
        """
        if not visible_creator_keys:
            return
        keys_to_remove = []
        for k in self._memory_cache.keys():
            if len(k) >= 3:
                svc, cid = k[1], k[2]
                if (svc, cid) not in visible_creator_keys:
                    keys_to_remove.append(k)
        for k in keys_to_remove:
            self._memory_cache.pop(k, None)

    def clear_memory_cache(self):
        """Completely flushes all cached pixmaps from RAM."""
        self._memory_cache.clear()

    def _prune_memory_cache_if_needed(self):
        while len(self._memory_cache) > self._max_capacity:
            self._memory_cache.popitem(last=False)

    def _on_task_loaded(self, kind: str, service: str, creator_id: str, disk_path: str):
        self._in_progress.discard((kind, service, creator_id))
        self.avatar_loaded.emit(service, creator_id)
        self.media_loaded.emit(kind, service, creator_id)

    def _on_task_failed(self, kind: str, service: str, creator_id: str):
        self._in_progress.discard((kind, service, creator_id))
        self._failed_keys.add((kind, service, creator_id))

    @staticmethod
    def _make_rounded_pixmap(pixmap: QPixmap, size: int, radius: int) -> QPixmap:
        scaled = pixmap.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation
        )
        x = (scaled.width() - size) // 2
        y = (scaled.height() - size) // 2
        cropped = scaled.copy(x, y, size, size)

        out = QPixmap(size, size)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, size, size, radius, radius)
        p.setClipPath(path)
        p.drawPixmap(0, 0, cropped)
        p.end()
        return out

    def shutdown(self):
        self._pool.clear()
        self._pool.waitForDone(500)


# Alias for compatibility
CreatorAvatarManager = CreatorMediaManager


# ─────────────────────────────────────────────────────────────────────────────
#  User Favorites Manager (Persistent Local Bookmarks)
# ─────────────────────────────────────────────────────────────────────────────

from ..core.favorites_manager import UserFavoritesManager


# ─────────────────────────────────────────────────────────────────────────────
#  Grid Item Delegate (Modern Banner + Avatar Vertical Cards)
# ─────────────────────────────────────────────────────────────────────────────

class CreatorCardDelegate(QStyledItemDelegate):
    """
    Renders each creator as a modern, responsive vertical card with:
    - Creator Banner image header with dark bottom gradient fade
    - Translucent star/favorite button in top-right (active gold if favorited, outline if not)
    - Overlapping rounded-corner Creator Avatar with dark boundary ring
    - Creator Name (bold, elided)
    - Platform pill badge (e.g. 'Pixiv Fanbox', 'Patreon') & 'Updated X ago'
    - Total archive favorites counter with user icon at bottom
    """

    SERVICE_DISPLAY_NAMES = {
        'fanbox': 'Pixiv Fanbox',
        'patreon': 'Patreon',
        'discord': 'Discord',
    }

    PLATFORM_PILL_STYLES = {
        'patreon': {
            'bg': QColor(232, 91, 70, 36),
            'border': QColor(232, 91, 70, 75),
            'text': QColor('#FFA494'),
        },
        'fanbox': {
            'bg': QColor(0, 164, 228, 36),
            'border': QColor(0, 164, 228, 75),
            'text': QColor('#5CC8F4'),
        },
        'discord': {
            'bg': QColor(88, 101, 242, 36),
            'border': QColor(88, 101, 242, 75),
            'text': QColor('#8C95FF'),
        },
        'other': {
            'bg': QColor(140, 146, 160, 36),
            'border': QColor(140, 146, 160, 75),
            'text': QColor('#C0C5D0'),
        },
    }

    def __init__(self, scale=1.0, avatar_manager=None, fav_manager=None, parent=None):
        super().__init__(parent)
        self.scale = scale
        self.avatar_manager = avatar_manager
        self.fav_manager = fav_manager
        self.card_w = int(220 * scale)
        self.card_h = int(205 * scale)

    def sizeHint(self, option, index):
        return QSize(getattr(self, "card_w", int(220 * self.scale)), getattr(self, "card_h", int(205 * self.scale)))

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            scale = self.scale
            r = option.rect.adjusted(int(4 * scale), int(4 * scale), int(-4 * scale), int(-4 * scale))
            fav_btn_sz = int(24 * scale)
            fav_btn_rect = QRectF(r.right() - fav_btn_sz - int(8 * scale), r.y() + int(8 * scale), fav_btn_sz, fav_btn_sz)
            pos = event.position() if hasattr(event, "position") else event.pos()
            if fav_btn_rect.contains(pos):
                creator = index.data(Qt.ItemDataRole.UserRole)
                if creator and self.fav_manager:
                    self.fav_manager.toggle(creator)
                    model.dataChanged.emit(index, index)
                    return True
        return super().editorEvent(event, model, option, index)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        scale = self.scale
        creator = index.data(Qt.ItemDataRole.UserRole) or {}
        name = creator.get('name') or 'Unknown'
        svc = (creator.get('service') or 'other').lower().strip()
        creator_id = str(creator.get('id') or '')
        fav = creator.get('favorited', 0)

        r = option.rect.adjusted(int(4 * scale), int(4 * scale), int(-4 * scale), int(-4 * scale))
        card_w = r.width()
        card_r = 10 * scale

        is_sel = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hov = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # ── Card Background & Border Colors ──
        if is_sel:
            bg_col = QColor('#182C4A')
            border_col = QColor('#3E8EE6')
            border_w = 1.5
        elif is_hov:
            bg_col = QColor('#252830')
            border_col = QColor('#474C5A')
            border_w = 1.2
        else:
            bg_col = QColor('#1E2026')
            border_col = QColor('#2C2F38')
            border_w = 1.0

        # Draw card container
        painter.setPen(QPen(border_col, border_w))
        painter.setBrush(QBrush(bg_col))
        painter.drawRoundedRect(r, card_r, card_r)

        # ── 1. Header Banner with Smooth Gradient Fade ──
        banner_h = int(82 * scale)
        banner_rect = QRectF(r.x(), r.y(), card_w, banner_h)

        painter.save()
        card_clip = QPainterPath()
        card_clip.addRoundedRect(r, card_r, card_r)
        painter.setClipPath(card_clip)

        banner_pix = None
        if self.avatar_manager:
            banner_pix = self.avatar_manager.get_banner(svc, creator_id, int(card_w), banner_h)

        if banner_pix and not banner_pix.isNull():
            painter.drawPixmap(int(r.x()), int(r.y()), banner_pix)
        else:
            # Fallback modern dark banner gradient
            b_grad = QLinearGradient(r.x(), r.y(), r.right(), r.y() + banner_h)
            b_grad.setColorAt(0.0, QColor('#2A2E38'))
            b_grad.setColorAt(0.5, QColor('#23262F'))
            b_grad.setColorAt(1.0, QColor('#1B1D23'))
            painter.fillRect(banner_rect, QBrush(b_grad))

        # Bottom gradient fade (transparent at top -> card background color at bottom)
        fade_h = int(46 * scale)
        fade_y = r.y() + banner_h - fade_h
        fade_grad = QLinearGradient(r.x(), fade_y, r.x(), r.y() + banner_h)
        fade_grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        fade_grad.setColorAt(0.5, QColor(bg_col.red(), bg_col.green(), bg_col.blue(), 140))
        fade_grad.setColorAt(1.0, bg_col)
        painter.fillRect(QRectF(r.x(), fade_y, card_w, fade_h + 1), QBrush(fade_grad))

        painter.restore()

        # ── 2. Top-Right Favorite Button (Over Banner) ──
        is_fav = False
        if self.fav_manager:
            is_fav = self.fav_manager.is_favorite(svc, creator_id)

        fav_btn_sz = int(24 * scale)
        fav_btn_rect = QRectF(r.right() - fav_btn_sz - int(8 * scale), r.y() + int(8 * scale), fav_btn_sz, fav_btn_sz)

        if is_fav:
            # Favorited by user: warm amber glow & bright gold star
            painter.setPen(QPen(QColor(245, 158, 11, 220), 1.2))
            painter.setBrush(QBrush(QColor(32, 22, 12, 200)))
            painter.drawRoundedRect(fav_btn_rect, 6 * scale, 6 * scale)

            star_sz = int(12 * scale)
            star_icon = QIcon(get_asset_path("assets/Svg/star.svg"))
            star_x = fav_btn_rect.x() + (fav_btn_sz - star_sz) / 2
            star_y = fav_btn_rect.y() + (fav_btn_sz - star_sz) / 2
            painter.drawPixmap(int(star_x), int(star_y), star_icon.pixmap(star_sz, star_sz))
        else:
            # Un-favorited: subtle translucent button with dim outline star
            painter.setPen(QPen(QColor(255, 255, 255, 28), 1.0))
            painter.setBrush(QBrush(QColor(0, 0, 0, 100)))
            painter.drawRoundedRect(fav_btn_rect, 6 * scale, 6 * scale)

            star_sz = int(12 * scale)
            star_icon = QIcon(get_asset_path("assets/Svg/star.svg"))
            star_x = fav_btn_rect.x() + (fav_btn_sz - star_sz) / 2
            star_y = fav_btn_rect.y() + (fav_btn_sz - star_sz) / 2
            painter.save()
            painter.setOpacity(0.35)
            painter.drawPixmap(int(star_x), int(star_y), star_icon.pixmap(star_sz, star_sz))
            painter.restore()

        # ── 3. Creator Avatar (Overlapping Lower Edge of Banner) ──
        avatar_sz = int(48 * scale)
        avatar_x = r.x() + int(12 * scale)
        avatar_y = r.y() + banner_h - int(24 * scale)
        avatar_r = int(8 * scale)

        # Outer ring / border (separates avatar visually from banner image)
        ring_pad = int(3 * scale)
        ring_rect = QRectF(avatar_x - ring_pad, avatar_y - ring_pad, avatar_sz + ring_pad * 2, avatar_sz + ring_pad * 2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(bg_col))
        painter.drawRoundedRect(ring_rect, avatar_r + int(2 * scale), avatar_r + int(2 * scale))

        avatar_rect = QRectF(avatar_x, avatar_y, avatar_sz, avatar_sz)
        pixmap = None
        if self.avatar_manager:
            pixmap = self.avatar_manager.get_avatar(svc, creator_id, avatar_sz, avatar_r)

        if pixmap and not pixmap.isNull():
            painter.drawPixmap(int(avatar_x), int(avatar_y), pixmap)
            painter.setPen(QPen(QColor(255, 255, 255, 25), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(avatar_rect, avatar_r, avatar_r)
        else:
            # Fallback placeholder with initials
            painter.setPen(QPen(QColor('#3E424D'), 1.0))
            painter.setBrush(QBrush(QColor('#252830')))
            painter.drawRoundedRect(avatar_rect, avatar_r, avatar_r)

            parts = [p for p in name.split() if p]
            initials = "".join([p[0].upper() for p in parts[:2]]) if parts else "?"
            font_initials = QFont('Segoe UI', int(11 * scale), QFont.Bold)
            painter.setFont(font_initials)
            painter.setPen(QColor('#E5A198'))
            painter.drawText(avatar_rect, Qt.AlignmentFlag.AlignCenter, initials)

        # ── 4. Creator Name ──
        name_y = avatar_y + avatar_sz + int(8 * scale)
        font_name = QFont('Segoe UI', int(9.5 * scale), QFont.Bold)
        painter.setFont(font_name)
        fm_name = QFontMetrics(font_name)
        avail_w = card_w - int(24 * scale)
        elided_name = fm_name.elidedText(name, Qt.TextElideMode.ElideRight, int(avail_w))
        name_rect = QRectF(r.x() + int(12 * scale), name_y, avail_w, int(20 * scale))
        painter.setPen(QColor('#FFFFFF') if is_sel else QColor('#F0F2F5'))
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_name)

        # ── 5. Platform Pill & 'Updated X ago' ──
        row2_y = name_y + int(22 * scale)

        svc_info = self.PLATFORM_PILL_STYLES.get(svc, self.PLATFORM_PILL_STYLES['other'])
        font_pill = QFont('Segoe UI', int(7.5 * scale), QFont.Bold)
        painter.setFont(font_pill)
        fm_pill = QFontMetrics(font_pill)
        svc_label = self.SERVICE_DISPLAY_NAMES.get(svc, svc.capitalize())
        pill_text_w = fm_pill.horizontalAdvance(svc_label)
        pill_w = pill_text_w + int(10 * scale)
        pill_h = int(17 * scale)
        pill_rect = QRectF(r.x() + int(12 * scale), row2_y, pill_w, pill_h)

        painter.setPen(QPen(svc_info['border'], 1.0))
        painter.setBrush(QBrush(svc_info['bg']))
        painter.drawRoundedRect(pill_rect, 3 * scale, 3 * scale)

        painter.setPen(svc_info['text'])
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, svc_label)

        updated_str = format_relative_time(creator.get("updated"))
        font_upd = QFont('Segoe UI', int(7.5 * scale), QFont.Normal)
        painter.setFont(font_upd)
        upd_x = pill_rect.right() + int(8 * scale)
        upd_w = r.right() - upd_x - int(8 * scale)
        upd_rect = QRectF(upd_x, row2_y, upd_w, pill_h)
        painter.setPen(QColor('#8E94A0'))
        painter.drawText(upd_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, updated_str)

        # ── 6. Bottom Row: Archive Favorites ──
        row3_y = r.bottom() - int(20 * scale)
        user_icon_sz = int(11 * scale)
        user_icon = QIcon(get_asset_path("assets/Svg/user.svg"))
        user_icon_x = r.x() + int(12 * scale)
        user_icon_y = row3_y + int(1 * scale)
        painter.drawPixmap(int(user_icon_x), int(user_icon_y), user_icon.pixmap(user_icon_sz, user_icon_sz))

        fav_text = f"{format_short_number(fav)} archive favorites"
        font_fav = QFont('Segoe UI', int(7.5 * scale), QFont.Normal)
        painter.setFont(font_fav)
        fav_rect = QRectF(user_icon_x + user_icon_sz + int(5 * scale), row3_y - int(1 * scale), card_w - int(32 * scale), int(16 * scale))
        painter.setPen(QColor('#8E94A0'))
        painter.drawText(fav_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, fav_text)

        painter.restore()


# ─────────────────────────────────────────────────────────────────────────────
#  Grid List Model (Virtualized)
# ─────────────────────────────────────────────────────────────────────────────

class CreatorsGridModel(QAbstractListModel):
    """Model backing the QListView grid."""

    def __init__(self, creators=None):
        super().__init__()
        self._data = creators or []

    def set_creators(self, creators):
        self.beginResetModel()
        self._data = creators
        self.endResetModel()

    def get_creator(self, row):
        if 0 <= row < len(self._data):
            return self._data[row]
        return None

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if role == Qt.ItemDataRole.UserRole:
            return self._data[row]
        elif role == Qt.ItemDataRole.DisplayRole:
            return self._data[row].get('name', '')
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Table Model (Alternative View)
# ─────────────────────────────────────────────────────────────────────────────

class CreatorsTableModel(QAbstractTableModel):
    """Virtualized table model for classic list view."""
    HEADERS = ["Fav", "Creator Name", "Platform", "Archive Favorites", "Creator ID", "Last Updated"]

    def __init__(self, creators=None, avatar_manager=None, fav_manager=None, scale=1.0):
        super().__init__()
        self._data = creators or []
        self.avatar_manager = avatar_manager
        self.fav_manager = fav_manager
        self.scale = scale

    def set_creators(self, creators):
        self.beginResetModel()
        self._data = creators
        self.endResetModel()

    def get_creator(self, row):
        if 0 <= row < len(self._data):
            return self._data[row]
        return None

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()
        creator = self._data[row]
        svc = (creator.get("service") or "").lower().strip()
        cid = str(creator.get("id") or "").strip()
        is_fav = self.fav_manager.is_favorite(svc, cid) if self.fav_manager else False

        if role == Qt.ItemDataRole.DecorationRole:
            if col == 0 and is_fav:
                return QIcon(get_asset_path("assets/Svg/star.svg"))
            elif col == 1 and self.avatar_manager:
                icon_sz = int(22 * self.scale)
                pix = self.avatar_manager.get_avatar(svc, cid, icon_sz, int(4 * self.scale))
                if pix and not pix.isNull():
                    return QIcon(pix)

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return ""
            elif col == 1:
                return creator.get("name") or "Unknown"
            elif col == 2:
                return svc.capitalize()
            elif col == 3:
                fav = creator.get("favorited", 0)
                return f"{fav:,}" if fav else "—"
            elif col == 4:
                return cid
            elif col == 5:
                updated = creator.get("updated")
                if updated:
                    try:
                        return datetime.fromtimestamp(updated).strftime("%Y-%m-%d")
                    except Exception:
                        return str(updated)
                return "—"

        elif role == Qt.ItemDataRole.ForegroundRole:
            if col == 0:
                return QColor("#F59E0B") if is_fav else QColor("#555A66")
            elif col == 3:
                return QColor("#9AA0AC")

        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (0, 2, 3, 4, 5):
                return Qt.AlignmentFlag.AlignCenter
            return Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft

        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Main Creators Tab Widget
# ─────────────────────────────────────────────────────────────────────────────

class CreatorsTabWidget(QWidget):
    """
    Responsive Grid Directory for Pawchive Creators.
    Cards automatically wrap and reflow to fit any monitor resolution.
    """

    def __init__(self, parent_app, scale=1.0, parent=None):
        super().__init__(parent)
        self.parent_app = parent_app
        self.scale = scale

        self.all_creators = []
        self.filtered_creators = []

        # 30 creators per page pagination state
        self.page_size = 30
        self.current_page = 1
        self.total_pages = 1

        if hasattr(self.parent_app, 'app_base_dir') and self.parent_app.app_base_dir:
            project_root = self.parent_app.app_base_dir
        else:
            project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
        self.creators_file_path = os.path.join(project_root, "data", "creators.json")
        if not os.path.exists(self.creators_file_path):
            from ..utils.file_utils import get_app_base_path
            bundled_creators = os.path.join(get_app_base_path(), "data", "creators.json")
            if os.path.exists(bundled_creators):
                self.creators_file_path = bundled_creators

        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(220)
        self.debounce_timer.timeout.connect(self._apply_filter)

        # Creator Media (Avatars & Banners) Cache & Async Loader
        self.media_cache_dir = os.path.join(project_root, "appdata", "cache")
        self.avatar_manager = CreatorMediaManager(self.media_cache_dir, max_concurrent=6, parent=self)
        self.avatar_refresh_timer = QTimer(self)
        self.avatar_refresh_timer.setSingleShot(True)
        self.avatar_refresh_timer.setInterval(40)
        self.avatar_refresh_timer.timeout.connect(self._on_avatars_refreshed)
        self.avatar_manager.avatar_loaded.connect(self._queue_avatar_repaint)

        # Debounced viewport prune timer (unloads off-screen avatars & banners on scroll pause)
        self._scroll_prune_timer = QTimer(self)
        self._scroll_prune_timer.setSingleShot(True)
        self._scroll_prune_timer.setInterval(120)
        self._scroll_prune_timer.timeout.connect(self._prune_offscreen_media)

        # User Favorites Manager
        if hasattr(parent_app, "favorites_manager") and getattr(parent_app, "favorites_manager", None):
            self.fav_manager = parent_app.favorites_manager
        else:
            self.fav_manager = UserFavoritesManager(getattr(parent_app, "app_base_dir", project_root), parent=self)
            if parent_app:
                parent_app.favorites_manager = self.fav_manager
        self.fav_manager.favorites_changed.connect(self._on_favorites_changed)

        self._build_ui()
        self._load_initial_data()

    def _build_ui(self):
        scale = self.scale
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(
            int(14 * scale), int(12 * scale), int(14 * scale), int(12 * scale)
        )
        root_layout.setSpacing(int(10 * scale))

        # ── Header bar ────────────────────────────────────────────────────────
        header_bar = QHBoxLayout()
        header_bar.setSpacing(int(10 * scale))

        title_col = QVBoxLayout()
        title_col.setSpacing(int(2 * scale))

        title_h = QHBoxLayout()
        title_h.setSpacing(int(6 * scale))
        title_icon = QLabel()
        title_icon.setPixmap(QIcon(get_asset_path("assets/Svg/user.svg")).pixmap(int(20 * scale), int(20 * scale)))
        title_lbl = QLabel("Creators Directory")
        title_lbl.setStyleSheet(
            f"font-size: {int(13 * scale)}pt; font-weight: bold; color: #EBB3AC;"
        )
        title_h.addWidget(title_icon)
        title_h.addWidget(title_lbl)
        title_h.addStretch(1)
        title_col.addLayout(title_h)

        subtitle_lbl = QLabel("Explore Pawchive creators (Patreon, Fanbox, Fantia, Subscribestar)")
        subtitle_lbl.setStyleSheet("font-size: 8.5pt; color: #8A8D93; font-weight: normal;")
        title_col.addWidget(subtitle_lbl)
        header_bar.addLayout(title_col, 1)

        # Status & Counter
        self.count_label = QLabel("Loading database…")
        self.count_label.setStyleSheet("font-size: 8.5pt; color: #E5A198; font-weight: bold;")
        header_bar.addWidget(self.count_label)

        # View Mode Toggle (Grid vs Table)
        self.view_toggle_btn = QPushButton("  Grid View")
        self.view_toggle_btn.setIcon(QIcon(get_asset_path("assets/Svg/folder.svg")))
        self.view_toggle_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.view_toggle_btn.setToolTip("Toggle between Responsive Grid and Classic Table list")
        self.view_toggle_btn.clicked.connect(self._toggle_view_mode)
        header_bar.addWidget(self.view_toggle_btn)

        # API Sync button
        self.sync_button = QPushButton("  Sync with API (/creators)")
        self.sync_button.setIcon(QIcon(get_asset_path("assets/Svg/sync.svg")))
        self.sync_button.setIconSize(QSize(int(15 * scale), int(15 * scale)))
        self.sync_button.setToolTip("Fetch the latest creators list from https://pawchive.pw/api/v1/creators")
        self.sync_button.clicked.connect(self._start_api_sync)
        header_bar.addWidget(self.sync_button)

        root_layout.addLayout(header_bar)

        # ── Filter Controls Row ───────────────────────────────────────────────
        controls_card = QFrame()
        controls_card.setObjectName("SectionCard")
        controls_card.setStyleSheet(
            "background-color: #31343B; border: 1px solid #424652; border-radius: 6px;"
        )
        ctrl_layout = QHBoxLayout(controls_card)
        ctrl_layout.setContentsMargins(
            int(10 * scale), int(8 * scale), int(10 * scale), int(8 * scale)
        )
        ctrl_layout.setSpacing(int(10 * scale))

        # Search box
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search creator name, ID, or paste URL…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        ctrl_layout.addWidget(self.search_input, 4)

        # Platform filter
        platform_lbl = QLabel("Platform:")
        platform_lbl.setStyleSheet("font-weight: normal; color: #A0A4AC;")
        ctrl_layout.addWidget(platform_lbl)

        self.platform_combo = QComboBox()
        self.platform_combo.addItems([
            "All Platforms",
            "My Favorites",
            "Patreon",
            "Fanbox",
            "Discord"
        ])
        self.platform_combo.setItemIcon(1, QIcon(get_asset_path("assets/Svg/star.svg")))
        self.platform_combo.currentTextChanged.connect(self._apply_filter)
        ctrl_layout.addWidget(self.platform_combo, 2)

        # Sort Order
        sort_lbl = QLabel("Sort:")
        sort_lbl.setStyleSheet("font-weight: normal; color: #A0A4AC;")
        ctrl_layout.addWidget(sort_lbl)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "Most Favorited",
            "Name (A-Z)",
            "Recently Updated"
        ])
        self.sort_combo.currentTextChanged.connect(self._apply_filter)
        ctrl_layout.addWidget(self.sort_combo, 2)

        root_layout.addWidget(controls_card)

        # Progress bar (hidden by default)
        self.sync_progress = QProgressBar()
        self.sync_progress.setRange(0, 0)
        self.sync_progress.setTextVisible(False)
        self.sync_progress.setFixedHeight(int(4 * scale))
        self.sync_progress.hide()
        root_layout.addWidget(self.sync_progress)

        # ── View Stack (Index 0: Responsive Grid | Index 1: Table) ────────────
        self.view_stack = QStackedWidget()

        # 1. Responsive Grid View (QListView in IconMode)
        self.grid_view = QListView()
        self.grid_view.setObjectName("CreatorsGrid")
        self.grid_view.setViewMode(QListView.ViewMode.IconMode)
        self.grid_view.setResizeMode(QListView.ResizeMode.Adjust)  # Key: auto-reflow to monitor resolution!
        self.grid_view.setMovement(QListView.Movement.Static)
        self.grid_view.setUniformItemSizes(True)  # Blazing fast virtualization for 280k+ items
        self.grid_view.setSpacing(0)
        self.grid_view.setStyleSheet(
            "QListView#CreatorsGrid { background-color: transparent; border: none; outline: none; } "
            "QListView#CreatorsGrid::item { border: none; outline: none; padding: 0px; margin: 0px; }"
        )
        self.grid_view.setMouseTracking(True)
        self.grid_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        self.grid_model = CreatorsGridModel([])
        self.grid_view.setModel(self.grid_model)
        self.card_delegate = CreatorCardDelegate(scale, self.avatar_manager, self.fav_manager, self.grid_view)
        self.grid_view.setItemDelegate(self.card_delegate)

        self.grid_view.doubleClicked.connect(self._on_double_click_item)
        self.grid_view.selectionModel().selectionChanged.connect(self._on_grid_selection_changed)
        self.grid_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid_view.customContextMenuRequested.connect(self._show_grid_context_menu)

        self.view_stack.addWidget(self.grid_view)

        # 2. Classic Table View
        self.table_view = QTableView()
        self.table_model = CreatorsTableModel([], self.avatar_manager, self.fav_manager, scale)
        self.table_view.setModel(self.table_model)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setShowGrid(True)
        self.table_view.verticalHeader().setVisible(False)
        self.table_view.doubleClicked.connect(self._on_double_click_item)
        self.table_view.selectionModel().selectionChanged.connect(self._on_table_selection_changed)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._show_table_context_menu)

        th = self.table_view.horizontalHeader()
        th.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        th.setStretchLastSection(False)
        th.setCascadingSectionResizes(False)
        th.resizeSection(0, int(100 * scale))
        th.resizeSection(1, int(340 * scale))
        th.resizeSection(2, int(115 * scale))
        th.resizeSection(3, int(120 * scale))
        th.resizeSection(4, int(120 * scale))

        self.view_stack.addWidget(self.table_view)

        # Dynamic viewport memory pruning on scroll
        self.grid_view.verticalScrollBar().valueChanged.connect(self._on_view_scrolled)
        self.table_view.verticalScrollBar().valueChanged.connect(self._on_view_scrolled)

        root_layout.addWidget(self.view_stack, 1)

        # ── Pagination Bar (30 Creators Per Page) ────────────────────────────
        pagination_card = QFrame()
        pagination_card.setObjectName("PaginationCard")
        pagination_card.setStyleSheet(
            "QFrame#PaginationCard { background-color: #252830; border: 1px solid #363A45; border-radius: 6px; }"
            "QPushButton { background-color: #2D313B; border: 1px solid #434754; border-radius: 4px; padding: 3px 10px; color: #DFE1E5; font-size: 8.5pt; font-weight: 500; }"
            "QPushButton:hover { background-color: #383D4A; border-color: #E5A198; color: #FFFFFF; }"
            "QPushButton:disabled { background-color: #202227; border-color: #2A2D35; color: #555863; }"
            "QLineEdit { background-color: #1A1C22; border: 1px solid #3E4351; border-radius: 4px; color: #DFE1E5; padding: 2px 4px; font-size: 8.5pt; }"
            "QLineEdit:focus { border-color: #E5A198; }"
        )
        pag_layout = QHBoxLayout(pagination_card)
        pag_layout.setContentsMargins(int(10 * scale), int(5 * scale), int(10 * scale), int(5 * scale))
        pag_layout.setSpacing(int(8 * scale))

        self.page_info_label = QLabel("Showing 0–0 of 0 creators")
        self.page_info_label.setStyleSheet("color: #8C929E; font-size: 8.5pt; font-weight: normal;")
        pag_layout.addWidget(self.page_info_label, 1)

        self.first_page_btn = QPushButton("« First")
        self.first_page_btn.setFixedHeight(int(26 * scale))
        self.first_page_btn.setToolTip("Go to first page")
        self.first_page_btn.clicked.connect(self._go_first_page)
        pag_layout.addWidget(self.first_page_btn)

        self.prev_page_btn = QPushButton("‹ Prev")
        self.prev_page_btn.setFixedHeight(int(26 * scale))
        self.prev_page_btn.setToolTip("Go to previous page")
        self.prev_page_btn.clicked.connect(self._go_prev_page)
        pag_layout.addWidget(self.prev_page_btn)

        self.page_num_label = QLabel("Page 1 of 1")
        self.page_num_label.setStyleSheet("color: #E5A198; font-weight: bold; font-size: 8.5pt; padding: 0 4px;")
        pag_layout.addWidget(self.page_num_label)

        jump_lbl = QLabel("Go to:")
        jump_lbl.setStyleSheet("color: #8C929E; font-size: 8pt;")
        pag_layout.addWidget(jump_lbl)

        self.page_jump_input = QLineEdit()
        self.page_jump_input.setPlaceholderText("#")
        self.page_jump_input.setFixedWidth(int(52 * scale))
        self.page_jump_input.setFixedHeight(int(24 * scale))
        self.page_jump_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_jump_input.returnPressed.connect(self._on_jump_page_entered)
        pag_layout.addWidget(self.page_jump_input)

        self.next_page_btn = QPushButton("Next ›")
        self.next_page_btn.setFixedHeight(int(26 * scale))
        self.next_page_btn.setToolTip("Go to next page")
        self.next_page_btn.clicked.connect(self._go_next_page)
        pag_layout.addWidget(self.next_page_btn)

        self.last_page_btn = QPushButton("Last »")
        self.last_page_btn.setFixedHeight(int(26 * scale))
        self.last_page_btn.setToolTip("Go to last page")
        self.last_page_btn.clicked.connect(self._go_last_page)
        pag_layout.addWidget(self.last_page_btn)

        root_layout.addWidget(pagination_card)

        # ── Bottom Action & Info Bar ──────────────────────────────────────────
        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(int(8 * scale))

        self.selection_label = QLabel("Click any creator card to inspect or send to Downloader")
        self.selection_label.setStyleSheet("color: #8C929E; font-size: 8.5pt; font-weight: normal;")
        bottom_bar.addWidget(self.selection_label, 1)

        self.copy_btn = QPushButton("  Copy URL")
        self.copy_btn.setIcon(QIcon(get_asset_path("assets/Svg/link.svg")))
        self.copy_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy_selected_url)
        bottom_bar.addWidget(self.copy_btn)

        self.browser_btn = QPushButton("  Open in Browser")
        self.browser_btn.setIcon(QIcon(get_asset_path("assets/Svg/eye_open.svg")))
        self.browser_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.browser_btn.setEnabled(False)
        self.browser_btn.clicked.connect(self._open_in_browser)
        bottom_bar.addWidget(self.browser_btn)

        self.view_posts_btn = QPushButton("  View Posts")
        self.view_posts_btn.setIcon(QIcon(get_asset_path("assets/Svg/doc.svg")))
        self.view_posts_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.view_posts_btn.setEnabled(False)
        self.view_posts_btn.setToolTip("Browse and select specific posts from this creator in the Posts tab")
        self.view_posts_btn.clicked.connect(self._open_posts_for_selected)
        bottom_bar.addWidget(self.view_posts_btn)

        self.send_btn = QPushButton("  Send to Downloader")
        self.send_btn.setObjectName("PrimaryButton")
        self.send_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.send_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.send_btn.setMinimumHeight(int(30 * scale))
        self.send_btn.setEnabled(False)
        self.send_btn.clicked.connect(self._send_selected_to_downloader)
        bottom_bar.addWidget(self.send_btn)

        root_layout.addLayout(bottom_bar)

    def _adjust_grid_columns(self):
        """Dynamically stretch grid columns to fill 100% of the viewport with zero dead gap."""
        if not hasattr(self, "grid_view") or not hasattr(self, "card_delegate"):
            return
        vp_w = self.grid_view.viewport().width()
        if vp_w <= 100:
            return
        scale = getattr(self, "scale", 1.0)
        base_w = int(220 * scale)
        base_h = int(205 * scale)
        cols = max(1, vp_w // base_w)
        slot_w = max(int(160 * scale), (vp_w - 2) // cols)
        slot_h = int(slot_w * (base_h / base_w))
        self.card_delegate.card_w = slot_w
        self.card_delegate.card_h = slot_h
        self.grid_view.setGridSize(QSize(slot_w, slot_h))

    def _init_table_header_width(self):
        if not getattr(self, "_table_header_initialized", False):
            th = self.table_view.horizontalHeader()
            vw = self.table_view.viewport().width()
            if vw > int(500 * self.scale):
                fixed_sum = int(100 * self.scale) + int(115 * self.scale) + int(120 * self.scale) + int(120 * self.scale)
                name_w = max(int(240 * self.scale), vw - fixed_sum - 2)
                th.resizeSection(1, name_w)
                self._table_header_initialized = True

    def showEvent(self, event):
        super().showEvent(event)
        self._adjust_grid_columns()
        self._init_table_header_width()
        self._scroll_prune_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adjust_grid_columns()
        self._scroll_prune_timer.start()

    def closeEvent(self, event):
        if hasattr(self, 'avatar_manager') and self.avatar_manager:
            self.avatar_manager.shutdown()
        super().closeEvent(event)

    def _on_view_scrolled(self):
        """Debounce viewport checking during rapid scrolling."""
        self._scroll_prune_timer.start()

    def _prune_offscreen_media(self):
        """Unloads avatars and banners that have scrolled outside the visible viewport."""
        if hasattr(self, 'avatar_manager') and self.avatar_manager:
            visible_keys = self.get_visible_creator_keys()
            if visible_keys:
                self.avatar_manager.unload_offscreen(visible_keys)

    def get_visible_creator_keys(self) -> set:
        """Determines the set of (service, creator_id) visible in the viewport plus a small buffer."""
        visible_keys = set()
        current_mode = self.view_stack.currentIndex()
        view = self.grid_view if current_mode == 0 else self.table_view
        model = self.grid_model if current_mode == 0 else self.table_model

        if not model or model.rowCount() == 0:
            return visible_keys

        total = model.rowCount()
        vp = view.viewport()
        vp_w, vp_h = vp.width(), vp.height()
        if vp_w <= 0 or vp_h <= 0:
            return visible_keys

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
            bot_row = min(top_row + 25, total - 1)

        start_row = max(0, min(top_row, bot_row) - 8)
        end_row = min(total - 1, max(top_row, bot_row) + 8)

        for r in range(start_row, end_row + 1):
            c = model.get_creator(r)
            if c and isinstance(c, dict):
                svc = (c.get("service") or "").lower().strip()
                cid = str(c.get("id") or "").strip()
                if svc and cid:
                    visible_keys.add((svc, cid))

        return visible_keys

    def on_tab_deactivated(self):
        """Called when user navigates away from the Creators tab: unloads memory cache immediately."""
        self._scroll_prune_timer.stop()
        if hasattr(self, 'avatar_manager') and self.avatar_manager:
            self.avatar_manager.clear_memory_cache()

    def on_tab_activated(self):
        """Called when user navigates back to Creators tab: re-renders visible viewport from disk cache."""
        self._adjust_grid_columns()
        if hasattr(self, 'view_stack'):
            if self.view_stack.currentIndex() == 0:
                self.grid_view.viewport().update()
            else:
                self.table_view.viewport().update()

    def _queue_avatar_repaint(self, service, creator_id):
        """Coalesce rapid avatar load events to prevent UI paint spikes."""
        if not self.avatar_refresh_timer.isActive():
            self.avatar_refresh_timer.start()

    def _on_avatars_refreshed(self):
        """Repaint active view so downloaded avatars appear smoothly."""
        if self.view_stack.currentIndex() == 0:
            self.grid_view.viewport().update()
        else:
            self.table_view.viewport().update()

    def _toggle_view_mode(self):
        """Toggles between responsive card grid and classic table."""
        current = self.view_stack.currentIndex()
        if current == 0:
            self.view_stack.setCurrentIndex(1)
            self.view_toggle_btn.setText("  Table View")
            self.view_toggle_btn.setIcon(QIcon(get_asset_path("assets/Svg/doc.svg")))
            self._init_table_header_width()
        else:
            self.view_stack.setCurrentIndex(0)
            self.view_toggle_btn.setText("  Grid View")
            self.view_toggle_btn.setIcon(QIcon(get_asset_path("assets/Svg/folder.svg")))
            self._adjust_grid_columns()
        self._scroll_prune_timer.start()

    def _load_initial_data(self):
        self.load_thread = LoadLocalCreatorsThread(self.creators_file_path)
        self.load_thread.loaded_signal.connect(self._on_local_data_loaded)
        self.load_thread.start()

    def _on_local_data_loaded(self, creators):
        self.all_creators = creators
        if creators:
            self.count_label.setText(f"{len(creators):,} creators cached")
            self._apply_filter()
            if hasattr(self.parent_app, 'creator_name_cache') and isinstance(self.parent_app.creator_name_cache, dict):
                for c in creators:
                    cid = str(c.get("id", "")).strip()
                    cname = (c.get("name") or "").strip()
                    csvc = (c.get("service") or "").lower().strip()
                    if cid and cname:
                        if csvc:
                            self.parent_app.creator_name_cache[(csvc, cid)] = cname
                        if cid not in self.parent_app.creator_name_cache:
                            self.parent_app.creator_name_cache[cid] = cname
        else:
            self.count_label.setText("No local database found. Click 'Sync with API' to fetch.")

    def _on_favorites_changed(self):
        self.grid_view.viewport().update()
        self.table_view.viewport().update()
        if "Favorites" in self.platform_combo.currentText():
            self._apply_filter()

    def _on_search_text_changed(self):
        self.debounce_timer.start()

    def _apply_filter(self):
        query = self.search_input.text().strip().lower()
        platform_filter = self.platform_combo.currentText()
        sort_mode = self.sort_combo.currentText()

        # Enforce Pawchive supported platforms strictly
        results = [
            c for c in self.all_creators
            if (c.get("service") or "").lower().strip() in SUPPORTED_PLATFORMS
        ]

        if "Favorites" in platform_filter:
            results = [
                c for c in results
                if self.fav_manager and self.fav_manager.is_favorite(c.get("service"), c.get("id"))
            ]
        elif platform_filter != "All Platforms":
            pf_lower = platform_filter.lower()
            results = [c for c in results if (c.get("service") or "").lower() == pf_lower]

        if query:
            if "pawchive.pw" in query or "kemono" in query or "/" in query:
                parts = [p for p in query.split("/") if p]
                if len(parts) >= 2:
                    q_id = parts[-1]
                    results = [c for c in results if str(c.get("id")) == q_id or query in (c.get("name") or "").lower()]
                else:
                    results = [c for c in results if query in (c.get("name") or "").lower()]
            else:
                results = [
                    c for c in results
                    if query in (c.get("name") or "").lower() or query in str(c.get("id", ""))
                ]

        if sort_mode == "Most Favorited":
            results.sort(key=lambda c: (-c.get("favorited", 0), (c.get("name") or "").lower()))
        elif sort_mode == "Name (A-Z)":
            results.sort(key=lambda c: (c.get("name") or "").lower())
        elif sort_mode == "Recently Updated":
            results.sort(key=lambda c: (-c.get("updated", 0), (c.get("name") or "").lower()))

        self.filtered_creators = results
        self.current_page = 1
        self._load_current_page()

        total = len(self.all_creators)
        shown = len(results)
        if total:
            if shown == total:
                self.count_label.setText(f"{total:,} creators available")
            else:
                self.count_label.setText(f"Found {shown:,} of {total:,} creators")

    # ─────────────────────────────────────────────────────────────────────────
    #  Pagination Methods (30 Creators Per Page)
    # ─────────────────────────────────────────────────────────────────────────

    def _go_first_page(self):
        if self.current_page > 1:
            self.current_page = 1
            self._load_current_page()

    def _go_prev_page(self):
        if self.current_page > 1:
            self.current_page -= 1
            self._load_current_page()

    def _go_next_page(self):
        if self.current_page < self.total_pages:
            self.current_page += 1
            self._load_current_page()

    def _go_last_page(self):
        if self.current_page < self.total_pages:
            self.current_page = self.total_pages
            self._load_current_page()

    def _on_jump_page_entered(self):
        txt = self.page_jump_input.text().strip()
        if txt.isdigit():
            target = int(txt)
            if 1 <= target <= self.total_pages:
                if target != self.current_page:
                    self.current_page = target
                    self._load_current_page()
                return
        self.page_jump_input.setText(str(self.current_page))

    def _load_current_page(self):
        """
        Loads the current 30 creators into the views and clears previous page banners/avatars from RAM.
        """
        total = len(self.filtered_creators)
        self.total_pages = max(1, (total + self.page_size - 1) // self.page_size)
        self.current_page = max(1, min(self.current_page, self.total_pages))

        start_idx = (self.current_page - 1) * self.page_size
        end_idx = min(start_idx + self.page_size, total)
        page_creators = self.filtered_creators[start_idx:end_idx] if total > 0 else []

        # 1. Unload previous page's banners and avatars from RAM immediately
        if hasattr(self, 'avatar_manager') and self.avatar_manager:
            self.avatar_manager.clear_memory_cache()

        # 2. Populate models with the current 30 creators only
        self.grid_model.set_creators(page_creators)
        self.table_model.set_creators(page_creators)
        self._adjust_grid_columns()

        # 3. Reset scroll position to top
        if hasattr(self, 'grid_view') and self.grid_view.verticalScrollBar():
            self.grid_view.verticalScrollBar().setValue(0)
        if hasattr(self, 'table_view') and self.table_view.verticalScrollBar():
            self.table_view.verticalScrollBar().setValue(0)

        # 4. Update pagination controls state
        has_prev = self.current_page > 1
        has_next = self.current_page < self.total_pages
        self.first_page_btn.setEnabled(has_prev)
        self.prev_page_btn.setEnabled(has_prev)
        self.next_page_btn.setEnabled(has_next)
        self.last_page_btn.setEnabled(has_next)

        if total > 0:
            self.page_info_label.setText(f"Showing {start_idx + 1}–{end_idx} of {total:,} creators (30/page)")
            self.page_num_label.setText(f"Page {self.current_page} of {self.total_pages:,}")
        else:
            self.page_info_label.setText("No creators found")
            self.page_num_label.setText("Page 1 of 1")
        self.page_jump_input.setText(str(self.current_page))

        # 5. Clear selection and update selection bar
        if hasattr(self, 'grid_view') and self.grid_view.selectionModel():
            self.grid_view.selectionModel().clearSelection()
        if hasattr(self, 'table_view') and self.table_view.selectionModel():
            self.table_view.selectionModel().clearSelection()
        self._update_selection_ui()

        # 6. Trigger viewport update to load current 30 avatars & banners
        if self.view_stack.currentIndex() == 0:
            self.grid_view.viewport().update()
        else:
            self.table_view.viewport().update()

        self._scroll_prune_timer.start()

    def _get_selected_creators(self):
        if self.view_stack.currentIndex() == 0:
            indexes = self.grid_view.selectionModel().selectedIndexes()
            if not indexes:
                return []
            return [self.grid_model.get_creator(idx.row()) for idx in indexes if idx.isValid()]
        else:
            indexes = self.table_view.selectionModel().selectedRows()
            if not indexes:
                return []
            return [self.table_model.get_creator(idx.row()) for idx in indexes if idx.isValid()]

    def _get_selected_creator(self):
        creators = self._get_selected_creators()
        return creators[0] if creators else None

    def _get_creator_url(self, creator):
        if not creator:
            return ""
        svc = (creator.get("service") or "patreon").lower().strip()
        uid = str(creator.get("id") or "").strip()
        url_type = "server" if svc == "discord" else "user"
        return f"https://pawchive.pw/{svc}/{url_type}/{uid}"

    def _update_selection_ui(self):
        creators = self._get_selected_creators()
        count = len(creators)
        has_sel = count > 0
        self.copy_btn.setEnabled(has_sel)
        self.browser_btn.setEnabled(has_sel)
        self.view_posts_btn.setEnabled(has_sel)
        self.send_btn.setEnabled(has_sel)

        if count == 1:
            creator = creators[0]
            name = creator.get("name") or "Unknown"
            svc = (creator.get("service") or "").capitalize()
            url = self._get_creator_url(creator)
            fav = creator.get("favorited", 0)
            self.selection_label.setText(
                f"Selected: <b>{name}</b> ({svc}) &nbsp;•&nbsp; Favorites: {fav:,} &nbsp;•&nbsp; <code>{url}</code>"
            )
            self.view_posts_btn.setText("  View Posts")
            self.send_btn.setText("  Send to Downloader")
        elif count > 1:
            names = [c.get("name") or "Unknown" for c in creators[:3]]
            more = f" +{count - 3} more" if count > 3 else ""
            summary = ", ".join(names) + more
            self.selection_label.setText(
                f"Selected: <b>{count} creators</b> ({summary})"
            )
            self.view_posts_btn.setText(f"  View Posts ({count})")
            self.send_btn.setText(f"  Send {count} Creators to Downloader")
        else:
            self.selection_label.setText("Click or drag/Ctrl-click to select creators, view posts, or send to downloader")
            self.view_posts_btn.setText("  View Posts")
            self.send_btn.setText("  Send to Downloader")

    def _on_grid_selection_changed(self):
        self._update_selection_ui()

    def _on_table_selection_changed(self):
        self._update_selection_ui()

    def _on_double_click_item(self, index):
        self._open_posts_for_selected()

    def _send_selected_to_downloader(self):
        creators = self._get_selected_creators()
        if not creators:
            return

        if len(creators) == 1:
            creator = creators[0]
            url = self._get_creator_url(creator)
            cname = (creator.get("name") or "").strip()
            cid = str(creator.get("id", "")).strip()
            csvc = (creator.get("service") or "").lower().strip()
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
                name = creator.get("name") or "Creator"
                self.parent_app.log_signal.emit(f"Selected creator '{name}' ({url})")
        else:
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

    def _copy_selected_url(self):
        creators = self._get_selected_creators()
        if not creators:
            return
        urls = [self._get_creator_url(c) for c in creators]
        clipboard = QApplication.clipboard()
        clipboard.setText("\n".join(urls))
        if len(urls) == 1:
            self.selection_label.setText(f"Copied URL to clipboard: <code>{urls[0]}</code>")
        else:
            self.selection_label.setText(f"Copied {len(urls)} URLs to clipboard")

    def _open_in_browser(self):
        creators = self._get_selected_creators()
        if not creators:
            return
        for c in creators[:5]:
            url = self._get_creator_url(c)
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _show_grid_context_menu(self, pos):
        creators = self._get_selected_creators()
        if not creators:
            return
        self._open_context_menu_at(self.grid_view.viewport().mapToGlobal(pos))

    def _show_table_context_menu(self, pos):
        creators = self._get_selected_creators()
        if not creators:
            return
        self._open_context_menu_at(self.table_view.viewport().mapToGlobal(pos))

    def _open_context_menu_at(self, global_pos):
        creators = self._get_selected_creators()
        if not creators:
            return
        count = len(creators)
        menu = QMenu(self)

        dl_text = f"Send {count} Creators to Downloader" if count > 1 else "Send to Downloader"
        send_act = QAction(QIcon(get_asset_path("assets/Svg/download.svg")), dl_text, self)
        send_act.triggered.connect(self._send_selected_to_downloader)
        menu.addAction(send_act)

        posts_text = f"View Posts for {count} Creators..." if count > 1 else "View Creator Posts..."
        posts_act = QAction(QIcon(get_asset_path("assets/Svg/doc.svg")), posts_text, self)
        posts_act.triggered.connect(self._open_posts_for_selected)
        menu.addAction(posts_act)

        if count == 1:
            c = creators[0]
            is_fav = self.fav_manager.is_favorite(c.get("service"), c.get("id"))
            fav_text = "Remove from Favorites" if is_fav else "Add to Favorites"
            fav_act = QAction(QIcon(get_asset_path("assets/Svg/star.svg")), fav_text, self)
            fav_act.triggered.connect(lambda: self.fav_manager.toggle(c))
            menu.addAction(fav_act)

        copy_text = f"Copy {count} Pawchive URLs" if count > 1 else "Copy Pawchive URL"
        copy_act = QAction(QIcon(get_asset_path("assets/Svg/copy.svg")), copy_text, self)
        copy_act.triggered.connect(self._copy_selected_url)
        menu.addAction(copy_act)

        open_text = f"Open {count} in Browser" if count > 1 else "Open in Browser"
        open_act = QAction(QIcon(get_asset_path("assets/Svg/eye_open.svg")), open_text, self)
        open_act.triggered.connect(self._open_in_browser)
        menu.addAction(open_act)

        menu.exec(global_pos)

    def _open_posts_for_selected(self):
        creators = self._get_selected_creators()
        if not creators:
            return
        if hasattr(self.parent_app, 'posts_tab') and hasattr(self.parent_app, 'switch_tab'):
            if hasattr(self.parent_app.posts_tab, '_close_post_detail'):
                self.parent_app.posts_tab._close_post_detail()
            if hasattr(self.parent_app.posts_tab, 'load_creators'):
                self.parent_app.posts_tab.load_creators(creators)
            else:
                self.parent_app.posts_tab.load_creator(creators[0])
            self.parent_app.switch_tab("posts")

    def _start_api_sync(self):
        self.sync_button.setEnabled(False)
        self.sync_progress.show()
        self.count_label.setText("Syncing /creators with Pawchive API…")

        self.sync_thread = FetchCreatorsApiThread(
            api_url="https://pawchive.pw/api/v1/creators",
            save_path=self.creators_file_path
        )
        self.sync_thread.status_signal.connect(lambda msg: self.count_label.setText(msg))
        self.sync_thread.finished_signal.connect(self._on_api_sync_finished)
        self.sync_thread.start()

    def _on_api_sync_finished(self, success, message, data):
        self.sync_button.setEnabled(True)
        self.sync_progress.hide()

        if success and data:
            self.all_creators = [
                c for c in data
                if (c.get("service") or "").lower().strip() in SUPPORTED_PLATFORMS
            ]
            self._apply_filter()
            if hasattr(self.parent_app, 'creator_name_cache') and isinstance(self.parent_app.creator_name_cache, dict):
                for c in self.all_creators:
                    cid = str(c.get("id", "")).strip()
                    cname = (c.get("name") or "").strip()
                    csvc = (c.get("service") or "").lower().strip()
                    if cid and cname:
                        if csvc:
                            self.parent_app.creator_name_cache[(csvc, cid)] = cname
                        if cid not in self.parent_app.creator_name_cache:
                            self.parent_app.creator_name_cache[cid] = cname
            self.count_label.setText(message)
            if hasattr(self.parent_app, 'log_signal'):
                self.parent_app.log_signal.emit(f"Creators directory synced: {len(data):,} creators loaded.")
        else:
            self.count_label.setText(f"{message} (Using cached creators)")
            if hasattr(self.parent_app, 'log_signal'):
                self.parent_app.log_signal.emit(f"API sync warning: {message}")
