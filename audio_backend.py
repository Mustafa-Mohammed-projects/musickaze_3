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
    sync()  start()  close()  poll()
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
import ipc

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

    def poll(self):
        pass

    def quit(self):
        self.close()

    def close(self):
        pass


# ======================================================================
# Android: talks to the playback engine (foreground service)
# ======================================================================
class ServiceClient(PlayerBase):
    SERVICE_NAME = 'Music'            # must match "services = Music:music_service.py" in buildozer.spec
    ACK_TIMEOUT_START = 7.0           # seconds the service gets to start and answer
    ACK_TIMEOUT = 4.0                 # seconds a normal command may take
    STALE_MS = 6000                   # a state older than this comes from a dead engine

    def __init__(self, data_dir):
        super().__init__()
        self.data_dir = data_dir
        self.queue_path = os.path.join(data_dir, 'playback_queue.json')
        self._qkey = None
        self._seq = 0
        self._target = 'service'              # 'service', or 'embedded' after a fallback
        self._embedded = None
        self._receiver = None
        self._state_mtime = 0.0
        self._last_ts = 0
        self._ack_seq = 0
        self._last_error_id = None
        self._waiting = None                  # (seq, deadline, command) of an unanswered command
        self._last_load = None                # last LOAD_PLAY command, replayed after a fallback
        self._playing = False
        self._pos_ms = 0.0
        self._dur_ms = 0.0
        self._ts_ms = 0.0

    # ---------------- lifecycle ----------------
    def start(self):
        try:
            from android.broadcast import BroadcastReceiver
            pkg = get_android_activity().getPackageName()
            self._receiver = BroadcastReceiver(self._on_state_broadcast, actions=[pkg + '.STATE'])
            self._receiver.start()
        except Exception as e:
            ipc.log(self.data_dir, 'app', 'state receiver failed (file polling still works):', e)
        self.poll()
        self.sync()

    def close(self):
        """The window is closing - the service (and the music) keep running."""
        try:
            if self._receiver is not None:
                self._receiver.stop()
        except Exception:
            pass
        self._receiver = None
        if self._embedded is not None:               # embedded music cannot outlive the app
            self._embedded.events.put(('cmd', {'cmd': 'QUIT'}))

    def sync(self):
        self._send({'cmd': 'SYNC'}, track=False)
        self.poll()

    def quit(self):
        """Stop the music for good: the service shuts down and removes its notification."""
        self._send({'cmd': 'QUIT'}, track=False)
        self.close()

    # ---------------- sending commands ----------------
    def _send(self, command, track=True):
        """Writes the command file (primary) and sends a broadcast (fast path)."""
        self._seq = max(self._seq + 1, int(time.time() * 1000))
        command = dict(command, seq=self._seq, target=self._target)
        try:
            ipc.send_command(self.data_dir, command)
        except Exception as e:
            ipc.log(self.data_dir, 'app', 'could not write command file:', e)
        try:
            from jnius import autoclass
            activity = get_android_activity()
            Intent = autoclass('android.content.Intent')
            intent = Intent(activity.getPackageName() + '.CMD')
            intent.setPackage(activity.getPackageName())
            for key, value in command.items():
                intent.putExtra(key, str(value))
            activity.sendBroadcast(intent)
        except Exception as e:
            ipc.log(self.data_dir, 'app', 'broadcast failed (file channel still works):', e)
        if track and self.current_song is not None and self._waiting is None:
            timeout = self.ACK_TIMEOUT_START if command.get('cmd') == 'LOAD_PLAY' else self.ACK_TIMEOUT
            self._waiting = (self._seq, time.time() + timeout, command)
        return self._seq

    def _start_service(self):
        from jnius import autoclass
        activity = get_android_activity()
        Service = autoclass('{}.Service{}'.format(activity.getPackageName(), self.SERVICE_NAME))
        try:
            Service.start(activity, self.data_dir)
        except Exception:
            Service.start(activity, '', 'KazeMusic', 'Playing music', self.data_dir)

    # ---------------- queue ----------------
    def set_queue_and_play(self, songs, index):
        if not songs or not (0 <= index < len(songs)):
            return
        key = hashlib.md5(('|'.join(s['id'] for s in songs) + '#' + str(len(songs))).encode('utf-8')).hexdigest()
        if key != self._qkey:
            slim = [{k: s.get(k) for k in ('id', 'title', 'artist', 'album', 'album_id', 'duration', 'uri')}
                    for s in songs]
            ipc.write_json_atomic(self.queue_path, {'songs': slim})
            self._qkey = key

        song = songs[index]
        # show something immediately; the engine confirms a moment later
        self.current_song = {'id': song['id'], 'title': song['title'], 'artist': song['artist'],
                             'album': song.get('album', ''), 'duration': song.get('duration') or 0}
        self.current_index = index
        self.cover_path = None
        self._playing = True
        self._pos_ms = 0.0
        self._dur_ms = (song.get('duration') or 0) * 1000.0
        self._ts_ms = time.time() * 1000
        self._waiting = None
        self._changed()

        command = {'cmd': 'LOAD_PLAY', 'qpath': self.queue_path, 'qkey': key, 'index': index}
        self._last_load = command
        self._send(command)                  # the command file exists BEFORE the service starts
        if self._target == 'service':
            try:
                self._start_service()
            except Exception as e:
                ipc.log(self.data_dir, 'app', 'service start failed:', e)
                self._fallback('The playback service could not be started: {}'.format(e))

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

    def poll(self):
        """Called by the app every few hundred ms: reads the state file and
        checks that the engine answers."""
        try:
            path = ipc.state_path(self.data_dir)
            mtime = os.path.getmtime(path)
            if mtime != self._state_mtime:
                self._state_mtime = mtime
                state = ipc.read_state(self.data_dir)
                if state:
                    self._apply_state(state, source='file')
        except OSError:
            pass
        waiting = self._waiting
        if waiting and self._target == 'service':
            seq, deadline, command = waiting
            if self._ack_seq >= seq:
                self._waiting = None
            elif time.time() > deadline:
                self._waiting = None
                self._fallback('The playback service did not answer.')

    def _on_state_broadcast(self, context, intent):
        """Runs on an Android thread (not the Kivy thread)."""
        try:
            state = {}
            for key in ('role', 'song_id', 'title', 'artist', 'album', 'duration', 'index', 'playing',
                        'pos_ms', 'dur_ms', 'ts', 'cover', 'error', 'error_id', 'ack_seq'):
                value = intent.getStringExtra(key)
                state[key] = '' if value is None else str(value)
            self._apply_state(state, source='broadcast')
        except Exception as e:
            print('Bad state from the service:', e)

    def _apply_state(self, st, source):
        def number(key):
            try:
                return float(st.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        if st.get('role') != self._target:
            return                                    # a state from the other engine
        ts = number('ts')
        if ts < self._last_ts:
            return                                    # older than what we already know
        if abs(time.time() * 1000 - ts) > self.STALE_MS and source == 'file':
            return                                    # left over from a dead engine
        self._last_ts = ts
        self._ack_seq = max(self._ack_seq, int(number('ack_seq')))
        if self._waiting and number('ack_seq') < self._waiting[0]:
            return                                    # engine has not seen our last command yet:
                                                      # keep the optimistic state

        song_id = str(st.get('song_id') or '')
        if song_id:
            self.current_song = {'id': song_id, 'title': st.get('title', ''), 'artist': st.get('artist', ''),
                                 'album': st.get('album', ''), 'duration': number('duration')}
            self.current_index = int(number('index'))
        else:
            self.current_song = None
            self.current_index = -1
        self._playing = str(st.get('playing')) == '1'
        self._pos_ms = number('pos_ms')
        self._dur_ms = number('dur_ms')
        self._ts_ms = ts or time.time() * 1000
        self.cover_path = st.get('cover') or None
        error, error_id = st.get('error'), st.get('error_id')
        self._changed()
        if self._last_error_id is None:
            self._last_error_id = error_id            # first state: don't replay an old error
        elif error and error_id != self._last_error_id:
            self._last_error_id = error_id
            self._error(error)

    # ---------------- fallback ----------------
    def _fallback(self, reason):
        """The service does not work: run the same engine inside the app."""
        if self._target == 'embedded':
            return
        tail = ipc.tail_log(self.data_dir, 20)
        ipc.log(self.data_dir, 'app', 'FALLBACK to the embedded engine:', reason)
        self._send({'cmd': 'QUIT'}, track=False)       # in case the service wakes up later
        self._target = 'embedded'
        self._last_ts = 0
        self._ack_seq = 0
        try:
            from engine import Engine
            import threading
            self._embedded = Engine(role='embedded', context=get_android_activity(), data_dir=self.data_dir)
            threading.Thread(target=self._embedded.run, daemon=True).start()
        except Exception as e:
            ipc.log(self.data_dir, 'app', 'embedded engine failed:', e)
            self._error('Playback failed: {}\n\n{}'.format(e, tail))
            return
        if self._last_load:
            self._send(dict(self._last_load), track=False)
        self._error('The background service did not start, so music plays inside the app only '
                    '(it stops when you close the app).\n\nReason: {}\n\nLog:\n{}'.format(reason, tail))


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
