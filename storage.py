"""
JSON-backed persistence for favorites and playlists.

Always stored under App.user_data_dir (never a relative path), so writes
never fail because of Android write permissions / Scoped Storage rules.
"""

import os
import json


class Library:
    def __init__(self, user_data_dir):
        self.data_file = os.path.join(user_data_dir, 'kazemusic_library.json')
        self._data = self._load()

    # ------------------------------------------------------------------
    def _load(self):
        data = {}
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        # Make sure the expected structure always exists (old / damaged files)
        if not isinstance(data.get('favorites'), list):
            data['favorites'] = []
        if not isinstance(data.get('playlists'), dict):
            data['playlists'] = {}
        return data

    def _save(self):
        """Atomic write: a crash in the middle can never destroy the data."""
        tmp = self.data_file + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.data_file)
        except OSError as e:
            print('Could not save library:', e)

    # ---------------- favorites ----------------
    def is_favorite(self, song_id):
        return song_id in self._data['favorites']

    def toggle_favorite(self, song_id):
        favs = self._data['favorites']
        if song_id in favs:
            favs.remove(song_id)
        else:
            favs.append(song_id)
        self._save()
        return song_id in favs

    def get_favorite_ids(self):
        return list(self._data['favorites'])

    # ---------------- playlists ----------------
    def get_playlists(self):
        return self._data['playlists']

    def create_playlist(self, name):
        self._data['playlists'].setdefault(name, [])
        self._save()

    def add_to_playlist(self, name, song_id):
        """Returns True when the song was added, False if already there."""
        pl = self._data['playlists'].setdefault(name, [])
        if song_id in pl:
            return False
        pl.append(song_id)
        self._save()
        return True

    def remove_from_playlist(self, name, song_id):
        pl = self._data['playlists'].get(name, [])
        if song_id in pl:
            pl.remove(song_id)
            self._save()

    def remove_playlist(self, name):
        self._data['playlists'].pop(name, None)
        self._save()
