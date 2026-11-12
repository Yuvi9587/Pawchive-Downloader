import json
import os
import sys
import requests
from PySide6.QtCore import Qt, QSize, QUrl, QThread, Signal
from PySide6.QtGui import QIcon, QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QGridLayout
)

from .assets import get_asset_path


# Optional public read-only JSON endpoint (e.g. GitHub Raw / Gist / Hugging Face).
# When set, Top Supporters are fetched in memory without writing any file to disk.
SUPPORTERS_JSON_URL = ""

_PLATFORM_META = {
    "kofi": ("Ko-fi", "assets/Ko-fi.png", "#F43F5E"),
    "ko-fi": ("Ko-fi", "assets/Ko-fi.png", "#F43F5E"),
    "bmac": ("Buy Me a Coffee", "assets/buymeacoffee.png", "#EAB308"),
    "buymeacoffee": ("Buy Me a Coffee", "assets/buymeacoffee.png", "#EAB308"),
    "patreon": ("Patreon", "assets/patreon.png", "#F97316"),
}

_RANK_COLORS = {
    1: ("#FBBF24", "rgba(251, 191, 36, 0.14)", "rgba(251, 191, 36, 0.35)"),
    2: ("#E2E8F0", "rgba(226, 232, 240, 0.10)", "rgba(226, 232, 240, 0.25)"),
    3: ("#FB923C", "rgba(251, 146, 60, 0.12)", "rgba(251, 146, 60, 0.30)"),
}


