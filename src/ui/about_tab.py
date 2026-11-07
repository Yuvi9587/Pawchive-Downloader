import os
import sys
from PySide6.QtCore import Qt, QSize, QUrl
from PySide6.QtGui import QIcon, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QStackedWidget, QScrollArea,
    QFrame, QSizePolicy, QApplication
)

from .assets import get_asset_path
from ..config.constants import APP_VERSION


class AboutTopicWidget(QWidget):
    """A scrollable content page for a single topic in the Pawchive guide."""
    def __init__(self, icon_name: str, title_text: str, content_html: str, parent=None, scale: float = 1.0):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(int(24 * scale), int(20 * scale), int(24 * scale), int(20 * scale))
        layout.setSpacing(int(14 * scale))

        # Title row with SVG icon
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(int(10 * scale))

        icon_lbl = QLabel()
        icon_dim = int(22 * scale)
        icon_pixmap = QIcon(get_asset_path(f"assets/Svg/{icon_name}.svg")).pixmap(QSize(icon_dim, icon_dim))
        icon_lbl.setPixmap(icon_pixmap)
        icon_lbl.setFixedSize(icon_dim, icon_dim)
        title_row.addWidget(icon_lbl)

        title_lbl = QLabel(title_text)
        title_font_size = int(14 * scale)
        title_lbl.setStyleSheet(f"font-size: {title_font_size}pt; font-weight: 700; color: #E5A198;")
        title_row.addWidget(title_lbl)
        title_row.addStretch(1)

        layout.addLayout(title_row)

        # Subtle separator under title
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setStyleSheet("background-color: #3D353A; max-height: 1px;")
        layout.addWidget(sep)

        # Scrollable content area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet("background-color: transparent; border: none;")

        content_widget = QWidget()
        content_widget.setStyleSheet("background-color: transparent;")
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, int(10 * scale), int(10 * scale), int(10 * scale))
        content_layout.setSpacing(int(12 * scale))

        content_lbl = QLabel(content_html)
        content_lbl.setWordWrap(True)
        content_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        content_lbl.setTextFormat(Qt.TextFormat.RichText)
        content_lbl.setOpenExternalLinks(True)
        content_font_size = int(9.5 * scale)
        content_lbl.setStyleSheet(f"""
            QLabel {{
                font-size: {content_font_size}pt;
                color: #D1D5DB;
                line-height: 1.65;
            }}
            h3 {{
                color: #EBB3AC;
                font-size: {int(11.5 * scale)}pt;
                margin-top: 14px;
                margin-bottom: 6px;
            }}
            h4 {{
                color: #CBD5E1;
                font-size: {int(10 * scale)}pt;
                margin-top: 10px;
                margin-bottom: 4px;
            }}
            ul, ol {{
                margin-left: 18px;
                margin-top: 4px;
                margin-bottom: 8px;
            }}
            li {{
                margin-bottom: 6px;
            }}
            code {{
                background-color: #332B2E;
                color: #FCEFEF;
                padding: 2px 6px;
                border-radius: 4px;
                font-family: Consolas, 'Courier New', monospace;
                border: 1px solid #483B40;
            }}
            a {{
                color: #E5A198;
                text-decoration: none;
            }}
            a:hover {{
                text-decoration: underline;
            }}
            .tip-box {{
                background-color: #382528;
                border-left: 4px solid #CC7A70;
                border-radius: 4px;
                padding: 10px 14px;
                margin: 10px 0px;
                color: #F8DFDC;
            }}
            .warn-box {{
                background-color: #2D2314;
                border-left: 4px solid #F59E0B;
                border-radius: 4px;
                padding: 10px 14px;
                margin: 10px 0px;
                color: #FEF3C7;
            }}
        """)
        content_layout.addWidget(content_lbl)
        content_layout.addStretch(1)

        scroll.setWidget(content_widget)
        layout.addWidget(scroll, 1)


