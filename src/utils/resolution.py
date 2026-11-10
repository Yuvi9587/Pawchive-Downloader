import os

from PySide6.QtWidgets import (
    QSplitter, QScrollArea, QFrame, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QStackedWidget, QButtonGroup, QRadioButton, QCheckBox,
    QListWidget, QTextEdit, QApplication, QSizePolicy, QToolButton
)
from PySide6.QtCore import Qt, QSize, QUrl
from PySide6.QtGui import QIntValidator, QFont, QIcon, QPixmap, QDesktopServices

from ..config.constants import *
from ..ui.assets import get_asset_path, get_asset_html_path


# ─────────────────────────────────────────────────────────────────────────────
#  Small UI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bold(widget):
    """Make widget text bold."""
    f = widget.font()
    f.setBold(True)
    widget.setFont(f)


def _sep():
    """Thin 1-px horizontal separator line."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setObjectName("FlatSep")
    return line


def _field_label(html_or_text: str) -> QLabel:
    """Small bold label that sits above an input row."""
    lbl = QLabel(html_or_text)
    lbl.setObjectName("FieldLabel")
    return lbl


def _section_label(html_or_text: str) -> QLabel:
    """Slightly larger section header label."""
    lbl = QLabel(html_or_text)
    lbl.setObjectName("SectionLabel")
    return lbl


def _nav_btn(icon_name: str, text: str, scale: float, selected: bool = False) -> QToolButton:
    """Sidebar navigation button: icon on top, label below."""
    btn = QToolButton()
    btn.setIcon(QIcon(get_asset_path(f"assets/Svg/{icon_name}.svg")))
    btn.setIconSize(QSize(int(22 * scale), int(22 * scale)))
    btn.setText(text)
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
    btn.setObjectName("NavButtonSelected" if selected else "NavButton")
    btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    btn.setMinimumHeight(int(60 * scale))
    return btn


# ─────────────────────────────────────────────────────────────────────────────
#  Main UI builder
# ─────────────────────────────────────────────────────────────────────────────

def setup_ui(main_app):
    """
    Initialises and scales the user interface for the DownloaderApp.

    Args:
        main_app: The instance of the main DownloaderApp.
    """
    scale = float(main_app.settings.value(UI_SCALE_KEY, 1.0))
    main_app.scale_factor = scale

    default_font = QApplication.font()
    default_font.setPointSize(int(9 * scale))
    main_app.setFont(default_font)

    apply_theme_to_app(main_app, main_app.current_theme, initial_load=True)

    # ── Main horizontal splitter (left content | right log) ──────────────────
    main_app.main_splitter = QSplitter(Qt.Orientation.Horizontal)

    # ══════════════════════════════════════════════════════════════════════════
    #  SIDEBAR
    # ══════════════════════════════════════════════════════════════════════════
    sidebar = QWidget()
    sidebar.setObjectName("Sidebar")
    sidebar.setFixedWidth(int(84 * scale))

    sb_layout = QVBoxLayout(sidebar)
    sb_layout.setContentsMargins(int(6 * scale), int(12 * scale), int(6 * scale), int(10 * scale))
    sb_layout.setSpacing(int(3 * scale))

    # Brand / app title
    brand_name = QLabel("Pawchive")
    brand_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
    brand_name.setObjectName("BrandName")
    sb_layout.addWidget(brand_name)

    sb_layout.addSpacing(int(8 * scale))
    sb_layout.addWidget(_sep())
    sb_layout.addSpacing(int(6 * scale))

    # Top nav items: Downloader, Queue, Creators, Posts, Auto-Sync, and Visual Sort
    main_app.nav_dl = _nav_btn("download", "Downloader", scale, selected=True)
    sb_layout.addWidget(main_app.nav_dl)

    main_app.nav_queue = _nav_btn("queue", "Queue", scale, selected=False)
    sb_layout.addWidget(main_app.nav_queue)

    main_app.nav_creators = _nav_btn("user", "Creators", scale, selected=False)
    sb_layout.addWidget(main_app.nav_creators)

    main_app.nav_posts = _nav_btn("doc", "Posts", scale, selected=False)
    sb_layout.addWidget(main_app.nav_posts)

    main_app.nav_sync = _nav_btn("sync", "Auto-Sync", scale, selected=False)
    sb_layout.addWidget(main_app.nav_sync)

    main_app.nav_visual_sort = _nav_btn("visual_sort", "Visual Sort", scale, selected=False)
    sb_layout.addWidget(main_app.nav_visual_sort)

    main_app.nav_favorites = _nav_btn("star", "Favorites", scale, selected=False)
    sb_layout.addWidget(main_app.nav_favorites)

    # Push bottom nav items to the bottom
    sb_layout.addStretch(1)

    # Nav items at bottom of sidebar: Settings & About
    main_app.future_settings_button = _nav_btn("settings", "Settings", scale)
    main_app.future_settings_button.clicked.connect(main_app._show_future_settings_dialog)
    sb_layout.addWidget(main_app.future_settings_button)

    main_app.known_names_help_button = _nav_btn("help", "About", scale)
    main_app.nav_about = main_app.known_names_help_button
    sb_layout.addWidget(main_app.known_names_help_button)

    main_app.nav_support = _nav_btn("heart", "Support", scale)
    sb_layout.addWidget(main_app.nav_support)

    sb_layout.addSpacing(int(12 * scale))

    # Pawchive website logo button at the very bottom
    main_app.paw_website_btn = QPushButton()
    main_app.paw_website_btn.setObjectName("PawWebsiteButton")
    paw_size = int(32 * scale)
    paw_icon = QIcon(get_asset_path("assets/Svg/paw-print.svg"))
    main_app.paw_website_btn.setIcon(paw_icon)
    main_app.paw_website_btn.setIconSize(QSize(paw_size, paw_size))
    main_app.paw_website_btn.setFixedSize(int(38 * scale), int(38 * scale))
    main_app.paw_website_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    main_app.paw_website_btn.setToolTip("Visit Pawchive (pawchive.pw)")
    main_app.paw_website_btn.setStyleSheet(
        "QPushButton { background: transparent; border: none; outline: none; padding: 0px; margin: 0px; } "
        "QPushButton:hover { background: transparent; border: none; } "
        "QPushButton:pressed { background: transparent; border: none; }"
    )
    main_app.paw_website_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://pawchive.pw")))
    sb_layout.addWidget(main_app.paw_website_btn, 0, Qt.AlignmentFlag.AlignCenter)

    # ══════════════════════════════════════════════════════════════════════════
    #  LEFT PANEL  (flat scrollable content area)
    # ══════════════════════════════════════════════════════════════════════════
    left_scroll = QScrollArea()
    left_scroll.setWidgetResizable(True)
    left_scroll.setFrameShape(QFrame.Shape.NoFrame)
    left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    left_widget = QWidget()
    left_widget.setObjectName("ContentArea")
    lv = QVBoxLayout(left_widget)
    lv.setContentsMargins(int(14 * scale), int(10 * scale), int(14 * scale), int(10 * scale))
    lv.setSpacing(int(6 * scale))
    left_scroll.setWidget(left_widget)

    # ── Section 1: URL ────────────────────────────────────────────────────────
    lv.addWidget(_field_label(
        f"<img src='{get_asset_html_path('assets/Svg/link.svg')}' "
        f"width='{int(15 * scale)}' height='{int(15 * scale)}' align='top'>&nbsp; Creator / Post (Pawchive URL)"
    ))

    # URL input widget (stacked with favourite placeholder)
    main_app.url_input_widget = QWidget()
    url_row = QHBoxLayout(main_app.url_input_widget)
    url_row.setContentsMargins(0, 0, 0, 0)
    url_row.setSpacing(int(5 * scale))

    main_app.url_label_widget = QLabel()
    main_app.url_label_widget.setVisible(False)  # Hidden: field_label above replaces it

    main_app.link_input = QLineEdit()
    main_app.link_input.setPlaceholderText(
        "e.g., https://pawchive.pw/patreon/user/12345 or .../post/98765"
    )
    main_app.link_input.textChanged.connect(main_app.update_custom_folder_visibility)
    url_row.addWidget(main_app.link_input, 1)

    main_app.empty_popup_button = QPushButton()   # kept for compat; hidden
    main_app.empty_popup_button.setVisible(False)

    main_app.page_range_label = QLabel(main_app._tr("page_range_label_text", "Page Range:"))
    main_app.page_range_label.setObjectName("FieldLabel")
    url_row.addWidget(main_app.page_range_label)

    main_app.start_page_input = QLineEdit()
    main_app.start_page_input.setPlaceholderText("Start")
    main_app.start_page_input.setFixedWidth(int(46 * scale))
    main_app.start_page_input.setValidator(QIntValidator(1, 99999))
    url_row.addWidget(main_app.start_page_input)

    main_app.to_label = QLabel("to")
    main_app.to_label.setStyleSheet("font-weight: normal; color: #808080; border: none;")
    url_row.addWidget(main_app.to_label)

    main_app.end_page_input = QLineEdit()
    main_app.end_page_input.setPlaceholderText("End")
    main_app.end_page_input.setFixedWidth(int(46 * scale))
    main_app.end_page_input.setToolTip(
        main_app._tr("end_page_input_tooltip", "For creator URLs: Specify the ending page number...")
    )
    main_app.end_page_input.setValidator(QIntValidator(1, 99999))
    url_row.addWidget(main_app.end_page_input)

    # Favourite mode placeholder (swaps with url_input_widget)
    main_app.url_placeholder_widget = QWidget()
    ph_row = QHBoxLayout(main_app.url_placeholder_widget)
    ph_row.setContentsMargins(0, 0, 0, 0)
    main_app.fav_mode_active_label = QLabel(
        main_app._tr(
            "fav_mode_active_label_text",
            f"<img src='{get_asset_html_path('assets/Svg/star.svg')}' "
            f"width='13' height='13' align='top'> Favorite Mode is active..."
        )
    )
    main_app.fav_mode_active_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    ph_row.addWidget(main_app.fav_mode_active_label)

    main_app.url_or_placeholder_stack = QStackedWidget()
    main_app.url_or_placeholder_stack.addWidget(main_app.url_input_widget)
    main_app.url_or_placeholder_stack.addWidget(main_app.url_placeholder_widget)
    lv.addWidget(main_app.url_or_placeholder_stack)

    lv.addWidget(_sep())

    # ── Section 2: Download Location ─────────────────────────────────────────
    lv.addWidget(_field_label(
        f"<img src='{get_asset_html_path('assets/Svg/add.svg')}' "
        f"width='{int(15 * scale)}' height='{int(15 * scale)}' align='top'>&nbsp; Download Location"
    ))

    main_app.download_location_label_widget = QLabel()
    main_app.download_location_label_widget.setVisible(False)   # compat; label above replaces it

    dir_row = QHBoxLayout()
    dir_row.setSpacing(int(5 * scale))
    main_app.dir_input = QLineEdit()
    main_app.dir_input.setPlaceholderText("Select folder where downloads will be saved")
    main_app.dir_button = QPushButton("  Browse…")
    main_app.dir_button.setIcon(QIcon(get_asset_path("assets/Svg/add.svg")))
    main_app.dir_button.setIconSize(QSize(int(14 * scale), int(14 * scale)))
    main_app.dir_button.clicked.connect(main_app.browse_directory)
    dir_row.addWidget(main_app.dir_input, 1)
    dir_row.addWidget(main_app.dir_button)
    lv.addLayout(dir_row)

    lv.addWidget(_sep())

    # ── Section 3: Filters ────────────────────────────────────────────────────
    # Row A: Character filter (left) | Custom folder / Filter Title (right)
    main_app.filters_and_custom_folder_container_widget = QWidget()
    cf_h = QHBoxLayout(main_app.filters_and_custom_folder_container_widget)
    cf_h.setContentsMargins(0, 0, 0, 0)
    cf_h.setSpacing(int(12 * scale))

    # Character filter
    main_app.character_filter_widget = QWidget()
    char_v = QVBoxLayout(main_app.character_filter_widget)
    char_v.setContentsMargins(0, 0, 0, 0)
    char_v.setSpacing(int(3 * scale))
    main_app.character_label = QLabel(
        f"<img src='{get_asset_html_path('assets/Svg/target.svg')}' "
        f"width='{int(15 * scale)}' height='{int(15 * scale)}' align='top'>&nbsp; Filter by Character(s) (comma-separated):"
    )
    main_app.character_label.setObjectName("FieldLabel")
    char_v.addWidget(main_app.character_label)
    char_input_row = QHBoxLayout()
    char_input_row.setSpacing(int(5 * scale))
    main_app.character_input = QLineEdit()
    main_app.character_input.setPlaceholderText("e.g., Tifa, Aerith, (Cloud, Zack)")
    char_input_row.addWidget(main_app.character_input, 3)
    main_app.char_filter_scope_toggle_button = QPushButton()
    main_app._update_char_filter_scope_button_text()
    char_input_row.addWidget(main_app.char_filter_scope_toggle_button, 1)
    char_v.addLayout(char_input_row)
    cf_h.addWidget(main_app.character_filter_widget, 3)

    # Custom folder (single post only — hidden by default)
    main_app.custom_folder_widget = QWidget()
    custom_v = QVBoxLayout(main_app.custom_folder_widget)
    custom_v.setContentsMargins(0, 0, 0, 0)
    custom_v.setSpacing(int(3 * scale))
    main_app.custom_folder_label = QLabel("🗄️ Custom Folder (Single Post):")
    main_app.custom_folder_label.setObjectName("FieldLabel")
    main_app.custom_folder_input = QLineEdit()
    main_app.custom_folder_input.setPlaceholderText("Optional: specific folder name")
    custom_v.addWidget(main_app.custom_folder_label)
    custom_v.addWidget(main_app.custom_folder_input)
    main_app.custom_folder_widget.setVisible(False)
    cf_h.addWidget(main_app.custom_folder_widget, 2)

    lv.addWidget(main_app.filters_and_custom_folder_container_widget)

    # Row B: Skip words (left) | Remove from filename (right)
    skip_remove_h = QHBoxLayout()
    skip_remove_h.setSpacing(int(12 * scale))

    skip_col = QVBoxLayout()
    skip_col.setSpacing(int(3 * scale))
    main_app.skip_words_label_widget = QLabel()
    main_app.skip_words_label_widget.setObjectName("FieldLabel")
    skip_col.addWidget(main_app.skip_words_label_widget)
    skip_input_h = QHBoxLayout()
    skip_input_h.setSpacing(int(5 * scale))
    main_app.skip_words_input = QLineEdit()
    main_app.skip_words_input.setPlaceholderText("e.g., WM, WIP, sketch, preview")
    skip_input_h.addWidget(main_app.skip_words_input, 1)
    main_app.skip_scope_toggle_button = QPushButton()
    main_app._update_skip_scope_button_text()
    skip_input_h.addWidget(main_app.skip_scope_toggle_button)
    skip_col.addLayout(skip_input_h)
    skip_remove_h.addLayout(skip_col, 3)

    remove_col = QVBoxLayout()
    remove_col.setSpacing(int(3 * scale))
    main_app.remove_from_filename_label_widget = QLabel()
    main_app.remove_from_filename_label_widget.setObjectName("FieldLabel")
    remove_col.addWidget(main_app.remove_from_filename_label_widget)
    main_app.remove_from_filename_input = QLineEdit()
    main_app.remove_from_filename_input.setPlaceholderText("e.g., patreon, HD")
    remove_col.addWidget(main_app.remove_from_filename_input)
    skip_remove_h.addLayout(remove_col, 2)

    lv.addLayout(skip_remove_h)

    lv.addWidget(_sep())

    # ── Section 4: File Type & Mode ───────────────────────────────────────────
    lv.addWidget(_field_label(
        f"<img src='{get_asset_html_path('assets/Svg/archive.svg')}' "
        f"width='{int(15 * scale)}' height='{int(15 * scale)}' align='top'>&nbsp; Filter Files"
    ))

    # Radio row
    radio_h = QHBoxLayout()
    radio_h.setSpacing(int(7 * scale))
    main_app.radio_group = QButtonGroup(main_app)
    main_app.radio_all            = QRadioButton("All")
    main_app.radio_images         = QRadioButton("Images/GIFs")
    main_app.radio_videos         = QRadioButton("Videos")
    main_app.radio_only_archives  = QRadioButton("Only Archives")
    main_app.radio_only_archives.setIcon(QIcon(get_asset_path("assets/Svg/archive.svg")))
    main_app.radio_only_archives.setIconSize(QSize(int(14 * scale), int(14 * scale)))
    main_app.radio_only_audio     = QRadioButton("Only Audio")
    main_app.radio_only_audio.setIcon(QIcon(get_asset_path("assets/Svg/audio.svg")))
    main_app.radio_only_audio.setIconSize(QSize(int(14 * scale), int(14 * scale)))
    main_app.radio_only_links     = QRadioButton("Only Links")
    main_app.radio_only_links.setIcon(QIcon(get_asset_path("assets/Svg/link.svg")))
    main_app.radio_only_links.setIconSize(QSize(int(14 * scale), int(14 * scale)))
    main_app.radio_more           = QRadioButton("More")
    main_app.checkbox_more        = QCheckBox("More")
    main_app.checkbox_more.setVisible(False)
    main_app.checkbox_more.setToolTip(
        "Combined Download: Create PDF while downloading files.\n"
        "Right-click or double-click to configure More Options."
    )
    main_app.radio_all.setChecked(True)

    for btn in [
        main_app.radio_all, main_app.radio_images, main_app.radio_videos,
        main_app.radio_only_archives, main_app.radio_only_audio,
        main_app.radio_only_links, main_app.radio_more,
    ]:
        main_app.radio_group.addButton(btn)
        radio_h.addWidget(btn)
    radio_h.addWidget(main_app.checkbox_more)
    radio_h.addStretch(1)
    lv.addLayout(radio_h)

    # Quick checkboxes row
    quick_h = QHBoxLayout()
    quick_h.setSpacing(int(12 * scale))
    main_app.skip_zip_checkbox = QCheckBox("Skip Archives")
    main_app.skip_zip_checkbox.setToolTip("Skip Common Archives (Eg.. Zip, Rar, 7z)")
    main_app.skip_zip_checkbox.setChecked(True)
    quick_h.addWidget(main_app.skip_zip_checkbox)
    main_app.download_thumbnails_checkbox = QCheckBox("Download thumbnails only")
    quick_h.addWidget(main_app.download_thumbnails_checkbox)
    main_app.scan_content_images_checkbox = QCheckBox("Scan for images")
    main_app.scan_content_images_checkbox.setChecked(main_app.scan_content_images_setting)
    quick_h.addWidget(main_app.scan_content_images_checkbox)
    main_app.compress_images_checkbox = QCheckBox("Compress to WebP")
    main_app.compress_images_checkbox.setToolTip("Compress images > 1.5 MB to WebP (requires Pillow).")
    quick_h.addWidget(main_app.compress_images_checkbox)
    main_app.keep_duplicates_checkbox = QCheckBox("Keep Duplicates")
    main_app.keep_duplicates_checkbox.setToolTip(
        "If checked, downloads all files from a post even if they have the same name."
    )
    quick_h.addWidget(main_app.keep_duplicates_checkbox)

    quick_h.addStretch(1)
    lv.addLayout(quick_h)

    lv.addWidget(_sep())

    # ── Section 5: Advanced Settings ──────────────────────────────────────────
    adv_header_h = QHBoxLayout()
    adv_header_h.setSpacing(int(6 * scale))
    adv_header_h.addWidget(_section_label(
        f"<img src='{get_asset_html_path('assets/Svg/settings.svg')}' "
        f"width='{int(15 * scale)}' height='{int(15 * scale)}' align='top'>&nbsp; Advanced Settings"
    ))
    adv_header_h.addStretch(1)
    lv.addLayout(adv_header_h)

    main_app.advanced_settings_widget = QWidget()
    adv_v = QVBoxLayout(main_app.advanced_settings_widget)
    adv_v.setContentsMargins(0, 0, 0, 0)
    adv_v.setSpacing(int(6 * scale))

    # Row i — Organisation toggles + Use Cookie checkbox
    main_app.advanced_row1_layout = QHBoxLayout()
    main_app.advanced_row1_layout.setSpacing(int(10 * scale))

    main_app.use_subfolder_per_post_checkbox = QCheckBox("Subfolder per post")
    main_app.use_subfolder_per_post_checkbox.toggled.connect(main_app.update_ui_for_subfolders)
    main_app.use_subfolder_per_post_checkbox.setChecked(True)
    main_app.advanced_row1_layout.addWidget(main_app.use_subfolder_per_post_checkbox)

    main_app.date_prefix_checkbox = QCheckBox("Date Prefix")
    main_app.date_prefix_checkbox.setToolTip(
        "When 'Subfolder per Post' is active, prefix the folder name with the post's upload date."
    )
    main_app.advanced_row1_layout.addWidget(main_app.date_prefix_checkbox)

    main_app.use_subfolders_checkbox = QCheckBox("Separate folders by known.txt")
    main_app.use_subfolders_checkbox.setChecked(False)
    main_app.use_subfolders_checkbox.toggled.connect(main_app.update_ui_for_subfolders)
    main_app.advanced_row1_layout.addWidget(main_app.use_subfolders_checkbox)

    main_app.revisions_checkbox = QCheckBox("Download Revisions")
    main_app.revisions_checkbox.setToolTip("Fetch post revisions history (Kemono.cr / Coomer.st only)")
    main_app.revisions_checkbox.setChecked(
        main_app.settings.value("download_revisions", False, type=bool)
    )
    main_app.advanced_row1_layout.addWidget(main_app.revisions_checkbox)

    main_app.use_cookie_checkbox = QCheckBox("Use cookie")
    main_app.use_cookie_checkbox.setChecked(main_app.use_cookie_setting)
    main_app.advanced_row1_layout.addWidget(main_app.use_cookie_checkbox)

    main_app.advanced_row1_layout.addStretch(1)
    adv_v.addLayout(main_app.advanced_row1_layout)

    # Row ii — Cookie path input
    cookie_h = QHBoxLayout()
    cookie_h.setSpacing(int(5 * scale))
    main_app.cookie_text_input = QLineEdit()
    main_app.cookie_text_input.setPlaceholderText("Cookie string or path from Browse…")
    main_app.cookie_text_input.setText(main_app.cookie_text_setting)
    cookie_h.addWidget(main_app.cookie_text_input, 1)
    main_app.cookie_browse_button = QPushButton("Browse…")
    main_app.cookie_browse_button.setFixedWidth(int(75 * scale))
    cookie_h.addWidget(main_app.cookie_browse_button)
    adv_v.addLayout(cookie_h)

    # Row iii — Multithreading + misc
    misc_h = QHBoxLayout()
    misc_h.setSpacing(int(10 * scale))
    main_app.use_multithreading_checkbox = QCheckBox("Use multithreading")
    main_app.use_multithreading_checkbox.setChecked(True)
    misc_h.addWidget(main_app.use_multithreading_checkbox)
    main_app.thread_count_label = QLabel("Threads:")
    main_app.thread_count_label.setStyleSheet("font-weight: normal; color: #B0B0B0;")
    misc_h.addWidget(main_app.thread_count_label)
    main_app.thread_count_input = QLineEdit("2")
    main_app.thread_count_input.setFixedWidth(int(38 * scale))
    main_app.thread_count_input.setValidator(QIntValidator(1, MAX_THREADS))
    misc_h.addWidget(main_app.thread_count_input)
    main_app.external_links_checkbox = QCheckBox("Show external links in log")
    misc_h.addWidget(main_app.external_links_checkbox)
    main_app.manga_mode_checkbox = QCheckBox("Renaming Mode")
    misc_h.addWidget(main_app.manga_mode_checkbox)
    misc_h.addStretch(1)
    adv_v.addLayout(misc_h)

    lv.addWidget(main_app.advanced_settings_widget)

    lv.addWidget(_sep())

    # ── Section 6: Action Buttons ─────────────────────────────────────────────
    main_app.standard_action_buttons_widget = QWidget()
    btn_h = QHBoxLayout(main_app.standard_action_buttons_widget)
    btn_h.setContentsMargins(0, 0, 0, 0)
    btn_h.setSpacing(int(7 * scale))

    main_app.download_btn = QPushButton(
        f"  {main_app._tr('start_download_button_text', 'Start Download')}"
    )
    main_app.download_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
    main_app.download_btn.setIconSize(QSize(int(16 * scale), int(16 * scale)))
    _bold(main_app.download_btn)
    main_app.download_btn.setObjectName("PrimaryButton")
    main_app.download_btn.setMinimumHeight(int(32 * scale))
    main_app.download_btn.clicked.connect(main_app.start_download)
    btn_h.addWidget(main_app.download_btn, 2)

    main_app.add_queue_btn = QPushButton("  Add to Queue")
    main_app.add_queue_btn.setIcon(QIcon(get_asset_path("assets/Svg/add.svg")))
    main_app.add_queue_btn.setIconSize(QSize(int(15 * scale), int(15 * scale)))
    main_app.add_queue_btn.setToolTip("Save current settings as a job for later execution.")
    main_app.add_queue_btn.clicked.connect(main_app.add_current_settings_to_queue)
    btn_h.addWidget(main_app.add_queue_btn, 1)

    main_app.pause_btn = QPushButton("  Pause Download")
    main_app.pause_btn.setIcon(QIcon(get_asset_path("assets/Svg/pause.svg")))
    main_app.pause_btn.setIconSize(QSize(int(15 * scale), int(15 * scale)))
    main_app.pause_btn.setEnabled(False)
    main_app.pause_btn.clicked.connect(main_app._handle_pause_resume_action)
    btn_h.addWidget(main_app.pause_btn, 1)

    main_app.cancel_btn = QPushButton("  Cancel / Reset UI")
    main_app.cancel_btn.setIcon(QIcon(get_asset_path("assets/Svg/cancel.svg")))
    main_app.cancel_btn.setIconSize(QSize(int(15 * scale), int(15 * scale)))
    main_app.cancel_btn.setEnabled(False)
    main_app.cancel_btn.clicked.connect(main_app.cancel_download_button_action)
    btn_h.addWidget(main_app.cancel_btn, 1)

    main_app.error_btn = QPushButton("  Error")
    main_app.error_btn.setIcon(QIcon(get_asset_path("assets/Svg/error.svg")))
    main_app.error_btn.setIconSize(QSize(int(15 * scale), int(15 * scale)))
    main_app.error_btn.setToolTip("View files skipped due to errors and optionally retry them.")
    main_app.error_btn.setEnabled(True)
    btn_h.addWidget(main_app.error_btn, 1)

    # Favourite action buttons (swaps with standard via stack)
    main_app.favorite_action_buttons_widget = QWidget()
    fav_h = QHBoxLayout(main_app.favorite_action_buttons_widget)
    fav_h.setContentsMargins(0, 0, 0, 0)
    fav_h.setSpacing(int(7 * scale))
    main_app.favorite_mode_artists_button = QPushButton("🖼️ Favorite Artists")
    main_app.favorite_mode_posts_button   = QPushButton("📄 Favorite Posts")
    main_app.favorite_scope_toggle_button  = QPushButton()
    fav_h.addWidget(main_app.favorite_mode_artists_button)
    fav_h.addWidget(main_app.favorite_mode_posts_button)
    fav_h.addWidget(main_app.favorite_scope_toggle_button)

    main_app.bottom_action_buttons_stack = QStackedWidget()
    main_app.bottom_action_buttons_stack.addWidget(main_app.standard_action_buttons_widget)
    main_app.bottom_action_buttons_stack.addWidget(main_app.favorite_action_buttons_widget)
    lv.addWidget(main_app.bottom_action_buttons_stack)

    lv.addWidget(_sep())

    # ── Section 7: Known Characters ───────────────────────────────────────────
    # Header row
    chars_header_h = QHBoxLayout()
    chars_header_h.setSpacing(int(8 * scale))

    chars_label_lhs = _section_label(
        f"<img src='{get_asset_html_path('assets/Svg/mask.svg')}' "
        f"width='{int(15 * scale)}' height='{int(15 * scale)}' align='top'>&nbsp; Known Series / Characters (for folder names):"
    )
    chars_header_h.addWidget(chars_label_lhs)

    main_app.known_chars_label = QLabel()       # compatibility; invisible
    main_app.known_chars_label.setVisible(False)

    main_app.open_known_txt_button = QPushButton("  Open Known.txt")
    main_app.open_known_txt_button.setIcon(QIcon(get_asset_path("assets/Svg/scroll.svg")))
    main_app.open_known_txt_button.setIconSize(QSize(int(13 * scale), int(13 * scale)))
    main_app.open_known_txt_button.setFixedWidth(int(130 * scale))
    chars_header_h.addWidget(main_app.open_known_txt_button)

    main_app.character_search_input = QLineEdit()
    main_app.character_search_input.setPlaceholderText("Search characters…")
    chars_header_h.addWidget(main_app.character_search_input, 1)

    lv.addLayout(chars_header_h)

    # List widget
    main_app.character_list = QListWidget()
    main_app.character_list.setSelectionMode(QListWidget.ExtendedSelection)
    lv.addWidget(main_app.character_list, 1)

    # Manage row
    char_manage_h = QHBoxLayout()
    char_manage_h.setSpacing(int(5 * scale))

    main_app.new_char_input = QLineEdit()
    main_app.new_char_input.setPlaceholderText("Add new series/character name")
    char_manage_h.addWidget(main_app.new_char_input, 2)

    main_app.add_char_button = QPushButton("  Add")
    main_app.add_char_button.setIcon(QIcon(get_asset_path("assets/Svg/add.svg")))
    main_app.add_char_button.setIconSize(QSize(int(14 * scale), int(14 * scale)))
    main_app.add_char_button.clicked.connect(main_app._handle_ui_add_new_character)
    main_app.new_char_input.returnPressed.connect(main_app.add_char_button.click)
    char_manage_h.addWidget(main_app.add_char_button)

    main_app.add_to_filter_button = QPushButton("  Add to Filter")
    main_app.add_to_filter_button.setIcon(QIcon(get_asset_path("assets/Svg/filter_add.svg")))
    main_app.add_to_filter_button.setIconSize(QSize(int(14 * scale), int(14 * scale)))
    main_app.add_to_filter_button.setToolTip(
        "Select names… to add to the 'Filter by Character(s)' field."
    )
    char_manage_h.addWidget(main_app.add_to_filter_button)

    main_app.delete_char_button = QPushButton("  Delete Selected")
    main_app.delete_char_button.setIcon(QIcon(get_asset_path("assets/Svg/trash.svg")))
    main_app.delete_char_button.setIconSize(QSize(int(14 * scale), int(14 * scale)))
    main_app.delete_char_button.setToolTip("Delete the selected name(s)…")
    main_app.delete_char_button.clicked.connect(main_app.delete_selected_character)
    char_manage_h.addWidget(main_app.delete_char_button)

    main_app.history_button = QPushButton()
    main_app.history_button.setIcon(QIcon(get_asset_path("assets/Svg/scroll.svg")))
    main_app.history_button.setIconSize(QSize(int(16 * scale), int(16 * scale)))
    main_app.history_button.setFixedWidth(int(32 * scale))
    main_app.history_button.setToolTip(
        main_app._tr("history_button_tooltip_text", "View download history")
    )
    char_manage_h.addWidget(main_app.history_button)

    # Reference sidebar nav button for any legacy auto_sync_button callers
    main_app.auto_sync_button = main_app.nav_sync

    lv.addLayout(char_manage_h)
    lv.addStretch(0)

    # ══════════════════════════════════════════════════════════════════════════
    #  RIGHT PANEL — Progress Log
    # ══════════════════════════════════════════════════════════════════════════
    right_widget = QWidget()
    right_widget.setObjectName("RightPanel")
    right_layout = QVBoxLayout(right_widget)
    right_layout.setContentsMargins(
        int(10 * scale), int(10 * scale), int(10 * scale), int(10 * scale)
    )
    right_layout.setSpacing(int(5 * scale))

    # Log toolbar
    log_title_h = QHBoxLayout()
    log_title_h.setSpacing(int(5 * scale))

    main_app.progress_log_label = QLabel(
        f"<img src='{get_asset_html_path('assets/Svg/scroll.svg')}' "
        f"width='13' height='13' align='top'>&nbsp; Progress Log:"
    )
    log_title_h.addWidget(main_app.progress_log_label)
    log_title_h.addStretch(1)

    main_app.link_search_input = QLineEdit()
    main_app.link_search_input.setPlaceholderText("Search Links…")
    main_app.link_search_input.setVisible(False)
    log_title_h.addWidget(main_app.link_search_input)

    main_app.link_search_button = QPushButton()
    main_app.link_search_button.setIcon(QIcon(get_asset_path("assets/Svg/search.svg")))
    main_app.link_search_button.setIconSize(QSize(int(14 * scale), int(14 * scale)))
    main_app.link_search_button.setVisible(False)
    main_app.link_search_button.setFixedWidth(int(28 * scale))
    log_title_h.addWidget(main_app.link_search_button)

    discord_h = QHBoxLayout()
    discord_h.setSpacing(int(4 * scale))
    main_app.discord_scope_toggle_button = QPushButton("Scope: Files")
    main_app.discord_scope_toggle_button.setVisible(False)
    discord_h.addWidget(main_app.discord_scope_toggle_button)
    main_app.discord_message_limit_input = QLineEdit(main_app)
    main_app.discord_message_limit_input.setPlaceholderText("Msg Limit")
    main_app.discord_message_limit_input.setToolTip(
        "Optional: Limit the number of recent messages to process."
    )
    main_app.discord_message_limit_input.setValidator(QIntValidator(1, 9999999, main_app))
    main_app.discord_message_limit_input.setFixedWidth(int(75 * scale))
    main_app.discord_message_limit_input.setVisible(False)
    discord_h.addWidget(main_app.discord_message_limit_input)
    log_title_h.addLayout(discord_h)

    main_app.manga_rename_toggle_button = QPushButton()
    main_app.manga_rename_toggle_button.setVisible(False)
    main_app.manga_rename_toggle_button.setFixedWidth(int(130 * scale))
    main_app._update_manga_filename_style_button_text()
    log_title_h.addWidget(main_app.manga_rename_toggle_button)

    main_app.custom_rename_dialog_button = QPushButton("Open Dialog")
    main_app.custom_rename_dialog_button.setVisible(False)
    main_app.custom_rename_dialog_button.clicked.connect(main_app._show_custom_rename_dialog)
    log_title_h.addWidget(main_app.custom_rename_dialog_button)

    main_app.manga_date_prefix_input = QLineEdit()
    main_app.manga_date_prefix_input.setPlaceholderText("Prefix for Manga Filenames")
    main_app.manga_date_prefix_input.setVisible(False)
    log_title_h.addWidget(main_app.manga_date_prefix_input)

    main_app.multipart_toggle_button = QPushButton()
    main_app.multipart_toggle_button.setToolTip(
        "Toggle between Multi-part and Single-stream downloads for large files."
    )
    main_app.multipart_toggle_button.setFixedWidth(int(120 * scale))
    main_app._update_multipart_toggle_button_text()
    log_title_h.addWidget(main_app.multipart_toggle_button)

    main_app.export_all_links_btn = QPushButton("  Export All Links")
    main_app.export_all_links_btn.setIcon(QIcon(get_asset_path("assets/Svg/export.svg")))
    main_app.export_all_links_btn.setIconSize(QSize(int(14 * scale), int(14 * scale)))
    main_app.export_all_links_btn.setToolTip(
        "Fetch all post file URLs from this creator/URL without downloading them.\n"
        "Exports a list of direct download links to a .txt file for use in IDM or other download managers."
    )
    main_app.export_all_links_btn.setEnabled(True)
    log_title_h.addWidget(main_app.export_all_links_btn)

    main_app.EYE_ICON = ""
    main_app.CLOSED_EYE_ICON = ""
    main_app.EYE_ICON_PATH = get_asset_path("assets/Svg/eye_open.svg")
    main_app.CLOSED_EYE_ICON_PATH = get_asset_path("assets/Svg/eye_closed.svg")

    main_app.log_verbosity_toggle_button = QPushButton()
    main_app.log_verbosity_toggle_button.setIcon(QIcon(main_app.EYE_ICON_PATH))
    main_app.log_verbosity_toggle_button.setIconSize(QSize(int(15 * scale), int(15 * scale)))
    main_app.log_verbosity_toggle_button.setFixedWidth(int(34 * scale))
    log_title_h.addWidget(main_app.log_verbosity_toggle_button)

    main_app.reset_button = QPushButton("🔄 Reset")
    main_app.reset_button.setFixedWidth(int(72 * scale))
    log_title_h.addWidget(main_app.reset_button)

    right_layout.addLayout(log_title_h)

    # Log text areas
    main_app.log_splitter = QSplitter(Qt.Orientation.Vertical)
    main_app.log_view_stack = QStackedWidget()

    main_app.main_log_output = QTextEdit()
    main_app.main_log_output.setReadOnly(True)
    main_app.main_log_output.setLineWrapMode(QTextEdit.NoWrap)
    main_app.log_view_stack.addWidget(main_app.main_log_output)

    main_app.missed_character_log_output = QTextEdit()
    main_app.missed_character_log_output.setReadOnly(True)
    main_app.missed_character_log_output.setLineWrapMode(QTextEdit.NoWrap)
    main_app.log_view_stack.addWidget(main_app.missed_character_log_output)

    main_app.external_log_output = QTextEdit()
    main_app.external_log_output.setReadOnly(True)
    main_app.external_log_output.setLineWrapMode(QTextEdit.NoWrap)
    main_app.external_log_output.hide()

    main_app.log_splitter.addWidget(main_app.log_view_stack)
    main_app.log_splitter.addWidget(main_app.external_log_output)
    main_app.log_splitter.setSizes([main_app.height(), 0])
    right_layout.addWidget(main_app.log_splitter, 1)

    # Export / display-mode row
    export_h = QHBoxLayout()
    export_h.addStretch(1)
    main_app.export_links_button = QPushButton(
        main_app._tr("export_links_button_text", "Export Links")
    )
    main_app.export_links_button.setFixedWidth(int(95 * scale))
    main_app.export_links_button.setEnabled(False)
    main_app.export_links_button.setVisible(False)
    export_h.addWidget(main_app.export_links_button)
    main_app.download_extracted_links_button = QPushButton(
        main_app._tr("download_extracted_links_button_text", "Download")
    )
    main_app.download_extracted_links_button.setFixedWidth(int(95 * scale))
    main_app.download_extracted_links_button.setEnabled(False)
    main_app.download_extracted_links_button.setVisible(False)
    export_h.addWidget(main_app.download_extracted_links_button)
    main_app.log_display_mode_toggle_button = QPushButton()
    main_app.log_display_mode_toggle_button.setFixedWidth(int(115 * scale))
    main_app.log_display_mode_toggle_button.setVisible(False)
    export_h.addWidget(main_app.log_display_mode_toggle_button)
    right_layout.addLayout(export_h)

    # Progress labels
    main_app.progress_label = QLabel("Progress: Idle")
    main_app.progress_label.setStyleSheet(
        "padding-top: 4px; font-style: italic; font-weight: normal;"
    )
    right_layout.addWidget(main_app.progress_label)

    main_app.file_progress_label = QLabel("")
    main_app.file_progress_label.setToolTip(
        "Shows the progress of individual file downloads, including speed and size."
    )
    main_app.file_progress_label.setWordWrap(True)
    main_app.file_progress_label.setStyleSheet(
        "padding-top: 2px; font-style: italic; color: #A0A0A0; font-weight: normal;"
    )
    right_layout.addWidget(main_app.file_progress_label)

    # ── Assembly ──────────────────────────────────────────────────────────────
    main_app.main_splitter.addWidget(left_scroll)
    main_app.main_splitter.addWidget(right_widget)
    main_app.main_splitter.setStretchFactor(0, 7)
    main_app.main_splitter.setStretchFactor(1, 3)

    # Content stack for tab navigation (Downloader vs Queue vs Creators vs Posts vs Auto-Sync vs Visual Sort)
    main_app.content_stack = QStackedWidget()
    main_app.content_stack.addWidget(main_app.main_splitter)  # Index 0: Downloader

    from ..ui.queue_tab import QueueTabWidget
    main_app.queue_tab = QueueTabWidget(main_app, scale)
    main_app.content_stack.addWidget(main_app.queue_tab)      # Index 1: Queue

    from ..core.favorites_manager import UserFavoritesManager
    base_dir = getattr(main_app, "app_base_dir", ".")
    main_app.favorites_manager = UserFavoritesManager(base_dir, parent=main_app)

    from ..ui.creators_tab import CreatorsTabWidget
    main_app.creators_tab = CreatorsTabWidget(main_app, scale)
    main_app.content_stack.addWidget(main_app.creators_tab)   # Index 2: Creators

    from ..ui.posts_tab import PostsTabWidget
    main_app.posts_tab = PostsTabWidget(main_app, scale)
    main_app.content_stack.addWidget(main_app.posts_tab)      # Index 3: Posts

    from ..ui.auto_sync_tab import AutoSyncTabWidget
    main_app.sync_tab = AutoSyncTabWidget(main_app, scale)
    main_app.content_stack.addWidget(main_app.sync_tab)       # Index 4: Auto-Sync
    main_app.active_sync_log_dialog = main_app.sync_tab

    from ..ui.visual_sort_tab import VisualSortTabWidget
    main_app.visual_sort_tab = VisualSortTabWidget(main_app, scale)
    main_app.content_stack.addWidget(main_app.visual_sort_tab) # Index 5: Visual Sort

    from ..ui.favorites_tab import FavoritesTabWidget
    main_app.favorites_tab = FavoritesTabWidget(main_app, scale)
    main_app.content_stack.addWidget(main_app.favorites_tab)   # Index 6: Favorites

    from ..ui.about_tab import AboutTabWidget
    main_app.about_tab = AboutTabWidget(main_app, scale)
    main_app.content_stack.addWidget(main_app.about_tab)       # Index 7: About

    from ..ui.support_tab import SupportTabWidget
    main_app.support_tab = SupportTabWidget(main_app, scale)
    main_app.content_stack.addWidget(main_app.support_tab)     # Index 8: Support

    def switch_tab(target):
        tab_names = {
            "dl": 0, "downloader": 0,
            "queue": 1,
            "creators": 2,
            "posts": 3,
            "sync": 4, "auto_sync": 4,
            "visual_sort": 5,
            "favorites": 6, "favorite": 6, "fav": 6,
            "about": 7, "help": 7, "guide": 7,
            "support": 8, "donate": 8, "heart": 8
        }
        if isinstance(target, str):
            index = tab_names.get(target.lower(), 0)
        else:
            index = target

        old_index = main_app.content_stack.currentIndex()
        if old_index != index:
            if old_index == 2 and hasattr(main_app, 'creators_tab'):
                main_app.creators_tab.on_tab_deactivated()
            elif old_index == 3 and hasattr(main_app, 'posts_tab'):
                main_app.posts_tab.on_tab_deactivated()

        main_app.content_stack.setCurrentIndex(index)
        nav_buttons = [
            (main_app.nav_dl, 0),
            (main_app.nav_queue, 1),
            (main_app.nav_creators, 2),
            (main_app.nav_posts, 3),
            (main_app.nav_sync, 4),
            (main_app.nav_visual_sort, 5),
            (main_app.nav_favorites, 6),
            (main_app.known_names_help_button, 7),
            (main_app.nav_support, 8),
        ]
        for btn, idx in nav_buttons:
            btn.setObjectName("NavButtonSelected" if index == idx else "NavButton")
            btn.setStyle(btn.style())

        if index == 1 and hasattr(main_app, 'queue_tab'):
            main_app.queue_tab.on_tab_activated()
        elif index == 2 and hasattr(main_app, 'creators_tab'):
            main_app.creators_tab.on_tab_activated()
        elif index == 3 and hasattr(main_app, 'posts_tab'):
            main_app.posts_tab.on_tab_activated()
        elif index == 4 and hasattr(main_app, 'sync_tab'):
            main_app.sync_tab.on_tab_activated()
        elif index == 5 and hasattr(main_app, 'visual_sort_tab'):
            main_app.visual_sort_tab.on_tab_activated()
        elif index == 6 and hasattr(main_app, 'favorites_tab'):
            main_app.favorites_tab.on_tab_activated()
        elif index == 7 and hasattr(main_app, 'about_tab'):
            if hasattr(main_app.about_tab, 'on_tab_activated'):
                main_app.about_tab.on_tab_activated()
        elif index == 8 and hasattr(main_app, 'support_tab'):
            if hasattr(main_app.support_tab, 'on_tab_activated'):
                main_app.support_tab.on_tab_activated()

    main_app.switch_tab = switch_tab
    main_app.nav_dl.clicked.connect(lambda: switch_tab(0))
    main_app.nav_queue.clicked.connect(lambda: switch_tab(1))
    main_app.nav_creators.clicked.connect(lambda: switch_tab(2))
    main_app.nav_posts.clicked.connect(lambda: switch_tab(3))
    main_app.nav_sync.clicked.connect(lambda: switch_tab(4))
    main_app.nav_visual_sort.clicked.connect(lambda: switch_tab(5))
    main_app.nav_favorites.clicked.connect(lambda: switch_tab(6))
    main_app.known_names_help_button.clicked.connect(lambda: switch_tab(7))
    main_app.nav_support.clicked.connect(lambda: switch_tab(8))

    top_level_layout = QHBoxLayout(main_app)
    top_level_layout.setContentsMargins(0, 0, 0, 0)
    top_level_layout.setSpacing(0)
    top_level_layout.addWidget(sidebar)
    top_level_layout.addWidget(main_app.content_stack, 1)

    # ── Post-build initialisations ────────────────────────────────────────────
    main_app.update_ui_for_subfolders(main_app.use_subfolders_checkbox.isChecked())
    main_app.update_external_links_setting(main_app.external_links_checkbox.isChecked())
    main_app.update_multithreading_label(main_app.thread_count_input.text())
    main_app.update_page_range_enabled_state()

    if main_app.manga_mode_checkbox:
        main_app.update_ui_for_manga_mode(main_app.manga_mode_checkbox.isChecked())

    if hasattr(main_app, 'link_input'):
        main_app.link_input.textChanged.connect(
            lambda: main_app.update_ui_for_manga_mode(
                main_app.manga_mode_checkbox.isChecked()
                if main_app.manga_mode_checkbox else False
            )
        )

    main_app.load_known_names_from_util()
    main_app._update_cookie_input_visibility(
        main_app.use_cookie_checkbox.isChecked()
        if hasattr(main_app, 'use_cookie_checkbox') else False
    )
    main_app._handle_multithreading_toggle(main_app.use_multithreading_checkbox.isChecked())

    if hasattr(main_app, 'radio_group') and main_app.radio_group.checkedButton():
        main_app._handle_filter_mode_change(main_app.radio_group.checkedButton(), True)
        main_app.radio_group.buttonToggled.connect(main_app._handle_more_options_toggled)

    if hasattr(main_app, 'checkbox_more'):
        main_app.checkbox_more.toggled.connect(main_app._handle_checkbox_more_toggled)
        main_app.checkbox_more.clicked.connect(main_app._handle_more_button_clicked)
        main_app.checkbox_more.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        main_app.checkbox_more.customContextMenuRequested.connect(
            main_app._show_more_options_context_menu
        )
        main_app.checkbox_more.installEventFilter(main_app)

    if hasattr(main_app, 'radio_more'):
        main_app.radio_more.clicked.connect(main_app._handle_more_button_clicked)
        main_app.radio_more.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        main_app.radio_more.customContextMenuRequested.connect(
            main_app._show_more_options_context_menu
        )

    main_app._update_manga_filename_style_button_text()
    main_app._update_skip_scope_button_text()
    main_app._update_char_filter_scope_button_text()
    main_app._update_multithreading_for_date_mode()

    if hasattr(main_app, 'download_thumbnails_checkbox'):
        main_app._handle_thumbnail_mode_change(
            main_app.download_thumbnails_checkbox.isChecked()
        )
    if hasattr(main_app, 'favorite_mode_checkbox') and main_app.favorite_mode_checkbox:
        main_app._handle_favorite_mode_toggle(False)


# ─────────────────────────────────────────────────────────────────────────────
#  Theme functions
# ─────────────────────────────────────────────────────────────────────────────

def get_dark_theme(scale=1):
    """Generates the dark-theme QSS stylesheet, scaled by *scale*."""
    fs       = int(9.5 * scale)       # base font size
    fs_sm    = int(8.0 * scale)       # small font size (nav, table headers)
    fs_field = round(8.0 * 1.25 * scale, 1)  # blue field heading font size (+25%)
    fs_sec   = round(8.5 * 1.25 * scale, 1)  # blue section heading font size (+25%)
    pad_le   = int(5  * scale)        # line-edit padding
    pad_bv  = int(5  * scale)        # button vertical padding
    pad_bh  = int(11 * scale)        # button horizontal padding
    pad_tt  = int(4  * scale)        # tooltip padding
    sb_w    = int(7  * scale)        # scrollbar thickness

    return f"""
    /* ── Base ─────────────────────────────────────────────────── */
    QWidget {{
        background-color: #272528;
        color: #DFE1E5;
        font-family: Segoe UI, Arial, sans-serif;
        font-size: {fs}pt;
    }}

    /* ── Inputs ────────────────────────────────────────────────── */
    QLineEdit, QListWidget, QTextEdit {{
        background-color: #363237;
        border: 1px solid #524B53;
        padding: {pad_le}px;
        color: #F0F0F0;
        border-radius: 4px;
        font-size: {fs}pt;
    }}
    QLineEdit:focus, QListWidget:focus, QTextEdit:focus {{
        border-color: #D48378;
    }}
    QLineEdit::placeholder {{ color: #827A81; }}
    QTextEdit {{ font-family: Consolas, Courier New, monospace; }}

    /* ── Buttons ────────────────────────────────────────────────── */
    QPushButton {{
        background-color: #423D43;
        color: #DCDFE2;
        border: 1px solid #5A525B;
        padding: {pad_bv}px {pad_bh}px;
        border-radius: 4px;
    }}
    QPushButton:hover  {{ background-color: #514B52; border-color: #6C626D; }}
    QPushButton:pressed {{ background-color: #383339; }}
    QPushButton:disabled {{ background-color: #342F35; color: #6E656D; border-color: #443E45; }}

    QPushButton#PrimaryButton {{
        background-color: #B55D52;
        border-color: #C96E63;
        color: #FFFFFF;
        font-weight: bold;
    }}
    QPushButton#PrimaryButton:hover  {{ background-color: #C76E62; border-color: #D67F74; }}
    QPushButton#PrimaryButton:pressed {{ background-color: #9E4940; }}
    QPushButton#PrimaryButton:disabled {{
        background-color: #4A3533; color: #7A605E; border-color: #4A3533;
    }}

    QPushButton#CollapseBtn {{
        background-color: transparent;
        border: 1px solid #4D454B;
        color: #E5A198;
        padding: 1px 2px;
        border-radius: 3px;
        font-size: {int(9 * scale)}pt;
        font-weight: bold;
    }}
    QPushButton#CollapseBtn:hover {{ background-color: #383034; }}

    /* ── Labels ─────────────────────────────────────────────────── */
    QLabel {{ font-weight: bold; color: #C5BCC0; }}
    QLabel#FieldLabel {{
        font-weight: bold;
        color: #E5A198;
        font-size: {fs_field}pt;
    }}
    QLabel#SectionLabel {{
        font-weight: bold;
        color: #F0B8B0;
        font-size: {fs_sec}pt;
    }}

    /* ── Sidebar ─────────────────────────────────────────────────── */
    QWidget#Sidebar {{
        background-color: #1D1B1D;
        border-right: 1px solid #363034;
    }}
    QLabel#BrandName {{
        font-size: {int(9 * scale)}pt;
        color: #E5A198;
        font-weight: bold;
        border: none;
        background: transparent;
        background-color: transparent;
        padding-top: {int(3 * scale)}px;
        padding-bottom: {int(2 * scale)}px;
    }}
    QPushButton#PawWebsiteButton {{
        background: transparent;
        background-color: transparent;
        border: none;
        outline: none;
        padding: 0px;
        margin: 0px;
    }}
    QPushButton#PawWebsiteButton:hover {{
        background: transparent;
        background-color: transparent;
        border: none;
        outline: none;
    }}
    QPushButton#PawWebsiteButton:pressed {{
        background: transparent;
        background-color: transparent;
        border: none;
        outline: none;
    }}
    QPushButton#PawWebsiteButton:focus {{
        border: none;
        outline: none;
    }}

    QToolButton#NavButton {{
        background-color: transparent;
        border: none;
        color: #7E7377;
        padding: {int(6 * scale)}px {int(2 * scale)}px;
        border-radius: 6px;
        font-size: {int(7.5 * scale)}pt;
        font-weight: normal;
    }}
    QToolButton#NavButton:hover {{
        background-color: #2E2628;
        color: #D5C6C8;
    }}
    QToolButton#NavButtonSelected {{
        background-color: #4C2727;
        border: none;
        color: #FFFFFF;
        padding: {int(6 * scale)}px {int(2 * scale)}px;
        border-radius: 6px;
        font-size: {int(7.5 * scale)}pt;
        font-weight: bold;
    }}
    QToolButton#NavButtonSelected:hover {{ background-color: #5E3030; }}

    /* ── Content area ────────────────────────────────────────────── */
    QWidget#ContentArea {{ background-color: #272528; }}
    QWidget#RightPanel  {{ background-color: #272528; }}

    /* ── Separators ──────────────────────────────────────────────── */
    QFrame#FlatSep {{
        background-color: #3A3439;
        border: none;
        max-height: 1px;
        margin-top: {int(2 * scale)}px;
        margin-bottom: {int(2 * scale)}px;
    }}

    /* ── Checkboxes / Radio ───────────────────────────────────────── */
    QRadioButton, QCheckBox {{
        spacing: {int(5 * scale)}px;
        color: #D2CBD0;
        font-weight: normal;
    }}
    QRadioButton::indicator, QCheckBox::indicator {{
        width: {int(13 * scale)}px;
        height: {int(13 * scale)}px;
    }}

    /* ── List & Tables ──────────────────────────────────────────── */
    QListWidget {{ alternate-background-color: #302C31; }}
    QListWidget::item:selected {{ background-color: #B55D52; color: #FFFFFF; }}
    QListWidget::item:hover {{ background-color: #3A3236; }}

    QListView#CreatorsGrid {{
        background-color: #222023;
        border: 1px solid #3B3439;
        border-radius: 6px;
        padding: {int(4 * scale)}px;
        outline: none;
    }}

    QTableView {{
        background-color: #2B282C;
        alternate-background-color: #322E33;
        color: #DFE1E5;
        gridline-color: #3D373D;
        border: 1px solid #484149;
        border-radius: 6px;
        selection-background-color: #B55D52;
        selection-color: #FFFFFF;
        font-size: {fs}pt;
    }}
    QTableView::item {{
        padding: {int(4 * scale)}px {int(6 * scale)}px;
    }}
    QTableView::item:selected {{
        background-color: #B55D52;
        color: #FFFFFF;
    }}
    QTableView::item:hover {{
        background-color: #3D3236;
    }}
    QHeaderView::section {{
        background-color: #232124;
        color: #E5A198;
        font-weight: bold;
        font-size: {fs_sm}pt;
        border: none;
        border-bottom: 1px solid #484149;
        border-right: 1px solid #3A3439;
        padding: {int(5 * scale)}px {int(8 * scale)}px;
    }}
    QComboBox {{
        background-color: #363237;
        border: 1px solid #524B53;
        border-radius: 4px;
        padding: {int(4 * scale)}px {int(8 * scale)}px;
        color: #F0F0F0;
        min-width: {int(90 * scale)}px;
    }}
    QComboBox:hover {{ border-color: #6C626D; }}
    QComboBox:focus {{ border-color: #D48378; }}
    QComboBox::drop-down {{
        border: none;
        width: {int(20 * scale)}px;
    }}
    QComboBox QAbstractItemView {{
        background-color: #2E2B30;
        border: 1px solid #524B53;
        selection-background-color: #B55D52;
        selection-color: #FFFFFF;
        color: #DFE1E5;
        padding: 4px;
    }}

    /* ── Progress Bar ────────────────────────────────────────────── */
    QProgressBar {{
        background-color: #2E2B30;
        border: 1px solid #474048;
        border-radius: 4px;
        text-align: center;
        color: #FFFFFF;
        font-size: {int(8 * scale)}pt;
        font-weight: bold;
    }}
    QProgressBar::chunk {{
        background-color: #B55D52;
        border-radius: 3px;
    }}

    /* ── Tooltip ─────────────────────────────────────────────────── */
    QToolTip {{
        background-color: #373238;
        color: #EAECEF;
        border: 1px solid #4F4750;
        padding: {pad_tt}px;
        border-radius: 3px;
        font-size: {fs}pt;
    }}

    /* ── Splitter ────────────────────────────────────────────────── */
    QSplitter::handle {{ background-color: #383238; }}
    QSplitter::handle:horizontal {{ width: {int(4 * scale)}px; }}
    QSplitter::handle:vertical   {{ height: {int(4 * scale)}px; }}

    /* ── Scrollbars ───────────────────────────────────────────────── */
    QScrollBar:vertical {{
        background: #201E21;
        width: {sb_w}px;
        border-radius: {int(3 * scale)}px;
    }}
    QScrollBar::handle:vertical {{
        background: #474048;
        border-radius: {int(3 * scale)}px;
        min-height: {int(20 * scale)}px;
    }}
    QScrollBar::handle:vertical:hover {{ background: #5A515B; }}
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {{ height: 0px; }}

    QScrollBar:horizontal {{
        background: #201E21;
        height: {sb_w}px;
        border-radius: {int(3 * scale)}px;
    }}
    QScrollBar::handle:horizontal {{
        background: #474048;
        border-radius: {int(3 * scale)}px;
        min-width: {int(20 * scale)}px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: #5A515B; }}
    QScrollBar::add-line:horizontal,
    QScrollBar::sub-line:horizontal {{ width: 0px; }}
    """


def apply_theme_to_app(main_app, theme_name, initial_load=False):
    """Applies the selected theme and scaling to the main application window."""
    main_app.current_theme = theme_name
    if not initial_load:
        main_app.settings.setValue(THEME_KEY, theme_name)
        main_app.settings.sync()

    if theme_name == "dark":
        scale = getattr(main_app, 'scale_factor', 1)
        main_app.setStyleSheet(get_dark_theme(scale))
        if not initial_load:
            main_app.log_signal.emit("🎨 Switched to Dark Mode.")
    else:
        main_app.setStyleSheet("")
        if not initial_load:
            main_app.log_signal.emit("🎨 Switched to Light Mode.")
    main_app.update()
