import os
import sys
import time
import json
import hashlib
import re
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QGroupBox,
    QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
    QLabel, QAbstractItemView, QMessageBox, QFileDialog, QProgressBar, QTextEdit,
    QSpinBox, QFormLayout, QLineEdit, QComboBox, QMenu, QFrame, QApplication
)
from PySide6.QtGui import QIcon, QAction, QColor, QDesktopServices, QFont
from PySide6.QtCore import Qt, QThread, Signal, Slot, QUrl, QSize, QTimer

from ..core.platform_database import PlatformDatabaseManager
from .assets import get_asset_path


# ─────────────────────────────────────────────────────────────────────────────
#  Database Repair Worker
# ─────────────────────────────────────────────────────────────────────────────

class RepairWorker(QThread):
    """
    Background worker thread that verifies database records against files on disk,
    heals moved files with matching SHA-256 hashes, and prunes deleted records.
    """
    progress_signal = Signal(int, int)
    log_signal = Signal(str)
    finished_signal = Signal(int, int, int)  # healed, deleted, total

    def __init__(self, appdata_dir, scan_path, deep_scan=False):
        super().__init__()
        self.appdata_dir = appdata_dir
        self.scan_path = scan_path
        self.deep_scan = deep_scan
        self.is_cancelled = False

    def run(self):
        self.log_signal.emit("[Database Repair] Starting Database Repair Engine...")
        self.log_signal.emit(f"[Target Area] {self.scan_path}")

        db_dir = os.path.join(self.appdata_dir, "databases")
        if not os.path.exists(db_dir):
            self.log_signal.emit("[Error] Database directory not found.")
            self.finished_signal.emit(0, 0, 0)
            return

        all_entries_to_check = []

        # 1. Gather all database entries
        self.log_signal.emit("[Analysis] Analyzing local databases...")
        for file in os.listdir(db_dir):
            if not file.endswith(".db"):
                continue
            platform_name = file.replace(".db", "")
            try:
                db_manager = PlatformDatabaseManager.get_instance(platform_name, self.appdata_dir)
                creators = db_manager.get_all_creators()
                for c in creators:
                    table_name = c['sanitized_table_name']
                    creator_id = c['creator_id']
                    service = c['service']

                    sync_settings_json = c.get('sync_settings') or '{}'
                    try:
                        sync_settings = json.loads(sync_settings_json)
                    except json.JSONDecodeError:
                        sync_settings = {}

                    old_output_dir = sync_settings.get('output_dir', '')

                    try:
                        db_manager.cursor.execute(
                            f"SELECT hash, original_filename, saved_filename, saved_path FROM {table_name}"
                        )
                        rows = db_manager.cursor.fetchall()
                        for row in rows:
                            file_hash, orig_name, saved_name, saved_path = row

                            rel_path = ""
                            if old_output_dir and saved_path and saved_path.startswith(old_output_dir):
                                rel_path = os.path.relpath(saved_path, old_output_dir)

                            all_entries_to_check.append({
                                'platform': platform_name,
                                'table': table_name,
                                'creator_id': creator_id,
                                'service': service,
                                'sync_settings_json': sync_settings_json,
                                'old_output_dir': old_output_dir,
                                'rel_path': rel_path,
                                'hash': file_hash,
                                'orig_name': orig_name,
                                'saved_name': saved_name,
                                'saved_path': saved_path
                            })
                    except Exception as col_err:
                        self.log_signal.emit(f"[Notice] Skipping table {table_name}: {col_err}")
            except Exception as e:
                self.log_signal.emit(f"[Error] Reading {file}: {e}")

        total_entries = len(all_entries_to_check)
        self.log_signal.emit(f"[Success] Found {total_entries:,} total downloaded file records.")

        # 2. Check which are physically missing
        missing_entries = []
        for i, entry in enumerate(all_entries_to_check):
            if self.is_cancelled:
                return

            path = entry['saved_path']
            if not path or not os.path.exists(path):
                missing_entries.append(entry)

            if i % 1000 == 0:
                self.progress_signal.emit(i, total_entries)

        self.progress_signal.emit(total_entries, total_entries)

        if not missing_entries:
            self.log_signal.emit("[Integrity Verified] All recorded files exist on disk.")
            self.finished_signal.emit(0, 0, total_entries)
            return

        self.log_signal.emit(f"[Attention] Found {len(missing_entries)} missing files. Initiating physical search...")

        # Build lookup for fast matching
        missing_lookup = {}
        for entry in missing_entries:
            orig = entry.get('orig_name')
            saved = entry.get('saved_name')
            if orig:
                missing_lookup.setdefault(orig, []).append(entry)
            if saved:
                missing_lookup.setdefault(saved, []).append(entry)

        healed_count = 0
        deleted_count = 0
        updated_creators = {}

        # 3. Walk the filesystem
        try:
            for root, _, files in os.walk(self.scan_path):
                if self.is_cancelled:
                    return
                for file_name in files:
                    if file_name in missing_lookup:
                        full_path = os.path.join(root, file_name)
                        try:
                            # Verify Hash
                            hasher = hashlib.sha256()
                            with open(full_path, "rb") as f:
                                for chunk in iter(lambda: f.read(4096), b""):
                                    hasher.update(chunk)
                                    if self.is_cancelled:
                                        return
                            file_hash = hasher.hexdigest()

                            candidates = missing_lookup[file_name]
                            for candidate in candidates:
                                if candidate.get('hash') == file_hash:
                                    platform_name = candidate['platform']
                                    table = candidate['table']
                                    creator_id = candidate['creator_id']
                                    service = candidate['service']
                                    rel_path = candidate.get('rel_path')
                                    old_output_dir = candidate.get('old_output_dir')

                                    db_manager = PlatformDatabaseManager.get_instance(platform_name, self.appdata_dir)
                                    db_manager.cursor.execute(
                                        f"UPDATE {table} SET saved_path = ? WHERE hash = ?",
                                        (full_path, file_hash)
                                    )
                                    db_manager.conn.commit()

                                    if rel_path and full_path.endswith(rel_path):
                                        new_out = full_path[:-len(rel_path)].rstrip('\\/')
                                        if new_out and new_out != old_output_dir:
                                            key = f"{platform_name}_{creator_id}"
                                            if key not in updated_creators:
                                                updated_creators[key] = {
                                                    'platform': platform_name,
                                                    'creator_id': creator_id,
                                                    'service': service,
                                                    'settings_json': candidate.get('sync_settings_json', '{}'),
                                                    'new_output_dir': new_out
                                                }

                                    self.log_signal.emit(f"[HEALED] {file_name}")
                                    healed_count += 1
                                    candidate['healed'] = True
                        except Exception as e:
                            self.log_signal.emit(f"[Warning] Could not verify {file_name}: {e}")
        except Exception as e:
            self.log_signal.emit(f"[Critical] Error during file search: {e}")

        # 4. Delete entries that are still missing
        self.log_signal.emit("[Pruning] Removing unrecoverable missing records from database...")
        for entry in missing_entries:
            if self.is_cancelled:
                return
            if not entry.get('healed'):
                try:
                    platform_name = entry['platform']
                    table = entry['table']
                    file_hash = entry['hash']

                    db_manager = PlatformDatabaseManager.get_instance(platform_name, self.appdata_dir)
                    db_manager.cursor.execute(f"DELETE FROM {table} WHERE hash = ?", (file_hash,))
                    db_manager.conn.commit()
                    deleted_count += 1
                except Exception:
                    pass

        # 5. Update creator mappings for deduced output directories
        if updated_creators:
            self.log_signal.emit(
                f"[Output Paths] Deduced new base directories for {len(updated_creators)} creator(s). Updating sync profiles..."
            )
            for key, info in updated_creators.items():
                try:
                    db_manager = PlatformDatabaseManager.get_instance(info['platform'], self.appdata_dir)
                    settings = json.loads(info['settings_json'])
                    settings['output_dir'] = info['new_output_dir']
                    new_settings_str = json.dumps(settings)
                    db_manager.update_sync_settings(info['creator_id'], info['service'], new_settings_str)
                except Exception as e:
                    self.log_signal.emit(f"[Warning] Failed to update sync profile for {info['creator_id']}: {e}")

        self.log_signal.emit(f"[Complete] Repair finished. Healed: {healed_count} | Pruned: {deleted_count}.")
        self.finished_signal.emit(healed_count, deleted_count, total_entries)


