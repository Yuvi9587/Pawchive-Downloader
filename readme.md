# Pawchive Downloader

A desktop app for downloading and organizing content from Pawchive.

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/pawchive/pawchive-downloader)
[![Python](https://img.shields.io/badge/python-3.10+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![GUI](https://img.shields.io/badge/GUI-PySide6%20%2F%20Qt-41CD52.svg?logo=qt&logoColor=white)](https://wiki.qt.io/Qt_for_Python)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Pawchive](https://img.shields.io/badge/Supported-Pawchive-purple.svg)](https://pawchive.pw)

---

## Overview

Pawchive Downloader is a desktop app for archiving content from **Pawchive** (`pawchive.st` and `pawchive.pw`). It's written in Python with PySide6 (Qt), has a dark interface, and uses a download engine that can handle big creator archives without choking.

It works for pretty much any kind of content: artwork, photo sets, videos, audio, and text posts. Downloads run in parallel, rate limits are handled for you, anything that fails gets retried at the end, and there's an offline AI tool that can group visually similar images.

---

## Features

- **Multithreaded downloads**: Grab multiple files and posts at the same time. You can set the number of worker threads, and each post can use chunked streams.
- **Rate-limit handling**: When the server sends back an HTTP 429, the app backs off with exponential delays and a bit of jitter, so you don't get blocked or lose requests.
- **Session recovery**: If the app closes unexpectedly or your PC restarts, your download state is saved. Hit restore and it picks up where it left off.
- **End-of-session retry**: Network drops and socket timeouts are tracked during a batch and retried automatically once the session finishes.
- **Duplicate prevention**: A `Known.txt` file keeps track of what you've already downloaded, so you won't grab the same thing twice across sessions.
- **Manga / comic ordering**: Posts get sorted from oldest to newest before downloading, and pages are renamed in order (by `post_title`, `date_based`, or a global counter) so they read correctly.
- **Filtering**:
  - Filter by media type: All, Images/GIFs, Videos, Archives, Audio, External Links, or Text Posts.
  - Match character keywords against the post title, filename, both, or comments.
  - Skip files by keyword or by minimum file size (for example, skip anything under a certain number of MB).
  - Clean up filenames by stripping out tags or keywords you don't want.
- **Cloud link extraction**: Finds external download links (Mega, Google Drive, Dropbox, Gofile) in post descriptions. You can export the URLs or download from them directly.
- **Local AI visual grouping**: Uses ONNX Runtime embeddings to cluster duplicate images, burst shots, and similar artwork. Everything runs on your machine.
- **Favorites sync**: Import your session cookie to pull in your favorited creators and bookmarked posts.

---

## Tabs

Everything lives in a sidebar with these tabs:

| Tab | Module | What it does |
| :--- | :--- | :--- |
| **Downloader** | Download | The main screen. Paste creator or post URLs, pick a save location, set your filters, and watch progress in real time. |
| **Queue** | Queue | Line up lots of creators or post URLs, reorder them, pause and resume, and let them run one after another. |
| **Creators** | Creators Directory | Browse and search creators across services (Patreon, Fanbox, Fantia, Subscribestar, Boosty, etc.) and start downloads from there. |
| **Posts** | Posts Feed | Search individual posts with thumbnail previews, filter by publish date, and download one or a batch. |
| **Auto-Sync** | Auto-Sync | Runs in the background, checks your subscribed creators for new posts every so often, and archives them automatically. |
| **Visual Sort** | AI Visual Sort | Groups similar images (burst shots, duplicates, variations) using on-device embeddings. No cloud APIs involved. |
| **Favorites** | Bookmarks Sync | Connects to your Pawchive account with a session cookie so you can sync bookmarked creators and saved posts. |
| **About** | User Guide | Built-in docs: URL formats, troubleshooting tips, keyboard shortcuts, and version info. |
| **Support** | Community & Donate | Links to the issue tracker, community channels, and ways to support the developer (Ko-fi, Patreon, Buy Me a Coffee). |

---

## Supported URL Formats

You can paste any of these into the **Downloader** tab:

- **Creator archive**:
  ```text
  https://pawchive.st/user/<service>/<creator_id>
  https://pawchive.pw/user/<service>/<creator_id>
  ```
  *Downloads every post and attachment from that creator.*

- **Single post**:
  ```text
  https://pawchive.st/<service>/user/<creator_id>/post/<post_id>
  https://pawchive.pw/<service>/user/<creator_id>/post/<post_id>
  ```
  *Downloads only the files from that one post.*

- **Batch file**:
  Type `pawchive.st` or `pawchive.pw` into the URL field. The app will read URLs line by line from `appdata/pawchive.st.txt` or `appdata/pawchive.pw.txt` and work through them in order.

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/pawchive/pawchive-downloader.git
cd pawchive-downloader
```

### 2. Set up a virtual environment (recommended)

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

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run it

```bash
python main.py
```

---

## Tips & Configuration

### Threads
The default is **2 threads**, which is the safest option for avoiding rate limits on Pawchive. You can raise it, but going too high can eat up your bandwidth or trigger temporary HTTP 429 errors. The app does back off automatically, but keeping the thread count low usually gets you through a batch faster in the end, with fewer interruptions.

### Using your cookie for Favorites
To sync bookmarked creators and posts in the **Favorites** tab:
1. Log in to your account on `pawchive.pw` or `pawchive.st` in your browser.
2. Open Developer Tools (`F12` or `Ctrl+Shift+I`) and go to the **Application** or **Storage** tab.
3. Under **Cookies**, copy the value of the session cookie (`session`, or whichever auth cookie your account uses).
4. Paste it into the cookie field in the **Favorites** or **Downloader** tab, or save it in a `cookies.txt` file and load it with the Browse button.

### Manga / comic mode
If you're archiving a comic or manga, turn on **Manga/Comic mode** in the Downloader options:
- Posts are downloaded from oldest to newest.
- Files are numbered in order or prefixed with the post date, so image viewers and comic readers show the pages in the right sequence.

### Visual Sort
The **Visual Sort** tool runs entirely on your computer:
- It reads images from a local folder and uses ONNX Runtime models to extract feature embeddings.
- Similar images (alternate colorings, crops, sketch-to-final versions, burst shots) get grouped into folders.
- Nothing gets uploaded anywhere.

---

## Project Structure

```text
Pawchive-Downloader/
├── assets/                  # Icons, SVGs, and other UI assets
├── data/                    # Local storage, cache, and sync configs
├── src/
│   ├── config/              # Settings, constants, and versioning
│   ├── core/                # Networking, rate limiting, and download logic
│   ├── parsers/             # Pawchive HTML and JSON API parsers
│   ├── ui/                  # PySide6 tabs, widgets, and dialogs
│   │   ├── about_tab.py     # Docs and user guide tab
│   │   ├── auto_sync_tab.py # Subscribed creator sync tab
│   │   ├── creators_tab.py  # Creator directory and search tab
│   │   ├── downloader_tab.py# Main download screen
│   │   ├── favorites_tab.py # Account favorites and bookmarks tab
│   │   ├── posts_tab.py     # Post feed and search tab
│   │   ├── queue_tab.py     # Batch download queue tab
│   │   ├── support_tab.py   # Community and sponsorship tab
│   │   └── visual_sort_tab.py # On-device image clustering tab
│   └── utils/               # File helpers, hash registers, and theme managers
├── LICENSE                  # MIT License
├── main.py                  # App entry point
├── readme.md                # You're reading it
└── requirements.txt         # Python dependencies
```

---

## Contributing

Bug reports, suggestions, and pull requests are all welcome.
1. Fork the repo.
2. Create a feature branch:
   ```bash
   git checkout -b feature/improvement-name
   ```
3. Commit your changes with clear messages.
4. Push your branch and open a pull request.

---

## Support

If Pawchive Downloader has been useful to you, you can help keep it maintained and growing:

- **Ko-fi**: [Support on Ko-fi](https://ko-fi.com/)
- **Patreon**: [Support on Patreon](https://patreon.com/)
- **Buy Me a Coffee**: [Buy Me a Coffee](https://buymeacoffee.com/)

The **Support** tab inside the app also has donation and community links.

---

## License

This project is licensed under the [MIT License](LICENSE).

---

## Disclaimer

Pawchive Downloader is an independent open-source tool made for personal backups and organizing your media. It isn't affiliated with, endorsed by, or connected to Pawchive. You're responsible for following the terms of service of any site or service you use it with.