class AboutTabWidget(QWidget):
    """
    Dedicated tab for Pawchive Downloader information, features guide,
    and helpful resources focused exclusively on Pawchive with SVG icons.
    """
    def __init__(self, main_app, scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.main_app = main_app
        self.scale = scale
        self._init_ui()

    def on_tab_activated(self):
        """Called when this tab is selected in content_stack."""
        if hasattr(self, 'topics_list') and self.topics_list.currentRow() < 0:
            self.topics_list.setCurrentRow(0)

    def _init_ui(self):
        scale = self.scale
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(int(16 * scale), int(14 * scale), int(16 * scale), int(14 * scale))
        root_layout.setSpacing(int(12 * scale))

        # ── Header Banner ─────────────────────────────────────────────────────
        header_frame = QFrame()
        header_frame.setObjectName("AboutHeaderFrame")
        header_frame.setStyleSheet("""
            QFrame#AboutHeaderFrame {
                background-color: #242125;
                border: 1px solid #3B3338;
                border-radius: 8px;
            }
        """)
        header_layout = QHBoxLayout(header_frame)
        header_layout.setContentsMargins(int(16 * scale), int(12 * scale), int(16 * scale), int(12 * scale))
        header_layout.setSpacing(int(14 * scale))

        # Logo icon
        paw_icon_btn = QPushButton()
        paw_icon_btn.setObjectName("AboutLogoButton")
        paw_icon_btn.setFixedSize(int(46 * scale), int(46 * scale))
        paw_icon_size = int(36 * scale)
        paw_icon_btn.setIcon(QIcon(get_asset_path("assets/Svg/paw-print.svg")))
        paw_icon_btn.setIconSize(QSize(paw_icon_size, paw_icon_size))
        paw_icon_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        paw_icon_btn.setToolTip("Visit Pawchive.pw")
        paw_icon_btn.setStyleSheet("background: transparent; border: none;")
        paw_icon_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://pawchive.pw")))
        header_layout.addWidget(paw_icon_btn)

        # Titles
        titles_vbox = QVBoxLayout()
        titles_vbox.setSpacing(int(2 * scale))

        app_title_row = QHBoxLayout()
        app_title_row.setSpacing(int(8 * scale))

        app_name_lbl = QLabel("Pawchive Downloader")
        app_name_lbl.setStyleSheet(f"font-size: {int(14 * scale)}pt; font-weight: 700; color: #FFFFFF;")
        app_title_row.addWidget(app_name_lbl)

        version_badge = QLabel(f"v{APP_VERSION}")
        version_badge.setStyleSheet(f"""
            QLabel {{
                background-color: #4A2826;
                color: #F5B0A8;
                border: 1px solid #783E3A;
                border-radius: 4px;
                padding: 1px {int(6 * scale)}px;
                font-size: {int(8.5 * scale)}pt;
                font-weight: 600;
            }}
        """)
        app_title_row.addWidget(version_badge)
        app_title_row.addStretch(1)
        titles_vbox.addLayout(app_title_row)

        subtitle_lbl = QLabel("Your friendly all-in-one archiver and media downloader for pawchive.pw")
        subtitle_lbl.setStyleSheet(f"font-size: {int(9 * scale)}pt; color: #9CA3AF;")
        titles_vbox.addWidget(subtitle_lbl)

        header_layout.addLayout(titles_vbox, 1)

        # Link Buttons in Header
        links_box = QHBoxLayout()
        links_box.setSpacing(int(8 * scale))

        def _make_link_btn(text: str, icon_path: str, url: str):
            btn = QPushButton(f" {text}")
            btn.setFixedHeight(int(30 * scale))
            btn.setIcon(QIcon(icon_path))
            btn.setIconSize(QSize(int(16 * scale), int(16 * scale)))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #2F2A2E;
                    border: 1px solid #453C42;
                    border-radius: 5px;
                    color: #D1D5DB;
                    font-size: {int(8.5 * scale)}pt;
                    font-weight: 600;
                    padding: 0 {int(10 * scale)}px;
                }}
                QPushButton:hover {{
                    background-color: #3B3439;
                    border-color: #D48378;
                    color: #FFFFFF;
                }}
            """)
            btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
            return btn

        links_box.addWidget(_make_link_btn("Pawchive.pw", get_asset_path("assets/Svg/paw-print.svg"), "https://pawchive.pw"))
        links_box.addWidget(_make_link_btn("GitHub", get_asset_path("assets/github.png"), "https://github.com/Yuvi63771/Kemono-Downloader"))
        links_box.addWidget(_make_link_btn("Discord", get_asset_path("assets/discord.png"), "https://discord.gg/BqP64XTdJN"))

        header_layout.addLayout(links_box)
        root_layout.addWidget(header_frame)

        # ── Topics Content Layout (Sidebar list + Stack) ──────────────────────
        body_frame = QFrame()
        body_frame.setObjectName("AboutBodyFrame")
        body_frame.setStyleSheet("""
            QFrame#AboutBodyFrame {
                background-color: #242125;
                border: 1px solid #3B3338;
                border-radius: 8px;
            }
        """)
        body_layout = QHBoxLayout(body_frame)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # Navigation List on Left with SVG icons
        self.topics_list = QListWidget()
        self.topics_list.setFixedWidth(int(240 * scale))
        self.topics_list.setIconSize(QSize(int(18 * scale), int(18 * scale)))
        self.topics_list.setFrameShape(QFrame.Shape.NoFrame)
        self.topics_list.setStyleSheet(f"""
            QListWidget {{
                background-color: #1D1A1C;
                border-top-left-radius: 8px;
                border-bottom-left-radius: 8px;
                border-right: 1px solid #332B30;
                font-size: {int(9 * scale)}pt;
                color: #C5BCC1;
                outline: none;
                padding: 6px;
            }}
            QListWidget::item {{
                padding: {int(10 * scale)}px {int(12 * scale)}px;
                border-radius: 6px;
                margin-bottom: 2px;
            }}
            QListWidget::item:hover {{
                background-color: #2C2427;
                color: #FFFFFF;
            }}
            QListWidget::item:selected {{
                background-color: #4A2826;
                color: #F5B0A8;
                font-weight: 600;
                border: 1px solid #783E3A;
            }}
        """)

        # Stacked Widget on Right
        self.content_stack = QStackedWidget()
        self.content_stack.setStyleSheet("background-color: #242125; border-top-right-radius: 8px; border-bottom-right-radius: 8px;")

        # Populate Topics with SVG icons
        topics = self._get_pawchive_topics()
        for idx, (icon_name, title, content) in enumerate(topics):
            item = QListWidgetItem()
            item.setIcon(QIcon(get_asset_path(f"assets/Svg/{icon_name}.svg")))
            item.setText(title)
            self.topics_list.addItem(item)

            page_widget = AboutTopicWidget(icon_name, title, content, scale=scale)
            self.content_stack.addWidget(page_widget)

        self.topics_list.currentRowChanged.connect(self.content_stack.setCurrentIndex)
        if self.topics_list.count() > 0:
            self.topics_list.setCurrentRow(0)

        body_layout.addWidget(self.topics_list)
        body_layout.addWidget(self.content_stack, 1)

        root_layout.addWidget(body_frame, 1)

    def _get_pawchive_topics(self):
        """
        Returns the curated guide topics exclusively covering Pawchive in a warm,
        casual, human tone. Each item is a tuple: (svg_icon_name, title, html_content).
        """
        return [
            ("paw-print", "Welcome & Overview", """
                <p>Hey there! Welcome to <b>Pawchive Downloader</b>.</p>
                <p>This tool was made to take the hassle out of saving your favorite artwork, creator feeds, comics, and media from <b>Pawchive</b> (<code>pawchive.pw</code>). Instead of right-clicking hundreds of images one by one, you can grab whole galleries with a single click, keep everything sorted into neat folders, and let the app handle the heavy lifting in the background.</p>
                
                <h3>What Can This App Do?</h3>
                <ul>
                    <li><b>Direct Pawchive Archiving:</b> Paste a creator's URL or a single post link and it grabs all the attached photos, videos, and full-res files.</li>
                    <li><b>Fast Concurrent Downloads:</b> Uses multiple download threads so you're not waiting around forever on large galleries.</li>
                    <li><b>Smart Rate-Limit Backoff:</b> If Pawchive's server says it needs a breather (HTTP 429), the app pauses politely, waits out the timer, and automatically picks right back up without freezing.</li>
                    <li><b>Network Drop Protection:</b> If your Wi-Fi flickers or you switch networks mid-download, it pauses safely and resumes as soon as you're back online.</li>
                    <li><b>Automatic End-of-Run Retries:</b> Any file that had a hiccup or timeout gets retried automatically before the job finishes.</li>
                    <li><b>Organized File Sorting:</b> Automatically sorts downloads into artist folders, character subfolders via <code>Known.txt</code>, or date prefixes so your hard drive stays tidy.</li>
                    <li><b>Manga & Comic Friendly:</b> Reverse-chronological downloading and clean page numbering so multi-page stories stay in reading order.</li>
                    <li><b>Favorites Sync:</b> Connect your Pawchive cookie to view and download from your followed artists directly inside the app.</li>
                </ul>

                <div class='tip-box'>
                    <b>Quick Tour:</b> Check out the tabs in the left sidebar whenever you need them: <b>Downloader</b> for direct links, <b>Queue</b> to line up a bunch of downloads, <b>Creators</b> and <b>Posts</b> to browse what you've saved, <b>Favorites</b> for your followed artists, and <b>Visual Sort</b> for AI-powered image organizing!
                </div>
            """),

            ("download", "Downloading from Pawchive", """
                <p>Downloading stuff is as simple as copy and paste. Here is how it works:</p>

                <h3>What Links Can I Paste?</h3>
                <ul>
                    <li><b>Creator Profile URLs:</b> Drop in any creator page (e.g. <code>https://pawchive.pw/creator/12345</code>) to download their full post history, photos, attachments, and videos.</li>
                    <li><b>Individual Post URLs:</b> Spotted a single post you love? Paste the direct link (e.g. <code>https://pawchive.pw/post/50191325</code>) to download just that post's attachments.</li>
                </ul>

                <h3>Useful Settings to Know</h3>
                <ul>
                    <li><b>Download Directory:</b> Click the folder icon next to the path box to choose where your files should go. The app remembers your folder for next time.</li>
                    <li><b>Page Range:</b> If an artist has 50 pages of posts but you only want the most recent stuff, set Start to <code>1</code> and End to <code>3</code> or <code>5</code>.</li>
                    <li><b>Multithreading:</b> Speeds things up by downloading multiple files at once. You can tweak the thread count depending on your connection speed.</li>
                    <li><b>Pause & Resume:</b> Need your bandwidth back for a gaming session or movie? Click Pause anytime and resume whenever you're ready. Clicking Cancel stops safely without leaving half-baked files behind.</li>
                </ul>

                <div class='tip-box'>
                    <b>Good to Know:</b> Don't worry about downloading the same file twice. The app checks file hashes automatically so it will skip anything you already have on disk.
                </div>
            """),

            ("queue", "Download Queue", """
                <p>Want to download ten different artists overnight? That's exactly why the <b>Queue</b> exists.</p>
                <p>Instead of waiting around for one download to finish before you can paste the next link, you can line them all up in advance and walk away.</p>

                <h3>How to Line Up Downloads</h3>
                <ol>
                    <li>In the main Downloader tab, paste your link and set whatever options you want (like a specific folder or filter).</li>
                    <li>Click <b>Add to Queue</b> instead of Start Download. Your link field clears and the job is saved.</li>
                    <li>Paste your next link, pick its settings, and hit Add to Queue again. Each queued job remembers its own individual folder and filter settings!</li>
                    <li>Head over to the <b>Queue</b> tab in the sidebar and click <b>Start Queue</b> to start downloading through the list.</li>
                </ol>

                <h3>Queue Table Tabs</h3>
                <ul>
                    <li><b>All:</b> Shows every task in the queue with live progress bars and download speeds.</li>
                    <li><b>Active:</b> Focuses purely on what is downloading right this second.</li>
                    <li><b>Queued:</b> Your upcoming tasks waiting their turn.</li>
                    <li><b>Done:</b> Everything that finished successfully.</li>
                    <li><b>Failed:</b> Any downloads that ran into an issue, making it easy to review or retry them.</li>
                </ul>
            """),

            ("target", "Content Filtering & Skip Rules", """
                <p>You don't always want every single file a creator uploads. These filters help you grab only what you actually care about—and skip the rest.</p>

                <h3>Filter by File Type</h3>
                <ul>
                    <li><b>All:</b> Grabs everything available (default).</li>
                    <li><b>Images/GIFs:</b> Downloads pictures and animations (JPG, PNG, WEBP, GIF).</li>
                    <li><b>Videos:</b> Grabs only video clips (MP4, WEBM, MOV, MKV).</li>
                    <li><b>Only Archives:</b> Grabs only compressed zip and rar archives.</li>
                    <li><b>Only Audio:</b> Grabs music or voice tracks (MP3, WAV, FLAC).</li>
                    <li><b>Only Links:</b> Scans descriptions for external links and lists them in your log instead of downloading files.</li>
                </ul>

                <h3>Looking for Specific Characters?</h3>
                <p>Type names into the <b>Filter by Character(s)</b> box:</p>
                <ul>
                    <li><b>Single or Multiple Names:</b> Type <code>Tifa, Aerith, Cloud</code> separated by commas.</li>
                    <li><b>Grouped Aliases:</b> If a character goes by different names, group them in parentheses like <code>(Tifa, Lockhart)</code> so either name counts toward the same character folder.</li>
                    <li><b>Filter Scope:</b> Click the scope button to choose whether the app looks for matches in the post <b>Title</b>, the <b>Files</b>, or <b>Both</b>.</li>
                </ul>

                <h3>Skipping Content & Size Filters</h3>
                <ul>
                    <li><b>Skip with Words:</b> Add keywords like <code>WIP, sketch, preview</code> to ignore work-in-progress drafts.</li>
                    <li><b>Skip Scope:</b> Choose whether matching a skip word skips the whole post or just the individual matching file.</li>
                    <li><b>Skip Archives:</b> A quick one-click toggle to ignore all zip and rar archives.</li>
                    <li><b>Skip by File Size:</b> Put <code>[200]</code> into the skip words box to skip any file smaller than 200 MB.</li>
                    <li><b>Remove Words from Filenames:</b> Enter text here to automatically strip annoying watermarks, tags, or prefixes from your saved filenames.</li>
                </ul>
            """),

            ("folder", "Folder Organization & Known.txt", """
                <p>Nobody likes a messy folder with thousands of loose files. Pawchive Downloader keeps your library sorted so you can easily find what you're looking for later.</p>

                <h3>Smart Sorting with Known.txt</h3>
                <p>When you check <b>Separate Folders by Known.txt</b>, the app checks each post's title and filenames against your list of favorite characters or series. If it finds a match, it drops the files straight into that character's folder.</p>
                
                <h4>How the App Decides on Folder Names:</h4>
                <ol>
                    <li>If you typed a name in the Character Filter box, that always wins.</li>
                    <li>Next, it checks if any name in your <code>Known.txt</code> list appears in the post title.</li>
                    <li>If that doesn't match, it checks the attachment filenames.</li>
                    <li>If nothing matches, it creates a clean folder using the post's title.</li>
                </ol>

                <h3>Subfolder Options</h3>
                <ul>
                    <li><b>Subfolder per Post:</b> Gives every post its own private subfolder inside the artist or character folder. Perfect for multi-image sets!</li>
                    <li><b>Date Prefix:</b> Sticks the post's upload date (<code>YYYY-MM-DD</code>) onto the folder or filename so everything sorts chronologically in Windows Explorer.</li>
                </ul>

                <h3>Managing Your Known Names</h3>
                <p>You can add or remove names anytime using the <b>Known Characters</b> panel on the right side of the main window, or click <b>Open Known.txt</b> to edit the plain text file in Notepad.</p>
            """),

            ("tag", "Renaming Mode (Manga / Comics)", """
                <p>Ever downloaded a comic only to find page 10 sitting before page 2? Renaming Mode fixes that completely.</p>

                <h3>Chronological Downloads</h3>
                <p>When <b>Renaming Mode</b> is turned on, the app automatically downloads posts in reverse—from the <b>oldest post to the newest</b>. That way, chapters and pages land on your drive in natural reading order.</p>

                <h3>Pick Your Favorite Naming Style</h3>
                <ul>
                    <li><b>Post Title:</b> Names files after the post title with clean sequential numbers (e.g. <code>Chapter 1_1.jpg</code>, <code>Chapter 1_2.jpg</code>).</li>
                    <li><b>Date + Original:</b> Prepends the upload date to the creator's original file name.</li>
                    <li><b>Date + Title:</b> Combines the upload date with the post title.</li>
                    <li><b>Post ID:</b> Uses Pawchive's unique post number plus the file index.</li>
                    <li><b>Date Based:</b> Simple, numbered files like <code>001.jpg</code>, <code>002.jpg</code>, with an optional prefix like "Chapter 1 ".</li>
                    <li><b>Custom Format:</b> Want complete control? Build your own pattern using placeholders like:
                        <br><code>{id}</code> - Post ID
                        <br><code>{creator_name}</code> - Artist name
                        <br><code>{title}</code> - Post title
                        <br><code>{published}</code> - Upload date
                        <br><code>{name}</code> - Original file name
                    </li>
                </ul>
            """),

            ("star", "Pawchive Favorites", """
                <p>Got a long list of followed creators on Pawchive? You can browse and download from them directly inside the app.</p>

                <h3>How to Connect Your Favorites</h3>
                <ol>
                    <li><b>Grab Your Cookie:</b> Log in to Pawchive in your web browser. Check <b>Use Cookie</b> in the downloader and paste in your session cookie (or point to your exported <code>cookies.txt</code>).</li>
                    <li><b>Open the Favorites Tab:</b> Click the <b>Favorites</b> button in the sidebar.</li>
                    <li><b>Fetch Your Lists:</b> Hit <b>Fetch Artists</b> to load creators you follow, or head to <b>Favorite Posts</b> and hit <b>Fetch Posts</b> to view your saved bookmarks.</li>
                    <li><b>Select & Queue:</b> Use the search bar to find who you're looking for, tick the checkboxes, and hit <b>Download Selected</b> to push them all straight to your download queue.</li>
                </ol>

                <h3>Where Should Files Go?</h3>
                <ul>
                    <li><b>Selected Location:</b> Pours everything into your main download directory.</li>
                    <li><b>Artist Folders:</b> Automatically creates a separate subfolder for each artist so your collection stays neatly organized.</li>
                </ul>
            """),

            ("sync", "Rate Limits & Auto-Retry", """
                <p>There's nothing more frustrating than starting an overnight download and waking up to find it got stuck on file 5 because of a temporary connection glitch. We built in several safety nets so you don't have to babysit your downloads.</p>

                <h3>Smart 429 Cooldowns</h3>
                <p>If Pawchive's servers tell the app to slow down (HTTP 429 Too Many Requests), the app handles it gracefully:</p>
                <ul>
                    <li>It catches the response immediately instead of crashing or erroring out.</li>
                    <li>It pauses active download workers and waits out an automatic cooldown.</li>
                    <li>Once the timer expires, it smoothly resumes right where it left off.</li>
                </ul>

                <h3>Wi-Fi & Network Drops</h3>
                <p>If your internet cuts out or you switch networks, the app pauses worker threads, pings the server every few seconds until connection is restored, and then gets right back to downloading.</p>

                <h3>Automatic Retries at the End</h3>
                <p>If a few files ran into random timeouts or server hiccups during a large run, the app collects them into a quiet retry pass right at the end of the session. You'll see a clean summary in the log showing what was recovered.</p>
            """),

            ("user", "Creators & Posts Explorers", """
                <p>Think of these tabs as your personal offline library browser for Pawchive.</p>

                <h3>Creators Tab</h3>
                <ul>
                    <li>Browse every creator stored in your local cache (<code>creators.json</code>).</li>
                    <li>Search creators by name or service.</li>
                    <li>Quickly send creators to the download queue or check them for new updates with one click.</li>
                </ul>

                <h3>Posts Tab</h3>
                <ul>
                    <li>Browse individual posts for any creator in a clean grid or list view.</li>
                    <li>Preview thumbnails, read post descriptions, and check attachment counts.</li>
                    <li>Select and download just the specific posts you want without having to pull down the creator's entire gallery.</li>
                </ul>
            """),

            ("visual_sort", "Visual Sort & AI Tools", """
                <p>Downloaded a huge pile of art and want to sort it without spending hours moving files by hand? Let your computer's AI do the heavy lifting.</p>

                <h3>Offline AI Vision</h3>
                <ul>
                    <li><b>Runs 100% Locally:</b> The AI vision models run right on your machine—no photos are ever uploaded anywhere.</li>
                    <li><b>Character & Tag Detection:</b> Automatically recognizes characters, art styles, and content tags in your images.</li>
                    <li><b>Automated Sorting:</b> Moves or copies images into categorized folders based on detection confidence thresholds.</li>
                    <li><b>Model Updates:</b> Check for and update vision models directly from the Visual Sort tab.</li>
                </ul>
            """)
        ]
