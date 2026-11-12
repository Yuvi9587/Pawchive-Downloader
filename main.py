import sys
import os
import time
import traceback
import socket

# Enforce reasonable default timeout for all sockets to prevent hung threads on network drops
try:
    socket.setdefaulttimeout(30)
except Exception:
    pass

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# --- Define Directories for Runtime Data and Bundled Resources ---
if getattr(sys, 'frozen', False):
    APP_RUNTIME_DIR = os.path.dirname(sys.executable)
    if hasattr(sys, '_MEIPASS'):
        APP_RESOURCE_DIR = sys._MEIPASS
    else:
        internal_dir = os.path.join(APP_RUNTIME_DIR, '_internal')
        APP_RESOURCE_DIR = internal_dir if os.path.isdir(internal_dir) else APP_RUNTIME_DIR
else:
    APP_RUNTIME_DIR = os.path.abspath(os.path.dirname(__file__))
    APP_RESOURCE_DIR = APP_RUNTIME_DIR

APP_BASE_DIR = APP_RUNTIME_DIR

# --- Windows DLL and Qt Plugin search paths bootstrap for PyInstaller standalone bundles ---
if sys.platform.startswith('win'):
    import ctypes
    _pyside_candidates = [
        os.path.join(APP_RESOURCE_DIR, 'PySide6'),
        os.path.join(APP_RUNTIME_DIR, '_internal', 'PySide6'),
        os.path.join(APP_RUNTIME_DIR, 'PySide6'),
    ]
    _shiboken_candidates = [
        os.path.join(APP_RESOURCE_DIR, 'shiboken6'),
        os.path.join(APP_RUNTIME_DIR, '_internal', 'shiboken6'),
        os.path.join(APP_RUNTIME_DIR, 'shiboken6'),
    ]
    _dll_dirs = [d for d in (_pyside_candidates + _shiboken_candidates + [APP_RESOURCE_DIR, APP_RUNTIME_DIR]) if os.path.isdir(d)]

    # 1. Prepend to PATH so standard LoadLibrary searches them
    os.environ['PATH'] = os.pathsep.join(_dll_dirs) + os.pathsep + os.environ.get('PATH', '')

    # 2. Add DLL directories for Python 3.8+ Windows loader
    if hasattr(os, 'add_dll_directory'):
        for d in _dll_dirs:
            try:
                os.add_dll_directory(d)
            except Exception:
                pass

    # 3. Set Windows kernel DLL directory
    for ps_dir in _pyside_candidates:
        if os.path.isdir(ps_dir):
            try:
                ctypes.windll.kernel32.SetDllDirectoryW(ps_dir)
                break
            except Exception:
                pass

    # 4. Explicitly pre-load Qt runtime libraries using ALTERED_SEARCH_PATH
    LOAD_WITH_ALTERED_SEARCH_PATH = 0x00000008
    for ps_dir in _pyside_candidates:
        if os.path.isdir(ps_dir):
            for dll_name in ('Qt6Core.dll', 'Qt6Gui.dll', 'Qt6Widgets.dll', 'Qt6Svg.dll', 'Qt6SvgWidgets.dll'):
                dll_full = os.path.join(ps_dir, dll_name)
                if os.path.exists(dll_full):
                    try:
                        ctypes.windll.kernel32.LoadLibraryExW(dll_full, 0, LOAD_WITH_ALTERED_SEARCH_PATH)
                    except Exception:
                        pass
            # Preload SVG and ICO plugins
            for sub, plg in [('imageformats', 'qsvg.dll'), ('imageformats', 'qico.dll'), ('iconengines', 'qsvgicon.dll')]:
                plg_path = os.path.join(ps_dir, 'plugins', sub, plg)
                if os.path.exists(plg_path):
                    try:
                        ctypes.windll.kernel32.LoadLibraryExW(plg_path, 0, LOAD_WITH_ALTERED_SEARCH_PATH)
                    except Exception:
                        pass
            break

# --- PySide6 Imports ---
from PySide6.QtWidgets import QApplication, QDialog
from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtGui import QIcon, QImageReader

try:
    import PySide6.QtSvg
    import PySide6.QtSvgWidgets
except ImportError:
    pass

try:
    import fpdf
except ImportError:
    pass

try:
    import docx
except ImportError:
    pass

# --- Local Application Imports ---
from src.ui.main_window import DownloaderApp
from src.core.visual_sorter import VisualSorter
from src.config.constants import CONFIG_ORGANIZATION_NAME, CONFIG_APP_NAME_MAIN

# Optional: Set a global variable or pass it into modules if needed
# Or re-export it via constants.py for cleaner imports

