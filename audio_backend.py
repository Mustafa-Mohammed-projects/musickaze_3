"""
Audio backend for KazeMusic (runs inside the app / UI process).

Android:
    The music is NOT played here.  A foreground service (service.py) owns the
    MediaPlayer, the queue and the notification, so playback survives when the
    app is closed.  ServiceClient only sends it commands and mirrors its state.

Desktop (testing only):
    LocalPlayer plays a local "music" folder with Kivy's SoundLoader.

Both classes offer the same interface to main.py:

    set_queue_and_play(songs, index)   toggle()   next()   prev()   seek_to(sec)
    current_song  current_index  cover_path  is_playing()  get_position()  get_duration()
    sync()  start()  close()
    callbacks (may be called from ANY thread):  on_change()   on_error(message)
"""

import os
import json
import time
import hashlib

from kivy.utils import platform
from kivy.clock import Clock

from arabic_text import fix_mojibake, search_key
from cover_art import CoverLoader

UNKNOWN_ARTIST = 'Unknown artist'
UNKNOWN_TITLE = 'Unknown title'


def get_android_activity():
    """The first call must come from the main (Kivy) thread; the activity is
    then cached so other threads can use it safely."""
    global _ACTIVITY
    if _ACTIVITY is None:
        from jnius import autoclass
        _ACTIVITY = autoclass('org.kivy.android.PythonActivity').mActivity
    return _ACTIVITY


_ACTIVITY = None


class PlayerBase:
    def __init__(self):
        self.on_change = None
        self.on_error = None
        self.current_song = None
        self.current_index = -1
        self.cover_path = None

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception as e:
                print('on_change failed:', e)

    def _error(self, message):
        if self.on_error:
            try:
                self.on_error(message)
            except Exception as e:
                print('on_error failed:', e)

    # default no-ops
    def start(self):
        pass

    def sync(self):
        self._changed()

    def close(self):
        pass


