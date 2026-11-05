import os
import requests

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QMessageBox, QRadioButton, QButtonGroup,
    QGroupBox, QTabWidget, QSlider, QSpinBox, QScrollArea,
    QCheckBox, QLineEdit, QFrame, QApplication
)
from PySide6.QtGui import QIcon, QFont
from PySide6.QtCore import Qt, QThread, Signal, QSettings, QSize

from .assets import get_asset_path


# ─────────────────────────────────────────────────────────────────────────────
#  Background Worker Threads
# ─────────────────────────────────────────────────────────────────────────────

class UpdateCheckerThread(QThread):
    """Background thread to silently check if a new remote file exists via ETag/Last-Modified."""
    update_available_signal = Signal(bool, str)

    def __init__(self, url, current_etag):
        super().__init__()
        self.url = url
        self.current_etag = current_etag

    def run(self):
        try:
            response = requests.head(self.url, timeout=10, allow_redirects=True)
            remote_etag = response.headers.get('ETag', '').strip('"')
            if not remote_etag:
                remote_etag = response.headers.get('Last-Modified', '')

            if remote_etag and remote_etag != self.current_etag:
                self.update_available_signal.emit(True, remote_etag)
            else:
                self.update_available_signal.emit(False, remote_etag)
        except Exception:
            self.update_available_signal.emit(False, self.current_etag)


