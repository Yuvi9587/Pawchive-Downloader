import os
import sys
from PySide6.QtCore import Qt, QSize, QUrl
from PySide6.QtGui import QIcon, QPixmap, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QGridLayout, QApplication
)

from .assets import get_asset_path


class SupportCard(QFrame):
    """
    A clickable card widget displaying an icon, title, subtitle,
    and a call-to-action button that opens the external link.
    """
    def __init__(
        self, icon_path: str, title: str, subtitle: str, url: str,
        scale: float = 1.0, hover_border_color: str = "#60A5FA",
        parent=None
    ):
        super().__init__(parent)
        self.url = url
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(int(110 * scale))

        self.setStyleSheet(f"""
            SupportCard {{
                background-color: #212429;
                border: 1px solid #32363F;
                border-radius: 8px;
            }}
            SupportCard:hover {{
                background-color: #2A2E37;
                border: 1px solid {hover_border_color};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(int(14 * scale), int(14 * scale), int(14 * scale), int(14 * scale))
        layout.setSpacing(int(8 * scale))

        # Top row: Icon + Title
        top_row = QHBoxLayout()
        top_row.setSpacing(int(12 * scale))

        icon_lbl = QLabel()
        icon_dim = int(36 * scale)
        icon_lbl.setFixedSize(icon_dim, icon_dim)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet("background: transparent; border: none;")

        if icon_path.lower().endswith(".svg"):
            pixmap = QIcon(icon_path).pixmap(QSize(icon_dim, icon_dim))
        else:
            pix = QPixmap(icon_path)
            if not pix.isNull():
                pixmap = pix.scaled(
                    QSize(icon_dim, icon_dim),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
            else:
                pixmap = QPixmap()
        icon_lbl.setPixmap(pixmap)
        top_row.addWidget(icon_lbl)

        title_vbox = QVBoxLayout()
        title_vbox.setSpacing(int(2 * scale))

        title_lbl = QLabel(title)
        title_font_size = int(11 * scale)
        title_lbl.setStyleSheet(f"font-size: {title_font_size}pt; font-weight: 700; color: #FFFFFF; background: transparent; border: none;")
        title_vbox.addWidget(title_lbl)

        sub_lbl = QLabel(subtitle)
        sub_font_size = int(8.5 * scale)
        sub_lbl.setStyleSheet(f"font-size: {sub_font_size}pt; color: #9CA3AF; background: transparent; border: none;")
        sub_lbl.setWordWrap(True)
        title_vbox.addWidget(sub_lbl)

        top_row.addLayout(title_vbox, 1)
        layout.addLayout(top_row)

        layout.addStretch(1)

        # Open link button at bottom of card
        action_btn = QPushButton("Open Link")
        action_btn.setFixedHeight(int(28 * scale))
        action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        action_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #2D313A;
                border: 1px solid #3E434F;
                border-radius: 4px;
                color: #D1D5DB;
                font-size: {int(8.5 * scale)}pt;
                font-weight: 600;
                padding: 0 {int(10 * scale)}px;
            }}
            QPushButton:hover {{
                background-color: #383D48;
                border-color: {hover_border_color};
                color: #FFFFFF;
            }}
        """)
        action_btn.clicked.connect(self._open_url)
        layout.addWidget(action_btn)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._open_url()
        super().mousePressEvent(event)

    def _open_url(self):
        if self.url:
            QDesktopServices.openUrl(QUrl(self.url))