# ======================================================================
# Android: talks to the foreground service
# ======================================================================
class ServiceClient(PlayerBase):
    SERVICE_NAME = 'Music'            # must match "services = Music:service.py" in buildozer.spec

    def __init__(self, data_dir):
        super().__init__()
        self.queue_path = os.path.join(data_dir, 'playback_queue.json')
        self.boot_path = os.path.join(data_dir, 'service_boot.json')
        self._qkey = None
        self._seq = 0
        self._receiver = None
        self._playing = False
        self._pos_ms = 0.0
        self._dur_ms = 0.0
        self._ts_ms = 0.0

    # ---------------- lifecycle ----------------
    def start(self):
        """Listen to the service's state broadcasts and ask for the current state."""
        try:
            from android.broadcast import BroadcastReceiver
            pkg = get_android_activity().getPackageName()
            self._receiver = BroadcastReceiver(self._on_state_broadcast, actions=[pkg + '.STATE'])
            self._receiver.start()
        except Exception as e:
            print('Could not listen to the playback service:', e)
        self.sync()

    def close(self):
        """The window is closing - the service (and the music) keep running."""
        try:
            if self._receiver is not None:
                self._receiver.stop()
        except Exception:
            pass
        self._receiver = None

    def sync(self):
        self._send({'cmd': 'SYNC'})

    # ---------------- sending commands ----------------
    def _next_seq(self):
        self._seq = max(self._seq + 1, int(time.time() * 1000))
        return self._seq

    def _send(self, command):
        try:
            from jnius import autoclass
            activity = get_android_activity()
            pkg = activity.getPackageName()
            Intent = autoclass('android.content.Intent')
            intent = Intent(pkg + '.CMD')
            intent.setPackage(pkg)
            for key, value in command.items():
                intent.putExtra(key, str(value))
            activity.sendBroadcast(intent)
        except Exception as e:
            print('Could not send a command to the service:', e)

    def _start_service(self, boot_command):
        from jnius import autoclass
        activity = get_android_activity()
        pkg = activity.getPackageName()
        # The first command is also stored in a file: the service reads it as
        # soon as it is ready, so it cannot be lost during startup.
        tmp = self.boot_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(boot_command, f)
        os.replace(tmp, self.boot_path)
        Service = autoclass('{}.Service{}'.format(pkg, self.SERVICE_NAME))
        try:
            Service.start(activity, self.boot_path)
        except Exception:
            Service.start(activity, '', 'KazeMusic', 'Playing music', self.boot_path)

    # ---------------- queue ----------------
    def set_queue_and_play(self, songs, index):
        if not songs or not (0 <= index < len(songs)):
            return
        key = hashlib.md5(('|'.join(s['id'] for s in songs) + '#' + str(len(songs))).encode('utf-8')).hexdigest()
        if key != self._qkey:
            slim = [{k: s.get(k) for k in ('id', 'title', 'artist', 'album', 'album_id', 'duration', 'uri')}
                    for s in songs]
            tmp = self.queue_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'songs': slim}, f, ensure_ascii=False)
            os.replace(tmp, self.queue_path)
            self._qkey = key

        command = {'cmd': 'LOAD_PLAY', 'qpath': self.queue_path, 'qkey': key,
                   'index': index, 'seq': self._next_seq()}

        # show something immediately; the service confirms a moment later
        song = songs[index]
        self.current_song = {'id': song['id'], 'title': song['title'], 'artist': song['artist'],
                             'album': song.get('album', ''), 'duration': song.get('duration') or 0}
        self.current_index = index
        self.cover_path = None
        self._playing = True
        self._pos_ms = 0.0
        self._dur_ms = (song.get('duration') or 0) * 1000.0
        self._ts_ms = time.time() * 1000
        self._changed()

        try:
            self._start_service(command)
        except Exception as e:
            self._error('Could not start the playback service: {}'.format(e))
            return
        self._send(command)

    def toggle(self):
        if self.current_song:
            self._playing = not self._playing          # instant feedback
            self._pos_ms = self.get_position() * 1000.0
            self._ts_ms = time.time() * 1000
            self._changed()
            self._send({'cmd': 'TOGGLE'})

    def next(self):
        self._send({'cmd': 'NEXT'})

    def prev(self):
        self._send({'cmd': 'PREV'})

    def seek_to(self, seconds):
        seconds = max(0.0, float(seconds))
        self._pos_ms = seconds * 1000.0
        self._ts_ms = time.time() * 1000
        self._send({'cmd': 'SEEK', 'ms': int(seconds * 1000)})

    # ---------------- state ----------------
    def is_playing(self):
        return bool(self.current_song and self._playing)

    def get_duration(self):
        if self._dur_ms > 0:
            return self._dur_ms / 1000.0
        return float((self.current_song or {}).get('duration') or 0)

    def get_position(self):
        if not self.current_song:
            return 0.0
        pos = self._pos_ms
        if self._playing:
            pos += max(0.0, time.time() * 1000 - self._ts_ms)
        pos /= 1000.0
        duration = self.get_duration()
        return min(pos, duration) if duration > 0 else pos

    def _on_state_broadcast(self, context, intent):
        """Runs on an Android thread (not the Kivy thread)."""
        try:
            def get(key, default=''):
                value = intent.getStringExtra(key)
                return default if value is None else str(value)

            def number(key):
                try:
                    return float(get(key, '0') or 0)
                except ValueError:
                    return 0.0

            song_id = get('song_id')
            if song_id:
                self.current_song = {'id': song_id, 'title': get('title'), 'artist': get('artist'),
                                     'album': get('album'), 'duration': number('duration')}
            else:
                self.current_song = None
            self.current_index = int(number('index')) if song_id else -1
            self._playing = get('playing') == '1'
            self._pos_ms = number('pos_ms')
            self._dur_ms = number('dur_ms')
            self._ts_ms = number('ts') or time.time() * 1000
            self.cover_path = get('cover') or None
            error = get('error')
        except Exception as e:
            print('Bad state from the service:', e)
            return
        self._changed()
        if error:
            self._error(error)


