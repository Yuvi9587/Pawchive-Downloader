"""
Pawchive Downloader - Download Queue & Activity Tab
===================================================
Provides a centralized, real-time control room for monitoring all active
downloads, per-file progress, data transfer metrics, multithreading status,
server response health, and queued batch jobs.
"""

import os
import time
import json
import glob
from collections import deque
from urllib.parse import urlparse

from PySide6.QtCore import Qt, QSize, QTimer, QThread, Signal, QUrl
from PySide6.QtGui import QIcon, QDesktopServices, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, QFrame,
    QTabWidget, QAbstractItemView, QMessageBox, QMenu, QApplication
)

from ..utils.resolution import get_asset_path


def format_bytes(num_bytes: float) -> str:
    """Format bytes into clean human-readable units (B, KB, MB, GB)."""
    if num_bytes is None or num_bytes < 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:.1f} {unit}" if unit != 'B' else f"{int(num_bytes)} B"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} PB"


def format_speed(bps: float) -> str:
    """Format bytes per second into human-readable speed string."""
    if bps is None or bps <= 0:
        return "0.0 KB/s"
    mbps = bps / (1024.0 * 1024.0)
    if mbps >= 1.0:
        return f"{mbps:.1f} MB/s"
    kbps = bps / 1024.0
    return f"{kbps:.0f} KB/s"


class ServerPingWorker(QThread):
    """
    Background worker that periodically checks target server response latency
    and HTTP status code without blocking the main GUI thread.
    """
    ping_result = Signal(int, int, str)  # status_code, latency_ms, domain

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        self.target_url = "https://pawchive.pw"

    def stop(self):
        self._running = False

    def set_target_url(self, url: str):
        if url and url.startswith("http"):
            try:
                parsed = urlparse(url)
                if parsed.netloc:
                    self.target_url = f"{parsed.scheme}://{parsed.netloc}"
            except Exception:
                pass

    def run(self):
        import requests
        while self._running:
            try:
                domain = urlparse(self.target_url).netloc or "pawchive.pw"
                t0 = time.perf_counter()
                resp = requests.head(
                    self.target_url,
                    timeout=6,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
                    verify=False
                )
                latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
                self.ping_result.emit(resp.status_code, latency_ms, domain)
            except Exception:
                domain = urlparse(self.target_url).netloc or "pawchive.pw"
                self.ping_result.emit(0, 0, domain)

            # Sleep 60 seconds between ping checks
            for _ in range(120):
                if not self._running:
                    return
                self.msleep(500)