def _load_icon_pixmap(rel_path: str, dim: int) -> QPixmap:
    """Loads an SVG or raster asset scaled smoothly to square dim x dim."""
    full_path = get_asset_path(rel_path)
    if full_path.lower().endswith(".svg"):
        return QIcon(full_path).pixmap(QSize(dim, dim))
    pix = QPixmap(full_path)
    if pix.isNull():
        return QPixmap()
    return pix.scaled(
        QSize(dim, dim),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _get_local_supporters_json_path() -> str:
    """Finds sanitized supporter/supporters.json if present locally; never reads raw PII CSVs."""
    candidates = []
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            candidates.append(sys._MEIPASS)
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(exe_dir)
        candidates.append(os.path.join(exe_dir, "_internal"))
    else:
        candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    for base in candidates:
        p = os.path.normpath(os.path.join(base, "supporter", "supporters.json"))
        if os.path.exists(p):
            return p
    return ""


def _sanitize_supporters_payload(data) -> tuple[list, str]:
    """Allowlists only non-private display fields from a parsed JSON payload."""
    if isinstance(data, dict):
        raw_list = data.get("supporters") or []
        updated_at = str(data.get("updated_at") or "").strip()
    elif isinstance(data, list):
        raw_list = data
        updated_at = ""
    else:
        return [], ""

    clean = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        platform_key = str(item.get("platform") or "kofi").strip().lower()
        try:
            total = float(item.get("total") or 0)
        except (TypeError, ValueError):
            total = 0.0
        try:
            coffees = int(item.get("coffees") or 0)
        except (TypeError, ValueError):
            coffees = 0
        amount = str(item.get("amount") or "").strip()
        if not amount and total > 0:
            amount = f"${int(total)}" if total.is_integer() else f"${total:.2f}"
        elif not amount and coffees > 0:
            amount = f"{coffees} {'coffee' if coffees == 1 else 'coffees'}"

        clean.append({
            "name": name,
            "platform": platform_key,
            "total": total if total > 0 else float(coffees * 5),
            "amount": amount,
            "type": str(item.get("type") or "").strip(),
            "since": str(item.get("since") or "").strip(),
            "note": str(item.get("note") or "").strip(),
        })

    clean.sort(key=lambda r: r["total"], reverse=True)
    return clean, updated_at


class _SupportersFetchWorker(QThread):
    """Fetches public supporters JSON strictly in memory without saving files to disk."""
    loaded = Signal(list, str)
    failed = Signal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        try:
            resp = requests.get(self.url, timeout=10)
            resp.raise_for_status()
            supporters, updated_at = _sanitize_supporters_payload(resp.json())
            self.loaded.emit(supporters, updated_at)
        except Exception as e:
            self.failed.emit(str(e))


class SupportLinkRow(QFrame):
    """Clean full-row interactive item inside a grouped panel (no bulky stacked card buttons)."""
    def __init__(
        self, icon_rel: str, title: str, subtitle: str, url: str,
        cta_label: str = "Open", accent: str = "#E5A198", scale: float = 1.0, parent=None
    ):
        super().__init__(parent)
        self.url = url
        self.setObjectName("SupportLinkRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("""
            QFrame#SupportLinkRow {
                background-color: transparent;
                border: none;
                border-radius: 6px;
            }
            QFrame#SupportLinkRow:hover {
                background-color: #222631;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(int(12 * scale), int(10 * scale), int(12 * scale), int(10 * scale))
        layout.setSpacing(int(12 * scale))

        icon_dim = int(28 * scale)
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(icon_dim, icon_dim)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet("background: transparent; border: none;")
        icon_lbl.setPixmap(_load_icon_pixmap(icon_rel, icon_dim))
        layout.addWidget(icon_lbl)

        text_col = QVBoxLayout()
        text_col.setSpacing(int(1 * scale))

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"font-size: {int(10 * scale)}pt; font-weight: 700; color: #FFFFFF; background: transparent; border: none;"
        )
        text_col.addWidget(title_lbl)

        sub_lbl = QLabel(subtitle)
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet(
            f"font-size: {int(8.5 * scale)}pt; color: #9CA3AF; background: transparent; border: none;"
        )
        text_col.addWidget(sub_lbl)
        layout.addLayout(text_col, 1)

        cta_btn = QPushButton(f"{cta_label}  ↗")
        cta_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cta_btn.setFixedHeight(int(26 * scale))
        cta_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #252934;
                border: 1px solid #353A48;
                border-radius: 5px;
                color: #DFE1E5;
                font-size: {int(8.5 * scale)}pt;
                font-weight: 600;
                padding: 0 {int(11 * scale)}px;
            }}
            QPushButton:hover {{
                background-color: #2D3240;
                border-color: {accent};
                color: #FFFFFF;
            }}
        """)
        cta_btn.clicked.connect(self._open_url)
        layout.addWidget(cta_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._open_url()
        super().mousePressEvent(event)

    def _open_url(self):
        if self.url:
            QDesktopServices.openUrl(QUrl(self.url))


class CommunityTile(QFrame):
    """Lightweight horizontal tile for community and repo links without heavy button bars."""
    def __init__(
        self, icon_rel: str, title: str, subtitle: str, url: str,
        hover_color: str = "#E5A198", scale: float = 1.0, parent=None
    ):
        super().__init__(parent)
        self.url = url
        self.setObjectName("CommunityTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QFrame#CommunityTile {{
                background-color: #181A22;
                border: 1px solid #282B36;
                border-radius: 8px;
            }}
            QFrame#CommunityTile:hover {{
                background-color: #1F222C;
                border-color: {hover_color};
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(int(12 * scale), int(10 * scale), int(12 * scale), int(10 * scale))
        layout.setSpacing(int(10 * scale))

        icon_dim = int(24 * scale)
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(icon_dim, icon_dim)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet("background: transparent; border: none;")
        icon_lbl.setPixmap(_load_icon_pixmap(icon_rel, icon_dim))
        layout.addWidget(icon_lbl)

        text_col = QVBoxLayout()
        text_col.setSpacing(int(1 * scale))

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"font-size: {int(9.5 * scale)}pt; font-weight: 700; color: #FFFFFF; background: transparent; border: none;"
        )
        text_col.addWidget(title_lbl)

        sub_lbl = QLabel(subtitle)
        sub_lbl.setStyleSheet(
            f"font-size: {int(8 * scale)}pt; color: #8C929E; background: transparent; border: none;"
        )
        text_col.addWidget(sub_lbl)
        layout.addLayout(text_col, 1)

        arrow_lbl = QLabel("↗")
        arrow_lbl.setStyleSheet(
            f"font-size: {int(10 * scale)}pt; color: #7C828E; font-weight: 600; background: transparent; border: none;"
        )
        layout.addWidget(arrow_lbl)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.url:
            QDesktopServices.openUrl(QUrl(self.url))
        super().mousePressEvent(event)


class SupporterRow(QFrame):
    """Minimal leaderboard row inside the unified Top Supporters panel."""
    def __init__(self, rank: int, supporter: dict, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.setObjectName("SupporterRow")
        self.setStyleSheet("QFrame#SupporterRow { background: transparent; border: none; }")

        accent, badge_bg, badge_border = _RANK_COLORS.get(
            rank, ("#9CA3AF", "rgba(156, 163, 175, 0.08)", "rgba(156, 163, 175, 0.18)")
        )
        platform_key = supporter.get("platform", "kofi")
        plat_name, plat_icon_rel, plat_color = _PLATFORM_META.get(
            platform_key, ("Supporter", "assets/Svg/heart.svg", "#E5A198")
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(int(4 * scale), int(8 * scale), int(4 * scale), int(8 * scale))
        layout.setSpacing(int(12 * scale))

        rank_dim = int(28 * scale)
        rank_lbl = QLabel(f"#{rank}")
        rank_lbl.setFixedSize(rank_dim, rank_dim)
        rank_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rank_lbl.setStyleSheet(f"""
            background-color: {badge_bg};
            border: 1px solid {badge_border};
            border-radius: {int(14 * scale)}px;
            color: {accent};
            font-size: {int(9 * scale)}pt;
            font-weight: 800;
        """)
        layout.addWidget(rank_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        info_col = QVBoxLayout()
        info_col.setSpacing(int(2 * scale))

        name_lbl = QLabel(supporter.get("name") or "Anonymous")
        name_lbl.setStyleSheet(
            f"font-size: {int(10 * scale)}pt; font-weight: 700; color: #F3F4F6; background: transparent; border: none;"
        )
        info_col.addWidget(name_lbl)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(int(5 * scale))

        plat_dim = int(13 * scale)
        plat_icon_lbl = QLabel()
        plat_icon_lbl.setFixedSize(plat_dim, plat_dim)
        plat_icon_lbl.setStyleSheet("background: transparent; border: none;")
        plat_icon_lbl.setPixmap(_load_icon_pixmap(plat_icon_rel, plat_dim))
        meta_row.addWidget(plat_icon_lbl)

        meta_parts = [plat_name]
        if supporter.get("type"):
            meta_parts.append(supporter["type"])
        if supporter.get("since"):
            meta_parts.append(supporter["since"])

        meta_lbl = QLabel("  •  ".join(meta_parts))
        meta_lbl.setStyleSheet(
            f"font-size: {int(8 * scale)}pt; color: #9CA3AF; background: transparent; border: none;"
        )
        meta_row.addWidget(meta_lbl)
        meta_row.addStretch(1)
        info_col.addLayout(meta_row)

        note = supporter.get("note") or ""
        if note:
            note_lbl = QLabel(f'"{note}"')
            note_lbl.setWordWrap(True)
            note_lbl.setStyleSheet(
                f"font-size: {int(8 * scale)}pt; color: #B4BAC6; font-style: italic; background: transparent; border: none;"
            )
            info_col.addWidget(note_lbl)

        layout.addLayout(info_col, 1)

        amount = supporter.get("amount") or ""
        if amount:
            amt_lbl = QLabel(amount)
            amt_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            amt_color = accent if rank <= 3 else plat_color
            amt_lbl.setStyleSheet(
                f"font-size: {int(10.5 * scale)}pt; font-weight: 800; color: {amt_color}; background: transparent; border: none;"
            )
            layout.addWidget(amt_lbl, 0, Qt.AlignmentFlag.AlignVCenter)


class SupportTabWidget(QWidget):
    """
    Dedicated tab for Pawchive Downloader support links, Top Supporters
    leaderboard, and community resources in a clean, low-clutter two-column layout.
    """
    def __init__(self, main_app, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.main_app = main_app
        self.scale = scale
        self._supporters_worker = None
        self._remote_loaded = False
        self._init_ui()
        self._load_initial_supporters()

    def on_tab_activated(self):
        """Called when this tab is selected in content_stack."""
        if SUPPORTERS_JSON_URL and not self._remote_loaded:
            self._fetch_remote_supporters()

    def _make_divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet("background-color: #242833; border: none;")
        return line

    def _make_panel_heading(self, title: str, icon_rel: str, subtitle: str = "") -> QWidget:
        scale = self.scale
        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        vbox = QVBoxLayout(wrap)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(int(3 * scale))

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(int(8 * scale))

        dim = int(16 * scale)
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(dim, dim)
        icon_lbl.setPixmap(_load_icon_pixmap(icon_rel, dim))
        row.addWidget(icon_lbl)

        lbl = QLabel(title)
        lbl.setStyleSheet(f"font-size: {int(11 * scale)}pt; font-weight: 700; color: #E5A198;")
        row.addWidget(lbl)
        row.addStretch(1)
        vbox.addLayout(row)

        if subtitle:
            sub = QLabel(subtitle)
            sub.setWordWrap(True)
            sub.setStyleSheet(f"font-size: {int(8.5 * scale)}pt; color: #8C929E;")
            vbox.addWidget(sub)

        return wrap

    def _init_ui(self):
        scale = self.scale
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet("background-color: transparent; border: none;")

        page_container = QWidget()
        page_container.setStyleSheet("background-color: transparent;")
        content_layout = QVBoxLayout(page_container)
        content_layout.setContentsMargins(int(24 * scale), int(18 * scale), int(24 * scale), int(20 * scale))
        content_layout.setSpacing(int(18 * scale))

        # ── Compact Editorial Header ─────────────────────────────────────────
        header_frame = QFrame()
        header_frame.setObjectName("SupportHero")
        header_frame.setStyleSheet("""
            QFrame#SupportHero {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #1D1A21, stop:1 #181A22);
                border: 1px solid #2F2A33;
                border-radius: 10px;
            }
        """)
        h_layout = QHBoxLayout(header_frame)
        h_layout.setContentsMargins(int(18 * scale), int(14 * scale), int(18 * scale), int(14 * scale))
        h_layout.setSpacing(int(14 * scale))

        heart_dim = int(34 * scale)
        heart_lbl = QLabel()
        heart_lbl.setFixedSize(heart_dim, heart_dim)
        heart_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heart_lbl.setStyleSheet("background: transparent; border: none;")
        heart_lbl.setPixmap(_load_icon_pixmap("assets/Svg/heart.svg", heart_dim))
        h_layout.addWidget(heart_lbl)

        h_titles = QVBoxLayout()
        h_titles.setSpacing(int(2 * scale))
        title_lbl = QLabel("Support & Community")
        title_lbl.setStyleSheet(
            f"font-size: {int(13.5 * scale)}pt; font-weight: 700; color: #FFFFFF; background: transparent; border: none;"
        )
        h_titles.addWidget(title_lbl)

        sub_lbl = QLabel(
            "Pawchive Downloader is free and open-source. Thank you for helping keep the project alive!"
        )
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet(
            f"font-size: {int(9 * scale)}pt; color: #A6ACB8; background: transparent; border: none;"
        )
        h_titles.addWidget(sub_lbl)
        h_layout.addLayout(h_titles, 1)

        star_btn = QPushButton("Star on GitHub  ↗")
        star_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        star_btn.setFixedHeight(int(30 * scale))
        star_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #2B2226;
                border: 1px solid #593B42;
                border-radius: 6px;
                color: #F5B0A8;
                font-size: {int(8.5 * scale)}pt;
                font-weight: 700;
                padding: 0 {int(12 * scale)}px;
            }}
            QPushButton:hover {{
                background-color: #382B30;
                border-color: #E5A198;
                color: #FFFFFF;
            }}
        """)
        star_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/Yuvi9587/Pawchive-Downloader"))
        )
        h_layout.addWidget(star_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        content_layout.addWidget(header_frame)

        # ── Two-Column Body: Left (Support + Community), Right (Top Supporters) ──
        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(int(18 * scale))

        # LEFT COLUMN
        left_col = QVBoxLayout()
        left_col.setSpacing(int(16 * scale))

        left_col.addWidget(self._make_panel_heading(
            "Support Development",
            "assets/Svg/heart.svg",
            "Every tip or membership helps cover server costs and ongoing updates.",
        ))

        support_group = QFrame()
        support_group.setObjectName("SupportGroupPanel")
        support_group.setStyleSheet("""
            QFrame#SupportGroupPanel {
                background-color: #181A22;
                border: 1px solid #282B36;
                border-radius: 10px;
            }
        """)
        sg_layout = QVBoxLayout(support_group)
        sg_layout.setContentsMargins(int(8 * scale), int(6 * scale), int(8 * scale), int(6 * scale))
        sg_layout.setSpacing(int(2 * scale))

        support_rows = [
            ("assets/Ko-fi.png", "Ko-fi", "Buy a coffee or send a one-time tip (0% platform fee).",
             "https://ko-fi.com/yuvi427183", "Tip", "#F43F5E"),
            ("assets/buymeacoffee.png", "Buy Me a Coffee", "Quick, hassle-free one-time support.",
             "https://buymeacoffee.com/yuvi9587", "Support", "#EAB308"),
            ("assets/patreon.png", "Patreon", "Become a monthly backer for ongoing updates.",
             "https://www.patreon.com/Yuvi102", "Join", "#F97316"),
        ]
        for idx, (icon_rel, title, subtitle, url, cta, accent) in enumerate(support_rows):
            sg_layout.addWidget(SupportLinkRow(
                icon_rel, title, subtitle, url, cta_label=cta, accent=accent, scale=scale
            ))
            if idx < len(support_rows) - 1:
                sg_layout.addWidget(self._make_divider())

        left_col.addWidget(support_group)

        left_col.addWidget(self._make_panel_heading(
            "Community & Links",
            "assets/Svg/user.svg",
            "Report bugs, suggest features, or connect with other archivists.",
        ))

        community_grid = QGridLayout()
        community_grid.setSpacing(int(10 * scale))
        community_grid.setColumnStretch(0, 1)
        community_grid.setColumnStretch(1, 1)

        community_links = [
            ("assets/discord.png", "Discord Server", "Chat & get help", "https://discord.gg/BqP64XTdJN", "#818CF8"),
            ("assets/github.png", "GitHub", "Source & issues", "https://github.com/Yuvi9587/Pawchive-Downloader", "#60A5FA"),
            ("assets/instagram.png", "Instagram", "Art & updates", "https://www.instagram.com/uvi.arts/", "#EC4899"),
            ("assets/Svg/paw-print.svg", "Pawchive.pw", "Official website", "https://pawchive.pw", "#38BDF8"),
        ]
        for idx, (icon_rel, title, sub, url, hover) in enumerate(community_links):
            r, c = divmod(idx, 2)
            community_grid.addWidget(CommunityTile(icon_rel, title, sub, url, hover_color=hover, scale=scale), r, c)

        left_col.addLayout(community_grid)

        # Clean "Where Support Goes" inline strip (no heavy box clutter)
        impact_panel = QFrame()
        impact_panel.setObjectName("ImpactStrip")
        impact_panel.setStyleSheet("""
            QFrame#ImpactStrip {
                background-color: #161820;
                border: 1px solid #242833;
                border-radius: 8px;
            }
        """)
        imp_layout = QHBoxLayout(impact_panel)
        imp_layout.setContentsMargins(int(16 * scale), int(12 * scale), int(16 * scale), int(12 * scale))
        imp_layout.setSpacing(int(16 * scale))

        for title, desc in [
            ("100% Free & Ad-Free", "No paywalls, tracking, or locked features."),
            ("Active Updates", "Fast fixes for creator platform & API changes."),
            ("Community Driven", "Features shaped by Discord & GitHub feedback."),
        ]:
            item_col = QVBoxLayout()
            item_col.setSpacing(int(2 * scale))
            t_lbl = QLabel(title)
            t_lbl.setStyleSheet(
                f"font-size: {int(9 * scale)}pt; font-weight: 700; color: #E5A198; background: transparent; border: none;"
            )
            d_lbl = QLabel(desc)
            d_lbl.setWordWrap(True)
            d_lbl.setStyleSheet(
                f"font-size: {int(8 * scale)}pt; color: #8C929E; background: transparent; border: none;"
            )
            item_col.addWidget(t_lbl)
            item_col.addWidget(d_lbl)
            imp_layout.addLayout(item_col, 1)

        left_col.addWidget(impact_panel)
        left_col.addStretch(1)
        columns_layout.addLayout(left_col, 5)

        # RIGHT COLUMN: Top Supporters Leaderboard (matches left column height cleanly)
        right_col = QVBoxLayout()
        right_col.setSpacing(int(10 * scale))

        sup_head_row = QHBoxLayout()
        sup_head_row.setContentsMargins(0, 0, 0, 0)
        sup_head_row.addWidget(self._make_panel_heading("Top Supporters", "assets/Svg/star.svg"))
        sup_head_row.addStretch(1)

        self.supporters_updated_lbl = QLabel("")
        self.supporters_updated_lbl.setStyleSheet(f"font-size: {int(8 * scale)}pt; color: #7C828E;")
        self.supporters_updated_lbl.setVisible(False)
        sup_head_row.addWidget(self.supporters_updated_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        right_col.addLayout(sup_head_row)

        supporters_panel = QFrame()
        supporters_panel.setObjectName("TopSupportersPanel")
        supporters_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        supporters_panel.setStyleSheet("""
            QFrame#TopSupportersPanel {
                background-color: #181A22;
                border: 1px solid #282B36;
                border-radius: 10px;
            }
        """)
        sp_layout = QVBoxLayout(supporters_panel)
        sp_layout.setContentsMargins(int(16 * scale), int(14 * scale), int(16 * scale), int(14 * scale))
        sp_layout.setSpacing(int(8 * scale))

        self.supporters_summary_lbl = QLabel("Ko-fi & Buy Me a Coffee Leaderboard")
        self.supporters_summary_lbl.setStyleSheet(
            f"font-size: {int(8.5 * scale)}pt; color: #9CA3AF; font-weight: 600; background: transparent;"
        )
        sp_layout.addWidget(self.supporters_summary_lbl)
        sp_layout.addWidget(self._make_divider())

        self.supporters_list_layout = QVBoxLayout()
        self.supporters_list_layout.setContentsMargins(0, 0, 0, 0)
        self.supporters_list_layout.setSpacing(int(2 * scale))
        sp_layout.addLayout(self.supporters_list_layout)

        self.supporters_empty_lbl = QLabel("No public supporters listed yet — be the first!")
        self.supporters_empty_lbl.setWordWrap(True)
        self.supporters_empty_lbl.setStyleSheet(
            f"font-size: {int(8.5 * scale)}pt; color: #8C929E; padding: {int(12 * scale)}px 0;"
        )
        self.supporters_empty_lbl.setVisible(False)
        sp_layout.addWidget(self.supporters_empty_lbl)

        sp_layout.addStretch(1)
        sp_layout.addWidget(self._make_divider())

        cta_note = QLabel(
            "Want to join the wall? Support via Ko-fi or Buy Me a Coffee with a public display name — "
            "emails and transaction IDs are always kept private."
        )
        cta_note.setWordWrap(True)
        cta_note.setStyleSheet(
            f"font-size: {int(8 * scale)}pt; color: #7C828E; padding-top: {int(2 * scale)}px; background: transparent;"
        )
        sp_layout.addWidget(cta_note)

        right_col.addWidget(supporters_panel, 1)
        columns_layout.addLayout(right_col, 4)

        content_layout.addLayout(columns_layout, 1)

        # Clean minimal footer bar
        content_layout.addWidget(self._make_divider())
        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(int(16 * scale))

        footer_note = QLabel("Thank you for using Pawchive Downloader and supporting independent open-source software.")
        footer_note.setStyleSheet(f"font-size: {int(8.5 * scale)}pt; color: #7C828E;")
        footer_row.addWidget(footer_note)
        footer_row.addStretch(1)

        for label_txt, link_url in [
            ("Report Bug ↗", "https://github.com/Yuvi9587/Pawchive-Downloader/issues"),
            ("Release Notes ↗", "https://github.com/Yuvi9587/Pawchive-Downloader/releases"),
        ]:
            link_btn = QPushButton(label_txt)
            link_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            link_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: none;
                    color: #9CA3AF;
                    font-size: {int(8.5 * scale)}pt;
                    font-weight: 600;
                    padding: 0 {int(4 * scale)}px;
                }}
                QPushButton:hover {{
                    color: #E5A198;
                }}
            """)
            link_btn.clicked.connect(lambda _=False, u=link_url: QDesktopServices.openUrl(QUrl(u)))
            footer_row.addWidget(link_btn)

        content_layout.addLayout(footer_row)

        scroll.setWidget(page_container)
        root_layout.addWidget(scroll, 1)

    def _load_initial_supporters(self):
        local_path = _get_local_supporters_json_path()
        if not local_path:
            self._render_supporters([], "")
            return
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            supporters, updated_at = _sanitize_supporters_payload(data)
            self._render_supporters(supporters, updated_at)
        except Exception:
            self._render_supporters([], "")

    def _fetch_remote_supporters(self):
        if not SUPPORTERS_JSON_URL:
            return
        if self._supporters_worker and self._supporters_worker.isRunning():
            return
        worker = _SupportersFetchWorker(SUPPORTERS_JSON_URL, parent=self)
        worker.loaded.connect(self._on_remote_supporters_loaded)
        self._supporters_worker = worker
        worker.start()

    def _on_remote_supporters_loaded(self, supporters: list, updated_at: str):
        self._remote_loaded = True
        if supporters:
            self._render_supporters(supporters, updated_at)

    def _render_supporters(self, supporters: list, updated_at: str = ""):
        while self.supporters_list_layout.count():
            item = self.supporters_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not supporters:
            self.supporters_summary_lbl.setText("Ko-fi & Buy Me a Coffee Leaderboard")
            self.supporters_empty_lbl.setVisible(True)
            self.supporters_updated_lbl.setVisible(False)
            return

        count = len(supporters)
        self.supporters_summary_lbl.setText(
            f"{count} Public {'Supporter' if count == 1 else 'Supporters'}  •  Ko-fi & Buy Me a Coffee"
        )
        self.supporters_empty_lbl.setVisible(False)
        if updated_at:
            self.supporters_updated_lbl.setText(f"Updated {updated_at}")
            self.supporters_updated_lbl.setVisible(True)
        else:
            self.supporters_updated_lbl.setVisible(False)

        for idx, supporter in enumerate(supporters):
            self.supporters_list_layout.addWidget(
                SupporterRow(idx + 1, supporter, scale=self.scale, parent=self)
            )
            if idx < len(supporters) - 1:
                self.supporters_list_layout.addWidget(self._make_divider())
