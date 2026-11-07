# Pawchive Downloader

A modern, high-performance desktop archiving and media organization suite built for Pawchive.

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/pawchive/pawchive-downloader)
[![Python](https://img.shields.io/badge/python-3.10+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![GUI](https://img.shields.io/badge/GUI-PySide6%20%2F%20Qt-41CD52.svg?logo=qt&logoColor=white)](https://wiki.qt.io/Qt_for_Python)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Pawchive](https://img.shields.io/badge/Supported-Pawchive-purple.svg)](https://pawchive.pw)

---

## Overview

**Pawchive Downloader** is a specialized desktop application engineered for fast, reliable, and neatly organized archiving of content from **Pawchive** (`pawchive.st` and `pawchive.pw`). Built with Python and PySide6 (Qt), it provides a clean, modern dark interface paired with an enterprise-grade download engine designed to handle large-scale creator archives smoothly.

Whether backing up artwork, high-resolution photo sets, videos, audio, or text posts, Pawchive Downloader automates the process with concurrent multithreading, intelligent rate-limit handling, automatic retry sweeps, and on-device AI visual sorting.

---

## Key Features

- **High-Speed Multithreaded Engine**: Download multiple files and posts concurrently with configurable worker threads and per-post chunked streams.
- **Rate-Limit Resilience & Auto-Backoff**: Dynamic exponential backoff and jitter handle HTTP 429 responses smoothly, preventing IP bans and dropped requests.
- **Interrupted Session Recovery**: Unexpected shutdowns or system restarts preserve download states. One-click session restoration lets you resume exactly where you stopped.
- **End-of-Session Retry Sweep**: Any transient network drops or socket timeouts during a batch are tracked and automatically retried at the conclusion of the session.
- **Deduplication with Known Registry**: The `Known.txt` database tracks downloaded items, preventing redundant downloads across different sessions.
- **Manga / Comic Chronological Renaming**: Reorders creator posts from oldest to newest before downloading, renaming pages sequentially (`post_title`, `date_based`, or global counter) to maintain correct reading order.
- **Granular Content Filtering**:
  - Filter downloads by media category: All, Images/GIFs, Videos, Archives, Audio, External Links, or Text Posts.
  - Character keyword matching with configurable scopes (Post Title, Filename, Both, or Comments).
  - Skip rules by keyword and minimum file size threshold (e.g., skip files smaller than a specified MB value).
  - Filename cleanup: Automatically strip unwanted tags or keywords from saved filenames.
- **Cloud Host Extraction**: Detects and extracts external download links (Mega, Google Drive, Dropbox, Gofile) found inside post descriptions, with options to export URLs or download directly.
- **On-Device AI Visual Grouping**: Uses local machine learning embeddings (ONNX Runtime) to cluster duplicate images, burst shots, and visually similar artwork completely offline.
- **Account Bookmarks Synchronization**: Import session cookies to sync and download your favorited creators and bookmarked posts directly.

---

## Integrated Tabs

Pawchive Downloader organizes all tools into a unified sidebar interface:

| Tab | Icon / Module | Description |
| :--- | :--- | :--- |
| **Downloader** | Download | The primary download center. Input creator or post URLs, choose destination paths, set filtering parameters, and monitor real-time download progress. |
| **Queue** | Queue | Batch queue manager. Queue up dozens of creator profiles or post URLs, reorder priority, pause/resume tasks, and process downloads sequentially. |
| **Creators** | Creators Directory | Built-in explorer to browse and search Pawchive creators across services (Patreon, Fanbox, Fantia, Subscribestar, Boosty, etc.) and launch direct downloads. |
| **Posts** | Posts Feed | Explore and search individual posts with thumbnail previews, filter by publication date, and trigger individual or batch downloads. |
| **Auto-Sync** | Auto-Sync | Background synchronization worker that periodically checks subscribed creators for new posts and automatically archives updates. |
| **Visual Sort** | AI Visual Sort | Local AI-driven image similarity clustering. Groups burst shots, duplicate files, and variations using on-device neural embeddings without external cloud APIs. |
| **Favorites** | Bookmarks Sync | Direct integration with your Pawchive account via session cookie. Sync bookmarked creators and saved posts with one click. |
| **About** | User Guide | Interactive built-in documentation, URL syntax rules, troubleshooting tips, keyboard shortcuts, and version details. |
| **Support** | Community & Donate | Quick access to project issue trackers, community channels, and developer sponsorship options (Ko-fi, Patreon, Buy Me a Coffee). |

---

## Supported URL Formats

Pawchive Downloader accepts the following URL formats directly in the **Downloader** tab:

- **Creator Archive**:
  ```text
  https://pawchive.st/user/<service>/<creator_id>
  https://pawchive.pw/user/<service>/<creator_id>
  ```
  *Downloads all posts and attachments published by the specified creator.*

- **Individual Post**:
  ```text
  https://pawchive.st/<service>/user/<creator_id>/post/<post_id>
  https://pawchive.pw/<service>/user/<creator_id>/post/<post_id>
  ```
  *Downloads only the files and media belonging to the specific post.*

- **Batch File Trigger**:
  Enter `pawchive.st` or `pawchive.pw` in the URL input field. The application will read line-by-line URLs from `appdata/pawchive.st.txt` or `appdata/pawchive.pw.txt` and process them in order.

---

## Installation & Setup

### 1. Clone the Repository

```bash
git clone https://github.com/pawchive/pawchive-downloader.git
cd pawchive-downloader
```

### 2. Create a Virtual Environment (Recommended)

**Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**Linux / macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Launch the Application

```bash
python main.py
```

---

## Configuration & Usage Tips

### Threading Optimization
- The default thread pool size is set to **2 threads**, which is the safest setting to avoid triggering rate limits on Pawchive endpoints.
- You can adjust the thread count if needed, but excessively high thread counts can saturate bandwidth or trigger temporary HTTP 429 rate limits. The application features built-in exponential backoff, but conservative thread settings yield faster and smoother overall completion.

### Using Account Cookies for Favorites
To sync your bookmarked creators and posts in the **Favorites** tab:
1. Log into your account on `pawchive.pw` or `pawchive.st` in your web browser.
2. Open Developer Tools (`F12` or `Ctrl+Shift+I`) and navigate to the **Application** or **Storage** tab.
3. Under **Cookies**, copy the session cookie value (`session` or relevant auth cookie).
4. Paste the cookie into the **Favorites** or **Downloader** cookie field, or save it to a `cookies.txt` file and load it via the Browse button.

### Manga / Comic Reading Order
When archiving comics or manga, enable **Manga/Comic mode** in the Downloader options:
- Posts will be downloaded chronologically from oldest to newest.
- Files will be numbered sequentially or prefixed with the post date, ensuring image viewers and comic readers display pages in correct story sequence.

### Local AI Visual Sorting
The **Visual Sort** module operates 100% locally on your computer:
- It processes local image directories using ONNX Runtime models to extract feature embeddings.
- Similar images (such as alternate colorings, crops, sketch-to-final versions, or burst shots) are clustered into organized folders.
- No images or metadata are uploaded to external services.

---

## Project Structure

```text
Pawchive-Downloader/
├── assets/                  # Icons, vector SVGs, and visual UI assets
├── data/                    # Local storage, cache, and sync configurations
├── src/
│   ├── config/              # Application settings, constants, and versioning
│   ├── core/                # Core networking, rate-limiting, and download routines
│   ├── parsers/             # Pawchive HTML and JSON API response parsers
│   ├── ui/                  # PySide6 user interface tabs, widgets, and dialogs
│   │   ├── about_tab.py     # Interactive documentation and user guide tab
│   │   ├── auto_sync_tab.py # Subscribed creator synchronization tab
│   │   ├── creators_tab.py  # Creator directory search and explorer tab
│   │   ├── downloader_tab.py# Main download center interface
│   │   ├── favorites_tab.py # Account favorites and bookmarks tab
│   │   ├── posts_tab.py     # Post feed and search tab
│   │   ├── queue_tab.py     # Batch download queue manager tab
│   │   ├── support_tab.py   # Community and sponsorship tab
│   │   └── visual_sort_tab.py # On-device AI image clustering tab
│   └── utils/               # File helpers, hash registers, and theme managers
├── LICENSE                  # MIT License
├── main.py                  # Application entry point
├── readme.md                # Project documentation
└── requirements.txt         # Python package dependencies
```

---

## Contributing

Contributions, bug reports, and suggestions are welcome.
1. Fork the repository.
2. Create a dedicated feature branch:
   ```bash
   git checkout -b feature/improvement-name
   ```
3. Commit your changes with clear, descriptive commit messages.
4. Push your branch and open a Pull Request.

---

## Support & Sponsorship

If Pawchive Downloader helps you organize and back up your collections, consider supporting continued maintenance and feature development:

- **Ko-fi**: [Support on Ko-fi](https://ko-fi.com/)
- **Patreon**: [Support on Patreon](https://patreon.com/)
- **Buy Me a Coffee**: [Buy Me a Coffee](https://buymeacoffee.com/)

You can also access direct donation and community links inside the application under the **Support** tab.

---

## License

This project is licensed under the [MIT License](LICENSE).

---

## Disclaimer

Pawchive Downloader is an independent open-source utility designed for personal backup and media organization. It is not affiliated with, endorsed by, or associated with Pawchive. Users are responsible for complying with the terms of service of the websites and services they access with this tool.
