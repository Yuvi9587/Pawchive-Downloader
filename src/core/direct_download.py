import os
import re
import time
import urllib.parse
import requests
from PySide6.QtCore import QThread, Signal
from ..utils.file_utils import clean_filename

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False


def is_direct_file_url(url: str) -> bool:
    """
    Determines if the given URL points directly to a media or attachment file
    rather than an API endpoint, creator profile, or post page.
    """
    if not url or not isinstance(url, str):
        return False
        
    url_stripped = url.strip()
    try:
        parsed = urllib.parse.urlparse(url_stripped)
        path = parsed.path.lower()
    except Exception:
        return False

    media_extensions = (
        '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.avif', '.svg',
        '.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.flv',
        '.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus',
        '.zip', '.rar', '.7z', '.tar', '.gz', '.pdf', '.psd', '.clip', '.sai', '.cbr', '.cbz',
        '.txt', '.rtf', '.epub'
    )

    if any(path.endswith(ext) for ext in media_extensions):
        return True

    qs = urllib.parse.parse_qs(parsed.query)
    if 'f' in qs and qs['f']:
        f_val = qs['f'][0].lower()
        if any(f_val.endswith(ext) for ext in media_extensions):
            return True

    # Kemono / Coomer / Pawchive data endpoints: /data/xx/yy/hash...
    if '/data/' in path and any(d in parsed.netloc.lower() for d in ('pawchive', 'kemono', 'coomer')):
        return True

    return False


