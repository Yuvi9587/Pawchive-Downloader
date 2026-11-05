import os
import re
import json
from datetime import datetime
from urllib.parse import urlparse

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QTableView, QHeaderView, QAbstractItemView, QFrame,
    QProgressBar, QMenu, QApplication, QDialog, QMessageBox
)
from PySide6.QtCore import (
    Qt, QSize, QThread, Signal, QAbstractTableModel, QModelIndex, QTimer, QUrl
)
from PySide6.QtGui import (
    QIcon, QColor, QFont, QDesktopServices
)

from ..ui.assets import get_asset_path


SUPPORTED_PLATFORMS = {"patreon", "fanbox", "discord"}


# ─────────────────────────────────────────────────────────────────────────────
#  URL Helper for Creator & Post Parsing
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
    # Match service, user/server, creator_id, and optional post_id
    pattern = r'/(patreon|fanbox|discord)/(?:user|server)/([a-zA-Z0-9_\-]+)(?:/post/([a-zA-Z0-9_\-]+))?'
    match = re.search(pattern, url_str, re.IGNORECASE)
    if match:
        svc = match.group(1).lower()
        cid = match.group(2)
        pid = match.group(3) if match.group(3) else None
        return svc, cid, pid
    return None, None, None


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
                file_info = post.get("file")
                attachments = post.get("attachments") or []
                parts = []
                if file_info and isinstance(file_info, dict) and file_info.get("name"):
                    parts.append("1 main file")
                if attachments and isinstance(attachments, list):
                    count = len(attachments)
                    parts.append(f"{count} attachment{'s' if count != 1 else ''}")
                if not parts:
                    return "Text / Content only"
                return " · ".join(parts)
            elif col == 5:
                return pid

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (0, 3, 5):
                return Qt.AlignmentFlag.AlignCenter
            return Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 1:
                return QColor("#A8C7FA")  # Creator name in soft bright blue
            elif col == 2:
                return QColor("#DFE1E5")  # Post title
            elif col == 3:
                return QColor("#8C929E")  # Published date
            elif col == 4:
                return QColor("#7EB6E0")  # Media count
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
        title.setStyleSheet(f"font-size: {int(11 * scale)}pt; font-weight: bold; color: #9BBCE8;")
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
            "color: #7EB6E0; font-size: 8.5pt; font-weight: normal; background-color: #23262C; "
            "border: 1px solid #383C45; border-radius: 4px; padding: 5px 8px;"
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
    Posts directory tab: allows searching/selecting a creator, loading their
    posts from the Pawchive API, inspecting post contents, and downloading
    specific selected posts.
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

        self._build_ui()
        self._load_local_creators_cache()

    def _build_ui(self):
        scale = self.scale
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(
            int(14 * scale), int(12 * scale), int(14 * scale), int(12 * scale)
        )
        root_layout.setSpacing(int(8 * scale))

        # ── 1. Header Bar ─────────────────────────────────────────────────────
        header_bar = QHBoxLayout()
        header_bar.setSpacing(int(10 * scale))

        title_col = QVBoxLayout()
        title_col.setSpacing(int(2 * scale))

        title_h = QHBoxLayout()
        title_h.setSpacing(int(6 * scale))
        title_icon = QLabel()
        title_icon.setPixmap(QIcon(get_asset_path("assets/Svg/doc.svg")).pixmap(int(20 * scale), int(20 * scale)))
        title_lbl = QLabel("Creator Posts")
        title_lbl.setStyleSheet(
            f"font-size: {int(13 * scale)}pt; font-weight: bold; color: #9BBCE8;"
        )
        title_h.addWidget(title_icon)
        title_h.addWidget(title_lbl)
        title_h.addStretch(1)
        title_col.addLayout(title_h)

        subtitle_lbl = QLabel("Select one or multiple Pawchive creators to inspect posts and download specific items.")
        subtitle_lbl.setStyleSheet("font-size: 8.5pt; color: #8A8D93; font-weight: normal;")
        title_col.addWidget(subtitle_lbl)
        header_bar.addLayout(title_col, 1)

        self.status_label = QLabel("No creator selected")
        self.status_label.setStyleSheet("font-size: 8.5pt; color: #7EB6E0; font-weight: bold;")
        header_bar.addWidget(self.status_label)

        root_layout.addLayout(header_bar)

        # ── 2. Creator Selection Card ─────────────────────────────────────────
        selector_card = QFrame()
        selector_card.setObjectName("SectionCard")
        selector_card.setStyleSheet(
            "background-color: #31343B; border: 1px solid #424652; border-radius: 6px;"
        )
        sel_layout = QVBoxLayout(selector_card)
        sel_layout.setContentsMargins(
            int(10 * scale), int(8 * scale), int(10 * scale), int(8 * scale)
        )
        sel_layout.setSpacing(int(6 * scale))

        row1 = QHBoxLayout()
        row1.setSpacing(int(6 * scale))

        # Platform combo
        self.platform_combo = QComboBox()
        self.platform_combo.addItems(["All Platforms", "Patreon", "Fanbox", "Discord"])
        self.platform_combo.setToolTip("Platform / Service filter for creators")
        row1.addWidget(self.platform_combo)

        # Search / URL input
        self.creator_input = QLineEdit()
        self.creator_input.setPlaceholderText(
            "Enter creator name(s), ID(s), or URLs (comma-separated)..."
        )
        self.creator_input.returnPressed.connect(self._on_load_posts_clicked)
        row1.addWidget(self.creator_input, 1)

        # Choose Creator Button
        self.pick_btn = QPushButton("  Choose Creator(s)...")
        self.pick_btn.setIcon(QIcon(get_asset_path("assets/Svg/search.svg")))
        self.pick_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.pick_btn.setToolTip("Browse and pick multiple creators from 98,000+ cached database")
        self.pick_btn.clicked.connect(self._open_creator_picker)
        row1.addWidget(self.pick_btn)

        # Load Posts Button
        self.load_btn = QPushButton("  Load Posts")
        self.load_btn.setObjectName("PrimaryButton")
        self.load_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.load_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.load_btn.setMinimumHeight(int(28 * scale))
        self.load_btn.setToolTip("Fetch posts for all active creators from Pawchive API")
        self.load_btn.clicked.connect(self._on_load_posts_clicked)
        row1.addWidget(self.load_btn)

        sel_layout.addLayout(row1)

        # Creator active info pill with Clear button
        active_row = QHBoxLayout()
        active_row.setSpacing(int(6 * scale))
        self.active_creator_label = QLabel("Tip: Click 'Choose Creator(s)...' or paste creator links (comma-separated) to load posts.")
        self.active_creator_label.setStyleSheet("color: #9AA0AC; font-size: 8pt; font-weight: normal;")
        active_row.addWidget(self.active_creator_label, 1)

        self.clear_creators_btn = QPushButton("Clear")
        self.clear_creators_btn.setFixedHeight(int(22 * scale))
        self.clear_creators_btn.setVisible(False)
        self.clear_creators_btn.setToolTip("Reset loaded creators and clear posts table")
        self.clear_creators_btn.clicked.connect(self._clear_creators)
        active_row.addWidget(self.clear_creators_btn)

        sel_layout.addLayout(active_row)

        root_layout.addWidget(selector_card)

        # ── 3. Post Filter & Selection Toolbar ─────────────────────────────────
        toolbar_card = QFrame()
        toolbar_card.setStyleSheet("background-color: #272A30; border: 1px solid #383C45; border-radius: 5px;")
        tool_layout = QHBoxLayout(toolbar_card)
        tool_layout.setContentsMargins(int(8 * scale), int(6 * scale), int(8 * scale), int(6 * scale))
        tool_layout.setSpacing(int(6 * scale))

        # Search within posts (searches entire creator catalog on Pawchive smoothly)
        self.post_search_input = QLineEdit()
        self.post_search_input.setPlaceholderText("Search all posts across creator archive by keyword (e.g. art, comic, pack)...")
        self.post_search_input.setClearButtonEnabled(True)
        tool_layout.addWidget(self.post_search_input, 1)

        self.search_btn = QPushButton("  Search")
        self.search_btn.setIcon(QIcon(get_asset_path("assets/Svg/search.svg")))
        self.search_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.search_btn.setFixedHeight(int(24 * scale))
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
        self.select_all_btn.setFixedHeight(int(24 * scale))
        self.select_all_btn.clicked.connect(lambda: self.model.select_all(True))
        tool_layout.addWidget(self.select_all_btn)

        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.setFixedHeight(int(24 * scale))
        self.deselect_all_btn.clicked.connect(lambda: self.model.select_all(False))
        tool_layout.addWidget(self.deselect_all_btn)

        self.invert_btn = QPushButton("Invert")
        self.invert_btn.setFixedHeight(int(24 * scale))
        self.invert_btn.clicked.connect(lambda: self.model.invert_selection())
        tool_layout.addWidget(self.invert_btn)

        # Pagination controls
        self.prev_page_btn = QPushButton("Previous 50")
        self.prev_page_btn.setFixedHeight(int(24 * scale))
        self.prev_page_btn.setEnabled(False)
        self.prev_page_btn.clicked.connect(self._load_prev_page)
        tool_layout.addWidget(self.prev_page_btn)

        self.page_info_lbl = QLabel("Offset: 0")
        self.page_info_lbl.setStyleSheet("color: #7EB6E0; font-weight: bold; font-size: 8pt;")
        tool_layout.addWidget(self.page_info_lbl)

        self.next_page_btn = QPushButton("Next 50")
        self.next_page_btn.setFixedHeight(int(24 * scale))
        self.next_page_btn.setEnabled(False)
        self.next_page_btn.clicked.connect(self._load_next_page)
        tool_layout.addWidget(self.next_page_btn)

        self.load_more_btn = QPushButton("Load More (+50)")
        self.load_more_btn.setFixedHeight(int(24 * scale))
        self.load_more_btn.setEnabled(False)
        self.load_more_btn.setToolTip("Appends the next 50 posts without clearing currently loaded ones")
        self.load_more_btn.clicked.connect(self._load_more_posts)
        tool_layout.addWidget(self.load_more_btn)

        root_layout.addWidget(toolbar_card)

        # ── 4. Posts Table View ───────────────────────────────────────────────
        self.model = PostsTableModel([], on_selection_changed=self._update_selection_counter)
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

        root_layout.addWidget(self.table_view, 1)

        # ── 5. Bottom Action Bar ──────────────────────────────────────────────
        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(int(8 * scale))

        self.selection_counter_lbl = QLabel("0 posts loaded | 0 selected")
        self.selection_counter_lbl.setStyleSheet("color: #8C929E; font-size: 8.5pt;")
        bottom_bar.addWidget(self.selection_counter_lbl, 1)

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

        self.send_single_btn = QPushButton("  Send to Downloader")
        self.send_single_btn.setIcon(QIcon(get_asset_path("assets/Svg/link.svg")))
        self.send_single_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.send_single_btn.setEnabled(False)
        self.send_single_btn.setToolTip("Sends the selected post URL to the main Downloader tab")
        self.send_single_btn.clicked.connect(self._send_focused_post_to_downloader)
        bottom_bar.addWidget(self.send_single_btn)

        self.download_creators_btn = QPushButton("  Download Entire Creator")
        self.download_creators_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.download_creators_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.download_creators_btn.setEnabled(False)
        self.download_creators_btn.setToolTip("Queue entire active creator(s) for complete download in the main Downloader tab")
        self.download_creators_btn.clicked.connect(self._download_entire_creators)
        bottom_bar.addWidget(self.download_creators_btn)

        self.download_selected_btn = QPushButton("  Download Selected Posts")
        self.download_selected_btn.setObjectName("PrimaryButton")
        self.download_selected_btn.setMinimumHeight(int(32 * scale))
        self.download_selected_btn.setEnabled(False)
        self.download_selected_btn.clicked.connect(self._download_selected_posts)
        bottom_bar.addWidget(self.download_selected_btn)

        root_layout.addLayout(bottom_bar)

        # Connect row selection changes to enable focused buttons
        self.table_view.selectionModel().selectionChanged.connect(self._on_table_row_selection_changed)

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

    # ─────────────────────────────────────────────────────────────────────────
    #  Creator Cache & Picker
    # ─────────────────────────────────────────────────────────────────────────

    def _load_local_creators_cache(self):
        """Loads data/creators.json asynchronously for the creator picker."""
        creators_path = os.path.join(
            getattr(self.parent_app, 'app_base_dir', '.'), "data", "creators.json"
        )
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
            self.download_creators_btn.setEnabled(False)
            self.download_creators_btn.setText("  Download Entire Creator")
        elif count == 1:
            c = self.current_creators[0]
            name = c.get("name") or "Unknown"
            svc = (c.get("service") or "patreon").capitalize()
            cid = c.get("id") or ""
            self.active_creator_label.setText(
                f"Active Creator: <b>{name}</b> &nbsp;|&nbsp; Platform: <b>{svc}</b> &nbsp;|&nbsp; Creator ID: <code>{cid}</code>"
            )
            self.clear_creators_btn.setVisible(True)
            self.download_creators_btn.setEnabled(True)
            self.download_creators_btn.setText("  Download Entire Creator")
        else:
            names = [f"<b>{c.get('name') or 'Unknown'}</b> [{(c.get('service') or '').capitalize()}]" for c in self.current_creators[:4]]
            more = f" +{count - 4} more" if count > 4 else ""
            summary = ", ".join(names) + more
            self.active_creator_label.setText(
                f"Active (<b>{count} creators</b>): {summary}"
            )
            self.clear_creators_btn.setVisible(True)
            self.download_creators_btn.setEnabled(True)
            self.download_creators_btn.setText(f"  Download {count} Entire Creators")

    def _clear_creators(self):
        """Clears all loaded creators and empties the posts table."""
        self.current_creators = []
        self.current_creator = None
        self.current_search_query = ""
        self._initial_posts = []
        self.creator_input.clear()
        self.post_search_input.clear()
        self.model.set_posts([])
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

        self.current_creators = list(creators)
        self.current_creator = creators[0]
        self.current_search_query = ""
        self._initial_posts = []
        self.post_search_input.clear()

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
                f"<b>{total}</b> post(s) matching \"{self.current_search_query}\"{c_info} | <b>{selected}</b> selected for download"
            )
        elif filtered != total:
            self.selection_counter_lbl.setText(
                f"Showing {filtered} of {total} posts{c_info} | <b>{selected}</b> selected for download"
            )
        else:
            self.selection_counter_lbl.setText(
                f"{total} posts loaded{c_info} | <b>{selected}</b> selected for download"
            )

        self.download_selected_btn.setEnabled(selected > 0)
        self.download_creators_btn.setEnabled(c_count > 0)
        if c_count > 1:
            self.download_creators_btn.setText(f"  Download {c_count} Entire Creators")
        else:
            self.download_creators_btn.setText("  Download Entire Creator")

    def _on_table_row_selection_changed(self):
        sm = self.table_view.selectionModel()
        has_sel = sm and sm.hasSelection()
        self.copy_post_url_btn.setEnabled(has_sel)
        self.browser_post_btn.setEnabled(has_sel)
        self.send_single_btn.setEnabled(has_sel)

    def _get_focused_post(self):
        sm = self.table_view.selectionModel()
        if not sm or not sm.hasSelection():
            return None
        row = sm.selectedRows()[0].row()
        return self.model.get_post_at(row)

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
        if hasattr(self.parent_app, 'link_input') and self.parent_app.link_input:
            self.parent_app.link_input.setText(url)

        if hasattr(self.parent_app, 'switch_tab'):
            self.parent_app.switch_tab(0)

        if hasattr(self.parent_app, 'log_signal'):
            title = post.get("title") or f"Post {post.get('id')}"
            cname = post.get("_creator_name") or "Creator"
            self.parent_app.log_signal.emit(f"Selected post '{title}' by {cname} ({url})")

    def _on_table_double_clicked(self, index: QModelIndex):
        """Double clicking a post row sends it to the Downloader."""
        self._send_focused_post_to_downloader()

    def _show_context_menu(self, pos):
        index = self.table_view.indexAt(pos)
        if not index.isValid():
            return

        post = self.model.get_post_at(index.row())
        if not post:
            return

        menu = QMenu(self)
        dl_act = menu.addAction(QIcon(get_asset_path("assets/Svg/download.svg")), "Download Selected Post(s)")
        dl_act.triggered.connect(self._download_selected_posts)
        menu.addSeparator()

        send_act = menu.addAction(QIcon(get_asset_path("assets/Svg/link.svg")), "Send to Downloader Tab")
        send_act.triggered.connect(self._send_focused_post_to_downloader)

        copy_act = menu.addAction(QIcon(get_asset_path("assets/Svg/copy.svg")), "Copy Post URL")
        copy_act.triggered.connect(self._copy_focused_post_url)

        browser_act = menu.addAction(QIcon(get_asset_path("assets/Svg/eye_open.svg")), "Open in Browser (pawchive.pw)")
        browser_act.triggered.connect(self._open_focused_post_in_browser)

        menu.addSeparator()
        if self.current_creators:
            c_text = f"Download {len(self.current_creators)} Entire Creators" if len(self.current_creators) > 1 else "Download Entire Creator"
            dl_creators_act = menu.addAction(QIcon(get_asset_path("assets/Svg/download.svg")), c_text)
            dl_creators_act.triggered.connect(self._download_entire_creators)
            menu.addSeparator()

        toggle_check_act = menu.addAction("Toggle Checkmark Selection")
        toggle_check_act.triggered.connect(
            lambda: self._toggle_post_check(index.row())
        )

        menu.exec(self.table_view.viewport().mapToGlobal(pos))

    def _toggle_post_check(self, row: int):
        post = self.model.get_post_at(row)
        if not post:
            return
        idx = self.model.index(row, 0)
        curr = self.model.data(idx, Qt.ItemDataRole.CheckStateRole)
        new_val = Qt.CheckState.Unchecked if curr == Qt.CheckState.Checked else Qt.CheckState.Checked
        self.model.setData(idx, new_val, Qt.ItemDataRole.CheckStateRole)

    # ─────────────────────────────────────────────────────────────────────────
    #  Batch Download of Selected Specific Posts Across Creators
    # ─────────────────────────────────────────────────────────────────────────

    def _download_selected_posts(self):
        selected_posts = self.model.get_selected_posts()
        if not selected_posts:
            focused = self._get_focused_post()
            if focused:
                selected_posts = [focused]
            else:
                QMessageBox.information(
                    self, "No Posts Selected",
                    "Please check the box next to at least one post to download."
                )
                return

        count = len(selected_posts)
        creator_names = set()

        if hasattr(self.parent_app, 'favorite_download_queue'):
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

            if hasattr(self.parent_app, 'log_signal'):
                summary = f"across {len(creator_names)} creator(s)" if len(creator_names) > 1 else f"for '{next(iter(creator_names))}'"
                self.parent_app.log_signal.emit(
                    f"Queued {count} specific post(s) {summary} for download."
                )

            # Trigger queue processing if idle
            if hasattr(self.parent_app, '_process_next_favorite_download'):
                if not getattr(self.parent_app, 'is_processing_favorites_queue', False):
                    self.parent_app._process_next_favorite_download()

            # Switch to Downloader tab so user can monitor progress
            if hasattr(self.parent_app, 'switch_tab'):
                self.parent_app.switch_tab(0)
        else:
            first_url = self._get_post_direct_url(selected_posts[0])
            if hasattr(self.parent_app, 'link_input') and self.parent_app.link_input:
                self.parent_app.link_input.setText(first_url)
            if hasattr(self.parent_app, 'switch_tab'):
                self.parent_app.switch_tab(0)
            if hasattr(self.parent_app, 'start_download'):
                self.parent_app.start_download()

    # ─────────────────────────────────────────────────────────────────────────
    #  Batch Download of Entire Creators (Full Archive)
    # ─────────────────────────────────────────────────────────────────────────

    def _download_entire_creators(self):
        """Queues all currently active creators for complete download in the main tab."""
        if not self.current_creators:
            QMessageBox.information(
                self, "No Creators Loaded",
                "Please select or load at least one creator first."
            )
            return

        creators = self.current_creators
        count = len(creators)

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
                    f"Queued {count} entire creator(s) for complete download. Starting queue..."
                )

            if hasattr(self.parent_app, '_process_next_favorite_download'):
                if not getattr(self.parent_app, 'is_processing_favorites_queue', False):
                    self.parent_app._process_next_favorite_download()

            if hasattr(self.parent_app, 'switch_tab'):
                self.parent_app.switch_tab(0)