# ─────────────────────────────────────────────────────────────────────────────
#  Main Auto-Sync Tab Widget
# ─────────────────────────────────────────────────────────────────────────────

class AutoSyncTabWidget(QWidget):
    """
    Full-page Auto-Sync Hub tab embedded in the main window stacked widget.
    Integrates monitored creator management, live background daemon logging,
    database repair engine, and daemon optimization settings.
    """

    def __init__(self, parent_app, scale: float = 1.0):
        super().__init__(parent_app)
        self.parent_app = parent_app
        self.scale = scale
        self.repair_worker = None

        # Per-session sync statistics
        self._sync_stats = {}
        self._current_sync_creator = None
        self._all_creators_data = []

        self._build_ui()
        self._load_auto_sync_settings()
        self._populate_sync_table()

        # Register this tab as the active sync log target on the parent app
        self.parent_app.active_sync_log_dialog = self

    # ─────────────────────────────────────────────────────────────────────────
    #  UI Assembly
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        scale = self.scale
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(
            int(14 * scale), int(12 * scale), int(14 * scale), int(12 * scale)
        )
        root_layout.setSpacing(int(10 * scale))

        # ── 1. Top Header Bar ─────────────────────────────────────────────────
        header_bar = QHBoxLayout()
        header_bar.setSpacing(int(10 * scale))

        title_col = QVBoxLayout()
        title_col.setSpacing(int(2 * scale))

        title_h = QHBoxLayout()
        title_h.setSpacing(int(8 * scale))

        title_icon = QLabel()
        title_icon.setPixmap(
            QIcon(get_asset_path("assets/Svg/sync.svg")).pixmap(int(22 * scale), int(22 * scale))
        )
        title_lbl = QLabel("Auto-Sync Hub")
        title_lbl.setStyleSheet(
            f"font-size: {int(13 * scale)}pt; font-weight: bold; color: #9BBCE8;"
        )
        title_h.addWidget(title_icon)
        title_h.addWidget(title_lbl)
        title_h.addStretch(1)
        title_col.addLayout(title_h)

        subtitle_lbl = QLabel(
            "Automate background downloads for subscribed creators and maintain database health."
        )
        subtitle_lbl.setStyleSheet("font-size: 8.5pt; color: #8A8D93; font-weight: normal;")
        title_col.addWidget(subtitle_lbl)
        header_bar.addLayout(title_col, 1)

        # Right Header Badges & Actions
        right_header = QHBoxLayout()
        right_header.setSpacing(int(8 * scale))

        self.daemon_badge = QLabel("Daemon Inactive")
        self.daemon_badge.setStyleSheet(
            "font-size: 8pt; font-weight: bold; color: #9A9EAA; background-color: #2D3035; "
            "border: 1px solid #424652; border-radius: 4px; padding: 4px 8px;"
        )
        right_header.addWidget(self.daemon_badge)

        self.quick_sync_btn = QPushButton(" Run Sync Now")
        self.quick_sync_btn.setIcon(QIcon(get_asset_path("assets/Svg/sync.svg")))
        self.quick_sync_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
        self.quick_sync_btn.setObjectName("PrimaryButton")
        self.quick_sync_btn.setToolTip("Trigger an immediate auto-sync cycle across all monitored creators.")
        self.quick_sync_btn.clicked.connect(self._run_sync_now)
        right_header.addWidget(self.quick_sync_btn)

        header_bar.addLayout(right_header)
        root_layout.addLayout(header_bar)

        # ── 2. Tab Widget ─────────────────────────────────────────────────────
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid #3E424B;
                background-color: #26282C;
                border-radius: 6px;
                top: -1px;
            }}
            QTabBar::tab {{
                background-color: #2D3035;
                color: #9A9EAA;
                border: 1px solid #3E424B;
                border-bottom: none;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                padding: {int(6 * scale)}px {int(16 * scale)}px;
                font-weight: 500;
                font-size: {int(8.5 * scale)}pt;
                margin-right: 3px;
            }}
            QTabBar::tab:selected {{
                background-color: #26282C;
                color: #60A5FA;
                font-weight: bold;
                border-bottom: 2px solid #2196F3;
            }}
            QTabBar::tab:hover:!selected {{
                background-color: #35383E;
                color: #D1D5DB;
            }}
        """)

        self.creators_tab_view = QWidget()
        self.activity_tab_view = QWidget()
        self.repair_tab_view = QWidget()
        self.advanced_tab_view = QWidget()

        self.tab_widget.addTab(self.creators_tab_view, "Monitored Creators")
        self.tab_widget.addTab(self.activity_tab_view, "Activity Log")
        self.tab_widget.addTab(self.repair_tab_view, "Database Repair")
        self.tab_widget.addTab(self.advanced_tab_view, "Advanced Settings")

        self._setup_creators_tab()
        self._setup_activity_tab()
        self._setup_repair_tab()
        self._setup_advanced_tab()

        root_layout.addWidget(self.tab_widget, 1)

    # ─────────────────────────────────────────────────────────────────────────
    #  Tab 1: Monitored Creators
    # ─────────────────────────────────────────────────────────────────────────

    def _setup_creators_tab(self):
        scale = self.scale
        layout = QVBoxLayout(self.creators_tab_view)
        layout.setContentsMargins(
            int(12 * scale), int(12 * scale), int(12 * scale), int(12 * scale)
        )
        layout.setSpacing(int(8 * scale))

        # Description / Info Card
        desc_card = QFrame()
        desc_card.setStyleSheet("background-color: #2D3035; border: 1px solid #3C3F46; border-radius: 5px;")
        desc_h = QHBoxLayout(desc_card)
        desc_h.setContentsMargins(int(10 * scale), int(8 * scale), int(10 * scale), int(8 * scale))
        desc_text = QLabel(
            "Check the box next to any creator to automatically download new posts in the background. "
            "Leave Pawchive running (or minimized to the system tray) for the background daemon to process them."
        )
        desc_text.setStyleSheet("color: #BAC0CB; font-size: 8.5pt;")
        desc_text.setWordWrap(True)
        desc_h.addWidget(desc_text)
        layout.addWidget(desc_card)

        # Filter / Search controls
        filter_row = QHBoxLayout()
        filter_row.setSpacing(int(6 * scale))

        self.creator_search_input = QLineEdit()
        self.creator_search_input.setPlaceholderText("Filter monitored creators by name, ID, or platform...")
        self.creator_search_input.setClearButtonEnabled(True)
        self.creator_search_input.textChanged.connect(self._filter_table_rows)
        filter_row.addWidget(self.creator_search_input, 1)

        self.creator_platform_combo = QComboBox()
        self.creator_platform_combo.addItems(["All Platforms", "Patreon", "Fanbox", "Discord"])
        self.creator_platform_combo.currentTextChanged.connect(self._filter_table_rows)
        filter_row.addWidget(self.creator_platform_combo)

        self.refresh_btn = QPushButton(" Refresh List")
        self.refresh_btn.setIcon(QIcon(get_asset_path("assets/Svg/sync.svg")))
        self.refresh_btn.setIconSize(QSize(int(13 * scale), int(13 * scale)))
        self.refresh_btn.clicked.connect(self._populate_sync_table)
        filter_row.addWidget(self.refresh_btn)

        layout.addLayout(filter_row)

        # Monitored Creators Table
        self.sync_table = QTableWidget(0, 5)
        self.sync_table.setHorizontalHeaderLabels([
            "Sync", "Creator Name", "Platform", "Service", "Output Directory"
        ])
        th = self.sync_table.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        th.resizeSection(0, int(60 * scale))
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(1, int(180 * scale))
        th.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(2, int(85 * scale))
        th.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(3, int(85 * scale))
        th.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        th.setStretchLastSection(True)

        self.sync_table.verticalHeader().setVisible(False)
        self.sync_table.verticalHeader().setDefaultSectionSize(int(42 * scale))
        self.sync_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sync_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.sync_table.setAlternatingRowColors(True)
        self.sync_table.itemChanged.connect(self._on_sync_item_changed)
        self.sync_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sync_table.customContextMenuRequested.connect(self._show_creator_context_menu)
        layout.addWidget(self.sync_table, 1)

        # Daemon Quick Settings Group
        settings_group = QGroupBox("Daemon Configuration")
        settings_group.setStyleSheet(
            "QGroupBox { font-weight: bold; color: #BAC0CB; border: 1px solid #3C3F46; "
            "border-radius: 5px; margin-top: 10px; padding-top: 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; }"
        )
        settings_layout = QHBoxLayout(settings_group)
        settings_layout.setContentsMargins(int(10 * scale), int(10 * scale), int(10 * scale), int(8 * scale))

        self.enable_sync_cb = QCheckBox("Enable Background Auto-Sync (Set & Forget)")
        self.enable_sync_cb.setStyleSheet("font-weight: 600; color: #E0E4EC;")
        self.enable_sync_cb.stateChanged.connect(self._save_auto_sync_settings)
        settings_layout.addWidget(self.enable_sync_cb)

        self.minimize_to_tray_cb = QCheckBox("Minimize to System Tray instead of closing")
        self.minimize_to_tray_cb.stateChanged.connect(self._save_auto_sync_settings)
        settings_layout.addWidget(self.minimize_to_tray_cb)

        settings_layout.addStretch(1)

        self.table_count_lbl = QLabel("0 creators monitored")
        self.table_count_lbl.setStyleSheet("color: #7EB6E0; font-size: 8.5pt;")
        settings_layout.addWidget(self.table_count_lbl)

        layout.addWidget(settings_group)

    # ─────────────────────────────────────────────────────────────────────────
    #  Tab 2: Activity Log
    # ─────────────────────────────────────────────────────────────────────────

    def _setup_activity_tab(self):
        scale = self.scale
        layout = QVBoxLayout(self.activity_tab_view)
        layout.setContentsMargins(
            int(12 * scale), int(12 * scale), int(12 * scale), int(12 * scale)
        )
        layout.setSpacing(int(8 * scale))

        # Description Card
        desc_card = QFrame()
        desc_card.setStyleSheet("background-color: #2D3035; border: 1px solid #3C3F46; border-radius: 5px;")
        desc_h = QHBoxLayout(desc_card)
        desc_h.setContentsMargins(int(10 * scale), int(8 * scale), int(10 * scale), int(8 * scale))
        desc_text = QLabel(
            "Live background sync daemon activity streams here automatically while keeping your main downloader log clean. "
            "Inspect newly downloaded or skipped posts, pause execution, or cancel the active cycle."
        )
        desc_text.setStyleSheet("color: #BAC0CB; font-size: 8.5pt;")
        desc_text.setWordWrap(True)
        desc_h.addWidget(desc_text)
        layout.addWidget(desc_card)

        # Action Buttons Row
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(int(6 * scale))

        self.act_sync_now_btn = QPushButton(" Run Sync Now")
        self.act_sync_now_btn.setIcon(QIcon(get_asset_path("assets/Svg/sync.svg")))
        self.act_sync_now_btn.clicked.connect(self._run_sync_now)
        btn_layout.addWidget(self.act_sync_now_btn)

        self.pause_sync_btn = QPushButton(" Pause Sync")
        self.pause_sync_btn.setIcon(QIcon(get_asset_path("assets/Svg/pause.svg")))
        self.pause_sync_btn.clicked.connect(self._toggle_pause)
        btn_layout.addWidget(self.pause_sync_btn)

        self.cancel_sync_btn = QPushButton(" Cancel Sync")
        self.cancel_sync_btn.setIcon(QIcon(get_asset_path("assets/Svg/cancel.svg")))
        self.cancel_sync_btn.clicked.connect(self._cancel_sync)
        btn_layout.addWidget(self.cancel_sync_btn)

        btn_layout.addStretch(1)

        self.show_summary_btn = QPushButton(" Show Summary")
        self.show_summary_btn.setEnabled(False)
        self.show_summary_btn.setToolTip("Available after monitored creators have been synced in this session.")
        self.show_summary_btn.clicked.connect(self._show_sync_summary)
        btn_layout.addWidget(self.show_summary_btn)

        self.clear_activity_log_btn = QPushButton(" Clear Log")
        self.clear_activity_log_btn.clicked.connect(self._clear_activity_log)
        btn_layout.addWidget(self.clear_activity_log_btn)

        self.auto_scroll_cb = QCheckBox("Auto-scroll")
        self.auto_scroll_cb.setChecked(True)
        btn_layout.addWidget(self.auto_scroll_cb)

        layout.addLayout(btn_layout)

        # Monospace Terminal Output
        self.activity_log_output = QTextEdit()
        self.activity_log_output.setReadOnly(True)
        self.activity_log_output.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.activity_log_output.setStyleSheet(
            "background-color: #1A1C1E; color: #D4D7DE; border: 1px solid #363940; "
            "border-radius: 5px; font-family: 'Consolas', 'Courier New', monospace; font-size: 8.5pt;"
        )
        self.activity_log_output.setPlaceholderText(
            "Auto-Sync activity will appear here when the background daemon runs..."
        )
        layout.addWidget(self.activity_log_output, 1)

    # ─────────────────────────────────────────────────────────────────────────
    #  Tab 3: Database Repair
    # ─────────────────────────────────────────────────────────────────────────

    def _setup_repair_tab(self):
        scale = self.scale
        layout = QVBoxLayout(self.repair_tab_view)
        layout.setContentsMargins(
            int(12 * scale), int(12 * scale), int(12 * scale), int(12 * scale)
        )
        layout.setSpacing(int(8 * scale))

        # Description Card
        desc_card = QFrame()
        desc_card.setStyleSheet("background-color: #2D3035; border: 1px solid #3C3F46; border-radius: 5px;")
        desc_h = QHBoxLayout(desc_card)
        desc_h.setContentsMargins(int(10 * scale), int(8 * scale), int(10 * scale), int(8 * scale))
        desc_text = QLabel(
            "<b>Self-Healing Database Engine:</b> If you move, rename, or reorganize downloaded files on your drives, "
            "the database can lose track of them. This tool scans your directories, verifies SHA-256 hashes, and "
            "restores accurate database paths. If files were permanently deleted from disk, missing records are cleaned so "
            "content can be downloaded cleanly."
        )
        desc_text.setStyleSheet("color: #BAC0CB; font-size: 8.5pt;")
        desc_text.setWordWrap(True)
        desc_h.addWidget(desc_text)
        layout.addWidget(desc_card)

        # Scan Action Buttons
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setSpacing(int(6 * scale))

        self.target_folder_btn = QPushButton(" Scan Target Folder (Fast)")
        self.target_folder_btn.setIcon(QIcon(get_asset_path("assets/Svg/folder.svg")))
        self.target_folder_btn.clicked.connect(self._scan_target)
        ctrl_layout.addWidget(self.target_folder_btn)

        self.deep_scan_btn = QPushButton(" Deep Scan Entire Drive (Slow)")
        self.deep_scan_btn.setIcon(QIcon(get_asset_path("assets/Svg/search.svg")))
        self.deep_scan_btn.clicked.connect(self._deep_scan)
        ctrl_layout.addWidget(self.deep_scan_btn)

        self.stop_btn = QPushButton(" Stop Repair")
        self.stop_btn.setIcon(QIcon(get_asset_path("assets/Svg/cancel.svg")))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_repair)
        ctrl_layout.addWidget(self.stop_btn)

        ctrl_layout.addStretch(1)
        layout.addLayout(ctrl_layout)

        # Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(int(14 * scale))
        self.progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #3E424B; border-radius: 4px; background: #232528; text-align: center; color: #BAC0CB; font-size: 7.5pt; } "
            "QProgressBar::chunk { background-color: #2196F3; border-radius: 3px; }"
        )
        layout.addWidget(self.progress_bar)

        # Repair Console Log
        self.repair_log_area = QTextEdit()
        self.repair_log_area.setReadOnly(True)
        self.repair_log_area.setStyleSheet(
            "background-color: #1A1C1E; color: #D4D7DE; border: 1px solid #363940; "
            "border-radius: 5px; font-family: 'Consolas', 'Courier New', monospace; font-size: 8.5pt;"
        )
        self.repair_log_area.setPlaceholderText("Database repair logs will appear here when a scan is initiated...")
        layout.addWidget(self.repair_log_area, 1)

    # ─────────────────────────────────────────────────────────────────────────
    #  Tab 4: Advanced Settings
    # ─────────────────────────────────────────────────────────────────────────

    def _setup_advanced_tab(self):
        scale = self.scale
        layout = QVBoxLayout(self.advanced_tab_view)
        layout.setContentsMargins(
            int(12 * scale), int(12 * scale), int(12 * scale), int(12 * scale)
        )
        layout.setSpacing(int(10 * scale))

        # 1. Daemon Trigger Options
        trigger_group = QGroupBox("Daemon Triggers")
        trigger_layout = QFormLayout(trigger_group)
        trigger_layout.setContentsMargins(int(10 * scale), int(12 * scale), int(10 * scale), int(10 * scale))

        self.idle_threshold_spin = QSpinBox()
        self.idle_threshold_spin.setRange(1, 3600)
        self.idle_threshold_spin.setValue(10)
        self.idle_threshold_spin.setSuffix(" seconds")
        self.idle_threshold_spin.setToolTip("User inactivity required before auto-sync begins scanning.")
        self.idle_threshold_spin.valueChanged.connect(self._save_auto_sync_settings)
        trigger_layout.addRow("Start Auto-Sync after app is idle for:", self.idle_threshold_spin)
        layout.addWidget(trigger_group)

        # 2. Scan Optimization Group
        scan_group = QGroupBox("Scan Optimization & Early Stop")
        scan_layout = QFormLayout(scan_group)
        scan_layout.setContentsMargins(int(10 * scale), int(12 * scale), int(10 * scale), int(10 * scale))

        self.enable_early_stop_cb = QCheckBox("Enable Early Stop (halt scanning when consecutive existing posts are encountered)")
        self.enable_early_stop_cb.setChecked(True)
        self.enable_early_stop_cb.stateChanged.connect(self._save_auto_sync_settings)
        scan_layout.addRow(self.enable_early_stop_cb)

        self.early_stop_threshold_spin = QSpinBox()
        self.early_stop_threshold_spin.setRange(0, 100)
        self.early_stop_threshold_spin.setValue(0)
        self.early_stop_threshold_spin.setToolTip("0 = Disabled (scans full page range)")
        self.early_stop_threshold_spin.valueChanged.connect(self._save_auto_sync_settings)
        scan_layout.addRow("Stop after consecutive already-downloaded posts (0 = disabled):", self.early_stop_threshold_spin)

        self.max_pages_spin = QSpinBox()
        self.max_pages_spin.setRange(0, 1000)
        self.max_pages_spin.setValue(1)
        self.max_pages_spin.setToolTip("0 = Unlimited")
        self.max_pages_spin.valueChanged.connect(self._save_auto_sync_settings)
        scan_layout.addRow("Max pages to scan per creator (0 = unlimited):", self.max_pages_spin)
        layout.addWidget(scan_group)

        # 3. Error Handling
        error_group = QGroupBox("Error Handling")
        error_layout = QVBoxLayout(error_group)
        error_layout.setContentsMargins(int(10 * scale), int(12 * scale), int(10 * scale), int(10 * scale))

        self.retry_404_cb = QCheckBox("Retry 404 Not Found errors on next auto-sync")
        self.retry_404_cb.setToolTip("If unchecked, files returning 404 Not Found on the server are marked skipped.")
        self.retry_404_cb.stateChanged.connect(self._save_auto_sync_settings)
        error_layout.addWidget(self.retry_404_cb)
        layout.addWidget(error_group)

        # 4. Proxy Configuration (Auto-Sync Dedicated)
        network_group = QGroupBox("Dedicated Proxy Settings (Auto-Sync Only)")
        network_layout = QFormLayout(network_group)
        network_layout.setContentsMargins(int(10 * scale), int(12 * scale), int(10 * scale), int(10 * scale))

        self.proxy_override_cb = QCheckBox("Override global proxy settings during background auto-sync")
        self.proxy_override_cb.setToolTip("If checked, auto-sync uses this custom proxy instead of global app proxy.")
        self.proxy_override_cb.stateChanged.connect(self._save_auto_sync_settings)
        network_layout.addRow(self.proxy_override_cb)

        self.use_proxy_cb = QCheckBox("Enable Proxy for Auto-Sync")
        self.use_proxy_cb.stateChanged.connect(self._save_auto_sync_settings)
        network_layout.addRow(self.use_proxy_cb)

        self.proxy_type_combo = QComboBox()
        self.proxy_type_combo.addItems(["HTTP", "SOCKS4", "SOCKS5"])
        self.proxy_type_combo.currentIndexChanged.connect(self._save_auto_sync_settings)
        network_layout.addRow("Proxy Type:", self.proxy_type_combo)

        self.proxy_host_input = QLineEdit()
        self.proxy_host_input.editingFinished.connect(self._save_auto_sync_settings)
        network_layout.addRow("Proxy Host:", self.proxy_host_input)

        self.proxy_port_input = QLineEdit()
        self.proxy_port_input.editingFinished.connect(self._save_auto_sync_settings)
        network_layout.addRow("Proxy Port:", self.proxy_port_input)

        self.proxy_user_input = QLineEdit()
        self.proxy_user_input.editingFinished.connect(self._save_auto_sync_settings)
        network_layout.addRow("Proxy Username (Optional):", self.proxy_user_input)

        self.proxy_pass_input = QLineEdit()
        self.proxy_pass_input.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.proxy_pass_input.editingFinished.connect(self._save_auto_sync_settings)
        network_layout.addRow("Proxy Password (Optional):", self.proxy_pass_input)

        # Connect override toggle to enable/disable proxy fields
        self.proxy_override_cb.toggled.connect(self._update_proxy_fields_state)
        layout.addWidget(network_group)

        layout.addStretch(1)

    def _update_proxy_fields_state(self, checked: bool):
        for widget in (
            self.use_proxy_cb, self.proxy_type_combo, self.proxy_host_input,
            self.proxy_port_input, self.proxy_user_input, self.proxy_pass_input
        ):
            widget.setEnabled(checked)

    # ─────────────────────────────────────────────────────────────────────────
    #  Tab Activation Hook
    # ─────────────────────────────────────────────────────────────────────────

    def on_tab_activated(self):
        """Called whenever the user clicks the Auto-Sync nav item on the sidebar."""
        self._load_auto_sync_settings()
        self._populate_sync_table()

    # ─────────────────────────────────────────────────────────────────────────
    #  Data Population & Filtering
    # ─────────────────────────────────────────────────────────────────────────

    def _populate_sync_table(self):
        self.sync_table.blockSignals(True)
        self.sync_table.setRowCount(0)
        appdata_dir = getattr(self.parent_app, 'app_base_dir', '')
        if not appdata_dir:
            self.sync_table.blockSignals(False)
            return

        appdata_dir = os.path.join(appdata_dir, "appdata")
        db_dir = os.path.join(appdata_dir, "databases")
        if not os.path.exists(db_dir):
            self.sync_table.blockSignals(False)
            return

        all_creators = []
        for file in os.listdir(db_dir):
            if file.endswith(".db"):
                platform_name = file.replace(".db", "")
                try:
                    db_manager = PlatformDatabaseManager.get_instance(platform_name, appdata_dir)
                    creators = db_manager.get_all_creators()
                    for c in creators:
                        c['platform'] = platform_name
                        all_creators.append(c)
                except Exception as e:
                    print(f"Error reading DB {file}: {e}")

        all_creators.sort(
            key=lambda x: (not x.get('is_synced', False), x.get('platform', ''), (x.get('original_name') or '').lower())
        )
        self._all_creators_data = all_creators

        synced_count = sum(1 for c in all_creators if c.get('is_synced'))
        self.table_count_lbl.setText(f"{synced_count} of {len(all_creators)} creator(s) monitored")

        self.sync_table.setRowCount(len(all_creators))
        for row, c in enumerate(all_creators):
            self.sync_table.setRowHeight(row, int(42 * self.scale))

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if c.get('is_synced') else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, (c['platform'], c['creator_id']))
            chk.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.sync_table.setItem(row, 0, chk)

            item1 = QTableWidgetItem(c.get('original_name', 'Unknown'))
            item1.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            self.sync_table.setItem(row, 1, item1)

            item2 = QTableWidgetItem(c.get('platform', '').capitalize())
            item2.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            self.sync_table.setItem(row, 2, item2)

            item3 = QTableWidgetItem(c.get('service', '') or 'Unknown')
            item3.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            self.sync_table.setItem(row, 3, item3)

            # Setup Output Directory column widget
            sync_settings = {}
            if c.get('sync_settings'):
                try:
                    sync_settings = json.loads(c['sync_settings'])
                except json.JSONDecodeError:
                    pass
            current_out_dir = sync_settings.get('output_dir', '')

            out_widget = QWidget()
            out_widget.setStyleSheet("background: transparent;")
            out_layout = QHBoxLayout(out_widget)
            out_layout.setContentsMargins(
                int(4 * self.scale), int(2 * self.scale), int(6 * self.scale), int(2 * self.scale)
            )
            out_layout.setSpacing(int(5 * self.scale))
            out_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

            line_edit = QLineEdit(current_out_dir)
            line_edit.setPlaceholderText("Default Download Directory")
            line_edit.setFixedHeight(int(30 * self.scale))
            line_edit.setToolTip(current_out_dir if current_out_dir else "Default Download Directory")
            line_edit.setStyleSheet(
                "QLineEdit {"
                "   background-color: #202328;"
                "   border: 1px solid #383D48;"
                "   border-radius: 4px;"
                "   color: #DDE2EB;"
                "   padding: 2px 8px;"
                "   font-size: 8.5pt;"
                "}"
                "QLineEdit:hover { border-color: #4C5362; }"
                "QLineEdit:focus { border: 1px solid #3B82F6; background-color: #1A1C20; }"
            )

            btn = QPushButton()
            btn.setIcon(QIcon(get_asset_path("assets/Svg/folder.svg")))
            btn.setIconSize(QSize(int(15 * self.scale), int(15 * self.scale)))
            btn.setFixedSize(int(30 * self.scale), int(30 * self.scale))
            btn.setToolTip("Browse for folder...")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton {"
                "   background-color: #2D313A;"
                "   border: 1px solid #3E4350;"
                "   border-radius: 4px;"
                "}"
                "QPushButton:hover {"
                "   background-color: #3B414E;"
                "   border-color: #60A5FA;"
                "}"
                "QPushButton:pressed {"
                "   background-color: #22252B;"
                "}"
            )

            out_layout.addWidget(line_edit, 1)
            out_layout.addWidget(btn)

            self.sync_table.setCellWidget(row, 4, out_widget)

            platform = c.get('platform')
            creator_id = c.get('creator_id')
            service = c.get('service')

            btn.clicked.connect(
                lambda checked=False, p=platform, cid=creator_id, s=service, le=line_edit:
                self._browse_creator_dir(p, cid, s, le)
            )
            line_edit.editingFinished.connect(
                lambda p=platform, cid=creator_id, s=service, le=line_edit:
                self._update_creator_dir(p, cid, s, le.text())
            )

        self.sync_table.blockSignals(False)
        self._filter_table_rows()

    def _filter_table_rows(self):
        """Filters the rows of sync_table by search text and platform."""
        query = self.creator_search_input.text().strip().lower()
        platform_filter = self.creator_platform_combo.currentText().lower()

        for row in range(self.sync_table.rowCount()):
            name_item = self.sync_table.item(row, 1)
            platform_item = self.sync_table.item(row, 2)
            service_item = self.sync_table.item(row, 3)

            name = name_item.text().lower() if name_item else ""
            platform = platform_item.text().lower() if platform_item else ""
            service = service_item.text().lower() if service_item else ""

            match_text = (not query) or (query in name or query in platform or query in service)
            match_plat = (platform_filter == "all platforms") or (platform_filter == platform or platform_filter == service)

            self.sync_table.setRowHidden(row, not (match_text and match_plat))

    def _on_sync_item_changed(self, item):
        if item.column() == 0:
            platform_id_data = item.data(Qt.ItemDataRole.UserRole)
            if platform_id_data:
                platform_name, creator_id = platform_id_data
                is_synced = item.checkState() == Qt.CheckState.Checked
                appdata_dir = os.path.join(getattr(self.parent_app, 'app_base_dir', ''), "appdata")
                db_manager = PlatformDatabaseManager.get_instance(platform_name, appdata_dir)
                db_manager.toggle_sync_status(creator_id, is_synced)

                # Update count label
                checked_count = 0
                for r in range(self.sync_table.rowCount()):
                    chk = self.sync_table.item(r, 0)
                    if chk and chk.checkState() == Qt.CheckState.Checked:
                        checked_count += 1
                self.table_count_lbl.setText(f"{checked_count} of {self.sync_table.rowCount()} creator(s) monitored")

    # ─────────────────────────────────────────────────────────────────────────
    #  Context Menu & Operations
    # ─────────────────────────────────────────────────────────────────────────

    def _show_creator_context_menu(self, pos):
        row = self.sync_table.rowAt(pos.y())
        if row < 0:
            return

        chk_item = self.sync_table.item(row, 0)
        if not chk_item:
            return
        platform_id_data = chk_item.data(Qt.ItemDataRole.UserRole)
        if not platform_id_data:
            return

        platform_name, creator_id = platform_id_data
        creator_name_item = self.sync_table.item(row, 1)
        creator_name = creator_name_item.text() if creator_name_item else f"ID: {creator_id}"
        service_item = self.sync_table.item(row, 3)
        service = service_item.text() if service_item else platform_name

        menu = QMenu(self)

        is_checked = chk_item.checkState() == Qt.CheckState.Checked
        toggle_text = "Uncheck from Auto-Sync" if is_checked else "Check for Auto-Sync"
        toggle_act = menu.addAction(toggle_text)
        toggle_act.triggered.connect(
            lambda: chk_item.setCheckState(Qt.CheckState.Unchecked if is_checked else Qt.CheckState.Checked)
        )

        menu.addSeparator()

        open_folder_act = menu.addAction(QIcon(get_asset_path("assets/Svg/folder.svg")), "Open Download Directory")
        open_folder_act.triggered.connect(lambda: self._open_creator_folder(row))

        copy_url_act = menu.addAction(QIcon(get_asset_path("assets/Svg/link.svg")), "Copy Creator URL")
        copy_url_act.triggered.connect(lambda: self._copy_creator_url(service, creator_id))

        menu.addSeparator()

        del_act = menu.addAction(QIcon(get_asset_path("assets/Svg/trash.svg")), f"Delete '{creator_name}' from Database")
        del_act.triggered.connect(
            lambda: self._delete_creator_from_db(platform_name, creator_id, creator_name)
        )

        menu.exec(self.sync_table.viewport().mapToGlobal(pos))

    def _open_creator_folder(self, row: int):
        cell_widget = self.sync_table.cellWidget(row, 4)
        if cell_widget:
            le = cell_widget.findChild(QLineEdit)
            if le and le.text() and os.path.isdir(le.text()):
                QDesktopServices.openUrl(QUrl.fromLocalFile(le.text()))
                return
        QMessageBox.information(self, "Folder Not Found", "No valid output directory found for this creator.")

    def _copy_creator_url(self, service: str, creator_id: str):
        svc = service.lower().strip()
        url_type = "server" if svc == "discord" else "user"
        url = f"https://pawchive.pw/{svc}/{url_type}/{creator_id}"
        QApplication.clipboard().setText(url)

    def _delete_creator_from_db(self, platform_name, creator_id, creator_name):
        reply = QMessageBox.warning(
            self,
            "Delete Creator Database Records",
            f"<b>Are you sure you want to delete all download records for:</b><br><br>"
            f"&nbsp;&nbsp;&nbsp;<b>{creator_name}</b> ({platform_name})<br><br>"
            f"This will remove file history for this creator from the database.<br>"
            f"<b>Downloaded files on your drive will NOT be deleted.</b><br><br>"
            f"If you re-download this creator later, files will be downloaded again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        appdata_dir = os.path.join(getattr(self.parent_app, 'app_base_dir', ''), "appdata")
        try:
            db_manager = PlatformDatabaseManager.get_instance(platform_name, appdata_dir)
            success = db_manager.delete_creator(creator_id)
            if success:
                QMessageBox.information(
                    self, "Deleted",
                    f"Database records for <b>{creator_name}</b> have been deleted successfully."
                )
                self._populate_sync_table()
            else:
                QMessageBox.warning(
                    self, "Not Found",
                    f"Could not find a database entry for '{creator_name}' (ID: {creator_id})."
                )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete creator: {e}")

    def _browse_creator_dir(self, platform, creator_id, service, line_edit):
        start_dir = line_edit.text() if os.path.isdir(line_edit.text()) else ""
        new_dir = QFileDialog.getExistingDirectory(self, "Select Download Location", start_dir)
        if new_dir:
            new_dir = os.path.normpath(new_dir)
            line_edit.setText(new_dir)
            self._update_creator_dir(platform, creator_id, service, new_dir)

    def _update_creator_dir(self, platform, creator_id, service, new_path):
        if not new_path:
            return
        appdata_dir = os.path.join(getattr(self.parent_app, 'app_base_dir', ''), "appdata")
        db_manager = PlatformDatabaseManager.get_instance(platform, appdata_dir)

        db_manager.cursor.execute(
            "SELECT sync_settings FROM creator_mappings WHERE creator_id = ?",
            (str(creator_id),)
        )
        row = db_manager.cursor.fetchone()
        if row:
            settings_json = row[0] or '{}'
            try:
                settings = json.loads(settings_json)
            except json.JSONDecodeError:
                settings = {}

            settings['output_dir'] = new_path
            db_manager.update_sync_settings(creator_id, service, json.dumps(settings))

    # ─────────────────────────────────────────────────────────────────────────
    #  Settings Management
    # ─────────────────────────────────────────────────────────────────────────

    def _load_auto_sync_settings(self):
        if not hasattr(self.parent_app, 'settings'):
            return

        settings = self.parent_app.settings
        is_enabled = settings.value("auto_sync_enabled", False, type=bool)
        self.enable_sync_cb.setChecked(is_enabled)
        self.minimize_to_tray_cb.setChecked(settings.value("minimize_to_tray", False, type=bool))

        # Update daemon status badge
        if is_enabled:
            self.daemon_badge.setText("Daemon Active")
            self.daemon_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #48BB78; background-color: #1A3626; "
                "border: 1px solid #276749; border-radius: 4px; padding: 4px 8px;"
            )
        else:
            self.daemon_badge.setText("Daemon Inactive")
            self.daemon_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #9A9EAA; background-color: #2D3035; "
                "border: 1px solid #424652; border-radius: 4px; padding: 4px 8px;"
            )

        self.idle_threshold_spin.setValue(settings.value("auto_sync_idle_threshold", 10, type=int))
        self.enable_early_stop_cb.setChecked(settings.value("auto_sync_enable_early_stop", True, type=bool))
        self.early_stop_threshold_spin.setValue(settings.value("auto_sync_early_stop_threshold", 0, type=int))
        self.max_pages_spin.setValue(settings.value("auto_sync_max_pages", 1, type=int))
        self.retry_404_cb.setChecked(settings.value("auto_sync_retry_404", False, type=bool))

        override_proxy = settings.value("auto_sync_proxy_override", True, type=bool)
        self.proxy_override_cb.setChecked(override_proxy)
        self.use_proxy_cb.setChecked(settings.value("auto_sync_proxy_enabled", False, type=bool))
        self.proxy_type_combo.setCurrentText(settings.value("auto_sync_proxy_type", "HTTP", type=str))
        self.proxy_host_input.setText(settings.value("auto_sync_proxy_host", "", type=str))
        self.proxy_port_input.setText(settings.value("auto_sync_proxy_port", "", type=str))
        self.proxy_user_input.setText(settings.value("auto_sync_proxy_username", "", type=str))
        self.proxy_pass_input.setText(settings.value("auto_sync_proxy_password", "", type=str))

        self._update_proxy_fields_state(override_proxy)

    def _save_auto_sync_settings(self):
        if not hasattr(self.parent_app, 'settings'):
            return

        settings = self.parent_app.settings
        is_enabled = self.enable_sync_cb.isChecked()
        settings.setValue("auto_sync_enabled", is_enabled)
        settings.setValue("minimize_to_tray", self.minimize_to_tray_cb.isChecked())

        # Update daemon status badge
        if is_enabled:
            self.daemon_badge.setText("Daemon Active")
            self.daemon_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #48BB78; background-color: #1A3626; "
                "border: 1px solid #276749; border-radius: 4px; padding: 4px 8px;"
            )
        else:
            self.daemon_badge.setText("Daemon Inactive")
            self.daemon_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #9A9EAA; background-color: #2D3035; "
                "border: 1px solid #424652; border-radius: 4px; padding: 4px 8px;"
            )

        settings.setValue("auto_sync_idle_threshold", self.idle_threshold_spin.value())
        settings.setValue("auto_sync_enable_early_stop", self.enable_early_stop_cb.isChecked())
        settings.setValue("auto_sync_early_stop_threshold", self.early_stop_threshold_spin.value())
        settings.setValue("auto_sync_max_pages", self.max_pages_spin.value())
        settings.setValue("auto_sync_retry_404", self.retry_404_cb.isChecked())

        settings.setValue("auto_sync_proxy_override", self.proxy_override_cb.isChecked())
        settings.setValue("auto_sync_proxy_enabled", self.use_proxy_cb.isChecked())
        settings.setValue("auto_sync_proxy_type", self.proxy_type_combo.currentText())
        settings.setValue("auto_sync_proxy_host", self.proxy_host_input.text())
        settings.setValue("auto_sync_proxy_port", self.proxy_port_input.text())
        settings.setValue("auto_sync_proxy_username", self.proxy_user_input.text())
        settings.setValue("auto_sync_proxy_password", self.proxy_pass_input.text())

        settings.sync()

        # Notify AutoSyncManager if available
        if hasattr(self.parent_app, 'auto_sync_manager'):
            self.parent_app.auto_sync_manager.check_and_start()

    # ─────────────────────────────────────────────────────────────────────────
    #  Live Activity Log & Daemon Control
    # ─────────────────────────────────────────────────────────────────────────

    def _run_sync_now(self):
        """Immediately starts an auto-sync cycle."""
        if hasattr(self.parent_app, 'auto_sync_manager'):
            if getattr(self.parent_app.auto_sync_manager, 'is_processing', False):
                QMessageBox.information(self, "Auto-Sync In Progress", "An auto-sync cycle is already currently running.")
                return
            self.activity_log_output.append(f"[{datetime.now().strftime('%H:%M:%S')}] Manual sync trigger initiated.")
            self.parent_app.auto_sync_manager.run_sync_cycle()
            self.tab_widget.setCurrentIndex(1)  # Switch to Activity Log
        else:
            QMessageBox.information(self, "Unavailable", "AutoSyncManager is not initialized yet.")

    def _toggle_pause(self):
        if hasattr(self.parent_app, 'pause_download'):
            self.parent_app.pause_download()
            if getattr(self.parent_app, 'is_paused', False):
                self.pause_sync_btn.setText(" Resume Sync")
                self.pause_sync_btn.setIcon(QIcon(get_asset_path("assets/Svg/resume.svg")))
            else:
                self.pause_sync_btn.setText(" Pause Sync")
                self.pause_sync_btn.setIcon(QIcon(get_asset_path("assets/Svg/pause.svg")))

    def _cancel_sync(self):
        if hasattr(self.parent_app, 'cancel_download'):
            reply = QMessageBox.question(
                self, "Cancel Auto-Sync",
                "Are you sure you want to stop the current background auto-sync?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.parent_app.cancel_download()
                timestamp = datetime.now().strftime("%H:%M:%S")
                self.activity_log_output.append(f"[{timestamp}] Auto-Sync cancelled by user.")

    @Slot(str)
    def append_sync_log(self, message: str):
        """
        Receives log messages during background sync and appends clean formatted output.
        Filters internal verbose noise automatically.
        """
        _noise_prefixes = (
            "--- DEBUG:", "==> Returning:", "single_thread_active:",
            "is_fetcher_thread_running:", "is_fetching_only:",
            "RENAMING_MODE_FETCH", "TEMP_FILE_PATH:", "   ->",
        )
        _noise_substrings = ("shiboken6", "libpyside")

        msg_stripped = message.strip()
        if not msg_stripped:
            return
        if any(msg_stripped.startswith(p) for p in _noise_prefixes):
            return
        if any(s in msg_stripped for s in _noise_substrings):
            return

        # Track per-creator sync stats
        creator_match = re.search(r'triggering download for:\s*(.+)', msg_stripped)
        if creator_match:
            url_part = creator_match.group(1).strip()
            if " | Creator: " in url_part:
                url, creator_label = url_part.split(" | Creator: ", 1)
            else:
                url = url_part
                tags = re.search(r'tags=([^&\s]+)', url)
                if tags:
                    creator_label = tags.group(1)
                else:
                    creator_label = url.rstrip('/').split('/')[-1]
            self._current_sync_creator = creator_label
            if creator_label not in self._sync_stats:
                self._sync_stats[creator_label] = {'downloaded': 0, 'skipped': 0, 'failed': 0}

        if self._current_sync_creator:
            key = self._current_sync_creator
            if key not in self._sync_stats:
                self._sync_stats[key] = {'downloaded': 0, 'skipped': 0, 'failed': 0}
            dl_match = re.search(
                r'Download complete.*?(\d+)\s+downloaded.*?(\d+)\s+skipped',
                msg_stripped, re.IGNORECASE
            )
            if dl_match:
                self._sync_stats[key]['downloaded'] += int(dl_match.group(1))
                self._sync_stats[key]['skipped'] += int(dl_match.group(2))
            if re.search(r'failed|error|\u274c', msg_stripped, re.IGNORECASE):
                self._sync_stats[key]['failed'] += 1

        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.activity_log_output.append(f"[{timestamp}] {msg_stripped}")

            if self.auto_scroll_cb.isChecked():
                scrollbar = self.activity_log_output.verticalScrollBar()
                scrollbar.setValue(scrollbar.maximum())

            # Mark activity tab dot if user is currently looking at another sub-tab
            if self.tab_widget.currentIndex() != 1:
                self.tab_widget.setTabText(1, "Activity Log \u2022")
        except Exception:
            pass

    def _clear_activity_log(self):
        self.activity_log_output.clear()
        self._sync_stats = {}
        self._current_sync_creator = None
        self.show_summary_btn.setEnabled(False)
        self.tab_widget.setTabText(1, "Activity Log")

    def on_sync_cycle_complete(self):
        """Called when all monitored creators have finished processing."""
        try:
            self.show_summary_btn.setEnabled(True)
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.activity_log_output.append(
                f"[{timestamp}] === All monitored creators have been synced. Click 'Show Summary' for details. ==="
            )
            if self.auto_scroll_cb.isChecked():
                scrollbar = self.activity_log_output.verticalScrollBar()
                scrollbar.setValue(scrollbar.maximum())
        except Exception:
            pass

    def _show_sync_summary(self):
        """Appends formatted session summary table to activity log."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        lines = [
            f"[{timestamp}] " + "=" * 58,
            f"[{timestamp}]   AUTO-SYNC SESSION SUMMARY",
            f"[{timestamp}] " + "=" * 58
        ]

        if not self._sync_stats:
            lines.append(f"[{timestamp}]   No download activity recorded during this session.")
        else:
            total_dl = total_sk = total_fail = 0
            col_w = 26
            lines.append(f"[{timestamp}]   {'Creator':<{col_w}} {'Downloaded':>12} {'Skipped':>9} {'Failed':>8}")
            lines.append(f"[{timestamp}]   {'-' * col_w} {'----------':>12} {'-------':>9} {'------':>8}")
            for creator, stats in self._sync_stats.items():
                dl = stats['downloaded']
                sk = stats['skipped']
                fl = stats['failed']
                total_dl += dl
                total_sk += sk
                total_fail += fl
                label = (creator[:col_w - 2] + '..') if len(creator) > col_w else creator
                lines.append(f"[{timestamp}]   {label:<{col_w}} {dl:>12} {sk:>9} {fl:>8}")
            lines.append(f"[{timestamp}]   {'-' * col_w} {'----------':>12} {'-------':>9} {'------':>8}")
            lines.append(f"[{timestamp}]   {'TOTAL':<{col_w}} {total_dl:>12} {total_sk:>9} {total_fail:>8}")

        lines.append(f"[{timestamp}] " + "=" * 58)
        for line in lines:
            self.activity_log_output.append(line)

        if self.auto_scroll_cb.isChecked():
            scrollbar = self.activity_log_output.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    # ─────────────────────────────────────────────────────────────────────────
    #  Database Repair Engine Actions
    # ─────────────────────────────────────────────────────────────────────────

    def _scan_target(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Scan for Missing Content")
        if folder:
            self._start_repair(folder, False)

    def _deep_scan(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Drive Root to Deep Scan (e.g. D:\\)")
        if folder:
            reply = QMessageBox.question(
                self, "Deep Scan Warning",
                "Deep Scans check every folder on the selected drive and can take a considerable amount of time. Proceed?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._start_repair(folder, True)

    def _start_repair(self, scan_path, deep_scan):
        self.target_folder_btn.setEnabled(False)
        self.deep_scan_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.repair_log_area.clear()
        self.progress_bar.setValue(0)

        appdata_dir = os.path.join(getattr(self.parent_app, 'app_base_dir', ''), "appdata")
        self.repair_worker = RepairWorker(appdata_dir, scan_path, deep_scan)
        self.repair_worker.log_signal.connect(lambda text: self.repair_log_area.append(text))
        self.repair_worker.progress_signal.connect(self._update_repair_progress)
        self.repair_worker.finished_signal.connect(self._on_repair_finished)
        self.repair_worker.start()

    def _update_repair_progress(self, current, total):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    def _stop_repair(self):
        if self.repair_worker:
            self.repair_worker.is_cancelled = True
            self.repair_worker.wait()
            self.repair_log_area.append("[Database Repair] Scan cancelled by user.")
            self._on_repair_finished(0, 0, 0)

    def _on_repair_finished(self, healed, deleted, total):
        self.target_folder_btn.setEnabled(True)
        self.deep_scan_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setValue(self.progress_bar.maximum())
        QMessageBox.information(
            self, "Repair Complete",
            f"Database Repair Engine Finished.\n\n"
            f"Total records checked: {total:,}\n"
            f"Healed file records: {healed:,}\n"
            f"Pruned missing records: {deleted:,}"
        )

