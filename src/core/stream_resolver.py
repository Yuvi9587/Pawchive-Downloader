# ==============================================================================
#  CloudStreamResolver — Resolves Mega / Google Drive / Dropbox links to
#  direct streamable URLs without downloading to disk.
#
#  Backends:
#    • Dropbox  — simple URL rewrite (synchronous, no network)
#    • Google Drive — extract file-ID, fetch confirm token for large files
#    • MEGA      — AES-128 CTR decrypt via pycryptodome + local HTTP proxy
# ==============================================================================

import re
import os
import threading
import base64
import struct
import hashlib
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse, urljoin

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, QMetaObject, Qt


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _base64_url_decode(data: str) -> bytes:
    """MEGA-style base64 URL decode (uses - and _ instead of + and /)."""
    data = data.replace("-", "+").replace("_", "/")
    pad = (4 - len(data) % 4) % 4
    return base64.b64decode(data + "=" * pad)


def _extract_drive_file_id(url: str) -> str | None:
    """Extract the file ID from various Google Drive URL formats."""
    # Folders and Google Docs/Sheets are not streamable video files
    if "/folders/" in url or "/folder/" in url or "docs.google.com" in url.lower():
        return None
    # /file/d/<id>
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    # ?id=<id>  or  &id=<id>
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    # /open?id=<id>
    m = re.search(r"/open\?id=([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  MEGA local HTTP proxy (AES-128 CTR decrypt on the fly)
# ─────────────────────────────────────────────────────────────────────────────

class _MegaProxy:
    """
    Downloads the MEGA encrypted file into memory in a background thread,
    decrypts it on-the-fly using AES-128 CTR, and serves it via a minimal
    single-request HTTP/1.1 server on 127.0.0.1 so QMediaPlayer can open it.

    Usage:
        proxy = _MegaProxy(mega_url)
        proxy.start()          # fetches meta, starts listen socket
        stream_url = proxy.local_url   # e.g. "http://127.0.0.1:PORT"
        # QMediaPlayer plays from stream_url
        # When done: proxy.stop()
    """

    def __init__(self, mega_url: str, on_progress=None):
        self.mega_url = mega_url
        self.on_progress = on_progress
        self.local_url: str = ""
        self._error: str = ""
        self._ready_event = threading.Event()
        self._stop_event = threading.Event()
        self._server_thread: threading.Thread | None = None
        self._mime = "video/mp4"
        self._data: bytes | None = None   # fully decrypted data (in RAM only)

    # ── Public API ──────────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Fetches MEGA metadata, decrypts, then starts the local server.
        Blocks until the proxy is ready (or fails).  Returns True on success.
        """
        try:
            self._fetch_and_decrypt()
        except Exception as exc:
            self._error = str(exc)
            return False

        self._server_thread = threading.Thread(target=self._serve, daemon=True)
        self._server_thread.start()
        self._ready_event.wait(timeout=5)
        return bool(self.local_url)

    def stop(self):
        self._stop_event.set()
        self._data = None   # release RAM

    @property
    def error(self) -> str:
        return self._error

    # ── MEGA fetch + AES-128 CTR decrypt ────────────────────────────────────

    def _fetch_and_decrypt(self):
        import requests
        from Crypto.Cipher import AES

        parsed = urlparse(self.mega_url)
        # Support both mega.nz/file/HASH#KEY and mega.nz/#!HASH!KEY
        fragment = parsed.fragment.lstrip("!")
        if "!" in fragment:
            # Legacy: #!HASH!KEY
            handle, enc_key_b64 = fragment.split("!", 1)
        else:
            # Modern: /file/HASH#KEY
            handle = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            enc_key_b64 = fragment

        enc_key = _base64_url_decode(enc_key_b64)
        if len(enc_key) == 32:
            # Folder key — take first 16 bytes XOR second 16
            a = struct.unpack(">IIII", enc_key[:16])
            b = struct.unpack(">IIII", enc_key[16:])
            key_parts = tuple(a[i] ^ b[i] for i in range(4))
            aes_key = struct.pack(">IIII", *key_parts)
            iv = enc_key[16:24] + b"\x00" * 8
        elif len(enc_key) == 16:
            aes_key = enc_key
            iv = b"\x00" * 16
        else:
            raise ValueError(f"Unexpected MEGA key length: {len(enc_key)}")

        # Request MEGA API for download URL
        api_url = "https://g.api.mega.co.nz/cs"
        payload = [{"a": "g", "g": 1, "p": handle}]
        resp = requests.post(api_url, json=payload, timeout=20)
        resp.raise_for_status()
        result = resp.json()
        if isinstance(result, list):
            result = result[0]
        if isinstance(result, int):
            raise RuntimeError(f"MEGA API error code: {result}")

        dl_url = result.get("g")
        size = result.get("s", 0)
        if not dl_url:
            raise RuntimeError("MEGA API did not return a download URL")

        # Guard against gigantic files exhausting system RAM
        if size > 600 * 1024 * 1024:
            raise RuntimeError(
                f"MEGA file is too large for in-memory streaming ({size / (1024*1024):.1f} MB). "
                f"Please queue it in the Downloader tab instead."
            )

        # Determine MIME type from attributes
        attr_b64 = result.get("at", "")
        if attr_b64:
            try:
                attr_enc = _base64_url_decode(attr_b64)
                # AES-128 CBC decrypt
                cipher = AES.new(aes_key, AES.MODE_CBC, b"\x00" * 16)
                attr_dec = cipher.decrypt(attr_enc + b"\x00" * (16 - len(attr_enc) % 16) if len(attr_enc) % 16 else attr_enc)
                attr_str = attr_dec.decode("utf-8", errors="replace").rstrip("\x00")
                if attr_str.startswith("MEGA"):
                    attr_json = __import__("json").loads(attr_str[4:])
                    name = attr_json.get("n", "")
                    ext = os.path.splitext(name)[1].lower()
                    mime_map = {
                        ".mp4": "video/mp4", ".mkv": "video/x-matroska",
                        ".webm": "video/webm", ".mov": "video/quicktime",
                        ".m4v": "video/mp4", ".avi": "video/x-msvideo",
                    }
                    self._mime = mime_map.get(ext, "video/mp4")
            except Exception:
                pass   # keep default MIME

        # Download encrypted data in chunks, respecting stop event
        dl_resp = requests.get(dl_url, timeout=60, stream=True)
        dl_resp.raise_for_status()
        total_size = size or int(dl_resp.headers.get("content-length", 0))
        downloaded = 0
        last_pct = 0
        chunks = []
        for chunk in dl_resp.iter_content(chunk_size=131072):
            if self._stop_event.is_set():
                dl_resp.close()
                raise RuntimeError("MEGA download cancelled")
            if chunk:
                chunks.append(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = int(downloaded * 100 / total_size)
                    if pct >= last_pct + 10:
                        last_pct = pct
                        if self.on_progress:
                            self.on_progress(f"Buffering MEGA video ({pct}%)...")
        enc_data = b"".join(chunks)

        if self.on_progress:
            self.on_progress("Decrypting MEGA stream...")

        # Decrypt: AES-128 CTR (MEGA's counter is big-endian, 128-bit)
        # MEGA uses a custom CTR: nonce is first 8 bytes of iv, counter starts at 0
        # but their IV layout means: iv = key[16:24] + b"\x00"*8 for files
        nonce = iv[:8]
        cipher_ctr = AES.new(aes_key, AES.MODE_CTR,
                             nonce=nonce,
                             initial_value=0)
        self._data = cipher_ctr.decrypt(enc_data)

    # ── Minimal HTTP/1.1 server ──────────────────────────────────────────────

    def _serve(self):
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(5)
        srv.settimeout(1.0)
        self.local_url = f"http://127.0.0.1:{port}"
        self._ready_event.set()

        total = len(self._data) if self._data else 0
        while not self._stop_event.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            try:
                self._handle_conn(conn, total)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        srv.close()

    def _handle_conn(self, conn, total: int):
        import socket
        conn.settimeout(10)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = conn.recv(4096)
            if not chunk:
                return
            raw += chunk

        head = raw.split(b"\r\n\r\n")[0].decode("utf-8", errors="replace")
        # Parse Range header
        start, end = 0, total - 1
        range_match = re.search(r"Range:\s*bytes=(\d+)-(\d*)", head, re.IGNORECASE)
        if range_match:
            start = int(range_match.group(1))
            if range_match.group(2):
                end = int(range_match.group(2))
            status = "206 Partial Content"
        else:
            status = "200 OK"

        end = min(end, total - 1)
        length = end - start + 1
        body = self._data[start:end + 1] if self._data else b""

        headers = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: {self._mime}\r\n"
            f"Content-Length: {length}\r\n"
            f"Accept-Ranges: bytes\r\n"
            f"Content-Range: bytes {start}-{end}/{total}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        conn.sendall(headers.encode() + body)


# ─────────────────────────────────────────────────────────────────────────────
#  Background resolve task (runs in Qt thread pool)
# ─────────────────────────────────────────────────────────────────────────────

class _ResolveSignals(QObject):
    resolved = Signal(str, str)        # original_url, stream_url
    failed   = Signal(str, str)        # original_url, error_message
    progress = Signal(str, str)        # original_url, status_message


class _StreamResolveTask(QRunnable):
    def __init__(self, url: str, signals: _ResolveSignals):
        super().__init__()
        self.url = url
        self.signals = signals
        self.setAutoDelete(True)
        self._proxy: _MegaProxy | None = None

    def run(self):
        url = self.url
        try:
            if CloudStreamResolver._is_dropbox(url):
                self.signals.progress.emit(url, "Resolving Dropbox link...")
                stream_url = CloudStreamResolver._resolve_dropbox(url)
                self.signals.resolved.emit(url, stream_url)

            elif CloudStreamResolver._is_drive(url):
                self.signals.progress.emit(url, "Resolving Google Drive link...")
                stream_url = CloudStreamResolver._resolve_drive(url)
                self.signals.resolved.emit(url, stream_url)

            elif CloudStreamResolver._is_mega(url):
                self.signals.progress.emit(url, "Connecting to MEGA...")
                proxy = _MegaProxy(url, on_progress=lambda msg: self.signals.progress.emit(url, msg))
                ok = proxy.start()
                if ok:
                    self._proxy = proxy   # keep alive until CloudStreamResolver cleans up
                    self.signals.resolved.emit(url, proxy.local_url)
                else:
                    self.signals.failed.emit(url, proxy.error or "MEGA proxy failed to start")
            else:
                self.signals.failed.emit(url, "URL is not a supported cloud link")

        except Exception as exc:
            self.signals.failed.emit(url, str(exc))


# ─────────────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────────────

class CloudStreamResolver(QObject):
    """
    Resolves Mega / Google Drive / Dropbox links to direct streamable URLs.
    Resolution runs in a Qt thread pool so the UI never blocks.

    Signals:
        resolved(original_url, stream_url)
        failed(original_url, error_message)
        progress(original_url, status_message)   # intermediate status updates
    """

    resolved = Signal(str, str)
    failed   = Signal(str, str)
    progress = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active_task: _StreamResolveTask | None = None
        self._active_proxy: _MegaProxy | None = None   # keeps proxy alive

    # ── Public ───────────────────────────────────────────────────────────────

    def resolve(self, url: str):
        """Start background resolution of a cloud URL."""
        # Clean up old proxy if any
        if self._active_proxy:
            self._active_proxy.stop()
            self._active_proxy = None

        signals = _ResolveSignals(parent=self)
        signals.resolved.connect(self._on_task_resolved)
        signals.failed.connect(self._on_task_failed)
        signals.progress.connect(self._on_task_progress)
        self._active_signals = signals

        task = _StreamResolveTask(url, signals)
        self._active_task = task
        QThreadPool.globalInstance().start(task)

    def stop(self):
        """Stop any running proxy and release RAM."""
        if self._active_proxy:
            self._active_proxy.stop()
            self._active_proxy = None

    # ── Internal slots ───────────────────────────────────────────────────────

    def _on_task_resolved(self, original_url: str, stream_url: str):
        # If this was a MEGA proxy, store the proxy object to keep it alive
        if self._active_task and self._active_task._proxy:
            self._active_proxy = self._active_task._proxy
        self.resolved.emit(original_url, stream_url)

    def _on_task_failed(self, original_url: str, error: str):
        self.failed.emit(original_url, error)

    def _on_task_progress(self, original_url: str, message: str):
        self.progress.emit(original_url, message)

    # ── Static helpers ───────────────────────────────────────────────────────

    @staticmethod
    def is_cloud_link(url: str) -> bool:
        """Returns True if the URL is a Mega, Google Drive, or Dropbox link."""
        return (
            CloudStreamResolver._is_mega(url)
            or CloudStreamResolver._is_drive(url)
            or CloudStreamResolver._is_dropbox(url)
        )

    @staticmethod
    def _is_mega(url: str) -> bool:
        if "/folder/" in url or "/#F!" in url or "#F!" in url:
            return False
        return bool(re.search(r"mega\.nz", url, re.IGNORECASE))

    @staticmethod
    def _is_drive(url: str) -> bool:
        if "/folders/" in url or "/folder/" in url or "docs.google.com" in url.lower():
            return False
        return bool(re.search(r"drive\.google\.com", url, re.IGNORECASE))

    @staticmethod
    def _is_dropbox(url: str) -> bool:
        if "/scl/fo/" in url or "/sh/" in url:
            return False
        return bool(re.search(r"dropbox\.com", url, re.IGNORECASE))

    @staticmethod
    def _resolve_dropbox(url: str) -> str:
        """Rewrites a Dropbox share URL to a direct stream URL (no download)."""
        # Replace www.dropbox.com with dl.dropboxusercontent.com
        url = re.sub(r"(?i)https?://(?:www\.)?dropbox\.com", "https://dl.dropboxusercontent.com", url)
        # Remove dl=0 / dl=1 params and add raw=1
        url = re.sub(r"[?&]dl=\d", "", url)
        url = re.sub(r"[?&]raw=\d", "", url)
        separator = "&" if "?" in url else "?"
        url = url + separator + "raw=1"
        return url

    @staticmethod
    def _resolve_drive(url: str) -> str:
        """Resolves a Google Drive share URL to a direct stream URL without buffering video in memory."""
        import requests as _req

        if "/folders/" in url or "/folder/" in url:
            raise ValueError("Link is a Google Drive folder, not a direct video file.")

        file_id = _extract_drive_file_id(url)
        if not file_id:
            raise ValueError(f"Could not extract Google Drive file ID from: {url}")

        stream_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=1"

        try:
            session = _req.Session()
            # CRITICAL: stream=True prevents downloading multi-gigabyte video files into RAM!
            resp = session.get(
                f"https://drive.google.com/uc?export=download&id={file_id}",
                allow_redirects=True,
                stream=True,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )

            # If directly redirected to Google CDN download stream
            if resp.url and "drive.google.com/uc" not in resp.url and "drive.google.com" not in resp.url:
                stream_url = resp.url
                resp.close()
                return stream_url

            # Look for confirm token in cookies or warning HTML
            confirm = None
            for cookie in session.cookies:
                if "download_warning" in cookie.name:
                    confirm = cookie.value
                    break

            if not confirm:
                chunk = next(resp.iter_content(chunk_size=32768), b"").decode("utf-8", errors="replace")
                m = re.search(r'confirm=([^&"\']+)', chunk)
                if m:
                    confirm = m.group(1)
                if not confirm:
                    m = re.search(r'name="confirm"\s+value="([^"]+)"', chunk)
                    if m:
                        confirm = m.group(1)

            resp.close()

            if confirm:
                stream_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm={confirm}"
        except Exception:
            pass

        return stream_url