class DownloadWorker(QThread):
    """Downloads AI model, CSV tags, or Known.txt with live progress reporting."""
    status_signal = Signal(str)
    progress_signal = Signal(int)
    finished_signal = Signal(bool, str, bool)

    def __init__(self, files, models_dir, model_path, csv_path, is_full_download=True):
        super().__init__()
        self.files = files
        self.models_dir = models_dir
        self.model_path = model_path
        self.csv_path = csv_path
        self.is_full_download = is_full_download

    def run(self):
        try:
            os.makedirs(self.models_dir, exist_ok=True)

            for url, path, label in self.files:
                self.status_signal.emit(label)
                response = requests.get(url, stream=True, timeout=30)
                response.raise_for_status()
                total = int(response.headers.get('content-length', 0))

                downloaded = 0
                with open(path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if self.isInterruptionRequested():
                            return
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                self.progress_signal.emit(int(downloaded / total * 100))

            if self.is_full_download or (self.files and "fallback" in self.files[0][0]):
                self.status_signal.emit("Refreshing AI engine tags in memory...")
                if os.path.exists(self.model_path) and os.path.exists(self.csv_path):
                    try:
                        from src.core.visual_sorter import VisualSorter
                    except ImportError:
                        from ..core.visual_sorter import VisualSorter

                    VisualSorter._instance = None
                    VisualSorter.get_instance(self.model_path, self.csv_path)

            self.finished_signal.emit(True, "Success", self.is_full_download)
        except Exception as e:
            self.finished_signal.emit(False, str(e), self.is_full_download)


# ─────────────────────────────────────────────────────────────────────────────
#  Main Visual Sort Tab Widget
# ─────────────────────────────────────────────────────────────────────────────

class VisualSortTabWidget(QWidget):
    """
    Dedicated full-app tab for Visual Sort (AI).
    Permanently manages AI model installation, character confidence thresholds,
    Known.txt series expansions, and master toggle state for all downloads.
    """

    def __init__(self, parent_app, scale: float = 1.0):
        super().__init__(parent_app)
        self.parent_app = parent_app
        self.scale = scale

        self.settings = QSettings("MediaDownloader", "VisualSort")

        self.models_dir = os.path.abspath(os.path.join("appdata", "models"))
        self.model_path = os.path.join(self.models_dir, "model.onnx")
        self.csv_path = os.path.join(self.models_dir, "selected_tags.csv")
        self.fallback_path = os.path.join(self.models_dir, "fallback.csv")
        self.fallback_url = "https://huggingface.co/datasets/Yuvi9587/Database/resolve/main/models/fallback.csv"

        self.known_txt_url = "https://huggingface.co/datasets/Yuvi9587/Database/resolve/main/Known.txt"
        self.worker = None
        self.fallback_checker = None
        self.known_checker = None
        self.latest_fallback_etag = self.settings.value("fallback_etag", "", type=str)
        self.latest_known_etag = self.settings.value("known_etag", "", type=str)

        self.model_urls = {
            0: {
                "model": "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/resolve/main/model.onnx",
                "csv": "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/resolve/main/selected_tags.csv"
            },
            1: {
                "model": "https://huggingface.co/SmilingWolf/wd-swinv2-tagger-v3/resolve/main/model.onnx",
                "csv": "https://huggingface.co/SmilingWolf/wd-swinv2-tagger-v3/resolve/main/selected_tags.csv"
            },
            2: {
                "model": "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/resolve/main/model.onnx",
                "csv": "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/resolve/main/selected_tags.csv"
            }
        }

        self.series_checkboxes = []

        self._build_ui()
        self._load_all_settings()

        # Check for remote updates in the background
        self._check_for_fallback_updates()
        self._check_for_known_updates()

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

        # ── 1. Top Header Bar ─────────────────────────────────────────────────
        header_bar = QHBoxLayout()
        header_bar.setSpacing(int(10 * scale))

        title_col = QVBoxLayout()
        title_col.setSpacing(int(2 * scale))

        title_h = QHBoxLayout()
        title_h.setSpacing(int(8 * scale))

        title_icon = QLabel()
        title_icon.setPixmap(
            QIcon(get_asset_path("assets/Svg/visual_sort.svg")).pixmap(int(22 * scale), int(22 * scale))
        )
        title_lbl = QLabel("Visual Sort (AI)")
        title_lbl.setStyleSheet(
            f"font-size: {int(13 * scale)}pt; font-weight: bold; color: #9BBCE8;"
        )
        title_h.addWidget(title_icon)
        title_h.addWidget(title_lbl)
        title_h.addStretch(1)
        title_col.addLayout(title_h)

        subtitle_lbl = QLabel(
            "Automatically classify and organize downloaded images into character folders using AI vision."
        )
        subtitle_lbl.setStyleSheet("font-size: 8.5pt; color: #8A8D93; font-weight: normal;")
        title_col.addWidget(subtitle_lbl)
        header_bar.addLayout(title_col, 1)

        # Status badge on the right
        self.status_badge = QLabel("Visual Sort Inactive")
        self.status_badge.setStyleSheet(
            "font-size: 8pt; font-weight: bold; color: #9A9EAA; background-color: #2D3035; "
            "border: 1px solid #424652; border-radius: 4px; padding: 4px 8px;"
        )
        header_bar.addWidget(self.status_badge)

        root_layout.addLayout(header_bar)

        # ── 2. Master Activation Card ─────────────────────────────────────────
        master_card = QFrame()
        master_card.setStyleSheet(
            "background-color: #2D3036; border: 1px solid #3E434F; border-radius: 6px;"
        )
        mc_layout = QHBoxLayout(master_card)
        mc_layout.setContentsMargins(
            int(14 * scale), int(10 * scale), int(14 * scale), int(10 * scale)
        )
        mc_layout.setSpacing(int(12 * scale))

        toggle_col = QVBoxLayout()
        toggle_col.setSpacing(int(2 * scale))

        self.master_enable_cb = QCheckBox("Enable Visual Sort for Downloads")
        self.master_enable_cb.setStyleSheet(
            f"font-size: {int(10.5 * scale)}pt; font-weight: bold; color: #E2E8F0;"
        )
        self.master_enable_cb.toggled.connect(self._on_master_toggled)
        toggle_col.addWidget(self.master_enable_cb)

        self.master_desc_lbl = QLabel(
            "When enabled, downloaded media will be analyzed with the selected AI model and sorted into character folders."
        )
        self.master_desc_lbl.setStyleSheet("font-size: 8.5pt; color: #94A3B8; font-weight: normal;")
        self.master_desc_lbl.setWordWrap(True)
        toggle_col.addWidget(self.master_desc_lbl)
        mc_layout.addLayout(toggle_col, 1)

        self.model_status_badge = QLabel("Checking model...")
        self.model_status_badge.setStyleSheet(
            "font-size: 8pt; font-weight: 500; color: #7EB6E0; background-color: #1E232B; "
            "border: 1px solid #323A48; border-radius: 4px; padding: 5px 10px;"
        )
        mc_layout.addWidget(self.model_status_badge)

        root_layout.addWidget(master_card)

        # ── 3. Sub-Tabs ───────────────────────────────────────────────────────
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

        self.model_tab_view = QWidget()
        self.thresholds_tab_view = QWidget()
        self.known_tab_view = QWidget()
        self.guide_tab_view = QWidget()

        self.tab_widget.addTab(self.model_tab_view, "AI Models & Fallbacks")
        self.tab_widget.addTab(self.thresholds_tab_view, "Sorting Thresholds")
        self.tab_widget.addTab(self.known_tab_view, "Known.txt & Expansions")
        self.tab_widget.addTab(self.guide_tab_view, "Pro Tips & How It Works")

        self._setup_model_tab()
        self._setup_thresholds_tab()
        self._setup_known_tab()
        self._setup_guide_tab()

        root_layout.addWidget(self.tab_widget, 1)

    # ─────────────────────────────────────────────────────────────────────────
    #  Sub-Tab 1: AI Model & Fallbacks
    # ─────────────────────────────────────────────────────────────────────────

    def _setup_model_tab(self):
        scale = self.scale
        layout = QVBoxLayout(self.model_tab_view)
        layout.setContentsMargins(
            int(14 * scale), int(12 * scale), int(14 * scale), int(12 * scale)
        )
        layout.setSpacing(int(10 * scale))

        # Model Selection Card
        self.group_box = QGroupBox("Select AI Model Architecture")
        self.group_box.setStyleSheet(
            "QGroupBox { font-weight: bold; color: #BAC0CB; border: 1px solid #3C3F46; "
            "border-radius: 5px; margin-top: 10px; padding-top: 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; }"
        )
        group_layout = QVBoxLayout(self.group_box)
        group_layout.setSpacing(int(8 * scale))
        group_layout.setContentsMargins(int(12 * scale), int(12 * scale), int(12 * scale), int(10 * scale))

        self.radio_group = QButtonGroup(self)
        self.rb_basic = QRadioButton("Basic - Fast (~379MB)")
        self.rb_basic.setToolTip("Recommended for quick sorting.\nDownloads faster and uses less system memory.")
        self.rb_balanced = QRadioButton("Balanced - Recommended (~440MB)")
        self.rb_balanced.setToolTip("Best middle-ground between speed and high accuracy.")
        self.rb_advanced = QRadioButton("Advanced - Best Accuracy (~1.26GB)")
        self.rb_advanced.setToolTip("Largest ViT model for maximum recognition quality.")

        self.radio_group.addButton(self.rb_basic, 0)
        self.radio_group.addButton(self.rb_balanced, 1)
        self.radio_group.addButton(self.rb_advanced, 2)

        installed_id = self.settings.value("installed_model", 1, type=int)
        button = self.radio_group.button(installed_id)
        if button:
            button.setChecked(True)
        else:
            self.rb_balanced.setChecked(True)

        group_layout.addWidget(self.rb_basic)
        group_layout.addWidget(self.rb_balanced)
        group_layout.addWidget(self.rb_advanced)
        layout.addWidget(self.group_box)

        # Fallback Tags Group
        fb_group = QGroupBox("Fallback Tags Database")
        fb_group.setStyleSheet(
            "QGroupBox { font-weight: bold; color: #BAC0CB; border: 1px solid #3C3F46; "
            "border-radius: 5px; margin-top: 6px; padding-top: 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; }"
        )
        fb_layout = QHBoxLayout(fb_group)
        fb_layout.setContentsMargins(int(12 * scale), int(10 * scale), int(12 * scale), int(10 * scale))

        self.fallback_status_label = QLabel("Checking for updates...")
        self.fallback_status_label.setStyleSheet("color: #8C929E; font-size: 8.5pt;")
        fb_layout.addWidget(self.fallback_status_label)

        fb_layout.addStretch(1)

        self.update_fallback_btn = QPushButton(" Update Tags Only")
        self.update_fallback_btn.setIcon(QIcon(get_asset_path("assets/Svg/sync.svg")))
        self.update_fallback_btn.setEnabled(False)
        self.update_fallback_btn.clicked.connect(self._start_fallback_update)
        fb_layout.addWidget(self.update_fallback_btn)
        layout.addWidget(fb_group)

        # Download / Action Controls
        self.status_label = QLabel("Ready.")
        self.status_label.setStyleSheet("color: #7EB6E0; font-size: 8.5pt;")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(int(16 * scale))
        self.progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #3E424B; border-radius: 4px; background: #232528; text-align: center; color: #BAC0CB; font-size: 7.5pt; } "
            "QProgressBar::chunk { background-color: #2196F3; border-radius: 3px; }"
        )
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        layout.addStretch(1)

        dl_row = QHBoxLayout()
        self.download_btn = QPushButton(" Download Selected Model")
        self.download_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.download_btn.setObjectName("PrimaryButton")
        self.download_btn.setMinimumHeight(int(36 * scale))
        self.download_btn.clicked.connect(self._start_model_download)
        dl_row.addWidget(self.download_btn)

        layout.addLayout(dl_row)
        self._update_installed_status()

    # ─────────────────────────────────────────────────────────────────────────
    #  Sub-Tab 2: Sorting Thresholds
    # ─────────────────────────────────────────────────────────────────────────

    def _setup_thresholds_tab(self):
        scale = self.scale
        layout = QVBoxLayout(self.thresholds_tab_view)
        layout.setContentsMargins(
            int(14 * scale), int(12 * scale), int(14 * scale), int(12 * scale)
        )
        layout.setSpacing(int(12 * scale))

        settings_group = QGroupBox("Recognition & Confidence Thresholds")
        settings_group.setStyleSheet(
            "QGroupBox { font-weight: bold; color: #BAC0CB; border: 1px solid #3C3F46; "
            "border-radius: 5px; margin-top: 10px; padding-top: 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; }"
        )
        settings_layout = QVBoxLayout(settings_group)
        settings_layout.setSpacing(int(12 * scale))
        settings_layout.setContentsMargins(int(14 * scale), int(14 * scale), int(14 * scale), int(14 * scale))

        # 1. Primary Character Threshold
        t1_lbl = QLabel("Primary Character Detection Threshold (%)")
        t1_lbl.setStyleSheet("font-weight: bold; color: #E2E8F0;")
        settings_layout.addWidget(t1_lbl)

        s1_row = QHBoxLayout()
        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setRange(1, 100)
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(1, 100)
        self.threshold_spin.setSuffix("%")
        self.threshold_spin.setMinimumWidth(int(70 * scale))

        saved_threshold = self.settings.value("char_threshold", 50, type=int)
        self.threshold_slider.setValue(saved_threshold)
        self.threshold_spin.setValue(saved_threshold)

        self.threshold_slider.valueChanged.connect(self.threshold_spin.setValue)
        self.threshold_spin.valueChanged.connect(self.threshold_slider.setValue)
        self.threshold_slider.valueChanged.connect(self._auto_save_thresholds)
        self.threshold_spin.valueChanged.connect(self._auto_save_thresholds)

        s1_row.addWidget(self.threshold_slider, 1)
        s1_row.addWidget(self.threshold_spin)
        settings_layout.addLayout(s1_row)

        desc1 = QLabel("Minimum AI confidence required to sort based on the Character's name directly. (Recommended: 50%)")
        desc1.setStyleSheet("color: #8C929E; font-size: 8.5pt;")
        desc1.setWordWrap(True)
        settings_layout.addWidget(desc1)

        # 2. Fallback Required Tag Matches
        t2_lbl = QLabel("Fallback: Required Tag Matches (1-5 tags)")
        t2_lbl.setStyleSheet("font-weight: bold; color: #E2E8F0; margin-top: 6px;")
        settings_layout.addWidget(t2_lbl)

        s2_row = QHBoxLayout()
        self.fallback_slider = QSlider(Qt.Orientation.Horizontal)
        self.fallback_slider.setRange(1, 5)
        self.fallback_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.fallback_slider.setTickInterval(1)
        self.fallback_spin = QSpinBox()
        self.fallback_spin.setRange(1, 5)
        self.fallback_spin.setSuffix(" tags")
        self.fallback_spin.setMinimumWidth(int(70 * scale))

        saved_fallback_matches = self.settings.value("fallback_tag_matches", 3, type=int)
        self.fallback_slider.setValue(saved_fallback_matches)
        self.fallback_spin.setValue(saved_fallback_matches)

        self.fallback_slider.valueChanged.connect(self.fallback_spin.setValue)
        self.fallback_spin.valueChanged.connect(self.fallback_slider.setValue)
        self.fallback_slider.valueChanged.connect(self._auto_save_thresholds)
        self.fallback_spin.valueChanged.connect(self._auto_save_thresholds)

        s2_row.addWidget(self.fallback_slider, 1)
        s2_row.addWidget(self.fallback_spin)
        settings_layout.addLayout(s2_row)

        desc2 = QLabel("How many visual tags from fallback.csv must match the image to identify the character. (Recommended: 3 or 4)")
        desc2.setStyleSheet("color: #8C929E; font-size: 8.5pt;")
        desc2.setWordWrap(True)
        settings_layout.addWidget(desc2)

        # 3. Fallback Tag Confidence Threshold
        t3_lbl = QLabel("Fallback: Tag Confidence Threshold (%)")
        t3_lbl.setStyleSheet("font-weight: bold; color: #E2E8F0; margin-top: 6px;")
        settings_layout.addWidget(t3_lbl)

        s3_row = QHBoxLayout()
        self.fb_thresh_slider = QSlider(Qt.Orientation.Horizontal)
        self.fb_thresh_slider.setRange(1, 100)
        self.fb_thresh_spin = QSpinBox()
        self.fb_thresh_spin.setRange(1, 100)
        self.fb_thresh_spin.setSuffix("%")
        self.fb_thresh_spin.setMinimumWidth(int(70 * scale))

        saved_fb_thresh = self.settings.value("fallback_threshold", 30, type=int)
        self.fb_thresh_slider.setValue(saved_fb_thresh)
        self.fb_thresh_spin.setValue(saved_fb_thresh)

        self.fb_thresh_slider.valueChanged.connect(self.fb_thresh_spin.setValue)
        self.fb_thresh_spin.valueChanged.connect(self.fb_thresh_slider.setValue)
        self.fb_thresh_slider.valueChanged.connect(self._auto_save_thresholds)
        self.fb_thresh_spin.valueChanged.connect(self._auto_save_thresholds)

        s3_row.addWidget(self.fb_thresh_slider, 1)
        s3_row.addWidget(self.fb_thresh_spin)
        settings_layout.addLayout(s3_row)

        desc3 = QLabel("How confident the AI needs to be for an individual trait tag (e.g. 'glasses', 'blue hair') to count. (Recommended: 20% - 35%)")
        desc3.setStyleSheet("color: #8C929E; font-size: 8.5pt;")
        desc3.setWordWrap(True)
        settings_layout.addWidget(desc3)

        layout.addWidget(settings_group)

        self.threshold_saved_lbl = QLabel("Settings saved automatically.")
        self.threshold_saved_lbl.setStyleSheet("color: #48BB78; font-size: 8pt; font-style: italic;")
        layout.addWidget(self.threshold_saved_lbl)

        layout.addStretch(1)

    def _auto_save_thresholds(self):
        self.settings.setValue("char_threshold", self.threshold_spin.value())
        self.settings.setValue("fallback_tag_matches", self.fallback_spin.value())
        self.settings.setValue("fallback_threshold", self.fb_thresh_spin.value())
        self.settings.sync()
        self.threshold_saved_lbl.setText("Settings saved automatically.")

    # ─────────────────────────────────────────────────────────────────────────
    #  Sub-Tab 3: Known.txt & Series Expansions
    # ─────────────────────────────────────────────────────────────────────────

    def _setup_known_tab(self):
        scale = self.scale
        layout = QVBoxLayout(self.known_tab_view)
        layout.setContentsMargins(
            int(14 * scale), int(12 * scale), int(14 * scale), int(12 * scale)
        )
        layout.setSpacing(int(10 * scale))

        # Master Known.txt Database Group
        master_group = QGroupBox("Master Known.txt Database")
        master_group.setStyleSheet(
            "QGroupBox { font-weight: bold; color: #BAC0CB; border: 1px solid #3C3F46; "
            "border-radius: 5px; margin-top: 10px; padding-top: 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; }"
        )
        master_layout = QVBoxLayout(master_group)
        master_layout.setSpacing(int(6 * scale))
        master_layout.setContentsMargins(int(12 * scale), int(10 * scale), int(12 * scale), int(10 * scale))

        master_desc = QLabel("Your core master list of character names, franchises, and alias groups.")
        master_desc.setStyleSheet("color: #8C929E; font-size: 8.5pt;")
        master_layout.addWidget(master_desc)

        self.known_status_label = QLabel("Checking for updates...")
        self.known_status_label.setStyleSheet("color: #8C929E; font-size: 8.5pt;")
        master_layout.addWidget(self.known_status_label)

        self.update_master_btn = QPushButton(" Download / Update Master List")
        self.update_master_btn.setIcon(QIcon(get_asset_path("assets/Svg/download.svg")))
        self.update_master_btn.clicked.connect(self._start_known_txt_download)
        master_layout.addWidget(self.update_master_btn)

        layout.addWidget(master_group)

        # Series Expansion Packs
        series_group = QGroupBox("Series Expansion Packs")
        series_group.setStyleSheet(
            "QGroupBox { font-weight: bold; color: #BAC0CB; border: 1px solid #3C3F46; "
            "border-radius: 5px; margin-top: 6px; padding-top: 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 0 5px; }"
        )
        series_layout = QVBoxLayout(series_group)
        series_layout.setSpacing(int(8 * scale))
        series_layout.setContentsMargins(int(12 * scale), int(10 * scale), int(12 * scale), int(10 * scale))

        series_desc = QLabel("Select which series to include in the AI character search. Unchecked series will be ignored.")
        series_desc.setStyleSheet("color: #8C929E; font-size: 8.5pt;")
        series_layout.addWidget(series_desc)

        filter_row = QHBoxLayout()
        self.series_search_input = QLineEdit()
        self.series_search_input.setPlaceholderText("Search expansion series...")
        self.series_search_input.setClearButtonEnabled(True)
        self.series_search_input.textChanged.connect(self._filter_series_checkboxes)
        filter_row.addWidget(self.series_search_input, 1)

        sel_all_btn = QPushButton("Select All")
        sel_all_btn.clicked.connect(lambda: self._set_all_series(True))
        filter_row.addWidget(sel_all_btn)

        clear_all_btn = QPushButton("Clear All")
        clear_all_btn.clicked.connect(lambda: self._set_all_series(False))
        filter_row.addWidget(clear_all_btn)
        series_layout.addLayout(filter_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            "QScrollArea { border: 1px solid #3C3F46; background-color: #202226; border-radius: 4px; }"
        )

        scroll_widget = QWidget()
        self.series_checkbox_layout = QVBoxLayout(scroll_widget)
        self.series_checkbox_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._populate_series_checkboxes()
        scroll.setWidget(scroll_widget)
        series_layout.addWidget(scroll, 1)

        layout.addWidget(series_group, 1)

    def _populate_series_checkboxes(self):
        known_path = self.parent_app.config_file if (self.parent_app and hasattr(self.parent_app, 'config_file')) else os.path.abspath(os.path.join("appdata", "Known.txt"))

        categories = []
        if os.path.exists(known_path):
            try:
                with open(known_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("# ---") and line.endswith("---"):
                            cat_name = line.replace("# ---", "").replace("---", "").strip()
                            if cat_name and cat_name not in categories:
                                categories.append(cat_name)
            except Exception:
                pass

        # Clear existing
        while self.series_checkbox_layout.count():
            item = self.series_checkbox_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.series_checkboxes.clear()

        if not categories:
            no_files_label = QLabel("No '# --- Series Name ---' headers found in Known.txt.")
            no_files_label.setStyleSheet("color: #8C929E; font-style: italic;")
            self.series_checkbox_layout.addWidget(no_files_label)
            return

        active_cats = getattr(self.parent_app, 'active_known_categories', [])
        if active_cats is None:
            active_cats = []

        for cat in categories:
            cb = QCheckBox(cat)
            cb.setStyleSheet("QCheckBox { color: #D4D7DE; padding: 3px 0; }")
            cb.setChecked(cat in active_cats)
            cb.toggled.connect(self._save_selected_categories)
            self.series_checkbox_layout.addWidget(cb)
            self.series_checkboxes.append(cb)

    def _filter_series_checkboxes(self, text: str):
        search_query = text.lower().strip()
        for cb in self.series_checkboxes:
            cb.setVisible(not search_query or search_query in cb.text().lower())

    def _set_all_series(self, checked: bool):
        for cb in self.series_checkboxes:
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)
        self._save_selected_categories()

    def _save_selected_categories(self):
        selected = [cb.text() for cb in self.series_checkboxes if cb.isChecked()]
        if self.parent_app:
            self.parent_app.active_known_categories = selected
            if hasattr(self.parent_app, 'load_known_names_from_util'):
                self.parent_app.load_known_names_from_util()
        self.settings.setValue("active_categories", selected)
        self.settings.sync()

    # ─────────────────────────────────────────────────────────────────────────
    #  Sub-Tab 4: Pro Tips & Guide
    # ─────────────────────────────────────────────────────────────────────────

    def _setup_guide_tab(self):
        scale = self.scale
        layout = QVBoxLayout(self.guide_tab_view)
        layout.setContentsMargins(
            int(14 * scale), int(12 * scale), int(14 * scale), int(12 * scale)
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        guide_card = QWidget()
        guide_layout = QVBoxLayout(guide_card)
        guide_layout.setSpacing(int(10 * scale))

        guide_html = QLabel("""
        <h3 style='color: #60A5FA; margin-top: 0;'>What is Visual Sort?</h3>
        <p style='color: #D1D5DB; font-size: 9.5pt; line-height: 140%;'>
        Visual Sort uses Computer Vision (WD Tagger AI) to analyze downloaded images, identify character faces,
        and place files into organized character subfolders.
        </p>

        <hr style='border: none; border-top: 1px solid #3E424B;'>

        <h3 style='color: #48BB78;'>The Golden Rule for Maximum Accuracy</h3>
        <p style='color: #D1D5DB; font-size: 9.5pt; line-height: 140%;'>
        Always use <b>Visual Sort</b> in combination with <b>"Separate folder by known.txt"</b>.<br>
        The AI works drastically faster and with near 100% accuracy when it checks against Known.txt aliases before analyzing unclassified images.
        </p>

        <hr style='border: none; border-top: 1px solid #3E424B;'>

        <h3 style='color: #60A5FA;'>How the 2-Stage Recognition Works</h3>
        <p style='color: #D1D5DB; font-size: 9.5pt; line-height: 140%;'>
        <b>Stage 1: Primary Character Recognition</b><br>
        The AI looks at the character's facial features and directly scores the character name. If its confidence exceeds the primary threshold (e.g. 50%), the file is sorted immediately.
        </p>
        <p style='color: #D1D5DB; font-size: 9.5pt; line-height: 140%;'>
        <b>Stage 2: Fallback Tag Verification</b><br>
        If the character's face is obstructed, stylized, or ambiguous, the <b>Fallback Tag Engine</b> scans general physical traits (such as "twin tails", "glasses", "blue hair"). When enough traits match the database profile, the image is successfully classified.
        </p>
        <p style='color: #E2E8F0; font-size: 9pt; font-style: italic;'>
        Note: If an image fails both stages, it is placed into an "Unknown" folder to prevent misfiling.
        </p>
        """)
        guide_html.setWordWrap(True)
        guide_layout.addWidget(guide_html)
        guide_layout.addStretch(1)

        scroll.setWidget(guide_card)
        layout.addWidget(scroll, 1)

    # ─────────────────────────────────────────────────────────────────────────
    #  Model & Download Management
    # ─────────────────────────────────────────────────────────────────────────

    def _update_installed_status(self):
        text_basic = "Basic - Fast (~379MB)"
        text_balanced = "Balanced - Recommended (~440MB)"
        text_advanced = "Advanced - Best Accuracy (~1.26GB)"

        self.rb_basic.setText(text_basic)
        self.rb_balanced.setText(text_balanced)
        self.rb_advanced.setText(text_advanced)

        is_installed = os.path.exists(self.model_path) and os.path.exists(self.csv_path)

        if is_installed:
            installed_id = self.settings.value("installed_model", -1, type=int)
            if installed_id == 0:
                self.rb_basic.setText(text_basic + "  [Installed & Active]")
            elif installed_id == 1:
                self.rb_balanced.setText(text_balanced + "  [Installed & Active]")
            elif installed_id == 2:
                self.rb_advanced.setText(text_advanced + "  [Installed & Active]")

            self.model_status_badge.setText("Model: Installed & Ready")
            self.model_status_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #48BB78; background-color: #1A3626; "
                "border: 1px solid #276749; border-radius: 4px; padding: 4px 8px;"
            )
        else:
            self.model_status_badge.setText("No Model Installed")
            self.model_status_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #E53E3E; background-color: #361A1A; "
                "border: 1px solid #742A2A; border-radius: 4px; padding: 4px 8px;"
            )

    def _start_model_download(self):
        selected_id = self.radio_group.checkedId()
        installed_id = self.settings.value("installed_model", -1, type=int)

        if selected_id == installed_id and os.path.exists(self.model_path):
            reply = QMessageBox.question(
                self, "Model Already Installed",
                "This AI model is already installed. Do you want to re-download and replace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.group_box.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.update_fallback_btn.setEnabled(False)
        self.progress_bar.show()
        self.progress_bar.setValue(0)

        try:
            if os.path.exists(self.model_path):
                os.remove(self.model_path)
            if os.path.exists(self.csv_path):
                os.remove(self.csv_path)
            if os.path.exists(self.fallback_path):
                os.remove(self.fallback_path)
        except OSError as e:
            print(f"Warning: Could not delete old model files: {e}")

        urls = self.model_urls[selected_id]

        files_to_download = [
            (urls["model"], self.model_path, "Downloading AI vision model..."),
            (urls["csv"], self.csv_path, "Downloading tags database..."),
            (self.fallback_url, self.fallback_path, "Downloading fallback tags...")
        ]

        self.worker = DownloadWorker(files_to_download, self.models_dir, self.model_path, self.csv_path, is_full_download=True)
        self.worker.status_signal.connect(self.status_label.setText)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.finished_signal.connect(self._on_download_finished)
        self.worker.start()

    def _start_fallback_update(self):
        self.group_box.setEnabled(False)
        self.download_btn.setEnabled(False)
        self.update_fallback_btn.setEnabled(False)
        self.progress_bar.show()
        self.progress_bar.setValue(0)

        files_to_download = [
            (self.fallback_url, self.fallback_path, "Updating Fallback Tags...")
        ]

        self.worker = DownloadWorker(files_to_download, self.models_dir, self.model_path, self.csv_path, is_full_download=False)
        self.worker.status_signal.connect(self.status_label.setText)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.finished_signal.connect(self._on_download_finished)
        self.worker.start()

    def _start_known_txt_download(self):
        target_path = self.parent_app.config_file if (self.parent_app and hasattr(self.parent_app, 'config_file')) else os.path.abspath(os.path.join("appdata", "Known.txt"))
        target_dir = os.path.dirname(target_path)

        if os.path.exists(target_path):
            reply = QMessageBox.question(
                self, "Overwrite Known.txt?",
                "A Known.txt file already exists in your app folder. Replace it with the master database?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.progress_bar.show()
        self.progress_bar.setValue(0)
        self.status_label.setText("Starting Known.txt download...")

        files_to_download = [(self.known_txt_url, target_path, "Downloading Master Known List...")]
        self.worker = DownloadWorker(files_to_download, target_dir, self.model_path, self.csv_path, is_full_download=False)
        self.worker.status_signal.connect(self.status_label.setText)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.finished_signal.connect(self._on_known_download_finished)
        self.worker.start()

    def _on_download_finished(self, success, error_msg, is_full_download):
        self.group_box.setEnabled(True)
        self.download_btn.setEnabled(True)
        self.progress_bar.hide()

        if success:
            self.settings.setValue("fallback_etag", self.latest_fallback_etag)
            self.fallback_status_label.setText("Status: Up to date")
            self.fallback_status_label.setStyleSheet("color: #48BB78;")

            if is_full_download:
                selected_id = self.radio_group.checkedId()
                self.settings.setValue("installed_model", selected_id)
                self._update_installed_status()
                self.status_label.setText("Model installed successfully.")
                QMessageBox.information(self, "Download Complete", "The AI model has been installed and is ready to use.")
            else:
                self.status_label.setText("Fallback tags updated.")
                QMessageBox.information(self, "Update Complete", "The custom Fallback Tags database has been updated.")
        else:
            QMessageBox.critical(self, "Download Error", f"Failed to download files:\n{error_msg}")
            self.progress_bar.setValue(0)
            self.status_label.setText("Download failed. Try again.")
            if not is_full_download:
                self.update_fallback_btn.setEnabled(True)

    def _on_known_download_finished(self, success, error_msg, is_full_download):
        self.progress_bar.hide()
        if success:
            self.settings.setValue("known_etag", self.latest_known_etag)
            self.known_status_label.setText("Status: Up to date")
            self.known_status_label.setStyleSheet("color: #48BB78;")
            self.status_label.setText("Known.txt updated successfully.")

            if self.parent_app and hasattr(self.parent_app, 'load_known_names_from_util'):
                self.parent_app.load_known_names_from_util()
            self._populate_series_checkboxes()
            QMessageBox.information(self, "Success", "Master Known.txt has been downloaded and installed.")
        else:
            QMessageBox.critical(self, "Download Error", f"Failed to update Known.txt:\n{error_msg}")

    # ─────────────────────────────────────────────────────────────────────────
    #  Remote Update Checks
    # ─────────────────────────────────────────────────────────────────────────

    def _check_for_fallback_updates(self):
        local_etag = self.latest_fallback_etag if os.path.exists(self.fallback_path) else ""
        self.fallback_checker = UpdateCheckerThread(self.fallback_url, local_etag)
        self.fallback_checker.update_available_signal.connect(self._on_fallback_check_result)
        self.fallback_checker.start()

    def _on_fallback_check_result(self, has_update, new_etag):
        self.latest_fallback_etag = new_etag
        if has_update:
            self.fallback_status_label.setText("Status: Update Available")
            self.fallback_status_label.setStyleSheet("color: #48BB78; font-weight: bold;")
            self.update_fallback_btn.setEnabled(True)
        else:
            self.fallback_status_label.setText("Status: Up to date")
            self.fallback_status_label.setStyleSheet("color: #8C929E;")
            self.update_fallback_btn.setEnabled(False)

    def _check_for_known_updates(self):
        known_path = self.parent_app.config_file if (self.parent_app and hasattr(self.parent_app, 'config_file')) else os.path.abspath(os.path.join("appdata", "Known.txt"))
        local_etag = self.latest_known_etag if os.path.exists(known_path) else ""
        self.known_checker = UpdateCheckerThread(self.known_txt_url, local_etag)
        self.known_checker.update_available_signal.connect(self._on_known_check_result)
        self.known_checker.start()

    def _on_known_check_result(self, has_update, new_etag):
        self.latest_known_etag = new_etag
        if has_update:
            self.known_status_label.setText("Status: Update Available")
            self.known_status_label.setStyleSheet("color: #48BB78; font-weight: bold;")
            self.update_master_btn.setText(" Update Available - Download Master List")
        else:
            self.known_status_label.setText("Status: Up to date")
            self.known_status_label.setStyleSheet("color: #8C929E;")
            self.update_master_btn.setText(" Download / Update Master List")

    # ─────────────────────────────────────────────────────────────────────────
    #  Master Toggle & State Sync
    # ─────────────────────────────────────────────────────────────────────────

    def _on_master_toggled(self, checked: bool):
        # Check if model is installed
        model_installed = os.path.exists(self.model_path) and os.path.exists(self.csv_path)
        if checked and not model_installed:
            self.model_status_badge.setText("Notice: Download Model Below")
            self.model_status_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #E53E3E; background-color: #361A1A; "
                "border: 1px solid #742A2A; border-radius: 4px; padding: 4px 8px;"
            )
            self.tab_widget.setCurrentIndex(0)
            if hasattr(self.parent_app, 'log_signal'):
                self.parent_app.log_signal.emit("[Visual Sort] Enabled. Note: Please download an AI model below.")

        # Persist permanently
        if hasattr(self.parent_app, 'settings'):
            self.parent_app.settings.setValue("visual_sort_active", checked)
            self.parent_app.settings.sync()
        self.settings.setValue("visual_sort_enabled", checked)
        self.settings.sync()

        # Update badge
        self._update_status_badge(checked)

        # Apply pipeline rules
        if hasattr(self.parent_app, '_enforce_visual_sort_pipeline_rules'):
            self.parent_app._enforce_visual_sort_pipeline_rules(True)

    def _update_status_badge(self, is_active: bool):
        if is_active:
            self.status_badge.setText("Visual Sort: Active")
            self.status_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #48BB78; background-color: #1A3626; "
                "border: 1px solid #276749; border-radius: 4px; padding: 4px 8px;"
            )
        else:
            self.status_badge.setText("Visual Sort: Inactive")
            self.status_badge.setStyleSheet(
                "font-size: 8pt; font-weight: bold; color: #9A9EAA; background-color: #2D3035; "
                "border: 1px solid #424652; border-radius: 4px; padding: 4px 8px;"
            )

    def _load_all_settings(self):
        # Load active state
        is_active = False
        if hasattr(self.parent_app, 'settings'):
            is_active = self.parent_app.settings.value("visual_sort_active", False, type=bool)
        if not is_active:
            is_active = self.settings.value("visual_sort_enabled", False, type=bool)

        self.master_enable_cb.blockSignals(True)
        self.master_enable_cb.setChecked(is_active)
        self.master_enable_cb.blockSignals(False)
        self._update_status_badge(is_active)
        self._update_installed_status()

    def on_tab_activated(self):
        """Called whenever user switches to the Visual Sort tab."""
        self._load_all_settings()
        self._update_installed_status()
        self._populate_series_checkboxes()
