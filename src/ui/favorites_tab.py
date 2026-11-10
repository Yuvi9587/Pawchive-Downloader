import html
import json
import os
import re
import sys
import time
import cloudscraper
from collections import defaultdict
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QProgressBar, QTabWidget, QFrame,
    QMessageBox, QComboBox, QCheckBox, QApplication, QMenu, QInputDialog
)
from PySide6.QtCore import Qt, QSize, QThread, Signal, QCoreApplication, QUrl
from PySide6.QtGui import QIcon, QFont, QAction, QColor, QDesktopServices

from .assets import get_asset_path
from ..utils.network_utils import prepare_cookies_for_request
from .dialogs.CookieHelpDialog import CookieHelpDialog
from ..core.favorites_manager import UserFavoritesManager


# ─────────────────────────────────────────────────────────────────────────────
#  Background Worker Threads (for Account Favorites with Cookies)
# ─────────────────────────────────────────────────────────────────────────────

class FavoriteArtistsFetcherThread(QThread):
    """Fetches favorite artists from Kemono, Coomer, and Pawchive in background."""
    status_signal = Signal(str)
    progress_signal = Signal(int, int)
    finished_signal = Signal(bool, str, list)

    def __init__(self, cookies_config, logger_func=None):
        super().__init__()
        self.cookies_config = cookies_config
        self.logger_func = logger_func

    def _logger(self, msg):
        if self.logger_func:
            self.logger_func(f"[FavArtistsFetcher] {msg}")

    def _get_domain_for_service(self, service_name, source_api=None):
        if source_api and "pawchive" in source_api.lower():
            return "pawchive.pw"
        service_lower = (service_name or "").lower()
        coomer_primary_services = {'onlyfans', 'fansly', 'manyvids', 'candfans'}
        if service_lower in coomer_primary_services:
            return "coomer.st"
        return "kemono.cr"

    def run(self):
        scraper = cloudscraper.create_scraper()
        all_artists = []
        errors = []
        fetched_any = False

        self.status_signal.emit("Connecting to favorites API...")
        self.progress_signal.emit(0, 0)

        api_sources = [
            {"name": "Kemono.cr", "url": "https://kemono.cr/api/v1/account/favorites?type=artist", "domain": "kemono.cr", "fallback": "kemono.su"},
            {"name": "Coomer.st", "url": "https://coomer.st/api/v1/account/favorites?type=artist", "domain": "coomer.st", "fallback": "coomer.su"},
            {"name": "Pawchive", "url": "https://pawchive.pw/api/v1/account/favorites?type=artist", "domain": "pawchive.pw", "fallback": "pawchive.st"}
        ]

        for idx, source in enumerate(api_sources):
            self.status_signal.emit(f"Fetching favorite artists from {source['name']}...")
            self.progress_signal.emit(idx, len(api_sources))

            cookies_dict = None
            if self.cookies_config.get('use_cookie'):
                cookies_dict = prepare_cookies_for_request(
                    True, self.cookies_config.get('cookie_text', ''),
                    self.cookies_config.get('selected_cookie_file'),
                    self.cookies_config.get('app_base_dir'), self._logger,
                    target_domain=source['domain']
                )
                if not cookies_dict and source.get('fallback'):
                    cookies_dict = prepare_cookies_for_request(
                        True, self.cookies_config.get('cookie_text', ''),
                        self.cookies_config.get('selected_cookie_file'),
                        self.cookies_config.get('app_base_dir'), self._logger,
                        target_domain=source['fallback']
                    )

            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                    'Referer': f"https://{source['domain']}/favorites",
                    'Accept': 'application/json, text/css, */*'
                }
                res = scraper.get(source['url'], headers=headers, cookies=cookies_dict, timeout=20)
                res.raise_for_status()
                data = res.json()

                if isinstance(data, list):
                    count_src = 0
                    for item in data:
                        a_id = item.get("id")
                        a_name = html.unescape(item.get("name", "Unknown Artist").strip())
                        a_service = item.get("service")
                        if a_id and a_name and a_service:
                            domain = self._get_domain_for_service(a_service, source['name'])
                            full_url = f"https://{domain}/{a_service}/user/{a_id}"
                            all_artists.append({
                                'name': a_name,
                                'url': full_url,
                                'service': a_service,
                                'id': str(a_id),
                                '_source_api': source['name']
                            })
                            count_src += 1
                    if count_src > 0:
                        fetched_any = True
                    self._logger(f"Fetched {count_src} artists from {source['name']}.")
            except Exception as e:
                err = f"{source['name']}: {e}"
                self._logger(f"Error fetching from {err}")
                errors.append(err)

        # Deduplicate
        unique_map = {}
        for a in all_artists:
            k = (a['service'].lower(), str(a['id']).lower())
            if k not in unique_map:
                unique_map[k] = a
        all_artists = list(unique_map.values())
        all_artists.sort(key=lambda x: x['name'].lower())

        self.progress_signal.emit(len(api_sources), len(api_sources))
        if fetched_any or all_artists:
            self.finished_signal.emit(True, "", all_artists)
        else:
            err_msg = "; ".join(errors) if errors else "No favorite artists found on connected accounts."
            self.finished_signal.emit(False, err_msg, [])


class FavoritePostsFetcherThread(QThread):
    """Fetches favorite posts from Kemono, Coomer, and Pawchive in background."""
    status_signal = Signal(str)
    progress_signal = Signal(int, int)
    finished_signal = Signal(bool, str, list)

    def __init__(self, cookies_config, target_domain_preference=None, logger_func=None, creator_name_cache=None):
        super().__init__()
        self.cookies_config = cookies_config
        self.target_domain_preference = target_domain_preference
        self.logger_func = logger_func
        self.creator_name_cache = creator_name_cache or {}

    def _logger(self, msg):
        if self.logger_func:
            self.logger_func(f"[FavPostsFetcher] {msg}")

    def run(self):
        scraper = cloudscraper.create_scraper()
        all_posts = []
        errors = []
        fetched_any = False

        self.status_signal.emit("Connecting to favorite posts API...")
        self.progress_signal.emit(0, 0)

        api_sources = [
            {"name": "Kemono.cr", "url": "https://kemono.cr/api/v1/account/favorites?type=post", "domain": "kemono.cr", "fallback": "kemono.su"},
            {"name": "Coomer.st", "url": "https://coomer.st/api/v1/account/favorites?type=post", "domain": "coomer.st", "fallback": "coomer.su"},
            {"name": "Pawchive", "url": "https://pawchive.pw/api/v1/account/favorites?type=post", "domain": "pawchive.pw", "fallback": "pawchive.st"}
        ]

        if self.target_domain_preference:
            pref = self.target_domain_preference.lower()
            matched = [s for s in api_sources if pref in s['domain'].lower() or pref in s['name'].lower()]
            if matched:
                api_sources = matched

        for idx, source in enumerate(api_sources):
            self.status_signal.emit(f"Fetching favorite posts from {source['name']}...")
            self.progress_signal.emit(idx, len(api_sources))

            cookies_dict = None
            if self.cookies_config.get('use_cookie'):
                cookies_dict = prepare_cookies_for_request(
                    True, self.cookies_config.get('cookie_text', ''),
                    self.cookies_config.get('selected_cookie_file'),
                    self.cookies_config.get('app_base_dir'), self._logger,
                    target_domain=source['domain']
                )
                if not cookies_dict and source.get('fallback'):
                    cookies_dict = prepare_cookies_for_request(
                        True, self.cookies_config.get('cookie_text', ''),
                        self.cookies_config.get('selected_cookie_file'),
                        self.cookies_config.get('app_base_dir'), self._logger,
                        target_domain=source['fallback']
                    )

            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                    'Referer': f"https://{source['domain']}/favorites",
                    'Accept': 'application/json, text/css, */*'
                }
                res = scraper.get(source['url'], headers=headers, cookies=cookies_dict, timeout=20)
                res.raise_for_status()
                data = res.json()

                if isinstance(data, list):
                    count_src = 0
                    for item in data:
                        p_id = item.get("id")
                        p_title = html.unescape(item.get("title", "Untitled Post").strip())
                        service = item.get("service")
                        creator_id = item.get("user")
                        added_date = item.get("added", item.get("published", ""))

                        if p_id and service and creator_id:
                            k_tuple = (service.lower(), str(creator_id))
                            creator_name = self.creator_name_cache.get(k_tuple, str(creator_id))
                            all_posts.append({
                                'post_id': str(p_id),
                                'title': p_title,
                                'service': service,
                                'creator_id': str(creator_id),
                                'creator_name_resolved': creator_name,
                                'added_date': added_date,
                                '_source_api': source['name']
                            })
                            count_src += 1
                    if count_src > 0:
                        fetched_any = True
                    self._logger(f"Fetched {count_src} posts from {source['name']}.")
            except Exception as e:
                err = f"{source['name']}: {e}"
                self._logger(f"Error fetching from {err}")
                errors.append(err)

        # Deduplicate
        unique_map = {}
        for p in all_posts:
            k = (p['service'].lower(), str(p['creator_id']).lower(), str(p['post_id']).lower())
            if k not in unique_map:
                unique_map[k] = p
        all_posts = list(unique_map.values())
        all_posts.sort(key=lambda x: (x.get('_source_api', ''), x.get('creator_name_resolved', '').lower(), x.get('added_date', '')), reverse=True)

        self.progress_signal.emit(len(api_sources), len(api_sources))
        if fetched_any or all_posts:
            self.finished_signal.emit(True, "", all_posts)
        else:
            err_msg = "; ".join(errors) if errors else "No favorite posts found on connected accounts."
            self.finished_signal.emit(False, err_msg, [])


