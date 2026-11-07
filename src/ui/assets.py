import os
import sys
from PySide6.QtGui import QIcon

_app_icon_cache = None

def get_asset_path(filename):
    """
    Return the path to an asset, works in both dev and packaged environments.
    Checks sys._MEIPASS (_internal), the executable directory, and dev workspace.
    """
    normalized_filename = filename.replace('\\', '/').lstrip('/')
    if not normalized_filename.startswith('assets/'):
        rel_path = os.path.join('assets', normalized_filename)
    else:
        rel_path = normalized_filename

    candidates = []
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            candidates.append(sys._MEIPASS)
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(exe_dir)
        candidates.append(os.path.join(exe_dir, '_internal'))
    else:
        candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

    for base in candidates:
        full = os.path.normpath(os.path.join(base, rel_path))
        if os.path.exists(full):
            return full

    primary_base = candidates[0] if candidates else os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    return os.path.normpath(os.path.join(primary_base, rel_path))

def get_asset_html_path(filename):
    """
    Return the asset path as a proper file:/// URL safe for use inside Qt HTML labels (src= attributes).
    
    Qt's HTML renderer does NOT accept Windows-style backslash paths (e.g. C:\\Users\\...) 
    in src= attributes when running from a frozen .exe. This converts the path to the 
    correct forward-slash file:/// URL format (e.g. file:///C:/Users/...).
    """
    path = get_asset_path(filename)
    return 'file:///' + path.replace('\\', '/').lstrip('/')

def get_app_icon_object():
    """
    Loads and caches the application icon from the assets folder.
    This function is now centralized to prevent circular imports.

    Returns:
        QIcon: The application icon object.
    """
    global _app_icon_cache
    if _app_icon_cache and not _app_icon_cache.isNull():
        return _app_icon_cache

    for candidate in ('Pawchive.ico', 'Pawchive.png', 'assets/Svg/paw-print.svg', 'Kemono.ico'):
        icon_path = get_asset_path(candidate)
        if os.path.exists(icon_path):
            _app_icon_cache = QIcon(icon_path)
            if not _app_icon_cache.isNull():
                return _app_icon_cache

    _app_icon_cache = QIcon()
    return _app_icon_cache