def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
    """
    Handles uncaught exceptions by logging them to a file for easier debugging,
    especially for bundled applications.
    """
    # Use APP_BASE_DIR to determine logging location
    log_dir = os.path.join(APP_BASE_DIR, "logs")
    log_file_path = os.path.join(log_dir, "uncaught_exceptions.log")

    try:
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        with open(log_file_path, "a", encoding="utf-8") as f:
            f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
            f.write("-" * 80 + "\n\n")
    except Exception as log_ex:
        # Fallback to stderr if logging to file fails
        print(f"CRITICAL: Failed to write to uncaught_exceptions.log: {log_ex}", file=sys.stderr)
        traceback.print_exception(exc_type, exc_value, exc_traceback, file=sys.stderr)
    
    # Also call the default excepthook
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

# In main.py
def preload_ai_model():
    """Try to preload the AI model if it exists. If not, continue normally."""
    try:
        models_dir = os.path.join(APP_BASE_DIR, "appdata", "models")
        model_path = os.path.join(models_dir, "model.onnx")
        csv_path = os.path.join(models_dir, "selected_tags.csv")
        
        if not os.path.exists(model_path) or not os.path.exists(csv_path):
            # Log to console or internal logging system
            print("ℹ️ Visual Sort AI model not found. This feature will be available once downloaded via settings.")
            return

        # Pre-initialize the singleton if files exist
        from src.core.visual_sorter import VisualSorter
        VisualSorter.get_instance(model_path, csv_path)
        print("✅ Visual Sort AI engine initialized successfully.")
    except Exception as e:
        print(f"⚠️ Note: Visual Sort engine could not be pre-loaded: {e}")

def _load_app_icon():
    """Loads the application QIcon with PNG fallback if ICO decoding fails."""
    for rel_name in ("Pawchive.ico", "Pawchive.png"):
        for base_dir in (APP_RESOURCE_DIR, APP_RUNTIME_DIR):
            candidate = os.path.join(base_dir, "assets", rel_name)
            if os.path.exists(candidate):
                icon = QIcon(candidate)
                if not icon.isNull():
                    return icon, candidate
    return None, None


def _apply_windows_taskbar_icon(window, ico_path=None):
    """Explicitly binds Win32 big/small HICON on both the HWND and window class."""
    if not sys.platform.startswith('win') or window is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_SHARED = 0x8000
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        GCLP_HICON = -14
        GCLP_HICONSM = -34
        SM_CXICON = 11
        SM_CYICON = 12
        SM_CXSMICON = 49
        SM_CYSMICON = 50

        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, ctypes.c_void_p, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HICON
        user32.SendMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.SendMessageW.restype = ctypes.c_ssize_t

        set_class_long = getattr(user32, 'SetClassLongPtrW', None) or user32.SetClassLongW
        set_class_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_class_long.restype = ctypes.c_ssize_t

        cx_big = user32.GetSystemMetrics(SM_CXICON) or 32
        cy_big = user32.GetSystemMetrics(SM_CYICON) or 32
        cx_sm = user32.GetSystemMetrics(SM_CXSMICON) or 16
        cy_sm = user32.GetSystemMetrics(SM_CYSMICON) or 16

        h_big = None
        h_sm = None

        # Prefer the EXE's own embedded RT_GROUP_ICON (resource id=1) when frozen
        if getattr(sys, 'frozen', False):
            h_mod = kernel32.GetModuleHandleW(None)
            if h_mod:
                h_big = user32.LoadImageW(h_mod, ctypes.c_void_p(1), IMAGE_ICON, cx_big, cy_big, LR_SHARED)
                h_sm = user32.LoadImageW(h_mod, ctypes.c_void_p(1), IMAGE_ICON, cx_sm, cy_sm, LR_SHARED)

        # Fallback to loading the .ico file directly from disk
        ico_candidate = ico_path if (ico_path and ico_path.lower().endswith('.ico')) else os.path.join(APP_RESOURCE_DIR, "assets", "Pawchive.ico")
        if (not h_big or not h_sm) and ico_candidate and os.path.exists(ico_candidate):
            ico_ptr = ctypes.cast(ctypes.c_wchar_p(os.path.abspath(ico_candidate)), ctypes.c_void_p)
            if not h_big:
                h_big = user32.LoadImageW(None, ico_ptr, IMAGE_ICON, cx_big, cy_big, LR_LOADFROMFILE)
            if not h_sm:
                h_sm = user32.LoadImageW(None, ico_ptr, IMAGE_ICON, cx_sm, cy_sm, LR_LOADFROMFILE)
            # Keep non-shared file HICONs alive for the lifetime of the window
            window._win_hicons = (h_big, h_sm)

        hwnd = int(window.winId())
        if not hwnd:
            return

        if h_big:
            h_big_val = ctypes.c_ssize_t(int(h_big)).value
            set_class_long(hwnd, GCLP_HICON, h_big_val)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, h_big_val)
        if h_sm:
            h_sm_val = ctypes.c_ssize_t(int(h_sm)).value
            set_class_long(hwnd, GCLP_HICONSM, h_sm_val)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, h_sm_val)
    except Exception:
        pass


