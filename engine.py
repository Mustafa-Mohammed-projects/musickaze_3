# -*- coding: utf-8 -*-
"""
KazeMusic playback engine (Android).

The engine owns the MediaPlayer, the play queue, the media notification and the
audio focus.  It normally runs inside the foreground service (music_service.py,
its own process) so music keeps playing when the app is closed.  If the
service cannot be started, the app runs the very same engine inside its own
process ("embedded" role) so playback still works.

Talking to the app:
    app -> engine : command files (primary, see ipc.py) + <package>.CMD broadcasts
    engine -> app : kaze_state.json (primary, written every second and on every
                    change) + <package>.STATE broadcasts
    notification buttons -> engine : <package>.CMD broadcasts

Everything runs on ONE thread (Engine.run).  Android callbacks only put events
into a queue.  This module must not import kivy.
"""

import os
import json
import time
import queue
import threading
import collections
import traceback

from jnius import autoclass, cast, PythonJavaClass, java_method
from android.broadcast import BroadcastReceiver

import ipc
from cover_art import CoverLoader

NOTIF_ID = 1001
CHANNEL_ID = 'kazemusic_playback'
NOISY_ACTION = 'android.media.AUDIO_BECOMING_NOISY'
SKIP_MS = 10000

FOCUS_GAIN = 1
FOCUS_LOSS = -1
FOCUS_LOSS_TRANSIENT = -2
FOCUS_LOSS_DUCK = -3

ERROR_TEXT = {
    -1004: 'cannot read the file (I/O error)',
    -1007: 'the file is damaged or malformed',
    -1010: 'this audio format is not supported',
    -110: 'timed out while opening the file',
    100: 'the media server stopped working',
}


# ----------------------------------------------------------------------
# Java callback interfaces
# ----------------------------------------------------------------------
class PreparedListener(PythonJavaClass):
    __javainterfaces__ = ['android/media/MediaPlayer$OnPreparedListener']
    __javacontext__ = 'app'

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    @java_method('(Landroid/media/MediaPlayer;)V')
    def onPrepared(self, mp):
        self.callback()


class CompletionListener(PythonJavaClass):
    __javainterfaces__ = ['android/media/MediaPlayer$OnCompletionListener']
    __javacontext__ = 'app'

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    @java_method('(Landroid/media/MediaPlayer;)V')
    def onCompletion(self, mp):
        self.callback()


class ErrorListener(PythonJavaClass):
    __javainterfaces__ = ['android/media/MediaPlayer$OnErrorListener']
    __javacontext__ = 'app'

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    @java_method('(Landroid/media/MediaPlayer;II)Z')
    def onError(self, mp, what, extra):
        self.callback(what, extra)
        return True


class FocusListener(PythonJavaClass):
    __javainterfaces__ = ['android/media/AudioManager$OnAudioFocusChangeListener']
    __javacontext__ = 'app'

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    @java_method('(I)V')
    def onAudioFocusChange(self, change):
        self.callback(change)


