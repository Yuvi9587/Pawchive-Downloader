import os
import json
from datetime import datetime
from PySide6.QtCore import QObject, Signal


class UserFavoritesManager(QObject):
    """
    Unified manager for local user favorites (Creators and Posts).
    Persists data locally in cache/user_favorites.json.
    Does NOT require any cookies or account authentication.
    Thread-safe and emits favorites_changed signal on any toggle or change.
    """
    favorites_changed = Signal()

    def __init__(self, base_dir=".", file_path=None, filepath=None, parent=None):
        super().__init__(parent)
        chosen_path = file_path or filepath
        self.file_path = chosen_path if chosen_path else os.path.join(base_dir, "cache", "user_favorites.json")
        self._creator_keys = set()
        self._creator_data = {}
        self._post_keys = set()
        self._post_data = {}
        self._load()

    # ─────────────────────────────────────────────────────────────────────────
    #  Creator Favorites
    # ─────────────────────────────────────────────────────────────────────────

    def _creator_key(self, service: str, creator_id: str) -> str:
        return f"{str(service or '').lower().strip()}:{str(creator_id or '').strip()}"

    def is_favorite(self, service: str, creator_id: str) -> bool:
        """Alias for is_favorite_creator for backward compatibility."""
        return self.is_favorite_creator(service, creator_id)

    def is_favorite_creator(self, service: str, creator_id: str) -> bool:
        if not service or not creator_id:
            return False
        return self._creator_key(service, creator_id) in self._creator_keys

    def toggle(self, creator: dict) -> bool:
        """Alias for toggle_creator for backward compatibility."""
        return self.toggle_creator(creator)

    def toggle_creator(self, creator: dict) -> bool:
        if not creator or not isinstance(creator, dict):
            return False
        svc = (creator.get("service") or "").lower().strip()
        cid = str(creator.get("id") or "").strip()
        if not svc or not cid:
            return False
        k = self._creator_key(svc, cid)
        if k in self._creator_keys:
            self._creator_keys.remove(k)
            self._creator_data.pop(k, None)
            is_fav = False
        else:
            self._creator_keys.add(k)
            self._creator_data[k] = {
                "service": svc,
                "id": cid,
                "name": creator.get("name") or "Unknown",
                "url": creator.get("url") or f"https://pawchive.pw/{svc}/user/{cid}",
                "favorited": creator.get("favorited", 0),
                "updated": creator.get("updated") or "",
                "date_added": datetime.now().isoformat()
            }
            is_fav = True
        self._save()
        self.favorites_changed.emit()
        return is_fav

    def add_creator(self, creator: dict) -> bool:
        if not creator or not isinstance(creator, dict):
            return False
        svc = (creator.get("service") or "").lower().strip()
        cid = str(creator.get("id") or "").strip()
        if not svc or not cid:
            return False
        k = self._creator_key(svc, cid)
        if k not in self._creator_keys:
            self._creator_keys.add(k)
            self._creator_data[k] = {
                "service": svc,
                "id": cid,
                "name": creator.get("name") or "Unknown",
                "url": creator.get("url") or f"https://pawchive.pw/{svc}/user/{cid}",
                "favorited": creator.get("favorited", 0),
                "updated": creator.get("updated") or "",
                "date_added": datetime.now().isoformat()
            }
            self._save()
            self.favorites_changed.emit()
            return True
        return False

    def remove_creator(self, service: str, creator_id: str) -> bool:
        k = self._creator_key(service, creator_id)
        if k in self._creator_keys:
            self._creator_keys.remove(k)
            self._creator_data.pop(k, None)
            self._save()
            self.favorites_changed.emit()
            return True
        return False

    def get_all_keys(self) -> set:
        """Returns set of all creator favorite keys (backward compatibility)."""
        return set(self._creator_keys)

    def get_all_creators(self) -> list:
        """Returns a list of all favorited creators."""
        return list(self._creator_data.values())

    def get_favorites_count(self) -> int:
        return len(self._creator_keys)

    # ─────────────────────────────────────────────────────────────────────────
    #  Post Favorites
    # ─────────────────────────────────────────────────────────────────────────

    def _post_key(self, service: str, creator_id: str, post_id: str) -> str:
        return f"{str(service or '').lower().strip()}:{str(creator_id or '').strip()}:{str(post_id or '').strip()}"

    def is_favorite_post(self, service: str, creator_id: str, post_id: str) -> bool:
        if not service or not post_id:
            return False
        key = self._post_key(service, creator_id, post_id)
        if key in self._post_keys:
            return True
        # Also check without creator_id if creator_id wasn't available
        for k in self._post_keys:
            parts = k.split(":")
            if len(parts) >= 3 and parts[0] == str(service).lower().strip() and parts[2] == str(post_id).strip():
                return True
        return False

    def toggle_post(self, post: dict) -> bool:
        if not post or not isinstance(post, dict):
            return False
        svc = (
            post.get("_creator_service")
            or post.get("service")
            or "patreon"
        ).lower().strip()
        cid = str(post.get("_creator_id") or post.get("user") or "").strip()
        pid = str(post.get("id") or post.get("post_id") or "").strip()
        if not svc or not pid:
            return False

        # Find matching key if already stored
        matched_key = None
        target_k = self._post_key(svc, cid, pid)
        if target_k in self._post_keys:
            matched_key = target_k
        else:
            for k in self._post_keys:
                parts = k.split(":")
                if len(parts) >= 3 and parts[0] == svc and parts[2] == pid:
                    matched_key = k
                    break

        if matched_key:
            self._post_keys.remove(matched_key)
            self._post_data.pop(matched_key, None)
            is_fav = False
        else:
            self._post_keys.add(target_k)
            cname = (
                post.get("_creator_name")
                or post.get("creator_name_resolved")
                or post.get("creator_name")
                or cid
                or "Creator"
            )
            title = post.get("title") or f"Post #{pid}"
            pub = post.get("published") or post.get("added") or ""
            thumb = post.get("_thumbnail") or post.get("thumb") or ""

            attachments = post.get("attachments") or []
            att_count = len(attachments) if isinstance(attachments, list) else 0
            if post.get("file"):
                att_count += 1

            self._post_data[target_k] = {
                "id": pid,
                "service": svc,
                "creator_id": cid,
                "creator_name": cname,
                "post_id": pid,
                "title": title,
                "url": f"https://pawchive.pw/{svc}/user/{cid}/post/{pid}" if cid else f"https://pawchive.pw/{svc}/post/{pid}",
                "published": str(pub),
                "added_date": datetime.now().isoformat(),
                "thumb_url": thumb,
                "attachments_count": att_count,
                "post_payload": dict(post)
            }
            is_fav = True

        self._save()
        self.favorites_changed.emit()
        return is_fav

    def add_post(self, post: dict) -> bool:
        if not post or not isinstance(post, dict):
            return False
        svc = (post.get("_creator_service") or post.get("service") or "patreon").lower().strip()
        cid = str(post.get("_creator_id") or post.get("user") or "").strip()
        pid = str(post.get("id") or post.get("post_id") or "").strip()
        if not svc or not pid:
            return False
        if not self.is_favorite_post(svc, cid, pid):
            return self.toggle_post(post)
        return False

    def remove_post(self, service: str, creator_id: str, post_id: str) -> bool:
        k = self._post_key(service, creator_id, post_id)
        matched_key = k if k in self._post_keys else None
        if not matched_key:
            for existing_k in self._post_keys:
                parts = existing_k.split(":")
                if len(parts) >= 3 and parts[0] == str(service).lower().strip() and parts[2] == str(post_id).strip():
                    matched_key = existing_k
                    break

        if matched_key:
            self._post_keys.remove(matched_key)
            self._post_data.pop(matched_key, None)
            self._save()
            self.favorites_changed.emit()
            return True
        return False

    def get_all_posts(self) -> list:
        """Returns a list of all favorited posts."""
        return list(self._post_data.values())

    def get_posts_count(self) -> int:
        return len(self._post_keys)

    # ─────────────────────────────────────────────────────────────────────────
    #  Persistence
    # ─────────────────────────────────────────────────────────────────────────

    def _load(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        if "creators" in data or "posts" in data:
                            self._creator_data = data.get("creators", {})
                            self._creator_keys = set(self._creator_data.keys())
                            self._post_data = data.get("posts", {})
                            self._post_keys = set(self._post_data.keys())
                        else:
                            # Legacy format: flat dictionary of creators
                            self._creator_data = data
                            self._creator_keys = set(data.keys())
                            self._post_data = {}
                            self._post_keys = set()
                    elif isinstance(data, list):
                        # Legacy format: list of keys
                        self._creator_keys = set(data)
                        self._creator_data = {k: {} for k in data}
                        self._post_data = {}
                        self._post_keys = set()
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            payload = {
                "version": 2,
                "creators": self._creator_data,
                "posts": self._post_data
            }
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