def main():
    """Main entry point for the Pawchive Downloader application."""
    
    # Set up global exception handling
    sys.excepthook = handle_uncaught_exception

    try:
        # Set up application metadata for QSettings
        QCoreApplication.setOrganizationName(CONFIG_ORGANIZATION_NAME)
        QCoreApplication.setApplicationName(CONFIG_APP_NAME_MAIN)
        preload_ai_model()

        # --- Windows Taskbar AppUserModelID ---
        # Only override AppUserModelID when running unfrozen (via python.exe) so dev
        # runs don't group under python.exe. Frozen .exe builds should keep their
        # native executable identity so Windows Explorer immediately resolves the
        # embedded PE icon on first launch without requiring an installer shortcut.
        if sys.platform.startswith('win') and not getattr(sys, 'frozen', False):
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "PawchiveDownloader.App.1"
                )
            except Exception:
                pass

        # Register Qt plugin paths for image formats (SVG, ICO, WebP) and platforms
        plugin_dirs = [
            os.path.join(APP_RESOURCE_DIR, 'PySide6', 'plugins'),
            os.path.join(APP_RESOURCE_DIR, 'plugins'),
            os.path.join(APP_RUNTIME_DIR, '_internal', 'PySide6', 'plugins'),
            os.path.join(APP_RUNTIME_DIR, '_internal', 'plugins'),
            os.path.join(APP_RUNTIME_DIR, 'plugins'),
        ]
        for p_dir in plugin_dirs:
            if os.path.isdir(p_dir):
                QCoreApplication.addLibraryPath(p_dir)

        qt_app = QApplication(sys.argv)
        qt_app.setStyle("Fusion")

        # Verify and ensure SVG format support
        supported_formats = [bytes(fmt).decode('utf-8', errors='ignore').lower() for fmt in QImageReader.supportedImageFormats()]
        if 'svg' not in supported_formats:
            # Fallback attempt: re-register all possible plugin paths
            for p_dir in plugin_dirs:
                if os.path.isdir(p_dir):
                    QCoreApplication.addLibraryPath(p_dir)
            supported_formats = [bytes(fmt).decode('utf-8', errors='ignore').lower() for fmt in QImageReader.supportedImageFormats()]
        print(f"Qt initialized. Supported image formats ({len(supported_formats)}): {', '.join(supported_formats)}")

        # --- Application-level icon (taskbar + all windows) ---
        _app_icon, _icon_path = _load_app_icon()
        if _app_icon and not _app_icon.isNull():
            qt_app.setWindowIcon(_app_icon)

        # Create the main application window
        downloader_app_instance = DownloaderApp()

        # Belt-and-suspenders: also set directly on the window in case the
        # QApplication-level icon isn't picked up by the specific platform.
        if _app_icon and not _app_icon.isNull():
            downloader_app_instance.setWindowIcon(_app_icon)


        # --- Window Sizing and Positioning ---
        primary_screen = QApplication.primaryScreen()
        if not primary_screen:
            downloader_app_instance.resize(1024, 768)
        else:
            available_geo = primary_screen.availableGeometry()
            screen_width = available_geo.width()
            screen_height = available_geo.height()
            
            min_app_width, min_app_height = 960, 680
            desired_width_ratio, desired_height_ratio = 0.80, 0.85

            app_width = max(min_app_width, int(screen_width * desired_width_ratio))
            app_height = max(min_app_height, int(screen_height * desired_height_ratio))

            app_width = min(app_width, screen_width)
            app_height = min(app_height, screen_height)
            
            downloader_app_instance.resize(app_width, app_height)

        # Show and center the main window
        downloader_app_instance.show()
        if hasattr(downloader_app_instance, '_center_on_screen'):
            downloader_app_instance._center_on_screen()

        # Bind Win32 HICON directly and flush initial window events before exec()
        # so Explorer never caches a blank icon during startup.
        _apply_windows_taskbar_icon(downloader_app_instance, _icon_path)
        qt_app.processEvents()
        QTimer.singleShot(0, lambda: _apply_windows_taskbar_icon(downloader_app_instance, _icon_path))

        # First-run guide is available in the dedicated About tab

        # --- Start Application ---
        exit_code = qt_app.exec()
        print(f"Application finished with exit code: {exit_code}")
        sys.exit(exit_code)

    except SystemExit:
        pass
    except Exception as e:
        print("--- CRITICAL APPLICATION STARTUP ERROR ---")
        print(f"An unhandled exception occurred during application startup: {e}")
        traceback.print_exc()
        print("--- END CRITICAL ERROR ---")


if __name__ == '__main__':
    main()