# ----------------------------------------------------------------------
# The engine
# ----------------------------------------------------------------------
class Engine:
    def __init__(self, role='service', context=None, data_dir=None):
        self.role = role                      # 'service' or 'embedded'
        if role == 'service':
            PythonService = autoclass('org.kivy.android.PythonService')
            self.service = PythonService.mService
        else:
            self.service = context            # the Activity
        self.pkg = self.service.getPackageName()
        self.data_dir = data_dir or self.service.getFilesDir().getAbsolutePath()

        self.events = queue.Queue()
        self.quit = False

        # queue
        self.songs = []
        self.qkey = None
        self.index = -1
        self.seen = collections.deque(maxlen=400)   # command numbers already executed
        self.ack_seq = 0

        # player
        self.player = None
        self.listeners = None
        self.gen = 0
        self.prepared = False
        self.want_play = False
        self.pending_seek = None
        self.error = ''
        self.error_id = 0

        # audio focus
        self.focus_listener = None
        self.has_focus = False
        self.resume_on_focus = False

        # cover / notification / session
        self.covers = CoverLoader(self.data_dir, context_provider=lambda: self.service)
        self.cover_path = None
        self._bitmap = None
        self._bitmap_path = None
        self.foreground = False
        self._fg_failed_at = 0.0
        self.session = None
        self.receiver = None
        self.notifier = Notifier(self)

    def log(self, *args):
        ipc.log(self.data_dir, self.role, *args)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        self.log('engine starting, package', self.pkg, 'dir', self.data_dir)
        try:
            self._register_receivers()
        except Exception:
            self.log('receiver registration FAILED (command files still work):', traceback.format_exc())
        self.write_state()
        self.log('engine running')
        last_beat = 0.0
        while not self.quit:
            self._poll_command_files()
            try:
                item = self.events.get(timeout=0.25)
            except queue.Empty:
                item = None
            if item is not None:
                try:
                    self._dispatch(item)
                except Exception:
                    self.log('event failed:', traceback.format_exc())
            now = time.time()
            if now - last_beat >= 1.0:          # heartbeat: the app checks that we are alive
                last_beat = now
                self.write_state()
        self._shutdown()

    def _register_receivers(self):
        cmd_action = self.pkg + '.CMD'

        def on_receive(context, intent):
            try:
                if intent.getAction() == NOISY_ACTION:
                    self.events.put(('noisy',))
                    return
                data = {}
                for key in ('cmd', 'seq', 'qpath', 'qkey', 'index', 'ms', 'target'):
                    value = intent.getStringExtra(key)
                    if value is not None:
                        data[key] = str(value)
                self.log('broadcast received:', data.get('cmd'), 'seq', data.get('seq', '-'))
                self.events.put(('cmd', data))
            except Exception:
                self.log('receive failed:', traceback.format_exc())

        self.receiver = BroadcastReceiver(on_receive, actions=[cmd_action, NOISY_ACTION])
        self.receiver.start()
        self.log('broadcast receiver registered for', cmd_action)

    def _poll_command_files(self):
        """Primary command channel: one small json file per command."""
        for path, data in ipc.pending_commands(self.data_dir):
            target = data.get('target')
            if target and target != self.role:
                continue                    # meant for the other engine
            try:
                os.remove(path)             # only the one who removes it runs it
            except OSError:
                continue
            self.events.put(('cmd', {k: str(v) for k, v in data.items()}))

    def _dispatch(self, item):
        kind = item[0]
        if kind == 'cmd':
            self._command(item[1])
        elif kind == 'noisy':
            if self.want_play:
                self.pause()
        elif kind == 'prepared':
            self._on_prepared(item[1])
        elif kind == 'complete':
            if item[1] == self.gen:
                self.next()
        elif kind == 'error':
            self._on_error(item[1], item[2], item[3])
        elif kind == 'cover':
            if item[1] == self.gen:
                self.cover_path = item[2]
                self.refresh()
        elif kind == 'focus':
            self._on_focus(item[1])

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def _command(self, data):
        target = data.get('target')
        if target and target != self.role:
            return
        cmd = data.get('cmd', '')
        seq = int(float(data.get('seq') or 0))
        if seq:
            if seq in self.seen:
                return                       # same command arrived by file AND broadcast
            self.seen.append(seq)
            self.ack_seq = max(self.ack_seq, seq)
        self.log('command', cmd, data.get('index', ''), data.get('ms', ''))
        self.write_state()                   # acknowledge at once: the app knows we are alive

        if cmd == 'LOAD_PLAY':
            self._load_queue(data.get('qpath'), data.get('qkey'))
            self.play_index(int(data.get('index') or 0))
        elif cmd == 'TOGGLE':
            self.pause() if self.want_play else self.resume()
        elif cmd == 'PAUSE':
            self.pause()
        elif cmd == 'RESUME':
            self.resume()
        elif cmd == 'NEXT':
            self.next()
        elif cmd == 'PREV':
            self.prev()
        elif cmd == 'SEEK':
            self.seek(int(float(data.get('ms') or 0)))
        elif cmd == 'REW':
            self.seek(self.position_ms() - SKIP_MS)
        elif cmd == 'FWD':
            limit = max(self.duration_ms() - 1000, 0)
            self.seek(min(self.position_ms() + SKIP_MS, limit))
        elif cmd == 'SYNC':
            pass
        elif cmd == 'QUIT':
            self.quit = True
        self.write_state()                   # the app sees the acknowledgement at once

    def _load_queue(self, qpath, qkey):
        if qkey and qkey == self.qkey and self.songs:
            return
        with open(qpath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.songs = list(data.get('songs', []))
        self.qkey = qkey

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------
    def current_song(self):
        if 0 <= self.index < len(self.songs):
            return self.songs[self.index]
        return None

    def play_index(self, index):
        if not self.songs:
            return
        index %= len(self.songs)
        song = self.songs[index]
        self._release_player()
        self.index = index
        self.gen += 1
        gen = self.gen
        self.prepared = False
        self.want_play = True
        self.pending_seek = None
        self.cover_path = None
        self.resume_on_focus = False
        self.error = ''

        MediaPlayer = autoclass('android.media.MediaPlayer')
        Uri = autoclass('android.net.Uri')
        player = MediaPlayer()
        listeners = (
            PreparedListener(lambda: self.events.put(('prepared', gen))),
            CompletionListener(lambda: self.events.put(('complete', gen))),
            ErrorListener(lambda what, extra: self.events.put(('error', gen, what, extra))),
        )
        try:
            player.setOnPreparedListener(listeners[0])
            player.setOnCompletionListener(listeners[1])
            player.setOnErrorListener(listeners[2])
            try:
                AudioAttributes = autoclass('android.media.AudioAttributes')
                Builder = autoclass('android.media.AudioAttributes$Builder')
                player.setAudioAttributes(
                    Builder().setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .setUsage(AudioAttributes.USAGE_MEDIA).build())
            except Exception:
                pass
            try:
                PowerManager = autoclass('android.os.PowerManager')
                player.setWakeMode(self.service, PowerManager.PARTIAL_WAKE_LOCK)
            except Exception:
                pass
            self.log('play_index: setDataSource', song['uri'])
            player.setDataSource(self.service, Uri.parse(song['uri']))
            player.prepareAsync()
            self.log('play_index: prepareAsync ok')
        except Exception as e:
            try:
                player.release()
            except Exception:
                pass
            self._fail('Failed to open the song: {}'.format(e))
            return

        self.player = player
        self.listeners = listeners          # keep references (no garbage collection)
        self._request_focus()
        self.covers.request(song, lambda s, path: self.events.put(('cover', gen, path)))
        self.refresh()
        self.log('play_index: done')

    def _release_player(self):
        player, self.player = self.player, None
        self.listeners = None
        self.prepared = False
        if player is not None:
            try:
                player.release()
            except Exception:
                pass

    def _on_prepared(self, gen):
        if gen != self.gen or self.player is None:
            return
        self.prepared = True
        try:
            if self.pending_seek is not None:
                self.player.seekTo(int(self.pending_seek))
                self.pending_seek = None
            if self.want_play:
                self.player.start()
        except Exception as e:
            self.log('start failed:', e)
        self.refresh()

    def _on_error(self, gen, what, extra):
        if gen != self.gen:
            return
        self.want_play = False
        self._release_player()                  # an errored MediaPlayer cannot be reused
        reason = ERROR_TEXT.get(extra) or ERROR_TEXT.get(what) or 'unknown error'
        self._fail('Cannot play this song: {} (code {}/{})'.format(reason, what, extra))

    def _fail(self, message):
        self.want_play = False
        self.error = message              # stays in the state until the next song starts
        self.error_id += 1
        self.refresh()

    def pause(self, keep_focus=False):
        self.want_play = False
        if self.player is not None and self.prepared:
            try:
                self.player.pause()
            except Exception:
                pass
        if not keep_focus:
            self._abandon_focus()
        self.refresh()

    def resume(self):
        if self.player is None:
            if self.current_song() is not None:
                self.play_index(self.index)       # e.g. after an error
            return
        self.want_play = True
        self._request_focus()
        if self.prepared:
            try:
                self.player.start()
            except Exception:
                pass
        self.refresh()

    def next(self):
        if self.songs:
            self.play_index(self.index + 1)

    def prev(self):
        if self.songs:
            self.play_index(self.index - 1)

    def seek(self, ms):
        ms = max(0, ms)
        if self.player is None:
            return
        if self.prepared:
            try:
                self.player.seekTo(ms)
            except Exception:
                pass
        else:
            self.pending_seek = ms
        self.refresh()

    def position_ms(self):
        if self.player is not None and self.prepared:
            try:
                return int(self.player.getCurrentPosition())
            except Exception:
                return 0
        return int(self.pending_seek or 0)

    def duration_ms(self):
        if self.player is not None and self.prepared:
            try:
                value = int(self.player.getDuration())
                if value > 0:
                    return value
            except Exception:
                pass
        song = self.current_song() or {}
        return int((song.get('duration') or 0) * 1000)

    # ------------------------------------------------------------------
    # Audio focus (calls, other music apps) - best effort
    # ------------------------------------------------------------------
    def _audio_manager(self):
        Context = autoclass('android.content.Context')
        return self.service.getSystemService(Context.AUDIO_SERVICE)

    def _request_focus(self):
        if self.has_focus:
            return
        try:
            AudioManager = autoclass('android.media.AudioManager')
            if self.focus_listener is None:
                self.focus_listener = FocusListener(lambda change: self.events.put(('focus', change)))
            result = self._audio_manager().requestAudioFocus(
                self.focus_listener, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN)
            self.has_focus = (result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED)
        except Exception as e:
            self.log('audio focus failed:', e)

    def _abandon_focus(self):
        if not self.has_focus or self.focus_listener is None:
            return
        try:
            self._audio_manager().abandonAudioFocus(self.focus_listener)
        except Exception:
            pass
        self.has_focus = False

    def _set_volume(self, value):
        if self.player is not None:
            try:
                self.player.setVolume(value, value)
            except Exception:
                pass

    def _on_focus(self, change):
        if change == FOCUS_GAIN:
            self._set_volume(1.0)
            if self.resume_on_focus:
                self.resume_on_focus = False
                self.resume()
        elif change == FOCUS_LOSS_DUCK:
            self._set_volume(0.2)
        elif change == FOCUS_LOSS_TRANSIENT:
            if self.want_play:
                self.resume_on_focus = True
                self.pause(keep_focus=True)
        elif change == FOCUS_LOSS:
            self.resume_on_focus = False
            self.has_focus = False
            if self.want_play:
                self.pause(keep_focus=True)

    # ------------------------------------------------------------------
    # State for the app
    # ------------------------------------------------------------------
    def state_dict(self):
        song = self.current_song()
        return {
            'role': self.role,
            'pid': os.getpid(),
            'song_id': song['id'] if song else '',
            'index': self.index,
            'title': song['title'] if song else '',
            'artist': song['artist'] if song else '',
            'album': (song.get('album') or '') if song else '',
            'duration': (song.get('duration') or 0) if song else 0,
            'playing': '1' if (self.want_play and self.player is not None) else '0',
            'pos_ms': self.position_ms(),
            'dur_ms': self.duration_ms(),
            'ts': int(time.time() * 1000),
            'cover': self.cover_path or '',
            'error': self.error,
            'error_id': self.error_id,
            'ack_seq': self.ack_seq,
        }

    def write_state(self):
        """Writes the state file (primary channel).  Never raises."""
        try:
            ipc.write_state(self.data_dir, self.state_dict())
        except Exception:
            self.log('state file failed:', traceback.format_exc())

    def broadcast_state(self):
        self.write_state()
        try:
            Intent = autoclass('android.content.Intent')
            intent = Intent(self.pkg + '.STATE')
            intent.setPackage(self.pkg)
            for key, value in self.state_dict().items():
                intent.putExtra(key, str(value))
            self.service.sendBroadcast(intent)
        except Exception:
            self.log('state broadcast failed:', traceback.format_exc())

    def refresh(self):
        """Tell the notification thread and the app about the new state."""
        self.notifier.request(self.snapshot())
        self.broadcast_state()

    def snapshot(self):
        """Plain copy of what the notification needs (taken on the engine thread)."""
        song = self.current_song()
        if song is None:
            return None
        return {
            'title': song['title'], 'artist': song['artist'],
            'playing': bool(self.want_play and self.player is not None),
            'pos_ms': self.position_ms(), 'dur_ms': self.duration_ms(),
            'cover': self.cover_path,
        }

    # ------------------------------------------------------------------
    # Notification + media session (runs on the Notifier thread, so a slow or
    # failing notification can never block or crash the playback logic)
    # ------------------------------------------------------------------
    def _cover_bitmap(self, path):
        if not path or not os.path.exists(path):
            self._bitmap, self._bitmap_path = None, None
            return None
        if self._bitmap_path != path:
            BitmapFactory = autoclass('android.graphics.BitmapFactory')
            Options = autoclass('android.graphics.BitmapFactory$Options')
            opts = Options()
            opts.inSampleSize = 4          # small: notifications travel through Binder (~1 MB limit)
            self._bitmap = BitmapFactory.decodeFile(path, opts)
            self._bitmap_path = path
        return self._bitmap

    def _update_session(self, snap, bitmap):
        if self.session is None:
            MediaSession = autoclass('android.media.session.MediaSession')
            self.session = MediaSession(self.service, 'KazeMusic')
            self.session.setActive(True)
        session = self.session

        MediaMetadata = autoclass('android.media.MediaMetadata')
        MetaBuilder = autoclass('android.media.MediaMetadata$Builder')
        meta = MetaBuilder()
        meta.putString(MediaMetadata.METADATA_KEY_TITLE, snap['title'])
        meta.putString(MediaMetadata.METADATA_KEY_ARTIST, snap['artist'])
        meta.putLong(MediaMetadata.METADATA_KEY_DURATION, int(snap['dur_ms']))
        if bitmap is not None:
            meta.putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, bitmap)
        session.setMetadata(meta.build())

        PlaybackState = autoclass('android.media.session.PlaybackState')
        StateBuilder = autoclass('android.media.session.PlaybackState$Builder')
        actions = (PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE |
                   PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_SKIP_TO_NEXT |
                   PlaybackState.ACTION_SKIP_TO_PREVIOUS | PlaybackState.ACTION_REWIND |
                   PlaybackState.ACTION_FAST_FORWARD)
        state = PlaybackState.STATE_PLAYING if snap['playing'] else PlaybackState.STATE_PAUSED
        sb = StateBuilder()
        sb.setActions(actions)
        sb.setState(state, int(snap['pos_ms']), 1.0 if snap['playing'] else 0.0)
        session.setPlaybackState(sb.build())
        return session.getSessionToken()

    def post_notification(self, snap):
        Context = autoclass('android.content.Context')
        Intent = autoclass('android.content.Intent')
        PendingIntent = autoclass('android.app.PendingIntent')
        NotificationManager = autoclass('android.app.NotificationManager')
        NotificationChannel = autoclass('android.app.NotificationChannel')
        Builder = autoclass('android.app.Notification$Builder')
        CharSequence = autoclass('java.lang.CharSequence')
        String = autoclass('java.lang.String')
        sdk = autoclass('android.os.Build$VERSION').SDK_INT
        manager = self.service.getSystemService(Context.NOTIFICATION_SERVICE)
        icons = autoclass('android.R$drawable')

        def cs(text):
            return cast(CharSequence, String(text))

        def new_builder():
            if sdk >= 26:
                return Builder(self.service, CHANNEL_ID)
            return Builder(self.service)

        if snap is None:                            # nothing to show any more
            manager.cancel(NOTIF_ID)
            if self.role == 'service' and self.foreground:
                self.service.stopForeground(True)
                self.foreground = False
            return

        playing = snap['playing']
        if sdk >= 26:
            channel = NotificationChannel(CHANNEL_ID, cs('KazeMusic Playback'),
                                          NotificationManager.IMPORTANCE_LOW)
            manager.createNotificationChannel(channel)

        # 1) A foreground service MUST start with a notification the system
        #    accepts, otherwise Android kills the process without any message.
        #    So the first one is minimal and uses a system icon that always exists.
        #    Android 12+ may REFUSE startForeground when the app is in the background;
        #    that must not prevent the notification (and its buttons) from showing.
        if self.role == 'service' and not self.foreground and time.time() - self._fg_failed_at > 5:
            try:
                self.log('notification: startForeground (minimal)')
                minimal = new_builder() \
                    .setSmallIcon(icons.ic_media_play) \
                    .setContentTitle(cs(snap['title'])) \
                    .setContentText(cs(snap['artist'])) \
                    .setOngoing(True) \
                    .build()
                self.service.startForeground(NOTIF_ID, minimal)
                self.foreground = True
                self.log('notification: startForeground ok')
            except Exception as e:
                self._fg_failed_at = time.time()
                self.log('startForeground refused (notification is shown anyway):', str(e)[:200])

        # 2) The full media notification replaces it (same id)
        bitmap = None
        try:
            bitmap = self._cover_bitmap(snap['cover'])
        except Exception as e:
            self.log('cover bitmap failed:', e)
        token = None
        try:
            token = self._update_session(snap, bitmap)
        except Exception as e:
            self.log('media session failed:', e)

        flags = (1 << 26) | (1 << 27)               # IMMUTABLE | UPDATE_CURRENT
        open_pending = None
        try:
            open_intent = self.service.getPackageManager().getLaunchIntentForPackage(self.pkg)
            if open_intent is not None:
                open_pending = PendingIntent.getActivity(self.service, 0, open_intent, flags)
        except Exception as e:
            self.log('open-app intent failed:', e)

        def button(cmd, code):
            intent = Intent(self.pkg + '.CMD')
            intent.setPackage(self.pkg)
            intent.putExtra('cmd', cmd)
            return PendingIntent.getBroadcast(self.service, code, intent, flags)

        builder = new_builder() \
            .setSmallIcon(icons.ic_media_play) \
            .setContentTitle(cs(snap['title'])) \
            .setContentText(cs(snap['artist'])) \
            .setOngoing(self.foreground or playing) \
            .setOnlyAlertOnce(True) \
            .setShowWhen(False) \
            .setVisibility(1)
        if open_pending is not None:
            builder.setContentIntent(open_pending)
        if bitmap is not None:
            builder.setLargeIcon(bitmap)

        # Previous | -10s | Play-Pause | +10s | Next   (swiping the notification away = quit, where Android allows it)
        builder.addAction(icons.ic_media_previous, cs('Previous'), button('PREV', 11))
        builder.addAction(icons.ic_media_rew, cs('Back 10s'), button('REW', 12))
        builder.addAction(icons.ic_media_pause if playing else icons.ic_media_play,
                          cs('Pause' if playing else 'Play'), button('TOGGLE', 13))
        builder.addAction(icons.ic_media_ff, cs('Forward 10s'), button('FWD', 14))
        builder.addAction(icons.ic_media_next, cs('Next'), button('NEXT', 15))
        builder.setDeleteIntent(button('QUIT', 16))

        MediaStyle = autoclass('android.app.Notification$MediaStyle')
        style = MediaStyle()
        try:
            style.setShowActionsInCompactView(0, 2, 4)     # Java varargs: separate ints, not a list
        except Exception as e:
            self.log('compact actions failed (not important):', e)
        if token is not None:
            try:
                style.setMediaSession(token)
            except Exception as e:
                self.log('attaching media session failed:', e)
        builder.setStyle(style)

        manager.notify(NOTIF_ID, builder.build())
        self.log('notification: posted (playing={})'.format(playing))

    # ------------------------------------------------------------------
    def _shutdown(self):
        self.log('shutting down')
        self.want_play = False
        self._release_player()
        self._abandon_focus()
        self.index = -1
        self.notifier.stop()
        self.broadcast_state()                  # tells the app that playback ended
        steps = [
            lambda: self.receiver.stop(),
            lambda: self.session.release(),
            lambda: self.service.getSystemService(
                autoclass('android.content.Context').NOTIFICATION_SERVICE).cancel(NOTIF_ID),
        ]
        if self.role == 'service':
            steps += [lambda: self.service.stopForeground(True),
                      lambda: self.service.stopSelf()]
        for step in steps:
            try:
                step()
            except Exception:
                pass


class Notifier:
    """Builds notifications on its own thread; only the newest request counts."""

    def __init__(self, engine):
        self.engine = engine
        self._cond = threading.Condition()
        self._pending = False
        self._snap = None
        self._thread = None
        self._stopped = False

    def request(self, snap):
        with self._cond:
            if self._stopped:
                return
            self._snap, self._pending = snap, True
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._stopped = True
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while not self._pending and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                snap, self._pending = self._snap, False
            try:
                self.engine.post_notification(snap)
            except Exception:
                self.engine.log('notification failed:', traceback.format_exc())


def run_service():
    """Entry point used by music_service.py (runs in the service process)."""
    data_dir = os.environ.get('PYTHON_SERVICE_ARGUMENT') or None
    try:
        Engine(role='service', data_dir=data_dir).run()
    except Exception:
        ipc.log(data_dir or '/tmp', 'service', 'FATAL:', traceback.format_exc())
