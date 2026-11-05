import re
import requests
from urllib.parse import urlparse

from ...utils.network_utils import prepare_cookies_for_request
from ...utils.file_utils import clean_folder_name 

from .discord_downloader_thread import DiscordDownloadThread
from .drive_downloader_thread import DriveDownloadThread
from .kemono_discord_downloader_thread import KemonoDiscordDownloadThread

def create_downloader_thread(main_app, api_url, service, id1, id2, effective_output_dir_for_run, export_all_links_mode=False):
    """
    Factory function to create and configure the correct QThread for a given URL.
    Returns a configured QThread instance, a specific error string ("COOKIE_ERROR", "FETCH_ERROR"),
    or None if no special handler is found (indicating fallback to generic BackendDownloadThread).
    """

    # Cloud file hosts (Mega, Google Drive, Dropbox, Gofile)
    platform = None
    if 'mega.nz' in api_url or 'mega.io' in api_url:
        platform = 'mega'
    elif 'drive.google.com' in api_url:
        platform = 'gdrive'
    elif 'dropbox.com' in api_url:
        platform = 'dropbox'
    elif 'gofile.io' in api_url:
        platform = 'gofile'
    if platform:
        use_post_subfolder = main_app.use_subfolder_per_post_checkbox.isChecked()
        return DriveDownloadThread(
            api_url, effective_output_dir_for_run, platform, use_post_subfolder,
            main_app.cancellation_event, main_app.pause_event, main_app.log_signal.emit,
            parent=main_app
        )

    # Pawchive / Kemono Discord channels
    if service == 'discord' and any(domain in api_url for domain in ['pawchive.st', 'pawchive.pw', 'kemono.cr', 'kemono.su', 'kemono.party']):
        main_app.log_signal.emit("ℹ️ Pawchive Discord URL detected. Starting dedicated downloader.")
        cookies = prepare_cookies_for_request(
            use_cookie_flag=main_app.use_cookie_checkbox.isChecked(),
            cookie_text_input=main_app.cookie_text_input.text(),
            selected_cookie_file_path=main_app.selected_cookie_filepath,
            app_base_dir=main_app.app_base_dir,
            logger_func=main_app.log_signal.emit,
            target_domain='pawchive.pw'
        )
        return KemonoDiscordDownloadThread(
            server_id=id1,
            channel_id=id2,
            output_dir=effective_output_dir_for_run,
            cookies_dict=cookies,
            parent=main_app
        )

    # Official Discord URL
    elif service == 'discord' and 'discord.com' in api_url:
        main_app.log_signal.emit("ℹ️ Official Discord URL detected. Starting dedicated downloader.")
        token = main_app.remove_from_filename_input.text().strip()
        if not token:
             main_app.log_signal.emit("❌ Official Discord requires an Authorization Token in the 'Remove Words' field.")
             return None

        limit_text = main_app.discord_message_limit_input.text().strip()
        message_limit = int(limit_text) if limit_text.isdigit() else None
        mode = main_app.discord_download_scope

        return DiscordDownloadThread(
            mode=mode,
            session=requests.Session(),
            token=token,
            output_dir=effective_output_dir_for_run,
            server_id=id1,
            channel_id=id2,
            url=api_url,
            app_base_dir=main_app.app_base_dir,
            limit=message_limit,
            parent=main_app
        )

    return None