class SupportTabWidget(QWidget):
    """
    Dedicated tab for Pawchive Downloader community links, donation
    options, and support resources in a clean, modern card layout.
    """
    def __init__(self, main_app, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.main_app = main_app
        self.scale = scale
        self._init_ui()

    def on_tab_activated(self):
        """Called when this tab is selected in content_stack."""
        pass

    def _init_ui(self):
        scale = self.scale
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(int(16 * scale), int(14 * scale), int(16 * scale), int(14 * scale))
        root_layout.setSpacing(int(12 * scale))

        # ── Scroll Area wrapper so it behaves nicely on any resolution ───────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet("background-color: transparent; border: none;")

        scroll_widget = QWidget()
        scroll_widget.setStyleSheet("background-color: transparent;")
        content_layout = QVBoxLayout(scroll_widget)
        content_layout.setContentsMargins(0, 0, int(6 * scale), 0)
        content_layout.setSpacing(int(16 * scale))

        # ── Top Header Banner ────────────────────────────────────────────────
        header_frame = QFrame()
        header_frame.setObjectName("SupportHeaderFrame")
        header_frame.setStyleSheet("""
            QFrame#SupportHeaderFrame {
                background-color: #212429;
                border: 1px solid #32363F;
                border-radius: 8px;
            }
        """)
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(int(18 * scale), int(14 * scale), int(18 * scale), int(14 * scale))
        header_layout.setSpacing(int(14 * scale))

        # Heart SVG icon
        heart_lbl = QLabel()
        heart_dim = int(38 * scale)
        heart_pix = QIcon(get_asset_path("assets/Svg/heart.svg")).pixmap(QSize(heart_dim, heart_dim))
        heart_lbl.setPixmap(heart_pix)
        heart_lbl.setFixedSize(heart_dim, heart_dim)
        heart_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(heart_lbl)

        # Header Titles
        titles_vbox = QVBoxLayout()
        titles_vbox.setSpacing(int(2 * scale))

        header_title = QLabel("Support & Community")
        header_title.setStyleSheet(f"font-size: {int(14 * scale)}pt; font-weight: 700; color: #FFFFFF;")
        titles_vbox.addWidget(header_title)

        header_sub = QLabel("Pawchive Downloader is free and open-source. Here is how you can support the project, report bugs, or get involved!")
        header_sub.setStyleSheet(f"font-size: {int(9 * scale)}pt; color: #9CA3AF;")
        header_sub.setWordWrap(True)
        titles_vbox.addWidget(header_sub)

        header_layout.addLayout(titles_vbox, 1)
        content_layout.addWidget(header_frame)

        # ── Section 1: Financial Support ──────────────────────────────────────
        sec1_header = self._make_section_header("Contribute Financially", "assets/Svg/heart.svg", scale)
        content_layout.addWidget(sec1_header)

        sec1_desc = QLabel("If Pawchive Downloader has saved you time and you'd like to help keep the project maintained, you can support development through any of these platforms:")
        sec1_desc.setWordWrap(True)
        sec1_desc.setStyleSheet(f"font-size: {int(9 * scale)}pt; color: #C3C7D0; line-height: 1.5;")
        content_layout.addWidget(sec1_desc)

        financial_grid = QGridLayout()
        financial_grid.setSpacing(int(12 * scale))

        kofi_card = SupportCard(
            icon_path=get_asset_path("assets/Ko-fi.png"),
            title="Ko-fi",
            subtitle="Buy a coffee or send a one-time tip.",
            url="https://ko-fi.com/yuvi427183",
            scale=scale,
            hover_border_color="#F43F5E"
        )
        patreon_card = SupportCard(
            icon_path=get_asset_path("assets/patreon.png"),
            title="Patreon",
            subtitle="Become a monthly backer to support ongoing updates.",
            url="https://www.patreon.com/Yuvi102",
            scale=scale,
            hover_border_color="#F97316"
        )
        bmac_card = SupportCard(
            icon_path=get_asset_path("assets/buymeacoffee.png"),
            title="Buy Me a Coffee",
            subtitle="Quick, hassle-free one-time support.",
            url="https://buymeacoffee.com/yuvi9587",
            scale=scale,
            hover_border_color="#EAB308"
        )

        financial_grid.addWidget(kofi_card, 0, 0)
        financial_grid.addWidget(patreon_card, 0, 1)
        financial_grid.addWidget(bmac_card, 0, 2)
        content_layout.addLayout(financial_grid)

        # Subtle separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #2F333C; max-height: 1px;")
        content_layout.addWidget(sep)

        # ── Section 2: Community & Help ───────────────────────────────────────
        sec2_header = self._make_section_header("Get Help & Join the Community", "assets/Svg/user.svg", scale)
        content_layout.addWidget(sec2_header)

        sec2_desc = QLabel("Have questions, want to request features, or found an issue? Join our community or connect directly with the developer:")
        sec2_desc.setWordWrap(True)
        sec2_desc.setStyleSheet(f"font-size: {int(9 * scale)}pt; color: #C3C7D0; line-height: 1.5;")
        content_layout.addWidget(sec2_desc)

        community_grid = QGridLayout()
        community_grid.setSpacing(int(12 * scale))

        discord_card = SupportCard(
            icon_path=get_asset_path("assets/discord.png"),
            title="Discord Server",
            subtitle="Chat with fellow archivists, get help, and stay updated.",
            url="https://discord.gg/BqP64XTdJN",
            scale=scale,
            hover_border_color="#818CF8"
        )
        github_card = SupportCard(
            icon_path=get_asset_path("assets/github.png"),
            title="GitHub Repository",
            subtitle="Star the repo, report bugs, or inspect the source code.",
            url="https://github.com/Yuvi9587/Kemono-Downloader",
            scale=scale,
            hover_border_color="#60A5FA"
        )
        instagram_card = SupportCard(
            icon_path=get_asset_path("assets/instagram.png"),
            title="Instagram",
            subtitle="Follow the developer for creative updates and art.",
            url="https://www.instagram.com/uvi.arts/",
            scale=scale,
            hover_border_color="#EC4899"
        )
        pawchive_card = SupportCard(
            icon_path=get_asset_path("assets/Svg/paw-print.svg"),
            title="Pawchive.pw",
            subtitle="Visit the official Pawchive website.",
            url="https://pawchive.pw",
            scale=scale,
            hover_border_color="#38BDF8"
        )

        community_grid.addWidget(discord_card, 0, 0)
        community_grid.addWidget(github_card, 0, 1)
        community_grid.addWidget(instagram_card, 0, 2)
        community_grid.addWidget(pawchive_card, 0, 3)
        content_layout.addLayout(community_grid)

        # ── Thank You Callout Box ─────────────────────────────────────────────
        thank_you_box = QFrame()
        thank_you_box.setStyleSheet("""
            QFrame {
                background-color: #1A2234;
                border-left: 4px solid #3B82F6;
                border-radius: 6px;
                padding: 12px 16px;
            }
        """)
        ty_layout = QVBoxLayout(thank_you_box)
        ty_layout.setContentsMargins(0, 0, 0, 0)
        ty_layout.setSpacing(int(4 * scale))

        ty_title = QLabel("Thank you for using Pawchive Downloader!")
        ty_title.setStyleSheet(f"font-size: {int(10.5 * scale)}pt; font-weight: 700; color: #93C5FD; background: transparent; border: none;")
        ty_layout.addWidget(ty_title)

        ty_text = QLabel(
            "Whether you're downloading your favorite creator archives, reporting issues, or simply sharing "
            "the project with other archivists, your support makes a huge difference. Happy downloading!"
        )
        ty_text.setWordWrap(True)
        ty_text.setStyleSheet(f"font-size: {int(9 * scale)}pt; color: #E2E8F0; line-height: 1.5; background: transparent; border: none;")
        ty_layout.addWidget(ty_text)

        content_layout.addWidget(thank_you_box)
        content_layout.addStretch(1)

        scroll.setWidget(scroll_widget)
        root_layout.addWidget(scroll, 1)

    def _make_section_header(self, text: str, svg_icon_path: str, scale: float) -> QWidget:
        widget = QWidget()
        widget.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(int(8 * scale))

        icon_lbl = QLabel()
        icon_dim = int(18 * scale)
        pixmap = QIcon(get_asset_path(svg_icon_path)).pixmap(QSize(icon_dim, icon_dim))
        icon_lbl.setPixmap(pixmap)
        icon_lbl.setFixedSize(icon_dim, icon_dim)
        layout.addWidget(icon_lbl)

        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-size: {int(11.5 * scale)}pt; font-weight: 700; color: #60A5FA;")
        layout.addWidget(lbl)
        layout.addStretch(1)
        return widget