# ─────────────────────────────────────────────────────────────────────────────
#  FavoritesTabWidget: Dedicated Sidebar Tab
# ─────────────────────────────────────────────────────────────────────────────

class FavoritesTabWidget(QWidget):
    """
    Dedicated Favorites Tab in the sidebar navigation.
    Supports both:
    1. ⭐ Local Favorites (Stored locally on disk in cache/user_favorites.json without needing cookies or login)
    2. 🌐 Account Favorites (Fetched from Kemono, Coomer, Pawchive via cookies)
    """

    def __init__(self, parent_app, scale=1.0):
        super().__init__(parent_app)
        self.parent_app = parent_app
        self.scale = scale

        # User Favorites Manager
        self.fav_manager = getattr(parent_app, "favorites_manager", None)
        if not self.fav_manager:
            base_dir = getattr(parent_app, "app_base_dir", ".")
            self.fav_manager = UserFavoritesManager(base_dir, parent=self)
            if parent_app:
                parent_app.favorites_manager = self.fav_manager

        self.fav_manager.favorites_changed.connect(self._on_local_favorites_changed)

        self.account_artists = []
        self.filtered_artists = []
        self.account_posts = []
        self.filtered_posts = []

        self.artists_thread = None
        self.posts_thread = None

        self._build_ui()
        self._update_cookie_status()
        self._update_local_stats_badge()
        self._filter_artists()
        self._filter_posts()

    # ─────────────────────────────────────────────────────────────────────────
    #  UI Construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        scale = self.scale
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(
            int(14 * scale), int(12 * scale), int(14 * scale), int(12 * scale)
        )
        root_layout.setSpacing(int(10 * scale))

        # ── 1. Header Bar ─────────────────────────────────────────────────────
        header_bar = QHBoxLayout()
        header_bar.setSpacing(int(10 * scale))

        title_col = QVBoxLayout()
        title_col.setSpacing(int(2 * scale))

        title_h = QHBoxLayout()
        title_h.setSpacing(int(8 * scale))

        title_icon = QLabel()
        title_icon.setPixmap(
            QIcon(get_asset_path("assets/Svg/star.svg")).pixmap(int(22 * scale), int(22 * scale))
        )
        title_lbl = QLabel("Favorites Manager")
        title_lbl.setStyleSheet(
            f"font-size: {int(13 * scale)}pt; font-weight: bold; color: #FCD34D;"
        )
        title_h.addWidget(title_icon)
        title_h.addWidget(title_lbl)
        title_h.addStretch(1)
        title_col.addLayout(title_h)

        subtitle_lbl = QLabel(
            "Browse, filter, and batch-download your favorite artists and posts stored locally (no account/cookies needed) or synced with online accounts."
        )
        subtitle_lbl.setStyleSheet("font-size: 8.5pt; color: #8A8D93; font-weight: normal;")
        title_col.addWidget(subtitle_lbl)
        header_bar.addLayout(title_col, 1)

        # Local storage badge
        self.local_stats_badge = QLabel("Local: 0 Artists • 0 Posts")
        self.local_stats_badge.setStyleSheet(
            "font-size: 8pt; font-weight: bold; color: #FCD34D; background-color: #2D271D; "
            "border: 1px solid #785A1D; border-radius: 4px; padding: 4px 8px;"
        )
        header_bar.addWidget(self.local_stats_badge)

        # Cookie Status indicator & Settings button
        self.cookie_status_badge = QLabel("Checking cookies...")
        self.cookie_status_badge.setStyleSheet(
            "font-size: 8pt; font-weight: bold; color: #9A9EAA; background-color: #2D3035; "
            "border: 1px solid #424652; border-radius: 4px; padding: 4px 8px;"
        )
        header_bar.addWidget(self.cookie_status_badge)

        self.cookie_help_btn = QPushButton(" Cookie Settings")
        self.cookie_help_btn.setIcon(QIcon(get_asset_path("assets/Svg/settings.svg")))
        self.cookie_help_btn.setFixedHeight(int(28 * scale))
        self.cookie_help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cookie_help_btn.setStyleSheet(
            "QPushButton { background: #2A2D34; border: 1px solid #383D48; border-radius: 4px; "
            "color: #D1D5DB; font-size: 8pt; padding: 0 10px; font-weight: 500; }"
            "QPushButton:hover { background: #353842; border-color: #60A5FA; color: #FFFFFF; }"
        )
        self.cookie_help_btn.clicked.connect(self._open_cookie_help)
        header_bar.addWidget(self.cookie_help_btn)

        root_layout.addLayout(header_bar)

        # ── 2. Sub-Tabs (Artists vs Posts) ────────────────────────────────────
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid #3E424B;
                background-color: #26282C;
                border-radius: 6px;
                top: -1px;
            }}
            QTabBar::tab {{
                background-color: #2D3035;
                color: #BAC0CB;
                border: 1px solid #3E424B;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: {int(7 * scale)}px {int(16 * scale)}px;
                font-weight: 600;
                font-size: 9pt;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background-color: #26282C;
                color: #60A5FA;
                border-bottom: 2px solid #3B82F6;
            }}
            QTabBar::tab:hover:!selected {{
                background-color: #353840;
                color: #E2E8F0;
            }}
        """)

        # Add Sub-tabs
        self.artists_widget = QWidget()
        self._build_artists_subtab(self.artists_widget)
        self.tabs.addTab(self.artists_widget, QIcon(get_asset_path("assets/uisvg/image.svg")), "Favorite Artists")

        self.posts_widget = QWidget()
        self._build_posts_subtab(self.posts_widget)
        self.tabs.addTab(self.posts_widget, QIcon(get_asset_path("assets/uisvg/manga.svg")), "Favorite Posts")

        root_layout.addWidget(self.tabs, 1)

    # ─────────────────────────────────────────────────────────────────────────
    #  Sub-Tab 1: Favorite Artists
    # ─────────────────────────────────────────────────────────────────────────

    def _build_artists_subtab(self, parent_widget):
        scale = self.scale
        layout = QVBoxLayout(parent_widget)
        layout.setContentsMargins(int(10 * scale), int(10 * scale), int(10 * scale), int(10 * scale))
        layout.setSpacing(int(8 * scale))

        # Action Toolbar
        tb = QHBoxLayout()
        tb.setSpacing(int(8 * scale))

        # Mode / Source Selector
        self.artists_source_combo = QComboBox()
        self.artists_source_combo.addItems([
            "⭐ Local Favorites (Offline / No Account)",
            "🌐 Account Favorites (Cookies / API)"
        ])
        self.artists_source_combo.setFixedHeight(int(30 * scale))
        self.artists_source_combo.setStyleSheet(
            "QComboBox { background: #1A1C20; border: 1.5px solid #E5A198; border-radius: 4px; "
            "color: #FFFFFF; padding: 0 10px; font-size: 8.5pt; font-weight: bold; }"
            "QComboBox QAbstractItemView { background: #2A2D34; color: #FFFFFF; selection-background-color: #3B82F6; }"
        )
        self.artists_source_combo.currentIndexChanged.connect(self._on_artists_source_changed)
        tb.addWidget(self.artists_source_combo)

        self.artists_search = QLineEdit()
        self.artists_search.setPlaceholderText("Filter artists by name, service, or URL...")
        self.artists_search.setFixedHeight(int(30 * scale))
        self.artists_search.setStyleSheet(
            "QLineEdit { background: #1A1C20; border: 1px solid #383D48; border-radius: 4px; "
            "color: #FFFFFF; padding: 0 10px; font-size: 8.5pt; }"
            "QLineEdit:focus { border-color: #60A5FA; }"
        )
        self.artists_search.textChanged.connect(self._filter_artists)
        tb.addWidget(self.artists_search, 2)

        # Quick Add Artist button (Local mode)
        self.add_artist_btn = QPushButton(" + Add Artist")
        self.add_artist_btn.setIcon(QIcon(get_asset_path("assets/Svg/star.svg")))
        self.add_artist_btn.setFixedHeight(int(30 * scale))
        self.add_artist_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_artist_btn.setStyleSheet(
            "QPushButton { background: #2F2424; border: 1px solid #5A3535; border-radius: 4px; "
            "color: #E5A198; font-size: 8pt; padding: 0 10px; font-weight: 600; }"
            "QPushButton:hover { background: #422626; color: #FFFFFF; border-color: #E5A198; }"
        )
        self.add_artist_btn.clicked.connect(self._prompt_add_artist)
        tb.addWidget(self.add_artist_btn)

        # Fetch button (Account mode)
        self.fetch_artists_btn = QPushButton(" Fetch from Account")
        self.fetch_artists_btn.setIcon(QIcon(get_asset_path("assets/Svg/sync.svg")))
        self.fetch_artists_btn.setFixedHeight(int(30 * scale))
        self.fetch_artists_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fetch_artists_btn.setStyleSheet(
            "QPushButton { background: #2A2D34; border: 1px solid #383D48; border-radius: 4px; "
            "color: #D1D5DB; font-size: 8pt; padding: 0 12px; font-weight: 600; }"
            "QPushButton:hover { background: #353842; border-color: #60A5FA; color: #FFFFFF; }"
        )
        self.fetch_artists_btn.clicked.connect(self._start_fetch_artists)
        self.fetch_artists_btn.setVisible(False)
        tb.addWidget(self.fetch_artists_btn)

        # Import to Local button (Account mode)
        self.import_artists_btn = QPushButton(" Save to Local")
        self.import_artists_btn.setIcon(QIcon(get_asset_path("assets/Svg/star.svg")))
        self.import_artists_btn.setFixedHeight(int(30 * scale))
        self.import_artists_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_artists_btn.setStyleSheet(
            "QPushButton { background: #382A1B; border: 1px solid #785A1D; border-radius: 4px; "
            "color: #FCD34D; font-size: 8pt; padding: 0 10px; font-weight: 600; }"
            "QPushButton:hover { background: #4A3824; color: #FFFFFF; border-color: #FCD34D; }"
        )
        self.import_artists_btn.clicked.connect(self._import_fetched_artists_to_local)
        self.import_artists_btn.setVisible(False)
        tb.addWidget(self.import_artists_btn)

        self.sel_all_artists_btn = QPushButton("Select All")
        self.sel_all_artists_btn.setFixedHeight(int(30 * scale))
        self.sel_all_artists_btn.setStyleSheet(
            "QPushButton { background: #2A2D34; border: 1px solid #383D48; border-radius: 4px; "
            "color: #BAC0CB; font-size: 8pt; padding: 0 10px; }"
            "QPushButton:hover { background: #353842; color: #FFFFFF; }"
        )
        self.sel_all_artists_btn.clicked.connect(self._select_all_artists)
        tb.addWidget(self.sel_all_artists_btn)

        self.desel_all_artists_btn = QPushButton("Deselect All")
        self.desel_all_artists_btn.setFixedHeight(int(30 * scale))
        self.desel_all_artists_btn.setStyleSheet(
            "QPushButton { background: #2A2D34; border: 1px solid #383D48; border-radius: 4px; "
            "color: #BAC0CB; font-size: 8pt; padding: 0 10px; }"
            "QPushButton:hover { background: #353842; color: #FFFFFF; }"
        )
        self.desel_all_artists_btn.clicked.connect(self._deselect_all_artists)
        tb.addWidget(self.desel_all_artists_btn)

        self.artists_count_lbl = QLabel("0 artists")
        self.artists_count_lbl.setStyleSheet("color: #E5A198; font-size: 8.5pt; font-weight: 500;")
        tb.addWidget(self.artists_count_lbl)

        tb.addStretch(1)

        # View Posts button (Local mode)
        self.view_artist_posts_btn = QPushButton("  View Posts")
        self.view_artist_posts_btn.setIcon(QIcon(get_asset_path("assets/uisvg/manga.svg")))
        self.view_artist_posts_btn.setFixedHeight(int(30 * scale))
        self.view_artist_posts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.view_artist_posts_btn.setEnabled(False)
        self.view_artist_posts_btn.setStyleSheet(
            "QPushButton { background: #20232B; border: 1px solid #333742; border-radius: 4px; "
            "color: #DFE1E5; font-size: 8.5pt; font-weight: bold; padding: 0 12px; }"
            "QPushButton:hover { background: #2A2E38; border-color: #E5A198; color: #FFFFFF; }"
            "QPushButton:disabled { background: #18191E; border-color: #262930; color: #555A66; }"
        )
        self.view_artist_posts_btn.clicked.connect(self._view_selected_artist_posts)
        tb.addWidget(self.view_artist_posts_btn)

        # Remove from local favorites button
        self.remove_artist_btn = QPushButton("  Remove")
        self.remove_artist_btn.setIcon(QIcon(get_asset_path("assets/uisvg/delete.svg")))
        self.remove_artist_btn.setFixedHeight(int(30 * scale))
        self.remove_artist_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_artist_btn.setEnabled(False)
        self.remove_artist_btn.setStyleSheet(
            "QPushButton { background: #2E1F1F; border: 1px solid #4D2626; border-radius: 4px; "
            "color: #F87171; font-size: 8.5pt; font-weight: bold; padding: 0 10px; }"
            "QPushButton:hover { background: #3D2222; border-color: #EF4444; color: #FFFFFF; }"
            "QPushButton:disabled { background: #1E1919; border-color: #2B2121; color: #554444; }"
        )
        self.remove_artist_btn.clicked.connect(self._remove_selected_artists)
        tb.addWidget(self.remove_artist_btn)

        self.download_artists_btn = QPushButton("  Download Selected")
        self.download_artists_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.download_artists_btn.setFixedHeight(int(30 * scale))
        self.download_artists_btn.setEnabled(False)
        self.download_artists_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_artists_btn.setStyleSheet(
            "QPushButton { background: #B55D52; border: 1px solid #C96E63; border-radius: 4px; "
            "color: #FFFFFF; font-size: 8.5pt; font-weight: bold; padding: 0 16px; }"
            "QPushButton:hover { background: #C76E62; }"
            "QPushButton:disabled { background: #342628; border-color: #4A3538; color: #7A6064; }"
        )
        self.download_artists_btn.clicked.connect(self._download_selected_artists)
        tb.addWidget(self.download_artists_btn)

        layout.addLayout(tb)

        # Status & Progress row
        self.artists_status_lbl = QLabel("Artists saved locally on this machine (no cookies or login required).")
        self.artists_status_lbl.setStyleSheet("color: #9CA3AF; font-size: 8.5pt; font-style: italic;")
        layout.addWidget(self.artists_status_lbl)

        self.artists_progress_bar = QProgressBar()
        self.artists_progress_bar.setFixedHeight(int(4 * scale))
        self.artists_progress_bar.setTextVisible(False)
        self.artists_progress_bar.setVisible(False)
        self.artists_progress_bar.setStyleSheet(
            "QProgressBar { background-color: #1A1C20; border: none; border-radius: 2px; }"
            "QProgressBar::chunk { background-color: #3B82F6; border-radius: 2px; }"
        )
        layout.addWidget(self.artists_progress_bar)

        # List Widget
        self.artists_list = QListWidget()
        self.artists_list.setAlternatingRowColors(True)
        self.artists_list.setStyleSheet("""
            QListWidget {
                background-color: #1E2024;
                alternate-background-color: #23262B;
                border: 1px solid #383D48;
                border-radius: 4px;
                color: #E2E8F0;
                font-size: 9pt;
                outline: none;
            }
            QListWidget::item {
                border-bottom: 1px solid #2D3036;
                padding: 6px 10px;
            }
            QListWidget::item:hover {
                background-color: #2C3038;
            }
            QListWidget::item:selected {
                background-color: #1E3A8A;
                color: #FFFFFF;
            }
        """)
        self.artists_list.itemChanged.connect(self._update_artists_count)
        self.artists_list.itemDoubleClicked.connect(self._on_artist_double_clicked)
        self.artists_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.artists_list.customContextMenuRequested.connect(self._show_artists_context_menu)
        layout.addWidget(self.artists_list, 1)

    # ─────────────────────────────────────────────────────────────────────────
    #  Sub-Tab 2: Favorite Posts
    # ─────────────────────────────────────────────────────────────────────────

    def _build_posts_subtab(self, parent_widget):
        scale = self.scale
        layout = QVBoxLayout(parent_widget)
        layout.setContentsMargins(int(10 * scale), int(10 * scale), int(10 * scale), int(10 * scale))
        layout.setSpacing(int(8 * scale))

        # Action Toolbar
        tb = QHBoxLayout()
        tb.setSpacing(int(8 * scale))

        # Mode / Source Selector
        self.posts_source_combo = QComboBox()
        self.posts_source_combo.addItems([
            "⭐ Local Favorites (Offline / No Account)",
            "🌐 Account Favorites (Cookies / API)"
        ])
        self.posts_source_combo.setFixedHeight(int(30 * scale))
        self.posts_source_combo.setStyleSheet(
            "QComboBox { background: #1A1C20; border: 1.5px solid #E5A198; border-radius: 4px; "
            "color: #FFFFFF; padding: 0 10px; font-size: 8.5pt; font-weight: bold; }"
            "QComboBox QAbstractItemView { background: #2A2D34; color: #FFFFFF; selection-background-color: #3B82F6; }"
        )
        self.posts_source_combo.currentIndexChanged.connect(self._on_posts_source_changed)
        tb.addWidget(self.posts_source_combo)

        self.posts_platform_combo = QComboBox()
        self.posts_platform_combo.addItems(["All Platforms", "Patreon", "Pixiv Fanbox", "Fantia", "Discord"])
        self.posts_platform_combo.setFixedHeight(int(30 * scale))
        self.posts_platform_combo.setStyleSheet(
            "QComboBox { background: #1A1C20; border: 1px solid #383D48; border-radius: 4px; "
            "color: #FFFFFF; padding: 0 10px; font-size: 8.5pt; font-weight: 500; }"
            "QComboBox QAbstractItemView { background: #2A2D34; color: #FFFFFF; selection-background-color: #3B82F6; }"
        )
        self.posts_platform_combo.currentIndexChanged.connect(self._filter_posts)
        tb.addWidget(self.posts_platform_combo)

        self.posts_search = QLineEdit()
        self.posts_search.setPlaceholderText("Filter posts by title, creator, ID, or service...")
        self.posts_search.setFixedHeight(int(30 * scale))
        self.posts_search.setStyleSheet(
            "QLineEdit { background: #1A1C20; border: 1px solid #383D48; border-radius: 4px; "
            "color: #FFFFFF; padding: 0 10px; font-size: 8.5pt; }"
            "QLineEdit:focus { border-color: #60A5FA; }"
        )
        self.posts_search.textChanged.connect(self._filter_posts)
        tb.addWidget(self.posts_search, 2)

        # Fetch button (Account mode)
        self.fetch_posts_btn = QPushButton(" Fetch from Account")
        self.fetch_posts_btn.setIcon(QIcon(get_asset_path("assets/Svg/sync.svg")))
        self.fetch_posts_btn.setFixedHeight(int(30 * scale))
        self.fetch_posts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fetch_posts_btn.setStyleSheet(
            "QPushButton { background: #2A2D34; border: 1px solid #383D48; border-radius: 4px; "
            "color: #D1D5DB; font-size: 8pt; padding: 0 12px; font-weight: 600; }"
            "QPushButton:hover { background: #353842; border-color: #60A5FA; color: #FFFFFF; }"
        )
        self.fetch_posts_btn.clicked.connect(self._start_fetch_posts)
        self.fetch_posts_btn.setVisible(False)
        tb.addWidget(self.fetch_posts_btn)

        # Import to Local button (Account mode)
        self.import_posts_btn = QPushButton(" Save to Local")
        self.import_posts_btn.setIcon(QIcon(get_asset_path("assets/Svg/star.svg")))
        self.import_posts_btn.setFixedHeight(int(30 * scale))
        self.import_posts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_posts_btn.setStyleSheet(
            "QPushButton { background: #382A1B; border: 1px solid #785A1D; border-radius: 4px; "
            "color: #FCD34D; font-size: 8pt; padding: 0 10px; font-weight: 600; }"
            "QPushButton:hover { background: #4A3824; color: #FFFFFF; border-color: #FCD34D; }"
        )
        self.import_posts_btn.clicked.connect(self._import_fetched_posts_to_local)
        self.import_posts_btn.setVisible(False)
        tb.addWidget(self.import_posts_btn)

        self.sel_all_posts_btn = QPushButton("Select All")
        self.sel_all_posts_btn.setFixedHeight(int(30 * scale))
        self.sel_all_posts_btn.setStyleSheet(
            "QPushButton { background: #2A2D34; border: 1px solid #383D48; border-radius: 4px; "
            "color: #BAC0CB; font-size: 8pt; padding: 0 10px; }"
            "QPushButton:hover { background: #353842; color: #FFFFFF; }"
        )
        self.sel_all_posts_btn.clicked.connect(self._select_all_posts)
        tb.addWidget(self.sel_all_posts_btn)

        self.desel_all_posts_btn = QPushButton("Deselect All")
        self.desel_all_posts_btn.setFixedHeight(int(30 * scale))
        self.desel_all_posts_btn.setStyleSheet(
            "QPushButton { background: #2A2D34; border: 1px solid #383D48; border-radius: 4px; "
            "color: #BAC0CB; font-size: 8pt; padding: 0 10px; }"
            "QPushButton:hover { background: #353842; color: #FFFFFF; }"
        )
        self.desel_all_posts_btn.clicked.connect(self._deselect_all_posts)
        tb.addWidget(self.desel_all_posts_btn)

        self.posts_count_lbl = QLabel("0 posts")
        self.posts_count_lbl.setStyleSheet("color: #E5A198; font-size: 8.5pt; font-weight: 500;")
        tb.addWidget(self.posts_count_lbl)

        tb.addStretch(1)

        # View Post in App button (Local mode)
        self.view_post_btn = QPushButton("  View in App")
        self.view_post_btn.setIcon(QIcon(get_asset_path("assets/uisvg/play.svg")))
        self.view_post_btn.setFixedHeight(int(30 * scale))
        self.view_post_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.view_post_btn.setEnabled(False)
        self.view_post_btn.setStyleSheet(
            "QPushButton { background: #20232B; border: 1px solid #333742; border-radius: 4px; "
            "color: #DFE1E5; font-size: 8.5pt; font-weight: bold; padding: 0 12px; }"
            "QPushButton:hover { background: #2A2E38; border-color: #E5A198; color: #FFFFFF; }"
            "QPushButton:disabled { background: #18191E; border-color: #262930; color: #555A66; }"
        )
        self.view_post_btn.clicked.connect(self._view_selected_post)
        tb.addWidget(self.view_post_btn)

        # Remove from local favorites button
        self.remove_post_btn = QPushButton("  Remove")
        self.remove_post_btn.setIcon(QIcon(get_asset_path("assets/uisvg/delete.svg")))
        self.remove_post_btn.setFixedHeight(int(30 * scale))
        self.remove_post_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_post_btn.setEnabled(False)
        self.remove_post_btn.setStyleSheet(
            "QPushButton { background: #2E1F1F; border: 1px solid #4D2626; border-radius: 4px; "
            "color: #F87171; font-size: 8.5pt; font-weight: bold; padding: 0 10px; }"
            "QPushButton:hover { background: #3D2222; border-color: #EF4444; color: #FFFFFF; }"
            "QPushButton:disabled { background: #1E1919; border-color: #2B2121; color: #554444; }"
        )
        self.remove_post_btn.clicked.connect(self._remove_selected_posts)
        tb.addWidget(self.remove_post_btn)

        self.download_posts_btn = QPushButton("  Download Selected")
        self.download_posts_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.download_posts_btn.setFixedHeight(int(30 * scale))
        self.download_posts_btn.setEnabled(False)
        self.download_posts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_posts_btn.setStyleSheet(
            "QPushButton { background: #B55D52; border: 1px solid #C96E63; border-radius: 4px; "
            "color: #FFFFFF; font-size: 8.5pt; font-weight: bold; padding: 0 16px; }"
            "QPushButton:hover { background: #C76E62; }"
            "QPushButton:disabled { background: #342628; border-color: #4A3538; color: #7A6064; }"
        )
        self.download_posts_btn.clicked.connect(self._download_selected_posts)
        tb.addWidget(self.download_posts_btn)

        layout.addLayout(tb)

        # Status & Progress row
        self.posts_status_lbl = QLabel("Posts saved locally on this machine (no cookies or login required).")
        self.posts_status_lbl.setStyleSheet("color: #9CA3AF; font-size: 8.5pt; font-style: italic;")
        layout.addWidget(self.posts_status_lbl)

        self.posts_progress_bar = QProgressBar()
        self.posts_progress_bar.setFixedHeight(int(4 * scale))
        self.posts_progress_bar.setTextVisible(False)
        self.posts_progress_bar.setVisible(False)
        self.posts_progress_bar.setStyleSheet(
            "QProgressBar { background-color: #1A1C20; border: none; border-radius: 2px; }"
            "QProgressBar::chunk { background-color: #10B981; border-radius: 2px; }"
        )
        layout.addWidget(self.posts_progress_bar)

        # List Widget
        self.posts_list = QListWidget()
        self.posts_list.setAlternatingRowColors(True)
        self.posts_list.setStyleSheet("""
            QListWidget {
                background-color: #1E2024;
                alternate-background-color: #23262B;
                border: 1px solid #383D48;
                border-radius: 4px;
                color: #E2E8F0;
                font-size: 9pt;
                outline: none;
            }
            QListWidget::item {
                border-bottom: 1px solid #2D3036;
                padding: 6px 10px;
            }
            QListWidget::item:hover {
                background-color: #2C3038;
            }
            QListWidget::item:selected {
                background-color: #1E3A8A;
                color: #FFFFFF;
            }
        """)
        self.posts_list.itemChanged.connect(self._update_posts_count)
        self.posts_list.itemDoubleClicked.connect(self._on_post_double_clicked)
        self.posts_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.posts_list.customContextMenuRequested.connect(self._show_posts_context_menu)
        layout.addWidget(self.posts_list, 1)

    # ─────────────────────────────────────────────────────────────────────────
    #  Tab Activation & Local State
    # ─────────────────────────────────────────────────────────────────────────

    def on_tab_activated(self):
        """Called whenever user switches to Favorites tab in the sidebar."""
        self._update_cookie_status()
        self._update_local_stats_badge()
        if self._is_local_artists_mode():
            self._filter_artists()
        if self._is_local_posts_mode():
            self._filter_posts()

    def _on_local_favorites_changed(self):
        self._update_local_stats_badge()
        if self._is_local_artists_mode():
            self._filter_artists()
        if self._is_local_posts_mode():
            self._filter_posts()

    def _update_local_stats_badge(self):
        if not hasattr(self, "local_stats_badge") or not self.fav_manager:
            return
        c_count = self.fav_manager.get_favorites_count()
        p_count = self.fav_manager.get_posts_count()
        self.local_stats_badge.setText(f"Local: {c_count} Artist{'s' if c_count != 1 else ''} • {p_count} Post{'s' if p_count != 1 else ''}")

    def _is_local_artists_mode(self) -> bool:
        return self.artists_source_combo.currentIndex() == 0

    def _is_local_posts_mode(self) -> bool:
        return self.posts_source_combo.currentIndex() == 0

    def _on_artists_source_changed(self, index: int):
        is_local = (index == 0)
        self.add_artist_btn.setVisible(is_local)
        self.fetch_artists_btn.setVisible(not is_local)
        self.import_artists_btn.setVisible(not is_local)
        self.view_artist_posts_btn.setVisible(is_local)
        self.remove_artist_btn.setVisible(is_local)
        self._filter_artists()

    def _on_posts_source_changed(self, index: int):
        is_local = (index == 0)
        self.fetch_posts_btn.setVisible(not is_local)
        self.import_posts_btn.setVisible(not is_local)
        self.view_post_btn.setVisible(is_local)
        self.remove_post_btn.setVisible(is_local)
        self._filter_posts()

    def _get_cookies_config(self):
        return {
            'use_cookie': self.parent_app.use_cookie_checkbox.isChecked() if hasattr(self.parent_app, 'use_cookie_checkbox') else False,
            'cookie_text': self.parent_app.cookie_text_input.text() if hasattr(self.parent_app, 'cookie_text_input') else "",
            'selected_cookie_file': getattr(self.parent_app, 'selected_cookie_filepath', None),
            'app_base_dir': getattr(self.parent_app, 'app_base_dir', "")
        }

    def _update_cookie_status(self):
        cfg = self._get_cookies_config()
        use_cookie = cfg['use_cookie']
        has_text = bool(cfg['cookie_text'].strip())
        has_file = bool(cfg['selected_cookie_file'] and os.path.exists(cfg['selected_cookie_file']))

        if use_cookie and (has_text or has_file):
            self.cookie_status_badge.setText("Cookies: Active")
            self.cookie_status_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #6EE7B7; background-color: #064E3B; "
                "border: 1px solid #059669; border-radius: 4px; padding: 4px 8px;"
            )
        elif use_cookie:
            self.cookie_status_badge.setText("Cookies: Enabled (No cookie entered)")
            self.cookie_status_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #FDE68A; background-color: #78350F; "
                "border: 1px solid #D97706; border-radius: 4px; padding: 4px 8px;"
            )
        else:
            self.cookie_status_badge.setText("Cookies: Off (Optional for Account Sync)")
            self.cookie_status_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #9CA3AF; background-color: #1F2937; "
                "border: 1px solid #374151; border-radius: 4px; padding: 4px 8px;"
            )

    def _open_cookie_help(self):
        dialog = CookieHelpDialog(self.parent_app, self)
        dialog.exec()
        self._update_cookie_status()

    # ─────────────────────────────────────────────────────────────────────────
    #  Favorite Artists Logic
    # ─────────────────────────────────────────────────────────────────────────

    def _prompt_add_artist(self):
        text, ok = QInputDialog.getText(
            self, "Add Favorite Artist",
            "Enter artist URL (e.g. https://pawchive.pw/patreon/user/3295915) or 'service:id':",
            QLineEdit.EchoMode.Normal
        )
        if ok and text.strip():
            raw = text.strip()
            svc, cid = None, None
            m = re.search(r'(?:kemono|coomer|pawchive)[^/]+/([^/]+)/user/([^/?#]+)', raw, re.IGNORECASE)
            if m:
                svc = m.group(1).lower()
                cid = m.group(2)
            elif ":" in raw:
                parts = raw.split(":")
                svc = parts[0].strip().lower()
                cid = parts[1].strip()

            if svc and cid:
                creator = {
                    "service": svc,
                    "id": cid,
                    "name": cid,
                    "url": f"https://pawchive.pw/{svc}/user/{cid}"
                }
                self.fav_manager.add_creator(creator)
                self._filter_artists()
                QMessageBox.information(self, "Artist Added", f"Added {svc.capitalize()} artist '{cid}' to local favorites!")
            else:
                QMessageBox.warning(self, "Invalid Format", "Could not parse artist service and ID from input.\nPlease use format: 'service:id' or paste full URL.")

    def _start_fetch_artists(self):
        if self.artists_thread and self.artists_thread.isRunning():
            return

        cfg = self._get_cookies_config()
        self.fetch_artists_btn.setEnabled(False)
        self.artists_progress_bar.setVisible(True)
        self.artists_progress_bar.setRange(0, 0)
        self.artists_status_lbl.setText("Connecting to favorites API...")

        logger_func = self.parent_app.log_signal.emit if hasattr(self.parent_app, 'log_signal') else print
        self.artists_thread = FavoriteArtistsFetcherThread(cfg, logger_func=logger_func)
        self.artists_thread.status_signal.connect(self.artists_status_lbl.setText)
        self.artists_thread.finished_signal.connect(self._on_artists_fetched)
        self.artists_thread.start()

    def _on_artists_fetched(self, success, err_msg, artists_list):
        self.fetch_artists_btn.setEnabled(True)
        self.artists_progress_bar.setVisible(False)

        if success and artists_list:
            self.account_artists = artists_list
            self.artists_status_lbl.setText(f"Found {len(self.account_artists)} favorite artist(s) from account.")
            self._filter_artists()
        else:
            self.artists_status_lbl.setText(f"{err_msg or 'No favorite artists found on connected accounts.'}")
            if not self.account_artists:
                self.artists_list.clear()
            self._update_artists_count()

    def _import_fetched_artists_to_local(self):
        if not self.account_artists:
            QMessageBox.information(self, "No Artists", "Please fetch account artists first before saving to local.")
            return
        added_count = 0
        for a in self.account_artists:
            creator = {
                "service": a.get("service") or "patreon",
                "id": str(a.get("id") or ""),
                "name": a.get("name") or "Artist",
                "url": a.get("url") or f"https://pawchive.pw/{a.get('service')}/user/{a.get('id')}"
            }
            if self.fav_manager.add_creator(creator):
                added_count += 1
        QMessageBox.information(
            self, "Import Completed",
            f"Saved {added_count} new artist(s) to your Local Favorites list!\n"
            f"You can now browse them offline without needing cookies."
        )
        self.artists_source_combo.setCurrentIndex(0)  # Switch back to local view

    def _filter_artists(self):
        query = self.artists_search.text().lower().strip()
        self.artists_list.blockSignals(True)
        self.artists_list.clear()

        is_local = self._is_local_artists_mode()
        source_data = self.fav_manager.get_all_creators() if is_local else self.account_artists

        if query:
            self.filtered_artists = [
                a for a in source_data
                if query in (a.get('name') or '').lower()
                or query in (a.get('service') or '').lower()
                or query in (a.get('url') or '').lower()
                or query in str(a.get('id') or '').lower()
            ]
        else:
            self.filtered_artists = list(source_data)

        for a in self.filtered_artists:
            item = QListWidgetItem()
            icon_file = "assets/Svg/star.svg" if is_local else "assets/uisvg/image.svg"
            item.setIcon(QIcon(get_asset_path(icon_file)))

            name = a.get('name') or "Unknown Artist"
            svc = (a.get('service') or 'N/A').capitalize()
            cid = str(a.get('id') or '')
            date_added = a.get('date_added', '')
            date_str = f" • Favorited: {date_added[:10]}" if date_added else ""
            src_str = f"   ({a.get('_source_api')})" if a.get('_source_api') else ""

            item.setText(f"{name}   [{svc}]   (ID: {cid}){src_str}{date_str}")
            item.setToolTip(f"Name: {name}\nService: {svc}\nArtist ID: {cid}\nURL: {a.get('url', '')}\nDouble-click to view posts in app")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, a)
            self.artists_list.addItem(item)

        self.artists_list.blockSignals(False)
        self._update_artists_count()

        if is_local:
            if not self.filtered_artists:
                self.artists_status_lbl.setText("No local favorite artists saved yet. Star creators in Creators tab or click '+ Add Artist'.")
            else:
                self.artists_status_lbl.setText(f"{len(self.filtered_artists)} favorite artist(s) stored locally on this PC.")

    def _select_all_artists(self):
        self.artists_list.blockSignals(True)
        for i in range(self.artists_list.count()):
            self.artists_list.item(i).setCheckState(Qt.CheckState.Checked)
        self.artists_list.blockSignals(False)
        self._update_artists_count()

    def _deselect_all_artists(self):
        self.artists_list.blockSignals(True)
        for i in range(self.artists_list.count()):
            self.artists_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.artists_list.blockSignals(False)
        self._update_artists_count()

    def _update_artists_count(self):
        total = self.artists_list.count()
        selected = sum(
            1 for i in range(total)
            if self.artists_list.item(i).checkState() == Qt.CheckState.Checked
        )
        self.artists_count_lbl.setText(f"{total} artists • {selected} selected")
        self.download_artists_btn.setEnabled(selected > 0)
        self.download_artists_btn.setText(f"  Download Selected ({selected})" if selected > 0 else "  Download Selected")
        self.view_artist_posts_btn.setEnabled(selected == 1)
        self.remove_artist_btn.setEnabled(selected > 0)

    def _get_selected_artists(self) -> list:
        selected = []
        for i in range(self.artists_list.count()):
            it = self.artists_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                data = it.data(Qt.ItemDataRole.UserRole)
                if data:
                    selected.append(data)
        return selected

    def _on_artist_double_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self._open_artist_posts(data)

    def _view_selected_artist_posts(self):
        selected = self._get_selected_artists()
        if selected:
            self._open_artist_posts(selected[0])

    def _open_artist_posts(self, artist_data: dict):
        if hasattr(self.parent_app, "posts_tab") and hasattr(self.parent_app, "switch_tab"):
            svc = artist_data.get("service") or "patreon"
            cid = str(artist_data.get("id") or "")
            creator = {
                "service": svc,
                "id": cid,
                "name": artist_data.get("name") or "Artist",
                "url": artist_data.get("url") or f"https://pawchive.pw/{svc}/user/{cid}"
            }
            if hasattr(self.parent_app.posts_tab, "_close_post_detail"):
                self.parent_app.posts_tab._close_post_detail()
            self.parent_app.switch_tab("posts")
            self.parent_app.posts_tab.load_creators([creator])

    def _remove_selected_artists(self):
        selected = self._get_selected_artists()
        if not selected:
            QMessageBox.information(self, "No Selection", "Please select at least one artist to remove.")
            return

        res = QMessageBox.question(
            self, "Remove Favorites",
            f"Remove {len(selected)} artist(s) from your local favorites list?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if res == QMessageBox.StandardButton.Yes:
            for a in selected:
                self.fav_manager.remove_creator(a.get("service"), a.get("id"))
            self._filter_artists()

    def _show_artists_context_menu(self, pos):
        item = self.artists_list.itemAt(pos)
        if not item:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return

        menu = QMenu(self)
        view_act = menu.addAction(QIcon(get_asset_path("assets/uisvg/manga.svg")), "View Posts in App")
        view_act.triggered.connect(lambda: self._open_artist_posts(data))

        dl_act = menu.addAction(QIcon(get_asset_path("assets/Svg/download.svg")), "Send to Downloader")
        dl_act.triggered.connect(lambda: self._download_single_artist(data))

        copy_act = menu.addAction(QIcon(get_asset_path("assets/Svg/copy.svg")), "Copy Artist URL")
        copy_act.triggered.connect(lambda: QApplication.clipboard().setText(data.get("url", "")))

        open_act = menu.addAction(QIcon(get_asset_path("assets/Svg/eye_open.svg")), "Open in Browser")
        open_act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(data.get("url", ""))))

        menu.addSeparator()
        if self._is_local_artists_mode():
            rem_act = menu.addAction(QIcon(get_asset_path("assets/uisvg/delete.svg")), "Remove from Local Favorites")
            rem_act.triggered.connect(lambda: (self.fav_manager.remove_creator(data.get("service"), data.get("id")), self._filter_artists()))
        else:
            save_act = menu.addAction(QIcon(get_asset_path("assets/Svg/star.svg")), "Save to Local Favorites")
            save_act.triggered.connect(lambda: (self.fav_manager.add_creator(data), QMessageBox.information(self, "Saved", f"Saved {data.get('name')} to local favorites!")))

        menu.exec(self.artists_list.viewport().mapToGlobal(pos))

    def _download_single_artist(self, a: dict):
        if not a:
            return
        url = a.get("url") or f"https://pawchive.pw/{a.get('service')}/user/{a.get('id')}"
        name = a.get("name") or "Artist"
        self.parent_app.favorite_download_queue.append({
            'url': url,
            'name': name,
            'name_for_folder': name,
            'type': 'artist'
        })
        if not self.parent_app.is_processing_favorites_queue:
            self.parent_app._process_next_favorite_download()
        if hasattr(self.parent_app, 'switch_tab'):
            self.parent_app.switch_tab("queue")

    def _download_selected_artists(self):
        selected_artists = self._get_selected_artists()
        if not selected_artists:
            QMessageBox.information(self, "No Selection", "Please select at least one artist to download.")
            return

        if hasattr(self.parent_app, 'log_signal'):
            self.parent_app.log_signal.emit(f"Queuing {len(selected_artists)} favorite artist(s) for download.")

        for a in selected_artists:
            url = a.get("url") or f"https://pawchive.pw/{a.get('service')}/user/{a.get('id')}"
            name = a.get("name") or "Artist"
            self.parent_app.favorite_download_queue.append({
                'url': url,
                'name': name,
                'name_for_folder': name,
                'type': 'artist'
            })

        if not self.parent_app.is_processing_favorites_queue:
            self.parent_app._process_next_favorite_download()

        if hasattr(self.parent_app, 'switch_tab'):
            self.parent_app.switch_tab("queue")

    # ─────────────────────────────────────────────────────────────────────────
    #  Favorite Posts Logic
    # ─────────────────────────────────────────────────────────────────────────

    def _start_fetch_posts(self):
        if self.posts_thread and self.posts_thread.isRunning():
            return

        cfg = self._get_cookies_config()
        self.fetch_posts_btn.setEnabled(False)
        self.posts_progress_bar.setVisible(True)
        self.posts_progress_bar.setRange(0, 0)
        self.posts_status_lbl.setText("Connecting to favorite posts API...")

        pref_platform = self.posts_platform_combo.currentText()
        target_domain = None
        if pref_platform == "Kemono":
            target_domain = "kemono.cr"
        elif pref_platform == "Coomer":
            target_domain = "coomer.st"
        elif pref_platform == "Pawchive":
            target_domain = "pawchive.pw"

        logger_func = self.parent_app.log_signal.emit if hasattr(self.parent_app, 'log_signal') else print
        creator_cache = getattr(self.parent_app, 'creator_name_cache', {})
        self.posts_thread = FavoritePostsFetcherThread(
            cfg, target_domain_preference=target_domain,
            logger_func=logger_func, creator_name_cache=creator_cache
        )
        self.posts_thread.status_signal.connect(self.posts_status_lbl.setText)
        self.posts_thread.finished_signal.connect(self._on_posts_fetched)
        self.posts_thread.start()

    def _on_posts_fetched(self, success, err_msg, posts_list):
        self.fetch_posts_btn.setEnabled(True)
        self.posts_progress_bar.setVisible(False)

        if success and posts_list:
            self.account_posts = posts_list
            self.posts_status_lbl.setText(f"Found {len(self.account_posts)} favorite post(s) from account.")
            self._filter_posts()
        else:
            self.posts_status_lbl.setText(f"{err_msg or 'No favorite posts found on connected accounts.'}")
            if not self.account_posts:
                self.posts_list.clear()
            self._update_posts_count()

    def _import_fetched_posts_to_local(self):
        if not self.account_posts:
            QMessageBox.information(self, "No Posts", "Please fetch account posts first before saving to local.")
            return
        added_count = 0
        for p in self.account_posts:
            post_dict = {
                "id": str(p.get("post_id") or ""),
                "title": p.get("title") or "Untitled Post",
                "service": p.get("service") or "patreon",
                "user": str(p.get("creator_id") or ""),
                "_creator_name": p.get("creator_name_resolved") or "",
                "_creator_service": p.get("service") or "",
                "_creator_id": str(p.get("creator_id") or ""),
                "published": p.get("added_date") or "",
            }
            if self.fav_manager.add_post(post_dict):
                added_count += 1
        QMessageBox.information(
            self, "Import Completed",
            f"Saved {added_count} new post(s) to your Local Favorites list!\n"
            f"You can now view them offline without needing cookies."
        )
        self.posts_source_combo.setCurrentIndex(0)  # Switch back to local view

    def _filter_posts(self):
        query = self.posts_search.text().lower().strip()
        pref = self.posts_platform_combo.currentText().lower()

        self.posts_list.blockSignals(True)
        self.posts_list.clear()

        is_local = self._is_local_posts_mode()
        source_data = self.fav_manager.get_all_posts() if is_local else self.account_posts

        filtered = []
        for p in source_data:
            svc = (p.get('service') or '').lower()
            if pref != "all platforms":
                src = (p.get('_source_api') or '').lower()
                if pref not in src and pref not in svc:
                    # Check friendly names
                    if pref == "pixiv fanbox" and svc != "fanbox":
                        continue
                    elif pref != "pixiv fanbox" and pref not in svc:
                        continue

            if query:
                t = (p.get('title') or '').lower()
                cr = (p.get('creator_name') or p.get('creator_name_resolved') or '').lower()
                cid = str(p.get('creator_id') or p.get('user') or '').lower()
                pid = str(p.get('post_id') or p.get('id') or '').lower()
                if query not in t and query not in cr and query not in cid and query not in pid and query not in svc:
                    continue
            filtered.append(p)

        self.filtered_posts = filtered

        # Populate list grouped by creator
        grouped = defaultdict(list)
        for p in filtered:
            svc = p.get('service', 'N/A')
            cid = str(p.get('creator_id') or p.get('user') or 'Unknown')
            cname = p.get('creator_name') or p.get('creator_name_resolved') or cid
            grouped[(svc, cid, cname)].append(p)

        for (svc, cid, cname), p_list in grouped.items():
            # Creator Header
            hdr = QListWidgetItem(f"{cname}  [{svc.capitalize()} / {cid}]  ({len(p_list)} post{'s' if len(p_list) != 1 else ''})")
            hdr.setIcon(QIcon(get_asset_path("assets/Svg/user.svg")))
            hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            font = hdr.font()
            font.setBold(True)
            hdr.setFont(font)
            hdr.setForeground(QColor("#E5A198"))
            self.posts_list.addItem(hdr)

            # Posts under this creator
            for post_data in p_list:
                it = QListWidgetItem()
                icon_file = "assets/Svg/star.svg" if is_local else "assets/uisvg/manga.svg"
                it.setIcon(QIcon(get_asset_path(icon_file)))

                title = post_data.get('title') or 'Untitled Post'
                pub_date = post_data.get('published') or post_data.get('added_date') or ''
                date_str = f" • {str(pub_date)[:10]}" if pub_date else ""
                it.setText(f"{title}{date_str}")

                pid = str(post_data.get('post_id') or post_data.get('id') or '')
                it.setToolTip(f"Post ID: {pid}\nCreator: {cname}\nService: {svc}\nDouble-click to view media & description in app")
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Unchecked)
                it.setData(Qt.ItemDataRole.UserRole, post_data)
                self.posts_list.addItem(it)

        self.posts_list.blockSignals(False)
        self._update_posts_count()

        if is_local:
            if not self.filtered_posts:
                self.posts_status_lbl.setText("No local favorite posts saved yet. Click '☆ Favorite' on any post to bookmark it.")
            else:
                self.posts_status_lbl.setText(f"{len(self.filtered_posts)} favorite post(s) stored locally on this PC.")

    def _select_all_posts(self):
        self.posts_list.blockSignals(True)
        for i in range(self.posts_list.count()):
            it = self.posts_list.item(i)
            if it.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                it.setCheckState(Qt.CheckState.Checked)
        self.posts_list.blockSignals(False)
        self._update_posts_count()

    def _deselect_all_posts(self):
        self.posts_list.blockSignals(True)
        for i in range(self.posts_list.count()):
            it = self.posts_list.item(i)
            if it.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                it.setCheckState(Qt.CheckState.Unchecked)
        self.posts_list.blockSignals(False)
        self._update_posts_count()

    def _update_posts_count(self):
        total = sum(
            1 for i in range(self.posts_list.count())
            if self.posts_list.item(i).flags() & Qt.ItemFlag.ItemIsUserCheckable
        )
        selected = sum(
            1 for i in range(self.posts_list.count())
            if self.posts_list.item(i).flags() & Qt.ItemFlag.ItemIsUserCheckable and self.posts_list.item(i).checkState() == Qt.CheckState.Checked
        )
        self.posts_count_lbl.setText(f"{total} posts • {selected} selected")
        self.download_posts_btn.setEnabled(selected > 0)
        self.download_posts_btn.setText(f"  Download Selected ({selected})" if selected > 0 else "  Download Selected")
        self.view_post_btn.setEnabled(selected == 1)
        self.remove_post_btn.setEnabled(selected > 0)

    def _get_selected_posts(self) -> list:
        selected = []
        for i in range(self.posts_list.count()):
            it = self.posts_list.item(i)
            if it.flags() & Qt.ItemFlag.ItemIsUserCheckable and it.checkState() == Qt.CheckState.Checked:
                data = it.data(Qt.ItemDataRole.UserRole)
                if data:
                    selected.append(data)
        return selected

    def _on_post_double_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self._open_post_in_app(data)

    def _view_selected_post(self):
        selected = self._get_selected_posts()
        if selected:
            self._open_post_in_app(selected[0])

    def _open_post_in_app(self, post_data: dict):
        if not hasattr(self.parent_app, "posts_tab") or not hasattr(self.parent_app, "switch_tab"):
            return

        post = post_data.get("post_payload")
        if not post:
            svc = post_data.get("service") or "patreon"
            cid = str(post_data.get("creator_id") or post_data.get("user") or "")
            pid = str(post_data.get("post_id") or post_data.get("id") or "")
            post = {
                "id": pid,
                "title": post_data.get("title") or f"Post #{pid}",
                "service": svc,
                "user": cid,
                "_creator_service": svc,
                "_creator_id": cid,
                "_creator_name": post_data.get("creator_name") or post_data.get("creator_name_resolved") or "Creator",
                "published": post_data.get("published") or post_data.get("added_date") or "",
            }

        self.parent_app.switch_tab("posts")
        self.parent_app.posts_tab._open_post_detail(post)

    def _remove_selected_posts(self):
        selected = self._get_selected_posts()
        if not selected:
            QMessageBox.information(self, "No Selection", "Please select at least one post to remove.")
            return

        res = QMessageBox.question(
            self, "Remove Favorites",
            f"Remove {len(selected)} post(s) from your local favorites list?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if res == QMessageBox.StandardButton.Yes:
            for p in selected:
                svc = p.get("service")
                cid = str(p.get("creator_id") or p.get("user") or "")
                pid = str(p.get("post_id") or p.get("id") or "")
                self.fav_manager.remove_post(svc, cid, pid)
            self._filter_posts()

    def _show_posts_context_menu(self, pos):
        item = self.posts_list.itemAt(pos)
        if not item or not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return

        menu = QMenu(self)
        view_act = menu.addAction(QIcon(get_asset_path("assets/uisvg/play.svg")), "View Post in App (Media Player)")
        view_act.triggered.connect(lambda: self._open_post_in_app(data))

        dl_act = menu.addAction(QIcon(get_asset_path("assets/Svg/download.svg")), "Send to Downloader")
        dl_act.triggered.connect(lambda: self._download_single_post(data))

        copy_act = menu.addAction(QIcon(get_asset_path("assets/Svg/copy.svg")), "Copy Post URL")
        copy_act.triggered.connect(lambda: QApplication.clipboard().setText(self._get_post_url(data)))

        open_act = menu.addAction(QIcon(get_asset_path("assets/Svg/eye_open.svg")), "Open in Browser")
        open_act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(self._get_post_url(data))))

        menu.addSeparator()
        if self._is_local_posts_mode():
            rem_act = menu.addAction(QIcon(get_asset_path("assets/uisvg/delete.svg")), "Remove from Local Favorites")
            svc = data.get("service")
            cid = str(data.get("creator_id") or data.get("user") or "")
            pid = str(data.get("post_id") or data.get("id") or "")
            rem_act.triggered.connect(lambda: (self.fav_manager.remove_post(svc, cid, pid), self._filter_posts()))
        else:
            save_act = menu.addAction(QIcon(get_asset_path("assets/Svg/star.svg")), "Save to Local Favorites")
            save_act.triggered.connect(lambda: (self.fav_manager.add_post(data), QMessageBox.information(self, "Saved", f"Saved post to local favorites!")))

        menu.exec(self.posts_list.viewport().mapToGlobal(pos))

    def _get_post_url(self, post_data: dict) -> str:
        if post_data.get("url"):
            return post_data["url"]
        svc = post_data.get("service") or "patreon"
        cid = str(post_data.get("creator_id") or post_data.get("user") or "")
        pid = str(post_data.get("post_id") or post_data.get("id") or "")
        return f"https://pawchive.pw/{svc}/user/{cid}/post/{pid}" if cid else f"https://pawchive.pw/{svc}/post/{pid}"

    def _download_single_post(self, post_data: dict):
        if not post_data:
            return
        direct_post_url = self._get_post_url(post_data)
        self.parent_app.favorite_download_queue.append({
            'url': direct_post_url,
            'name': post_data.get('title') or f"Post #{post_data.get('post_id')}",
            'name_for_folder': post_data.get('creator_name', post_data.get('creator_name_resolved', post_data.get('creator_id', 'Unknown'))),
            'type': 'post'
        })
        if not self.parent_app.is_processing_favorites_queue:
            self.parent_app._process_next_favorite_download()
        if hasattr(self.parent_app, 'switch_tab'):
            self.parent_app.switch_tab("queue")

    def _download_selected_posts(self):
        selected_posts = self._get_selected_posts()
        if not selected_posts:
            QMessageBox.information(self, "No Selection", "Please select at least one post to download.")
            return

        if hasattr(self.parent_app, 'log_signal'):
            self.parent_app.log_signal.emit(f"Queuing {len(selected_posts)} favorite post(s) for download.")

        for post_data in selected_posts:
            direct_post_url = self._get_post_url(post_data)
            self.parent_app.favorite_download_queue.append({
                'url': direct_post_url,
                'name': post_data.get('title') or f"Post #{post_data.get('post_id')}",
                'name_for_folder': post_data.get('creator_name', post_data.get('creator_name_resolved', post_data.get('creator_id', 'Unknown'))),
                'type': 'post'
            })

        if not self.parent_app.is_processing_favorites_queue:
            self.parent_app._process_next_favorite_download()

        if hasattr(self.parent_app, 'switch_tab'):
            self.parent_app.switch_tab("queue")