# ======================================================================
# Desktop: plays in-process (testing only)
# ======================================================================
class _DesktopTrack:
    """Plays ONE file with Kivy's SoundLoader."""

    def __init__(self):
        self._sound = None
        self._paused = False
        self._pause_pos = 0.0
        self._gen = 0
        self.want_play = False
        self.on_complete = None
        self.on_error = None

    def play(self, song):
        self.stop()
        from kivy.core.audio import SoundLoader
        sound = SoundLoader.load(song.get('path') or '')
        if sound is None:
            if self.on_error:
                self.on_error('Desktop: cannot load ' + str(song.get('path')))
            return
        gen = self._gen

        def on_stop(*_):
            # ignore the stop caused by pause() / stop() / playing another song
            if gen == self._gen and self.want_play and not self._paused:
                Clock.schedule_once(lambda dt: self._finished(gen), 0)

        sound.bind(on_stop=on_stop)
        self.want_play = True
        sound.play()
        self._sound = sound

    def _finished(self, gen):
        if gen == self._gen and self.on_complete:
            self.on_complete()

    def pause(self):
        self.want_play = False
        if self._sound is not None and not self._paused:
            try:
                self._pause_pos = self._sound.get_pos() or 0.0
                self._paused = True
                self._sound.stop()
            except Exception:
                pass

    def resume(self):
        if self._sound is None:
            return
        self.want_play = True
        if self._paused:
            try:
                self._paused = False
                self._sound.play()
                if self._pause_pos:
                    self._sound.seek(self._pause_pos)
            except Exception:
                pass

    def stop(self):
        self._gen += 1
        self.want_play = False
        sound, self._sound = self._sound, None
        self._paused = False
        self._pause_pos = 0.0
        if sound is not None:
            try:
                sound.stop()
                sound.unload()
            except Exception:
                pass

    def seek(self, seconds):
        if self._sound is None:
            return
        try:
            if self._paused:
                self._pause_pos = seconds
            else:
                self._sound.seek(seconds)
        except Exception:
            pass

    def position(self):
        if self._sound is None:
            return 0.0
        if self._paused:
            return self._pause_pos
        return self._sound.get_pos() or 0.0

    def duration(self):
        return (self._sound.length or 0.0) if self._sound is not None else 0.0

    def is_playing(self):
        return bool(self.want_play and self._sound is not None)


class LocalPlayer(PlayerBase):
    def __init__(self, data_dir):
        super().__init__()
        self._track = _DesktopTrack()
        self._track.on_complete = self.next
        self._track.on_error = self._error
        self._queue = []
        self._covers = CoverLoader(data_dir)

    def set_queue_and_play(self, songs, index):
        if not songs or not (0 <= index < len(songs)):
            return
        self._queue = list(songs)
        self._play_index(index)

    def _play_index(self, index):
        index %= len(self._queue)
        song = self._queue[index]
        self.current_index = index
        self.current_song = song
        self.cover_path = None
        self._track.play(song)
        self._covers.request(song, self._on_cover)
        self._changed()

    def _on_cover(self, song, path):
        if self.current_song and self.current_song['id'] == song['id']:
            self.cover_path = path
            self._changed()

    def toggle(self):
        if not self.current_song:
            return
        if self._track.is_playing():
            self._track.pause()
        elif self._track._sound is not None:
            self._track.resume()
        else:
            self._play_index(self.current_index)
        self._changed()

    def next(self):
        if self._queue:
            self._play_index(self.current_index + 1)

    def prev(self):
        if self._queue:
            self._play_index(self.current_index - 1)

    def seek_to(self, seconds):
        self._track.seek(max(0.0, float(seconds)))

    def is_playing(self):
        return self._track.is_playing()

    def get_position(self):
        return self._track.position()

    def get_duration(self):
        return self._track.duration() or float((self.current_song or {}).get('duration') or 0)

    def close(self):
        self._track.stop()


