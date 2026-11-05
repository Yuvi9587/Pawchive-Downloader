import os
import json
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
    QAbstractListModel, QModelIndex, QRectF
)
from PySide6.QtGui import (
    QIcon, QDesktopServices, QAction, QPainter, QColor, QFont,
    QPen, QBrush, QFontMetrics
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
#  Grid Item Delegate (Responsive Rectangle Cards)
# ─────────────────────────────────────────────────────────────────────────────

class CreatorCardDelegate(QStyledItemDelegate):
    """
    Renders each creator as a modern, responsive rectangle card with:
    - Platform pill badge (custom branded colors)
    - Favorites count (gold star)
    - Creator Name (bold, elided)
    - Creator ID & Double-click action hint
    """

    PLATFORM_COLORS = {
        'patreon': QColor('#E85B46'),
        'fanbox': QColor('#F5A623'),
        'discord': QColor('#5865F2'),
    }

    def __init__(self, scale=1.0, parent=None):
        super().__init__(parent)
        self.scale = scale

    def sizeHint(self, option, index):
        scale = self.scale
        return QSize(int(240 * scale), int(96 * scale))

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        scale = self.scale
        creator = index.data(Qt.ItemDataRole.UserRole) or {}
        name = creator.get('name') or 'Unknown'
        svc = (creator.get('service') or 'other').lower()
        creator_id = str(creator.get('id') or '')
        fav = creator.get('favorited', 0)

        r = option.rect.adjusted(int(4 * scale), int(4 * scale), int(-4 * scale), int(-4 * scale))

        is_sel = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hov = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # ── Card Background & Border ──
        if is_sel:
            bg_col = QColor('#1B3B66')
            border_col = QColor('#3E8EE6')
            border_w = 1.5
        elif is_hov:
            bg_col = QColor('#373B45')
            border_col = QColor('#555D6E')
            border_w = 1.2
        else:
            bg_col = QColor('#2E3138')
            border_col = QColor('#3E424D')
            border_w = 1.0

        painter.setPen(QPen(border_col, border_w))
        painter.setBrush(QBrush(bg_col))
        painter.drawRoundedRect(r, 7 * scale, 7 * scale)

        # ── 1. Platform Pill (Top-Left) ──
        pill_col = self.PLATFORM_COLORS.get(svc, QColor('#4E5564'))
        font_pill = QFont('Segoe UI', int(7.5 * scale), QFont.Bold)
        painter.setFont(font_pill)
        svc_text = svc.capitalize()
        fm_pill = QFontMetrics(font_pill)
        text_w = fm_pill.horizontalAdvance(svc_text)
        pill_w = text_w + int(12 * scale)
        pill_h = int(18 * scale)
        pill_rect = QRectF(r.x() + int(10 * scale), r.y() + int(9 * scale), pill_w, pill_h)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(pill_col))
        painter.drawRoundedRect(pill_rect, 4 * scale, 4 * scale)

        painter.setPen(QColor('#FFFFFF'))
        painter.drawText(pill_rect, Qt.AlignCenter, svc_text)

        # ── 2. Favorites (Top-Right) ──
        font_fav = QFont('Segoe UI', int(8 * scale), QFont.Bold)
        painter.setFont(font_fav)
        fav_val_text = f"{fav:,}" if fav else "—"
        fm_fav = QFontMetrics(font_fav)
        fav_w = fm_fav.horizontalAdvance(fav_val_text)

        # Draw SVG star icon
        star_sz = int(12 * scale)
        star_icon = QIcon(get_asset_path("assets/Svg/star.svg"))
        star_x = r.right() - fav_w - star_sz - int(14 * scale)
        star_y = r.y() + int(9 * scale) + (pill_h - star_sz) / 2
        painter.drawPixmap(int(star_x), int(star_y), star_icon.pixmap(star_sz, star_sz))

        fav_rect = QRectF(r.right() - fav_w - int(10 * scale), r.y() + int(9 * scale), fav_w, pill_h)
        painter.setPen(QColor('#F0C24B'))
        painter.drawText(fav_rect, Qt.AlignVCenter | Qt.AlignRight, fav_val_text)

        # ── 3. Creator Name (Middle) ──
        font_name = QFont('Segoe UI', int(10 * scale), QFont.Bold)
        painter.setFont(font_name)
        fm_name = QFontMetrics(font_name)
        avail_w = r.width() - int(20 * scale)
        elided_name = fm_name.elidedText(name, Qt.TextElideMode.ElideRight, int(avail_w))
        name_rect = QRectF(r.x() + int(10 * scale), r.y() + int(34 * scale), avail_w, int(22 * scale))
        painter.setPen(QColor('#FFFFFF') if is_sel else QColor('#F0F2F5'))
        painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, elided_name)

        # ── 4. Bottom Row: ID & Prompt ──
        font_sub = QFont('Segoe UI', int(7.5 * scale), QFont.Normal)
        painter.setFont(font_sub)
        id_text = f"ID: {creator_id}"
        id_rect = QRectF(r.x() + int(10 * scale), r.bottom() - int(24 * scale), int(120 * scale), int(18 * scale))
        painter.setPen(QColor('#8E94A0'))
        painter.drawText(id_rect, Qt.AlignVCenter | Qt.AlignLeft, id_text)

        action_text = "Double-click"
        act_rect = QRectF(r.right() - int(110 * scale), r.bottom() - int(24 * scale), int(100 * scale), int(18 * scale))
        painter.setPen(QColor('#7EB6E0') if is_sel else QColor('#586C85'))
        painter.drawText(act_rect, Qt.AlignVCenter | Qt.AlignRight, action_text)

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
    HEADERS = ["Favorites", "Creator Name", "Platform", "Creator ID", "Last Updated"]

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

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()
        creator = self._data[row]

        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return QIcon(get_asset_path("assets/Svg/star.svg"))

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                fav = creator.get("favorited", 0)
                return f"{fav:,}" if fav else "—"
            elif col == 1:
                return creator.get("name") or "Unknown"
            elif col == 2:
                svc = creator.get("service") or ""
                return svc.capitalize()
            elif col == 3:
                return str(creator.get("id", ""))
            elif col == 4:
                updated = creator.get("updated")
                if updated:
                    try:
                        return datetime.fromtimestamp(updated).strftime("%Y-%m-%d")
                    except Exception:
                        return str(updated)
                return "—"

        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (0, 2, 3, 4):
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

        if hasattr(self.parent_app, 'app_base_dir') and self.parent_app.app_base_dir:
            project_root = self.parent_app.app_base_dir
        else:
            project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
        self.creators_file_path = os.path.join(project_root, "data", "creators.json")

        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(220)
        self.debounce_timer.timeout.connect(self._apply_filter)

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
            f"font-size: {int(13 * scale)}pt; font-weight: bold; color: #9BBCE8;"
        )
        title_h.addWidget(title_icon)
        title_h.addWidget(title_lbl)
        title_h.addStretch(1)
        title_col.addLayout(title_h)

        subtitle_lbl = QLabel("Explore Pawchive creators (Patreon, Fanbox, Discord)")
        subtitle_lbl.setStyleSheet("font-size: 8.5pt; color: #8A8D93; font-weight: normal;")
        title_col.addWidget(subtitle_lbl)
        header_bar.addLayout(title_col, 1)

        # Status & Counter
        self.count_label = QLabel("Loading database…")
        self.count_label.setStyleSheet("font-size: 8.5pt; color: #7EB6E0; font-weight: bold;")
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
            "Patreon",
            "Fanbox",
            "Discord"
        ])
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
        self.grid_view.setSpacing(int(8 * scale))
        self.grid_view.setMouseTracking(True)
        self.grid_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        self.grid_model = CreatorsGridModel([])
        self.grid_view.setModel(self.grid_model)
        self.card_delegate = CreatorCardDelegate(scale, self.grid_view)
        self.grid_view.setItemDelegate(self.card_delegate)

        self.grid_view.doubleClicked.connect(self._on_double_click_item)
        self.grid_view.selectionModel().selectionChanged.connect(self._on_grid_selection_changed)
        self.grid_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid_view.customContextMenuRequested.connect(self._show_grid_context_menu)

        self.view_stack.addWidget(self.grid_view)

        # 2. Classic Table View
        self.table_view = QTableView()
        self.table_model = CreatorsTableModel([])
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

        root_layout.addWidget(self.view_stack, 1)

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
        self._init_table_header_width()

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

    def _load_initial_data(self):
        self.load_thread = LoadLocalCreatorsThread(self.creators_file_path)
        self.load_thread.loaded_signal.connect(self._on_local_data_loaded)
        self.load_thread.start()

    def _on_local_data_loaded(self, creators):
        self.all_creators = creators
        if creators:
            self.count_label.setText(f"{len(creators):,} creators cached")
            self._apply_filter()
        else:
            self.count_label.setText("No local database found. Click 'Sync with API' to fetch.")

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

        if platform_filter != "All Platforms":
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
        self.grid_model.set_creators(results)
        self.table_model.set_creators(results)

        total = len(self.all_creators)
        shown = len(results)
        if total:
            if shown == total:
                self.count_label.setText(f"{total:,} creators available")
            else:
                self.count_label.setText(f"Showing {shown:,} of {total:,} creators")

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
            self.send_btn.setText(f"  Download {count} Creators")
        else:
            self.selection_label.setText("Click or drag/Ctrl-click to select creators, view posts, or download")
            self.view_posts_btn.setText("  View Posts")
            self.send_btn.setText("  Send to Downloader")

    def _on_grid_selection_changed(self):
        self._update_selection_ui()

    def _on_table_selection_changed(self):
        self._update_selection_ui()

    def _on_double_click_item(self, index):
        self._send_selected_to_downloader()

    def _send_selected_to_downloader(self):
        creators = self._get_selected_creators()
        if not creators:
            return

        if len(creators) == 1:
            creator = creators[0]
            url = self._get_creator_url(creator)
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
            self.count_label.setText(message)
            if hasattr(self.parent_app, 'log_signal'):
                self.parent_app.log_signal.emit(f"Creators directory synced: {len(data):,} creators loaded.")
        else:
            self.count_label.setText(f"{message} (Using cached creators)")
            if hasattr(self.parent_app, 'log_signal'):
                self.parent_app.log_signal.emit(f"API sync warning: {message}")