class QueueTabWidget(QWidget):
    """
    Queue Tab Widget displaying:
    - Real-time download metrics cards (Speed, Active Threads, Queue count, Server Response, Total Data)
    - Live file downloads table with granular per-file progress, completed vs remaining bytes, speed & actions
    - Batch job queue view for inspecting and managing pending queue files in appdata/jobs
    """

    def __init__(self, parent_app, scale=1.0):
        super().__init__()
        self.parent_app = parent_app
        self.scale = scale

        # State tracking
        self.files_data = {}         # key: str(filename) -> dict of file state
        self.file_row_map = {}       # key: str(filename) -> row_index
        self.row_file_map = {}       # key: int(row_index) -> file_key
        self.speed_history = deque(maxlen=20)
        self.peak_speed_bps = 0.0
        self.current_speed_bps = 0.0
        self.total_downloaded_session = 0
        self.current_filter = "all"  # all, downloading, queued, completed, failed
        self.search_query = ""

        # Auto-scroll tracking in "all" tab
        self.auto_scroll_enabled = True
        self._last_auto_scroll_time = 0.0
        self._last_scrolled_row = -1
        self._user_is_scrolling = False

        # Server response state
        self.server_status_code = 200
        self.server_latency_ms = 45
        self.server_domain = "pawchive.pw"

        self._setup_ui()

        # Update and speed calculation timer (runs every 1 second)
        self.stats_timer = QTimer(self)
        self.stats_timer.timeout.connect(self._recalculate_and_refresh_stats)
        self.stats_timer.start(1000)

        # Batch job queue auto-refresh timer (runs every 4 seconds)
        self.jobs_timer = QTimer(self)
        self.jobs_timer.timeout.connect(self.refresh_jobs_queue)
        self.jobs_timer.start(4000)

        # Background server ping worker
        self.ping_worker = ServerPingWorker(self)
        self.ping_worker.ping_result.connect(self._on_server_ping_result)
        self.ping_worker.start()

    # ─────────────────────────────────────────────────────────────────────────
    #  UI Construction
    # ─────────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        scale = self.scale
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            int(14 * scale), int(12 * scale), int(14 * scale), int(12 * scale)
        )
        layout.setSpacing(int(10 * scale))

        # Top Header & Stats Cards Banner
        self._setup_metric_cards(layout)

        # Toolbar (Filter Chips, Search bar, and Action Buttons)
        self._setup_toolbar(layout)

        # Main Segmented View (Live Downloads & Job Queue)
        self.tab_view = QTabWidget()
        self.tab_view.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #383D48; border-radius: 6px; background-color: #24262B; }"
            "QTabBar::tab { background: #2A2D34; color: #BAC0CB; padding: 6px 14px; font-weight: bold; border-top-left-radius: 5px; border-top-right-radius: 5px; margin-right: 2px; }"
            "QTabBar::tab:selected { background: #383D48; color: #60A5FA; border-bottom: 2px solid #3B82F6; }"
            "QTabBar::tab:hover { background: #32353E; color: #FFFFFF; }"
        )

        # Page 1: Live Downloads Table
        self.files_page = QWidget()
        files_v = QVBoxLayout(self.files_page)
        files_v.setContentsMargins(int(8 * scale), int(8 * scale), int(8 * scale), int(8 * scale))
        self._setup_files_table(files_v)
        self.tab_view.addTab(self.files_page, "Live File Downloads")

        # Page 2: Batch Job Queue Table
        self.jobs_page = QWidget()
        jobs_v = QVBoxLayout(self.jobs_page)
        jobs_v.setContentsMargins(int(8 * scale), int(8 * scale), int(8 * scale), int(8 * scale))
        self._setup_jobs_table(jobs_v)
        self.tab_view.addTab(self.jobs_page, "Batch Job Queue (0)")

        layout.addWidget(self.tab_view, 1)

    def _setup_metric_cards(self, parent_layout):
        scale = self.scale
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(int(8 * scale))

        def create_card(title: str, default_val: str, default_sub: str, icon_name: str):
            card = QFrame()
            card.setStyleSheet(
                "QFrame {"
                "   background-color: #24262B;"
                "   border: 1px solid #383D48;"
                "   border-radius: 6px;"
                "}"
                "QFrame:hover {"
                "   border-color: #4C5362;"
                "}"
            )
            card_v = QVBoxLayout(card)
            card_v.setContentsMargins(int(10 * scale), int(8 * scale), int(10 * scale), int(8 * scale))
            card_v.setSpacing(int(2 * scale))

            # Header row (Icon + Title)
            header_h = QHBoxLayout()
            header_h.setSpacing(int(5 * scale))

            icon_lbl = QLabel()
            icon_lbl.setPixmap(QIcon(get_asset_path(f"assets/Svg/{icon_name}.svg")).pixmap(int(14 * scale), int(14 * scale)))
            header_h.addWidget(icon_lbl)

            title_lbl = QLabel(title.upper())
            title_lbl.setStyleSheet("color: #8E95A5; font-size: 7.5pt; font-weight: bold; letter-spacing: 0.5px;")
            header_h.addWidget(title_lbl)
            header_h.addStretch(1)
            card_v.addLayout(header_h)

            # Large Value
            val_lbl = QLabel(default_val)
            val_lbl.setStyleSheet("color: #F3F4F6; font-size: 13pt; font-weight: 700; padding-top: 1px;")
            card_v.addWidget(val_lbl)

            # Subtitle / Details
            sub_lbl = QLabel(default_sub)
            sub_lbl.setStyleSheet("color: #717888; font-size: 8pt;")
            sub_lbl.setWordWrap(True)
            card_v.addWidget(sub_lbl)

            return card, val_lbl, sub_lbl

        # 1. Download Speed Card
        self.card_speed, self.val_speed, self.sub_speed = create_card(
            "Download Speed", "0.0 KB/s", "Average: 0.0 KB/s • Peak: 0.0 KB/s", "download"
        )
        cards_layout.addWidget(self.card_speed, 1)

        # 2. Queue Status Card
        self.card_queue, self.val_queue, self.sub_queue = create_card(
            "Queue Status", "Idle", "0 in queue • 0 active • 0 done", "queue"
        )
        cards_layout.addWidget(self.card_queue, 1)

        # 3. Active Threads Card
        self.card_threads, self.val_threads, self.sub_threads = create_card(
            "Worker Threads", "0 Active", "Configured: 4 threads", "settings"
        )
        cards_layout.addWidget(self.card_threads, 1)

        # 4. Server Response Health Card
        self.card_server, self.val_server, self.sub_server = create_card(
            "Server Response", "200 OK • 45ms", "pawchive.pw • Healthy", "check"
        )
        cards_layout.addWidget(self.card_server, 1)

        # 5. Data Transferred Card
        self.card_data, self.val_data, self.sub_data = create_card(
            "Transferred", "0 B", "Completed: 0 B • Remaining: 0 B", "archive"
        )
        cards_layout.addWidget(self.card_data, 1)

        parent_layout.addLayout(cards_layout)

    def _setup_toolbar(self, parent_layout):
        scale = self.scale
        toolbar_h = QHBoxLayout()
        toolbar_h.setSpacing(int(6 * scale))

        # Filter Chips Group
        self.filter_btns = {}
        for key, label in [("all", "All"), ("downloading", "Active"), ("queued", "Queued"), ("completed", "Done"), ("failed", "Failed")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == "all")
            btn.setFixedHeight(int(26 * scale))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton {"
                "   background-color: #2A2D34; border: 1px solid #383D48; border-radius: 4px;"
                "   color: #BAC0CB; font-size: 8.5pt; font-weight: 600; padding: 2px 10px;"
                "}"
                "QPushButton:checked {"
                "   background-color: #2563EB; border-color: #3B82F6; color: #FFFFFF;"
                "}"
                "QPushButton:hover:!checked {"
                "   background-color: #353842; border-color: #4C5362;"
                "}"
            )
            btn.clicked.connect(lambda checked, k=key: self._set_filter(k))
            toolbar_h.addWidget(btn)
            self.filter_btns[key] = btn

        toolbar_h.addSpacing(int(8 * scale))

        # Search Bar
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter files by name, creator, or status...")
        self.search_input.setFixedHeight(int(26 * scale))
        self.search_input.setStyleSheet(
            "QLineEdit {"
            "   background-color: #202328; border: 1px solid #383D48; border-radius: 4px;"
            "   color: #DDE2EB; padding: 2px 8px; font-size: 8.5pt;"
            "}"
            "QLineEdit:focus { border: 1px solid #3B82F6; background-color: #1A1C20; }"
        )
        self.search_input.textChanged.connect(self._on_search_changed)
        toolbar_h.addWidget(self.search_input, 1)

        toolbar_h.addSpacing(int(8 * scale))

        # Quick Control Buttons
        self.pause_resume_btn = QPushButton(" Pause")
        self.pause_resume_btn.setIcon(QIcon(get_asset_path("assets/Svg/pause.svg")))
        self.pause_resume_btn.setFixedHeight(int(26 * scale))
        self.pause_resume_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_resume_btn.setStyleSheet(
            "QPushButton {"
            "   background-color: #2A2D34; border: 1px solid #383D48; border-radius: 4px;"
            "   color: #E2E8F0; font-size: 8.5pt; padding: 2px 10px;"
            "}"
            "QPushButton:hover { background-color: #353842; border-color: #60A5FA; }"
        )
        self.pause_resume_btn.clicked.connect(self._toggle_pause_resume)
        toolbar_h.addWidget(self.pause_resume_btn)

        self.cancel_btn = QPushButton(" Cancel Active")
        self.cancel_btn.setIcon(QIcon(get_asset_path("assets/Svg/cancel.svg")))
        self.cancel_btn.setFixedHeight(int(26 * scale))
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setStyleSheet(
            "QPushButton {"
            "   background-color: #2A2D34; border: 1px solid #383D48; border-radius: 4px;"
            "   color: #F87171; font-size: 8.5pt; padding: 2px 10px;"
            "}"
            "QPushButton:hover { background-color: #451A1A; border-color: #EF4444; }"
        )
        self.cancel_btn.clicked.connect(self._cancel_active_downloads)
        toolbar_h.addWidget(self.cancel_btn)

        self.clear_completed_btn = QPushButton(" Clear Done")
        self.clear_completed_btn.setIcon(QIcon(get_asset_path("assets/Svg/trash.svg")))
        self.clear_completed_btn.setFixedHeight(int(26 * scale))
        self.clear_completed_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_completed_btn.setStyleSheet(
            "QPushButton {"
            "   background-color: #2A2D34; border: 1px solid #383D48; border-radius: 4px;"
            "   color: #BAC0CB; font-size: 8.5pt; padding: 2px 10px;"
            "}"
            "QPushButton:hover { background-color: #353842; border-color: #4C5362; }"
        )
        self.clear_completed_btn.clicked.connect(self.clear_completed)
        toolbar_h.addWidget(self.clear_completed_btn)

        self.open_folder_btn = QPushButton(" Open Folder")
        self.open_folder_btn.setIcon(QIcon(get_asset_path("assets/Svg/folder.svg")))
        self.open_folder_btn.setFixedHeight(int(26 * scale))
        self.open_folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_folder_btn.setStyleSheet(
            "QPushButton {"
            "   background-color: #2A2D34; border: 1px solid #383D48; border-radius: 4px;"
            "   color: #BAC0CB; font-size: 8.5pt; padding: 2px 10px;"
            "}"
            "QPushButton:hover { background-color: #353842; border-color: #60A5FA; }"
        )
        self.open_folder_btn.clicked.connect(self._open_current_download_dir)
        toolbar_h.addWidget(self.open_folder_btn)

        self.auto_scroll_btn = QPushButton(" Auto-Scroll")
        self.auto_scroll_btn.setIcon(QIcon(get_asset_path("assets/Svg/scroll.svg")))
        self.auto_scroll_btn.setCheckable(True)
        self.auto_scroll_btn.setChecked(True)
        self.auto_scroll_btn.setFixedHeight(int(26 * scale))
        self.auto_scroll_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_scroll_btn.setStyleSheet(
            "QPushButton {"
            "   background-color: #2A2D34; border: 1px solid #383D48; border-radius: 4px;"
            "   color: #BAC0CB; font-size: 8.5pt; padding: 2px 10px;"
            "}"
            "QPushButton:hover { background-color: #353842; border-color: #60A5FA; }"
            "QPushButton:checked {"
            "   background-color: #1E3A8A; border: 1px solid #3B82F6; color: #93C5FD; font-weight: bold;"
            "}"
        )
        self.auto_scroll_btn.setToolTip("Automatically scroll to active downloads in the All tab as progress advances.")
        self.auto_scroll_btn.toggled.connect(self._toggle_auto_scroll)
        toolbar_h.addWidget(self.auto_scroll_btn)

        parent_layout.addLayout(toolbar_h)

    def _setup_files_table(self, parent_layout):
        scale = self.scale
        # Columns: Status, File Name, Creator/Post, Progress, Completed, Remaining, Total Size, Speed, Actions
        self.files_table = QTableWidget(0, 9)
        self.files_table.setHorizontalHeaderLabels([
            "Status", "File Name", "Creator / Post", "Progress",
            "Completed", "Remaining", "Total Size", "Speed", "Actions"
        ])
        th = self.files_table.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        th.resizeSection(0, int(96 * scale))
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        th.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(2, int(150 * scale))
        th.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(3, int(160 * scale))
        th.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(4, int(85 * scale))
        th.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(5, int(85 * scale))
        th.setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(6, int(85 * scale))
        th.setSectionResizeMode(7, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(7, int(85 * scale))
        th.setSectionResizeMode(8, QHeaderView.ResizeMode.Fixed)
        th.resizeSection(8, int(60 * scale))

        self.files_table.verticalHeader().setVisible(False)
        self.files_table.verticalHeader().setDefaultSectionSize(int(38 * scale))
        self.files_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.files_table.setAlternatingRowColors(True)
        self.files_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.files_table.customContextMenuRequested.connect(self._show_file_context_menu)

        vsb = self.files_table.verticalScrollBar()
        if vsb:
            vsb.sliderPressed.connect(self._on_scroll_slider_pressed)
            vsb.sliderReleased.connect(self._on_scroll_slider_released)

        parent_layout.addWidget(self.files_table, 1)

    def _setup_jobs_table(self, parent_layout):
        scale = self.scale
        # Columns: #, Job File, Target URL, Output Directory, Scheduled Time, Actions
        self.jobs_table = QTableWidget(0, 6)
        self.jobs_table.setHorizontalHeaderLabels([
            "#", "Job File", "Target URL / Creator", "Output Directory", "Created", "Actions"
        ])
        th = self.jobs_table.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        th.resizeSection(0, int(40 * scale))
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(1, int(180 * scale))
        th.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        th.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(3, int(220 * scale))
        th.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        th.resizeSection(4, int(120 * scale))
        th.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        th.resizeSection(5, int(120 * scale))

        self.jobs_table.verticalHeader().setVisible(False)
        self.jobs_table.verticalHeader().setDefaultSectionSize(int(36 * scale))
        self.jobs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs_table.setAlternatingRowColors(True)

        # Bottom row for jobs: Execute All button & clear button
        btn_row = QHBoxLayout()
        btn_row.setSpacing(int(8 * scale))
        self.exec_jobs_btn = QPushButton(" Execute All Queued Jobs")
        self.exec_jobs_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.exec_jobs_btn.setFixedHeight(int(28 * scale))
        self.exec_jobs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.exec_jobs_btn.setStyleSheet(
            "QPushButton {"
            "   background-color: #1565A8; border: 1px solid #1E88E5; border-radius: 4px;"
            "   color: #FFFFFF; font-weight: bold; font-size: 8.5pt; padding: 2px 14px;"
            "}"
            "QPushButton:hover { background-color: #1976D2; }"
        )
        self.exec_jobs_btn.clicked.connect(self._execute_all_jobs)
        btn_row.addWidget(self.exec_jobs_btn)

        self.refresh_jobs_btn = QPushButton(" Refresh")
        self.refresh_jobs_btn.setIcon(QIcon(get_asset_path("assets/Svg/sync.svg")))
        self.refresh_jobs_btn.setFixedHeight(int(28 * scale))
        self.refresh_jobs_btn.clicked.connect(self.refresh_jobs_queue)
        btn_row.addWidget(self.refresh_jobs_btn)

        btn_row.addStretch(1)
        self.jobs_count_lbl = QLabel("0 queued batch jobs found.")
        self.jobs_count_lbl.setStyleSheet("color: #7EB6E0; font-size: 8.5pt;")
        btn_row.addWidget(self.jobs_count_lbl)

        parent_layout.addWidget(self.jobs_table, 1)
        parent_layout.addLayout(btn_row)

    # ─────────────────────────────────────────────────────────────────────────
    #  Live Signal Handlers
    # ─────────────────────────────────────────────────────────────────────────

    def on_files_queued(self, files_list: list):
        """
        Invoked when candidate files are discovered from processed posts and are
        placed in the queue waiting for worker threads to become available.
        """
        if not files_list:
            return

        now = time.time()
        for finfo in files_list:
            fname = finfo.get('filename') or finfo.get('name') or ""
            if not fname:
                continue
            file_key = str(fname)
            if file_key not in self.files_data:
                size = finfo.get('size', 0) or 0
                creator = finfo.get('creator') or getattr(self.parent_app, 'last_known_creator_name', "") or "Creator"
                self.files_data[file_key] = {
                    'id': file_key,
                    'filename': fname,
                    'creator': creator,
                    'post_title': finfo.get('post_title', ''),
                    'post_id': finfo.get('post_id', ''),
                    'status': 'queued',
                    'downloaded_bytes': 0,
                    'total_bytes': size,
                    'speed_bps': 0.0,
                    'start_time': now,
                    'last_time': now,
                    'last_bytes': 0,
                    'save_path': "",
                    'error_msg': ""
                }
                self._update_or_insert_file_row(file_key)

        self._recalculate_and_refresh_stats()

    def on_file_skipped(self, filename: str):
        """
        Invoked when a file is skipped (hash match in DB/cache, already exists on disk,
        or size/filter threshold).
        """
        if not filename:
            return
        file_key = str(filename)
        if file_key not in self.files_data:
            # Check if any recorded file matches disk name or stem
            for k, f in self.files_data.items():
                if f.get('filename') == filename or os.path.basename(f.get('save_path', '')) == filename:
                    file_key = k
                    break

        if file_key not in self.files_data:
            now = time.time()
            self.files_data[file_key] = {
                'id': file_key,
                'filename': filename,
                'creator': getattr(self.parent_app, 'last_known_creator_name', "") or "Skipped File",
                'status': 'skipped',
                'downloaded_bytes': 0,
                'total_bytes': 0,
                'speed_bps': 0.0,
                'start_time': now,
                'last_time': now,
                'last_bytes': 0,
                'save_path': "",
                'error_msg': ""
            }

        file_item = self.files_data[file_key]
        file_item['status'] = 'skipped'
        file_item['speed_bps'] = 0.0
        self._update_or_insert_file_row(file_key)
        self._recalculate_and_refresh_stats()

    def on_file_progress(self, filename: str, progress_info):
        """
        Invoked on each file download chunk or update.
        progress_info:
          - tuple: (downloaded_bytes, total_bytes)
          - list: [{'downloaded': int, 'total': int, 'speed_bps': int, 'active': bool}, ...]
          - None / str
        """
        if not filename:
            return

        now = time.time()
        file_key = str(filename)

        if file_key not in self.files_data:
            for k, f in self.files_data.items():
                if f.get('filename') == filename:
                    file_key = k
                    break

        if file_key in self.files_data and self.files_data[file_key].get('status') in ('completed', 'skipped'):
            # Already completed/skipped - ignore chunk progress so it never regresses to downloading
            return

        if file_key not in self.files_data:
            self.files_data[file_key] = {
                'id': file_key,
                'filename': filename,
                'creator': getattr(self.parent_app, 'last_known_creator_name', "") or "Active Post",
                'status': 'downloading',
                'downloaded_bytes': 0,
                'total_bytes': 0,
                'speed_bps': 0.0,
                'start_time': now,
                'last_time': now,
                'last_bytes': 0,
                'save_path': "",
                'error_msg': ""
            }

        file_item = self.files_data[file_key]

        # Handle tuple progress (downloaded, total)
        if isinstance(progress_info, tuple) and len(progress_info) == 2:
            downloaded, total = progress_info
            file_item['downloaded_bytes'] = max(file_item['downloaded_bytes'], downloaded)
            if total > 0:
                file_item['total_bytes'] = total

            # Check if finished: if downloaded reached total, mark completed!
            if total > 0 and downloaded >= total:
                file_item['status'] = 'completed'
                file_item['speed_bps'] = 0.0
            elif file_item.get('status') != 'completed':
                file_item['status'] = 'downloading'
                file_item['error_msg'] = ""

            # Compute instantaneous speed only if still downloading
            if file_item['status'] == 'downloading':
                dt = now - file_item['last_time']
                if dt >= 0.5:
                    delta_b = file_item['downloaded_bytes'] - file_item['last_bytes']
                    file_item['speed_bps'] = max(0.0, delta_b / dt)
                    file_item['last_time'] = now
                    file_item['last_bytes'] = file_item['downloaded_bytes']

        # Handle multipart chunks list
        elif isinstance(progress_info, list):
            tot_dl = sum(cs.get('downloaded', 0) for cs in progress_info)
            tot_sz = sum(cs.get('total', 0) for cs in progress_info)
            combined_speed = sum(cs.get('speed_bps', 0) for cs in progress_info) / 8.0  # bytes/sec

            file_item['downloaded_bytes'] = tot_dl
            if tot_sz > 0:
                file_item['total_bytes'] = tot_sz

            if tot_sz > 0 and tot_dl >= tot_sz:
                file_item['status'] = 'completed'
                file_item['speed_bps'] = 0.0
            elif file_item.get('status') != 'completed':
                file_item['status'] = 'downloading'
                file_item['error_msg'] = ""
                file_item['speed_bps'] = combined_speed

        elif progress_info is None:
            # File progress cleared, but wait for on_file_completed for final 100%
            pass

        self._update_or_insert_file_row(file_key)

    def on_file_completed(self, file_details_dict: dict):
        """Invoked when a file has successfully finished downloading."""
        if not file_details_dict:
            return

        disk_name = str(file_details_dict.get('disk_filename', '') or '')
        orig_name = str(file_details_dict.get('api_original_filename', '') or '')

        file_key = None
        if disk_name and disk_name in self.files_data:
            file_key = disk_name
        elif orig_name and orig_name in self.files_data:
            file_key = orig_name
        else:
            # Match by filename field
            for k, f in self.files_data.items():
                if f.get('filename') in (disk_name, orig_name) or k in (disk_name, orig_name):
                    file_key = k
                    break

        if not file_key:
            # Enhanced fallback matching: normalize and check basenames / sub-strings
            norm_targets = {
                disk_name.lower(), orig_name.lower(),
                os.path.basename(disk_name).lower(), os.path.basename(orig_name).lower()
            }
            norm_targets.discard("")
            for k, f in self.files_data.items():
                fn = (f.get('filename') or "").lower()
                k_norm = str(k).lower()
                if fn in norm_targets or k_norm in norm_targets:
                    file_key = k
                    break
                if any(t and (t in fn or fn in t) for t in norm_targets if len(t) > 6):
                    file_key = k
                    break

        now = time.time()
        if not file_key:
            file_key = disk_name or orig_name
            if not file_key:
                return
            self.files_data[file_key] = {
                'id': file_key,
                'filename': disk_name or orig_name,
                'creator': file_details_dict.get('creator_display_name', "Creator"),
                'status': 'completed',
                'downloaded_bytes': 0,
                'total_bytes': 0,
                'speed_bps': 0.0,
                'start_time': now,
                'last_time': now,
                'last_bytes': 0,
                'save_path': "",
                'error_msg': ""
            }

        file_item = self.files_data[file_key]
        file_item['status'] = 'completed'
        file_item['speed_bps'] = 0.0
        if disk_name:
            file_item['filename'] = disk_name

        # Alias both disk_name and orig_name in files_data to this file_item
        if disk_name:
            self.files_data[disk_name] = file_item
        if orig_name:
            self.files_data[orig_name] = file_item

        file_item['save_path'] = os.path.join(
            file_details_dict.get('download_path', ""),
            disk_name or orig_name
        )
        if file_details_dict.get('creator_display_name'):
            file_item['creator'] = file_details_dict['creator_display_name']

        # Determine file size from disk if available
        if file_item['save_path'] and os.path.exists(file_item['save_path']):
            try:
                sz = os.path.getsize(file_item['save_path'])
                file_item['total_bytes'] = sz
                file_item['downloaded_bytes'] = sz
                self.total_downloaded_session += sz
            except OSError:
                pass
        else:
            if file_item['total_bytes'] > 0:
                file_item['downloaded_bytes'] = file_item['total_bytes']

        self._update_or_insert_file_row(file_key)
        # Update row mappings for both names if row is known
        if file_key in self.file_row_map:
            row = self.file_row_map[file_key]
            if disk_name:
                self.file_row_map[disk_name] = row
            if orig_name:
                self.file_row_map[orig_name] = row
            self.row_file_map[row] = file_key

        self._recalculate_and_refresh_stats()

    def on_file_failed(self, failure_list: list, retryable: bool = False):
        """Invoked when files fail during download."""
        if not failure_list:
            return

        for fail_info in failure_list:
            if not fail_info:
                continue

            candidates = []
            err_msg = "Download error"
            creator = ""

            if isinstance(fail_info, str):
                candidates.append(fail_info.strip())
                candidates.append(os.path.basename(fail_info.strip()))
            elif isinstance(fail_info, dict):
                err_msg = (
                    fail_info.get('error')
                    or fail_info.get('error_reason')
                    or fail_info.get('reason')
                    or "Download error"
                )
                creator = fail_info.get('creator') or fail_info.get('creator_display_name') or ""

                for key in ('filename', 'forced_filename_override', 'api_original_filename', 'name'):
                    val = fail_info.get(key)
                    if val and isinstance(val, str):
                        clean_val = val.strip()
                        candidates.append(clean_val)
                        candidates.append(os.path.basename(clean_val))

                file_info = fail_info.get('file_info')
                if isinstance(file_info, dict):
                    for key in ('name', 'filename', 'original_filename', 'path'):
                        val = file_info.get(key)
                        if val and isinstance(val, str):
                            clean_val = val.strip()
                            candidates.append(clean_val)
                            candidates.append(os.path.basename(clean_val))

            # Deduplicate preserving order
            seen_cands = set()
            clean_candidates = []
            for c in candidates:
                if c and c not in seen_cands:
                    seen_cands.add(c)
                    clean_candidates.append(c)

            if not clean_candidates:
                continue

            # Find matching file_key in files_data
            file_key = None
            for cand in clean_candidates:
                if cand in self.files_data:
                    file_key = cand
                    break

            if not file_key:
                for k, item in self.files_data.items():
                    fn = item.get('filename', '')
                    sp = os.path.basename(item.get('save_path', ''))
                    for cand in clean_candidates:
                        if cand in (k, fn, sp) or cand.lower() in (k.lower(), fn.lower(), sp.lower()):
                            file_key = k
                            break
                    if file_key:
                        break

            now = time.time()
            if not file_key:
                file_key = clean_candidates[0]
                self.files_data[file_key] = {
                    'id': file_key,
                    'filename': file_key,
                    'creator': creator or getattr(self.parent_app, 'last_known_creator_name', "") or "Creator",
                    'status': 'failed',
                    'downloaded_bytes': 0,
                    'total_bytes': 0,
                    'speed_bps': 0.0,
                    'start_time': now,
                    'last_time': now,
                    'last_bytes': 0,
                    'save_path': "",
                    'error_msg': err_msg
                }

            file_item = self.files_data[file_key]
            file_item['status'] = 'failed'
            file_item['speed_bps'] = 0.0
            file_item['error_msg'] = err_msg
            if creator and not file_item.get('creator'):
                file_item['creator'] = creator

            # Alias candidate names in files_data
            for cand in clean_candidates:
                if cand:
                    self.files_data[cand] = file_item

            self._update_or_insert_file_row(file_key)

        self._recalculate_and_refresh_stats()

    def on_overall_progress(self, total: int, processed: int, unit: str = "posts"):
        """Called on batch post-level progress updates."""
        # Update queue status subtitle
        active_count = sum(1 for f in self.files_data.values() if f.get('status') == 'downloading')
        done_count = sum(1 for f in self.files_data.values() if f.get('status') == 'completed')
        queued_count = max(0, total - processed) if total > 0 else 0
        self.sub_queue.setText(f"{queued_count} in queue • {active_count} active • {done_count} done")

    def on_download_started(self):
        """Called when a download cycle starts."""
        self.pause_resume_btn.setText(" Pause")
        self.pause_resume_btn.setIcon(QIcon(get_asset_path("assets/Svg/pause.svg")))
        self.val_queue.setText("Active")
        self._recalculate_and_refresh_stats()

    def on_download_finished(self):
        """Called when a download cycle finishes."""
        self.pause_resume_btn.setText(" Pause")
        self.val_speed.setText("0.0 KB/s")
        self.val_threads.setText("0 Active")
        self.current_speed_bps = 0.0
        for f in self.files_data.values():
            if f.get('status') == 'downloading':
                f['status'] = 'completed'
                f['speed_bps'] = 0.0
                self._update_or_insert_file_row(f['id'])
        self._recalculate_and_refresh_stats()

    def on_pause_resumed(self, is_paused: bool):
        """Called when pause/resume state changes."""
        if is_paused:
            self.pause_resume_btn.setText(" Resume")
            self.pause_resume_btn.setIcon(QIcon(get_asset_path("assets/Svg/sync.svg")))
            self.val_queue.setText("Paused")
            self.val_speed.setText("0.0 KB/s")
        else:
            self.pause_resume_btn.setText(" Pause")
            self.pause_resume_btn.setIcon(QIcon(get_asset_path("assets/Svg/pause.svg")))
            self.val_queue.setText("Active")

    def on_tab_activated(self):
        """Called whenever the user clicks to view the Queue tab."""
        self.refresh_jobs_queue()
        self._recalculate_and_refresh_stats()
        if self.current_filter == "all" and getattr(self, 'auto_scroll_enabled', True):
            self._scroll_to_latest_active(ensure_visible_only=True)

    # ─────────────────────────────────────────────────────────────────────────
    #  Table Row Rendering & Updating
    # ─────────────────────────────────────────────────────────────────────────

    def _update_or_insert_file_row(self, file_key: str):
        file_item = self.files_data.get(file_key)
        if not file_item:
            return

        scale = self.scale
        table = self.files_table

        # Determine row index safely with multi-tier checks
        row = None
        if file_key in self.file_row_map:
            row = self.file_row_map[file_key]
        elif file_item.get('id') and file_item.get('id') in self.file_row_map:
            row = self.file_row_map[file_item['id']]
        elif file_item.get('filename') and file_item.get('filename') in self.file_row_map:
            row = self.file_row_map[file_item['filename']]
        else:
            for r in range(table.rowCount()):
                it = table.item(r, 1)
                if it and (it.data(Qt.ItemDataRole.UserRole) == file_key or it.text() == file_key or it.text() == file_item.get('filename')):
                    row = r
                    break

        if row is None or row >= table.rowCount():
            row = table.rowCount()
            table.insertRow(row)
            table.setRowHeight(row, int(38 * scale))

            # Setup widgets once on row insertion
            status_lbl = QLabel()
            status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setCellWidget(row, 0, status_lbl)

            it_fn = QTableWidgetItem(file_item.get('filename', ''))
            it_fn.setData(Qt.ItemDataRole.UserRole, file_item.get('id') or file_key)
            table.setItem(row, 1, it_fn)

            table.setItem(row, 2, QTableWidgetItem(file_item.get('creator', '')))

            prog_bar = QProgressBar()
            prog_bar.setRange(0, 100)
            prog_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            prog_bar.setFixedHeight(int(18 * scale))
            prog_bar.setStyleSheet(
                "QProgressBar {"
                "   background-color: #1A1C20; border: 1px solid #383D48; border-radius: 4px;"
                "   text-align: center; color: #FFFFFF; font-size: 8pt; font-weight: bold;"
                "}"
                "QProgressBar::chunk {"
                "   background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563EB, stop:1 #3B82F6);"
                "   border-radius: 3px;"
                "}"
            )
            table.setCellWidget(row, 3, prog_bar)

            table.setItem(row, 4, QTableWidgetItem("0 B"))
            table.setItem(row, 5, QTableWidgetItem("0 B"))
            table.setItem(row, 6, QTableWidgetItem("Unknown"))
            table.setItem(row, 7, QTableWidgetItem("0.0 KB/s"))

            # Actions button (Folder icon)
            btn = QPushButton()
            btn.setIcon(QIcon(get_asset_path("assets/Svg/folder.svg")))
            btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
            btn.setFixedSize(int(26 * scale), int(26 * scale))
            btn.setToolTip("Open folder containing file")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { background: #2A2D34; border: 1px solid #383D48; border-radius: 4px; }"
                "QPushButton:hover { background: #353842; border-color: #60A5FA; }"
            )
            btn.clicked.connect(lambda chk=False, fk=file_key: self._open_file_folder(fk))
            action_container = QWidget()
            action_layout = QHBoxLayout(action_container)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            action_layout.addWidget(btn)
            table.setCellWidget(row, 8, action_container)

        # Update mapping
        self.file_row_map[file_key] = row
        self.row_file_map[row] = file_key
        if file_item.get('id'):
            self.file_row_map[file_item['id']] = row
        if file_item.get('filename'):
            self.file_row_map[file_item['filename']] = row

        # Update row data
        status = file_item.get('status', 'downloading')
        dl_bytes = file_item.get('downloaded_bytes', 0)
        tot_bytes = file_item.get('total_bytes', 0)
        speed = file_item.get('speed_bps', 0.0)

        # Safeguard: if downloaded bytes reached total or >= 100%, status must be completed
        if status == 'downloading' and tot_bytes > 0 and (dl_bytes >= tot_bytes or int((dl_bytes / tot_bytes) * 100) >= 100):
            status = 'completed'
            file_item['status'] = 'completed'
            file_item['speed_bps'] = 0.0
            speed = 0.0

        # 0. Status Badge
        status_lbl = table.cellWidget(row, 0)
        if status_lbl:
            if status == 'downloading':
                status_lbl.setText("Downloading")
                status_lbl.setStyleSheet(
                    "background-color: #1E3A8A; color: #93C5FD; border: 1px solid #2563EB; "
                    "border-radius: 4px; font-size: 7.5pt; font-weight: bold; padding: 2px 4px;"
                )
            elif status == 'completed':
                status_lbl.setText("Completed")
                status_lbl.setStyleSheet(
                    "background-color: #064E3B; color: #6EE7B7; border: 1px solid #059669; "
                    "border-radius: 4px; font-size: 7.5pt; font-weight: bold; padding: 2px 4px;"
                )
            elif status == 'skipped':
                status_lbl.setText("Skipped")
                status_lbl.setStyleSheet(
                    "background-color: #312E81; color: #C7D2FE; border: 1px solid #4F46E5; "
                    "border-radius: 4px; font-size: 7.5pt; font-weight: bold; padding: 2px 4px;"
                )
            elif status == 'failed':
                status_lbl.setText("Failed")
                status_lbl.setStyleSheet(
                    "background-color: #7F1D1D; color: #FCA5A5; border: 1px solid #DC2626; "
                    "border-radius: 4px; font-size: 7.5pt; font-weight: bold; padding: 2px 4px;"
                )
                status_lbl.setToolTip(file_item.get('error_msg') or "Failed")
            else:
                status_lbl.setText("Queued")
                status_lbl.setStyleSheet(
                    "background-color: #78350F; color: #FDE68A; border: 1px solid #D97706; "
                    "border-radius: 4px; font-size: 7.5pt; font-weight: bold; padding: 2px 4px;"
                )
                status_lbl.setToolTip("")

        # 1. Filename & 2. Creator
        it_fn = table.item(row, 1)
        if it_fn:
            it_fn.setText(file_item.get('filename', ''))
            it_fn.setData(Qt.ItemDataRole.UserRole, file_item.get('id') or file_key)
        it_cr = table.item(row, 2)
        if it_cr:
            it_cr.setText(file_item.get('creator', ''))

        # 3. Progress Bar & Percent
        prog_bar = table.cellWidget(row, 3)
        if prog_bar:
            if status == 'completed':
                prog_bar.setFormat("%p%")
                prog_bar.setValue(100)
                prog_bar.setStyleSheet(
                    "QProgressBar {"
                    "   background-color: #1A1C20; border: 1px solid #059669; border-radius: 4px;"
                    "   text-align: center; color: #FFFFFF; font-size: 8pt; font-weight: bold;"
                    "}"
                    "QProgressBar::chunk { background-color: #10B981; border-radius: 3px; }"
                )
            elif status == 'skipped':
                prog_bar.setFormat("Skipped")
                prog_bar.setValue(100)
                prog_bar.setStyleSheet(
                    "QProgressBar {"
                    "   background-color: #1A1C20; border: 1px solid #4F46E5; border-radius: 4px;"
                    "   text-align: center; color: #C7D2FE; font-size: 8pt; font-weight: bold;"
                    "}"
                    "QProgressBar::chunk { background-color: #4F46E5; border-radius: 3px; }"
                )
            elif status == 'failed':
                err_text = file_item.get('error_msg') or "Failed"
                if "429" in err_text:
                    disp = "Failed (429 Rate Limit)"
                elif "403" in err_text:
                    disp = "Failed (403 Forbidden)"
                elif "404" in err_text:
                    disp = "Failed (404 Not Found)"
                elif any(c in err_text for c in ("500", "502", "503", "504", "Server Error")):
                    disp = "Failed (Server Error)"
                elif "Timeout" in err_text or "timed out" in err_text.lower():
                    disp = "Failed (Timeout)"
                else:
                    disp = f"Failed ({err_text[:16]})" if len(err_text) <= 20 else "Failed"
                prog_bar.setFormat(disp)
                prog_bar.setValue(0)
                prog_bar.setStyleSheet(
                    "QProgressBar {"
                    "   background-color: #1A1C20; border: 1px solid #7F1D1D; border-radius: 4px;"
                    "   text-align: center; color: #FCA5A5; font-size: 7.5pt; font-weight: bold;"
                    "}"
                    "QProgressBar::chunk { background-color: #7F1D1D; border-radius: 3px; }"
                )
            elif status == 'queued':
                prog_bar.setFormat("Waiting for thread...")
                prog_bar.setValue(0)
                prog_bar.setStyleSheet(
                    "QProgressBar {"
                    "   background-color: #1A1C20; border: 1px solid #78350F; border-radius: 4px;"
                    "   text-align: center; color: #FDE68A; font-size: 7.5pt; font-weight: 600;"
                    "}"
                    "QProgressBar::chunk { background-color: #78350F; border-radius: 3px; }"
                )
            elif tot_bytes > 0:
                prog_bar.setFormat("%p%")
                pct = min(100, max(0, int((dl_bytes / tot_bytes) * 100)))
                prog_bar.setValue(pct)
                prog_bar.setStyleSheet(
                    "QProgressBar {"
                    "   background-color: #1A1C20; border: 1px solid #383D48; border-radius: 4px;"
                    "   text-align: center; color: #FFFFFF; font-size: 8pt; font-weight: bold;"
                    "}"
                    "QProgressBar::chunk {"
                    "   background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563EB, stop:1 #3B82F6);"
                    "   border-radius: 3px;"
                    "}"
                )
            else:
                prog_bar.setFormat("%p%")
                prog_bar.setValue(0)
                prog_bar.setStyleSheet(
                    "QProgressBar {"
                    "   background-color: #1A1C20; border: 1px solid #383D48; border-radius: 4px;"
                    "   text-align: center; color: #FFFFFF; font-size: 8pt; font-weight: bold;"
                    "}"
                    "QProgressBar::chunk {"
                    "   background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563EB, stop:1 #3B82F6);"
                    "   border-radius: 3px;"
                    "}"
                )

        # 4. Completed Bytes
        if table.item(row, 4):
            table.item(row, 4).setText(format_bytes(dl_bytes))

        # 5. Remaining Bytes
        if table.item(row, 5):
            if status in ('completed', 'skipped'):
                table.item(row, 5).setText("0 B")
            elif status == 'failed':
                table.item(row, 5).setText("-")
            elif status == 'queued':
                table.item(row, 5).setText(format_bytes(tot_bytes) if tot_bytes > 0 else "Waiting")
            elif tot_bytes > 0:
                rem_bytes = max(0, tot_bytes - dl_bytes)
                table.item(row, 5).setText(format_bytes(rem_bytes))
            else:
                table.item(row, 5).setText("Unknown")

        # 6. Total Size
        if table.item(row, 6):
            table.item(row, 6).setText(format_bytes(tot_bytes) if tot_bytes > 0 else "Unknown")

        # 7. Speed
        if table.item(row, 7):
            if status == 'downloading' and speed > 0:
                table.item(row, 7).setText(format_speed(speed))
            elif status in ('queued', 'failed', 'completed', 'skipped'):
                table.item(row, 7).setText("-")
            else:
                table.item(row, 7).setText("0.0 KB/s")

        self._apply_row_filter(row, file_item)

        # In "All" tab, auto-scroll to keep active/progressing downloads visible
        if self.current_filter == "all" and getattr(self, 'auto_scroll_enabled', True):
            if status == 'downloading':
                self._maybe_auto_scroll(row)

    def _get_file_item_for_row(self, row: int) -> dict:
        """
        Retrieves the file_item dictionary corresponding to a table row, using
        multi-tier lookups (row_file_map, QTableWidgetItem UserRole, table text,
        or searching files_data values).
        """
        # Tier 1: Look up row index in row_file_map
        fk = self.row_file_map.get(row)
        if fk and fk in self.files_data:
            return self.files_data[fk]

        # Tier 2: Look up UserRole stored in table item
        fn_item = self.files_table.item(row, 1)
        if fn_item:
            role_key = fn_item.data(Qt.ItemDataRole.UserRole)
            if role_key and role_key in self.files_data:
                return self.files_data[role_key]

            # Tier 3: Look up item text directly in files_data
            txt = fn_item.text().strip()
            if txt and txt in self.files_data:
                return self.files_data[txt]

            # Tier 4: Search files_data values by filename or save_path
            if txt:
                for f in self.files_data.values():
                    if f.get('filename') == txt or os.path.basename(f.get('save_path', '')) == txt:
                        return f

        return {}

    def _apply_row_filter(self, row: int, file_item: dict = None):
        if not file_item:
            file_item = self._get_file_item_for_row(row)

        status = file_item.get('status')
        # Visual fallback: If status is missing or ambiguous, read cell widgets directly
        if not status:
            status_lbl = self.files_table.cellWidget(row, 0)
            if status_lbl and isinstance(status_lbl, QLabel):
                badge_text = status_lbl.text().strip().lower()
                if badge_text in ('completed', 'done'):
                    status = 'completed'
                elif badge_text in ('downloading', 'active'):
                    status = 'downloading'
                elif badge_text == 'queued':
                    status = 'queued'
                elif badge_text == 'skipped':
                    status = 'skipped'
                elif badge_text == 'failed':
                    status = 'failed'

            # Secondary fallback: if progress bar is 100%, treat as completed
            prog_bar = self.files_table.cellWidget(row, 3)
            if prog_bar and isinstance(prog_bar, QProgressBar) and prog_bar.value() >= 100:
                status = 'completed'
                if file_item.get('status') == 'downloading':
                    file_item['status'] = 'completed'
            elif not status:
                status = 'downloading'

        fn_text = self.files_table.item(row, 1).text() if self.files_table.item(row, 1) else ''
        cr_text = self.files_table.item(row, 2).text() if self.files_table.item(row, 2) else ''
        filename = (file_item.get('filename') or fn_text).lower()
        creator = (file_item.get('creator') or cr_text).lower()

        # Status filter
        if self.current_filter == "downloading" and status != "downloading":
            self.files_table.setRowHidden(row, True)
            return
        elif self.current_filter == "queued" and status != "queued":
            self.files_table.setRowHidden(row, True)
            return
        elif self.current_filter == "completed" and status not in ("completed", "skipped"):
            self.files_table.setRowHidden(row, True)
            return
        elif self.current_filter == "failed" and status != "failed":
            self.files_table.setRowHidden(row, True)
            return

        # Search query filter
        if self.search_query:
            if self.search_query not in filename and self.search_query not in creator:
                self.files_table.setRowHidden(row, True)
                return

        self.files_table.setRowHidden(row, False)

    # ─────────────────────────────────────────────────────────────────────────
    #  Stats Recalculation Engine
    # ─────────────────────────────────────────────────────────────────────────

    def _recalculate_and_refresh_stats(self):
        """Called every second by stats_timer to calculate live speeds, threads, and metrics."""
        now = time.time()
        # Deduplicate files by object id to avoid double-counting aliased entries
        unique_items = list({id(f): f for f in self.files_data.values()}.values())
        active_items = [f for f in unique_items if f.get('status') == 'downloading']
        queued_items = [f for f in unique_items if f.get('status') == 'queued']
        completed_items = [f for f in unique_items if f.get('status') in ('completed', 'skipped')]
        failed_items = [f for f in unique_items if f.get('status') == 'failed']

        # Update Filter Chip labels with live counts
        if hasattr(self, 'filter_btns') and self.filter_btns:
            if "all" in self.filter_btns:
                self.filter_btns["all"].setText(f"All ({len(unique_items)})")
            if "downloading" in self.filter_btns:
                self.filter_btns["downloading"].setText(f"Active ({len(active_items)})")
            if "queued" in self.filter_btns:
                self.filter_btns["queued"].setText(f"Queued ({len(queued_items)})")
            if "completed" in self.filter_btns:
                self.filter_btns["completed"].setText(f"Done ({len(completed_items)})")
            if "failed" in self.filter_btns:
                self.filter_btns["failed"].setText(f"Failed ({len(failed_items)})")

        # 1. Total Current Speed & Average Speed
        total_curr_speed = sum(f.get('speed_bps', 0.0) for f in active_items)
        self.current_speed_bps = total_curr_speed
        if total_curr_speed > self.peak_speed_bps:
            self.peak_speed_bps = total_curr_speed

        self.speed_history.append(total_curr_speed)
        avg_speed = sum(self.speed_history) / len(self.speed_history) if self.speed_history else 0.0

        self.val_speed.setText(format_speed(total_curr_speed))
        self.sub_speed.setText(f"Average: {format_speed(avg_speed)} • Peak: {format_speed(self.peak_speed_bps)}")

        # 2. Worker Threads
        configured_threads = 4
        if hasattr(self.parent_app, 'thread_count_input') and self.parent_app.thread_count_input:
            try:
                configured_threads = int(self.parent_app.thread_count_input.text() or 4)
            except (ValueError, AttributeError, TypeError):
                configured_threads = 4

        is_multithread = True
        if hasattr(self.parent_app, 'use_multithreading_checkbox') and self.parent_app.use_multithreading_checkbox:
            try:
                is_multithread = self.parent_app.use_multithreading_checkbox.isChecked()
            except (AttributeError, TypeError):
                is_multithread = True

        if active_items:
            active_threads = min(len(active_items), configured_threads) if is_multithread else 1
            self.val_threads.setText(f"{active_threads} Active")
            self.sub_threads.setText(f"{active_threads} of {configured_threads} threads in use ({len(queued_items)} waiting)")
        else:
            self.val_threads.setText("0 Active")
            self.sub_threads.setText(f"Configured: {configured_threads} threads" if len(queued_items) == 0 else f"{len(queued_items)} files waiting for thread")

        # 3. Queue Status Count
        total_items = len(unique_items)
        queued_count = len(queued_items)

        # Add batch jobs count
        batch_jobs = self._get_pending_job_files()
        total_queued = queued_count + len(batch_jobs)

        if active_items or queued_items:
            self.val_queue.setText(f"{total_queued} Queued" if total_queued > 0 else f"{len(active_items)} Active")
            self.sub_queue.setText(f"{len(active_items)} downloading • {len(queued_items)} waiting for thread • {len(completed_items)} done")
        elif total_queued > 0:
            self.val_queue.setText(f"{total_queued} Queued")
            self.sub_queue.setText(f"{len(batch_jobs)} batch job(s) pending execution")
        else:
            self.val_queue.setText("Idle")
            self.sub_queue.setText(f"{len(completed_items)} completed • 0 active")

        # 4. Transferred Data & Remaining Data
        total_dl = sum(f.get('downloaded_bytes', 0) for f in unique_items)
        total_sz = sum(f.get('total_bytes', 0) for f in unique_items if f.get('total_bytes', 0) > 0)
        remaining = max(0, total_sz - total_dl)

        self.val_data.setText(format_bytes(total_dl))
        if total_sz > 0:
            pct = int((total_dl / total_sz) * 100)
            self.sub_data.setText(f"Completed: {format_bytes(total_dl)} • Remaining: {format_bytes(remaining)} ({pct}%)")
        else:
            self.sub_data.setText(f"Completed: {format_bytes(total_dl)} • Remaining: -")

    def _on_server_ping_result(self, status_code: int, latency_ms: int, domain: str):
        self.server_status_code = status_code
        self.server_latency_ms = latency_ms
        self.server_domain = domain

        if status_code in [200, 204, 301, 302]:
            self.val_server.setText(f"200 OK • {latency_ms}ms")
            self.val_server.setStyleSheet("color: #34D399; font-size: 13pt; font-weight: 700; padding-top: 1px;")
            self.sub_server.setText(f"{domain} • Fast & Healthy")
        elif status_code == 429:
            self.val_server.setText("429 Rate Limit")
            self.val_server.setStyleSheet("color: #FBBF24; font-size: 13pt; font-weight: 700; padding-top: 1px;")
            self.sub_server.setText(f"{domain} • Rate limited, auto-throttling")
        elif status_code == 403:
            self.val_server.setText("403 Forbidden")
            self.val_server.setStyleSheet("color: #F87171; font-size: 13pt; font-weight: 700; padding-top: 1px;")
            self.sub_server.setText(f"{domain} • Cloudflare / auth challenge")
        elif status_code > 0:
            self.val_server.setText(f"{status_code} Status")
            self.val_server.setStyleSheet("color: #F87171; font-size: 13pt; font-weight: 700; padding-top: 1px;")
            self.sub_server.setText(f"{domain} • Response error")
        else:
            self.val_server.setText("Offline / Timeout")
            self.val_server.setStyleSheet("color: #94A3B8; font-size: 13pt; font-weight: 700; padding-top: 1px;")
            self.sub_server.setText(f"{domain} • No connection")

    # ─────────────────────────────────────────────────────────────────────────
    #  Batch Job Queue
    # ─────────────────────────────────────────────────────────────────────────

    def _get_pending_job_files(self) -> list:
        jobs_dir = getattr(self.parent_app, 'jobs_dir', '')
        if not jobs_dir or not os.path.exists(jobs_dir):
            return []
        return sorted(glob.glob(os.path.join(jobs_dir, "job_*.json")))

    def refresh_jobs_queue(self):
        """Scans appdata/jobs and populates the Batch Job Queue page."""
        job_files = self._get_pending_job_files()
        scale = self.scale
        self.tab_view.setTabText(1, f"Batch Job Queue ({len(job_files)})")
        self.jobs_count_lbl.setText(f"{len(job_files)} queued batch job(s) found.")

        self.jobs_table.setRowCount(len(job_files))
        for row, job_path in enumerate(job_files):
            self.jobs_table.setRowHeight(row, int(36 * scale))

            # 0. Index
            it_idx = QTableWidgetItem(str(row + 1))
            it_idx.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.jobs_table.setItem(row, 0, it_idx)

            # 1. Job Filename
            job_name = os.path.basename(job_path)
            self.jobs_table.setItem(row, 1, QTableWidgetItem(job_name))

            # Parse JSON
            url_str = ""
            out_dir = ""
            try:
                with open(job_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    url_str = data.get('api_url', '')
                    out_dir = data.get('output_dir', '')
            except Exception:
                pass

            # 2. Target URL
            self.jobs_table.setItem(row, 2, QTableWidgetItem(url_str))

            # 3. Output Directory
            self.jobs_table.setItem(row, 3, QTableWidgetItem(out_dir))

            # 4. Created Date
            try:
                mtime = os.path.getmtime(job_path)
                mtime_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))
            except OSError:
                mtime_str = "N/A"
            self.jobs_table.setItem(row, 4, QTableWidgetItem(mtime_str))

            # 5. Actions (Delete button)
            del_btn = QPushButton("Delete")
            del_btn.setFixedHeight(int(24 * scale))
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setStyleSheet(
                "QPushButton { background-color: #374151; color: #F87171; border: 1px solid #4B5563; border-radius: 3px; font-size: 8pt; }"
                "QPushButton:hover { background-color: #7F1D1D; color: #FFFFFF; border-color: #DC2626; }"
            )
            del_btn.clicked.connect(lambda chk=False, jp=job_path: self._delete_job_file(jp))
            self.jobs_table.setCellWidget(row, 5, del_btn)

    def _delete_job_file(self, job_path: str):
        try:
            if os.path.exists(job_path):
                os.remove(job_path)
                self.refresh_jobs_queue()
        except OSError as e:
            QMessageBox.warning(self, "Error", f"Could not remove job file: {e}")

    def _execute_all_jobs(self):
        if hasattr(self.parent_app, 'execute_job_queue'):
            self.parent_app.execute_job_queue()
            self.refresh_jobs_queue()

    # ─────────────────────────────────────────────────────────────────────────
    #  Actions & Context Menus
    # ─────────────────────────────────────────────────────────────────────────

    def _set_filter(self, filter_key: str):
        self.current_filter = filter_key
        for k, btn in self.filter_btns.items():
            btn.setChecked(k == filter_key)

        for row in range(self.files_table.rowCount()):
            file_item = self._get_file_item_for_row(row)
            self._apply_row_filter(row, file_item)

        if filter_key == "all" and getattr(self, 'auto_scroll_enabled', True):
            self._scroll_to_latest_active(ensure_visible_only=True)

    def _on_search_changed(self, text: str):
        self.search_query = text.strip().lower()
        for row in range(self.files_table.rowCount()):
            file_item = self._get_file_item_for_row(row)
            self._apply_row_filter(row, file_item)

    # ─────────────────────────────────────────────────────────────────────────
    #  Auto-Scroll Engine for All Tab
    # ─────────────────────────────────────────────────────────────────────────

    def _on_scroll_slider_pressed(self):
        self._user_is_scrolling = True

    def _on_scroll_slider_released(self):
        self._user_is_scrolling = False

    def _toggle_auto_scroll(self, checked: bool):
        self.auto_scroll_enabled = checked
        if checked and self.current_filter == "all":
            self._scroll_to_latest_active(ensure_visible_only=False)

    def _scroll_to_latest_active(self, ensure_visible_only: bool = True):
        if not getattr(self, 'auto_scroll_enabled', True):
            return

        active_rows = []
        for file_key, f in self.files_data.items():
            if f.get('status') == 'downloading':
                r = self.file_row_map.get(file_key)
                if r is not None and 0 <= r < self.files_table.rowCount() and not self.files_table.isRowHidden(r):
                    active_rows.append(r)

        target_row = max(active_rows) if active_rows else None
        if target_row is not None:
            it = self.files_table.item(target_row, 1)
            if it:
                hint = (
                    QAbstractItemView.ScrollHint.EnsureVisible
                    if ensure_visible_only
                    else QAbstractItemView.ScrollHint.PositionAtCenter
                )
                self.files_table.scrollToItem(it, hint)

    def _maybe_auto_scroll(self, row: int):
        if not getattr(self, 'auto_scroll_enabled', True) or self.current_filter != "all":
            return
        if row < 0 or row >= self.files_table.rowCount():
            return
        if self.files_table.isRowHidden(row):
            return
        if getattr(self, '_user_is_scrolling', False):
            return

        now = time.time()
        # Rate-limit to at most once per 200ms unless moving to a different row
        if (now - getattr(self, '_last_auto_scroll_time', 0.0) < 0.20) and (row == getattr(self, '_last_scrolled_row', -1)):
            return

        self._last_auto_scroll_time = now
        self._last_scrolled_row = row

        it = self.files_table.item(row, 1)
        if it:
            self.files_table.scrollToItem(it, QAbstractItemView.ScrollHint.EnsureVisible)

    def _toggle_pause_resume(self):
        if hasattr(self.parent_app, 'pause_resume_download'):
            self.parent_app.pause_resume_download()

    def _cancel_active_downloads(self):
        reply = QMessageBox.question(
            self, "Cancel Active Downloads",
            "Are you sure you want to cancel the currently active download session?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            if hasattr(self.parent_app, 'cancel_download'):
                self.parent_app.cancel_download()
            elif hasattr(self.parent_app, 'cancellation_event'):
                self.parent_app.cancellation_event.set()

    def clear_completed(self):
        """Cleans up completed items from the table and memory."""
        unique_completed = [f for f in {id(f): f for f in self.files_data.values()}.values() if f.get('status') in ('completed', 'skipped')]
        completed_ids = {id(f) for f in unique_completed}

        # Remove all aliases referencing completed items
        self.files_data = {k: v for k, v in self.files_data.items() if id(v) not in completed_ids}

        self.files_table.setRowCount(0)
        self.file_row_map.clear()
        self.row_file_map.clear()

        # Re-insert remaining unique items
        unique_remaining = list({id(f): f for f in self.files_data.values()}.values())
        for f in unique_remaining:
            fk = f.get('id') or f.get('filename')
            if fk:
                self._update_or_insert_file_row(fk)

        self._recalculate_and_refresh_stats()

    def _open_file_folder(self, file_key: str):
        file_item = self.files_data.get(file_key)
        if not file_item:
            for f in self.files_data.values():
                if f.get('filename') == file_key or f.get('id') == file_key:
                    file_item = f
                    break
        if not file_item:
            file_item = {}

        save_path = file_item.get('save_path', '')
        if save_path and os.path.exists(save_path):
            folder = os.path.dirname(save_path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
            return

        self._open_current_download_dir()

    def _open_current_download_dir(self):
        target_dir = ""
        if hasattr(self.parent_app, 'dir_input') and self.parent_app.dir_input.text():
            target_dir = self.parent_app.dir_input.text()
        if not target_dir or not os.path.exists(target_dir):
            target_dir = getattr(self.parent_app, 'app_base_dir', '')

        if target_dir and os.path.exists(target_dir):
            QDesktopServices.openUrl(QUrl.fromLocalFile(target_dir))
        else:
            QMessageBox.information(self, "Folder", "No valid download directory configured yet.")

    def _show_file_context_menu(self, pos):
        row = self.files_table.rowAt(pos.y())
        if row < 0:
            return

        file_item = self._get_file_item_for_row(row)
        file_key = self.row_file_map.get(row) or file_item.get('id') or (self.files_table.item(row, 1).text() if self.files_table.item(row, 1) else "")

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #24262B; color: #DFE1E5; border: 1px solid #3C3F46; border-radius: 4px; padding: 4px; }"
            "QMenu::item { padding: 5px 20px 5px 20px; border-radius: 3px; }"
            "QMenu::item:selected { background-color: #1565A8; color: #FFFFFF; }"
        )

        act_folder = menu.addAction("Open Containing Folder")
        act_copy = menu.addAction("Copy File Name")
        act_remove = menu.addAction("Remove from List")

        selected = menu.exec(self.files_table.viewport().mapToGlobal(pos))
        if selected == act_folder:
            self._open_file_folder(file_key)
        elif selected == act_copy:
            QApplication.clipboard().setText(file_item.get('filename', ''))
        elif selected == act_remove:
            item_id = id(file_item) if file_item else None
            if item_id:
                self.files_data = {k: v for k, v in self.files_data.items() if id(v) != item_id}
            elif file_key in self.files_data:
                del self.files_data[file_key]

            self.files_table.removeRow(row)
            self.file_row_map.clear()
            self.row_file_map.clear()
            for r in range(self.files_table.rowCount()):
                item = self.files_table.item(r, 1)
                if item:
                    fk = item.data(Qt.ItemDataRole.UserRole) or item.text()
                    self.file_row_map[fk] = r
                    self.row_file_map[r] = fk
            self._recalculate_and_refresh_stats()