def create_player(data_dir):
    if platform == 'android':
        return ServiceClient(data_dir)
    return LocalPlayer(data_dir)


# ======================================================================
# Library scanning
# ======================================================================
def scan_device_library(activity=None):
    """Returns a list of dicts: id, title, artist, album, album_id,
    duration, uri, path.  `activity` should be passed from the main thread."""
    if platform == 'android':
        return _scan_mediastore(activity)
    return _scan_desktop_folder()


def _clean_text(value, fallback):
    if value is None:
        return fallback
    value = fix_mojibake(str(value).strip())
    if not value or value == '<unknown>':
        return fallback
    return value


def _scan_mediastore(activity=None):
    """Scans MediaStore for audio files."""
    from jnius import autoclass

    MediaStoreAudio = autoclass('android.provider.MediaStore$Audio$Media')
    if activity is None:
        activity = get_android_activity()
    if activity is None:
        return []
    resolver = activity.getContentResolver()
    if resolver is None:
        return []

    projection = ['_id', 'title', 'artist', 'album', 'album_id', 'duration', '_display_name']
    selection = 'is_music != 0'

    cursor = resolver.query(MediaStoreAudio.EXTERNAL_CONTENT_URI, projection, selection, None, None)
    if cursor is None:
        return []

    songs = []
    try:
        id_idx = cursor.getColumnIndexOrThrow('_id')
        title_idx = cursor.getColumnIndexOrThrow('title')
        artist_idx = cursor.getColumnIndexOrThrow('artist')
        album_idx = cursor.getColumnIndexOrThrow('album')
        album_id_idx = cursor.getColumnIndexOrThrow('album_id')
        dur_idx = cursor.getColumnIndexOrThrow('duration')
        name_idx = cursor.getColumnIndexOrThrow('_display_name')

        while cursor.moveToNext():
            song_id = cursor.getLong(id_idx)
            file_name = cursor.getString(name_idx)
            file_stem = os.path.splitext(file_name)[0] if file_name else ''
            title = _clean_text(cursor.getString(title_idx), '') \
                or _clean_text(file_stem, UNKNOWN_TITLE)
            artist = _clean_text(cursor.getString(artist_idx), UNKNOWN_ARTIST)
            album = _clean_text(cursor.getString(album_idx), '')
            songs.append({
                'id': str(song_id),
                'title': title,
                'artist': artist,
                'album': album,
                'album_id': str(cursor.getLong(album_id_idx)),
                'duration': cursor.getLong(dur_idx) / 1000.0,
                'uri': 'content://media/external/audio/media/{}'.format(song_id),
                'path': None,
            })
    finally:
        cursor.close()

    songs.sort(key=lambda s: search_key(s['title']))
    return songs


def _scan_desktop_folder():
    """Looks for a 'music' folder next to this file, for local testing only."""
    here = os.path.dirname(os.path.abspath(__file__))
    music_dir = os.path.join(here, 'music')
    songs = []
    if not os.path.isdir(music_dir):
        return songs
    exts = ('.mp3', '.wav', '.ogg', '.flac', '.m4a')
    for name in sorted(os.listdir(music_dir)):
        if name.lower().endswith(exts):
            path = os.path.join(music_dir, name)
            songs.append({
                # stable id (does not change when other files are added)
                'id': hashlib.md5(name.encode('utf-8')).hexdigest()[:12],
                'title': os.path.splitext(name)[0],
                'artist': UNKNOWN_ARTIST,
                'album': '',
                'album_id': '',
                'duration': 0,
                'uri': None,
                'path': path,
            })
    songs.sort(key=lambda s: search_key(s['title']))
    return songs