class DirectFileDownloadThread(QThread):
    """
    A dedicated background thread for downloading individual files or attachments
    directly to disk with progress reporting, pause, and cancellation support.
    """
    progress_signal = Signal(str)
    file_progress_signal = Signal(str, object)
    overall_progress_signal = Signal(int, int)
    finished_signal = Signal(int, int, bool, list)

    def __init__(
        self,
        url: str,
        output_dir: str,
        filename: str = None,
        pause_event=None,
        cancellation_event=None,
        cookies=None,
        proxies=None,
        fallback_urls=None,
        parent=None
    ):
        super().__init__(parent)
        self.url = url
        self.output_dir = output_dir
        self.custom_filename = filename
        self.pause_event = pause_event
        self.cancellation_event = cancellation_event
        self.cookies = cookies or {}
        self.proxies = proxies
        self.fallback_urls = fallback_urls or []
        self._is_paused = False

    def pause(self):
        self._is_paused = True

    def resume(self):
        self._is_paused = False

    def cancel(self):
        self.requestInterruption()

    def _is_cancelled(self) -> bool:
        if self.isInterruptionRequested():
            return True
        if self.cancellation_event and self.cancellation_event.is_set():
            return True
        return False

    def _wait_if_paused(self) -> bool:
        """Pauses the thread loop while pause flag is active. Returns False if cancelled while paused."""
        while (self.pause_event and self.pause_event.is_set()) or self._is_paused:
            if self._is_cancelled():
                return False
            time.sleep(0.2)
        return not self._is_cancelled()

    def _get_headers_for_url(self, target_url: str) -> dict:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*"
        }
        low = target_url.lower()
        if "kemono" in low:
            headers["Referer"] = "https://kemono.su/"
        elif "coomer" in low:
            headers["Referer"] = "https://coomer.su/"
        elif "pawchive" in low:
            headers["Referer"] = "https://pawchive.pw/"
            headers["User-Agent"] = "PawchiveClient/1.0.5 (Desktop App)"
        return headers

    def _resolve_filename(self, target_url: str) -> str:
        if self.custom_filename:
            return clean_filename(self.custom_filename)

        try:
            parsed = urllib.parse.urlparse(target_url)
            qs = urllib.parse.parse_qs(parsed.query)
            if 'f' in qs and qs['f']:
                name = urllib.parse.unquote(qs['f'][0])
                if name:
                    return clean_filename(name)

            path_base = os.path.basename(parsed.path)
            if path_base:
                return clean_filename(urllib.parse.unquote(path_base))
        except Exception:
            pass

        return clean_filename(f"download_{int(time.time())}.bin")

    def run(self):
        if self._is_cancelled():
            self.finished_signal.emit(0, 0, True, [])
            return

        urls_to_try = [self.url] + [u for u in self.fallback_urls if u and u != self.url]
        filename = self._resolve_filename(self.url)

        try:
            os.makedirs(self.output_dir, exist_ok=True)
        except Exception as e:
            self.progress_signal.emit(f"❌ Could not create destination folder '{self.output_dir}': {e}")
            self.finished_signal.emit(0, 0, False, [])
            return

        target_path = os.path.join(self.output_dir, filename)
        part_path = target_path + ".part"

        self.progress_signal.emit(f"⬇️ Starting direct download: '{filename}'")
        self.overall_progress_signal.emit(1, 0)
        self.file_progress_signal.emit(filename, (0, 0))

        downloaded_successfully = False
        skipped_existing = False
        last_error = ""

        for current_url in urls_to_try:
            if self._is_cancelled():
                break

            headers = self._get_headers_for_url(current_url)

            # Step 1: Probe remote size & check if target file already exists and is identical
            total_size = 0
            try:
                head_res = requests.head(
                    current_url,
                    headers=headers,
                    timeout=12,
                    proxies=self.proxies,
                    cookies=self.cookies,
                    allow_redirects=True
                )
                if head_res.status_code == 200:
                    total_size = int(head_res.headers.get("content-length", 0))
            except Exception:
                pass

            if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                local_size = os.path.getsize(target_path)
                if total_size > 0 and local_size == total_size:
                    size_mb = local_size / (1024 * 1024)
                    self.progress_signal.emit(f"ℹ️ File '{filename}' already exists and matches remote size ({size_mb:.2f} MB). Skipping.")
                    self.overall_progress_signal.emit(1, 1)
                    self.file_progress_signal.emit(filename, (local_size, local_size))
                    self.finished_signal.emit(0, 1, False, [])
                    return

            # Step 2: Attempt streaming download
            success, err = self._perform_stream_download(current_url, headers, part_path, filename, total_size)
            if success:
                # Rename part to final target
                try:
                    if os.path.exists(target_path):
                        os.remove(target_path)
                    os.replace(part_path, target_path)
                    final_size = os.path.getsize(target_path)
                    final_mb = final_size / (1024 * 1024)
                    self.progress_signal.emit(f"✅ Successfully downloaded '{filename}' ({final_mb:.2f} MB) -> '{target_path}'")
                    self.overall_progress_signal.emit(1, 1)
                    self.file_progress_signal.emit(filename, (final_size, final_size))
                    downloaded_successfully = True
                    break
                except Exception as e:
                    last_error = f"Failed to rename part file: {e}"
            else:
                last_error = err
                if self._is_cancelled():
                    break
                self.progress_signal.emit(f"⚠️ Direct download attempt failed for {current_url}: {err}")

        # Post download handling
        if self._is_cancelled():
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except Exception:
                    pass
            self.progress_signal.emit(f"🛑 Download of '{filename}' cancelled by user.")
            self.finished_signal.emit(0, 0, True, [])
        elif downloaded_successfully:
            self.finished_signal.emit(1, 0, False, [])
        else:
            self.progress_signal.emit(f"❌ Failed to download '{filename}': {last_error or 'All URLs exhausted'}")
            self.finished_signal.emit(0, 0, False, [])

    def _perform_stream_download(self, target_url: str, headers: dict, part_path: str, filename: str, total_size: int) -> tuple:
        """
        Streams file chunks to part_path with pause, cancellation, and curl_cffi fallback on 403.
        Returns (success: bool, error_message: str).
        """
        # Determine resume support
        existing_bytes = 0
        open_mode = "wb"
        if os.path.exists(part_path) and os.path.getsize(part_path) > 0:
            existing_bytes = os.path.getsize(part_path)
            if total_size > 0 and existing_bytes < total_size:
                headers = dict(headers)
                headers["Range"] = f"bytes={existing_bytes}-"
                open_mode = "ab"
            else:
                existing_bytes = 0

        # Try standard requests
        try:
            res = requests.get(
                target_url,
                headers=headers,
                stream=True,
                timeout=25,
                proxies=self.proxies,
                cookies=self.cookies,
                allow_redirects=True
            )

            # If range was not accepted, restart from beginning
            if open_mode == "ab" and res.status_code == 200:
                open_mode = "wb"
                existing_bytes = 0

            if res.status_code in (200, 206):
                content_len = int(res.headers.get("content-length", 0))
                expected_total = (existing_bytes + content_len) if content_len > 0 else total_size
                return self._write_stream(res.iter_content(chunk_size=65536), part_path, open_mode, filename, existing_bytes, expected_total)

            # Cloudflare 403 fallback with curl_cffi
            if res.status_code == 403 and CURL_CFFI_AVAILABLE:
                try:
                    cffi_res = cffi_requests.get(
                        target_url,
                        headers=headers,
                        stream=True,
                        impersonate="chrome120",
                        timeout=30,
                        proxies=self.proxies,
                        cookies=self.cookies
                    )
                    if cffi_res.status_code in (200, 206):
                        c_len = int(cffi_res.headers.get("content-length", 0))
                        expected_total = (existing_bytes + c_len) if c_len > 0 else total_size
                        return self._write_stream(cffi_res.iter_content(chunk_size=65536), part_path, open_mode, filename, existing_bytes, expected_total)
                except Exception as e:
                    return False, f"curl_cffi error: {e}"

            return False, f"HTTP {res.status_code}"
        except Exception as e:
            return False, str(e)

    def _write_stream(self, chunk_iterator, part_path: str, open_mode: str, filename: str, start_bytes: int, total_size: int) -> tuple:
        downloaded = start_bytes
        last_emit_time = 0.0

        try:
            with open(part_path, open_mode) as f:
                for chunk in chunk_iterator:
                    if self._is_cancelled():
                        return False, "cancelled"

                    if not self._wait_if_paused():
                        return False, "cancelled"

                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)

                        now = time.time()
                        if now - last_emit_time >= 0.2:
                            self.file_progress_signal.emit(filename, (downloaded, total_size))
                            last_emit_time = now

            self.file_progress_signal.emit(filename, (downloaded, total_size))
            return True, ""
        except Exception as e:
            return False, str(e)

