# -*- coding: utf-8 -*-
"""
KazeMusic playback service (Android foreground service).

This script runs in its OWN process (python-for-android "services" entry in
buildozer.spec).  It owns the MediaPlayer, the play queue, the media
notification and the audio focus, so the music keeps playing - and the
notification buttons keep working - when the app window is closed.

Talking to the app (all through explicit, package-local broadcasts):

    app      -> service : <package>.CMD    (extras: cmd, seq, qpath, qkey, index, ms)
    service  -> app     : <package>.STATE  (extras: song_id, title, playing, pos_ms ...)
    notification buttons send the same CMD broadcasts straight to the service.

Everything below runs on ONE thread (the service thread).  Android callbacks
(broadcasts, MediaPlayer, audio focus) only put events into a queue.

This module must not import kivy.
"""

import os
import json
import time
import queue
import traceback

from jnius import autoclass, cast, PythonJavaClass, java_method
from android.broadcast import BroadcastReceiver

from cover_art import CoverLoader

NOTIF_ID = 1001
CHANNEL_ID = 'kazemusic_playback'
NOISY_ACTION = 'android.media.AUDIO_BECOMING_NOISY'

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


def log(*args):
    print('[KazeService]', *args, flush=True)


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
    def __init__(self):
        PythonService = autoclass('org.kivy.android.PythonService')
        self.service = PythonService.mService
        self.pkg = self.service.getPackageName()
        self.data_dir = self.service.getFilesDir().getAbsolutePath()

        self.events = queue.Queue()
        self.quit = False

        # queue
        self.songs = []
        self.qkey = None
        self.index = -1
        self.last_seq = 0

        # player
        self.player = None
        self.listeners = None
        self.gen = 0
        self.prepared = False
        self.want_play = False
        self.pending_seek = None
        self.error = ''

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
        self.session = None
        self.receiver = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        self._register_receivers()
        self._load_boot_command()
        log('service running')
        while not self.quit:
            try:
                item = self.events.get(timeout=5)
            except queue.Empty:
                # heartbeat: keeps the app's position estimate accurate
                if self.player is not None and self.want_play:
                    self.broadcast_state()
                continue
            try:
                self._dispatch(item)
            except Exception:
                log('event failed:', traceback.format_exc())
        self._shutdown()

    def _register_receivers(self):
        cmd_action = self.pkg + '.CMD'

        def on_receive(context, intent):
            try:
                if intent.getAction() == NOISY_ACTION:
                    self.events.put(('noisy',))
                    return
                data = {}
                for key in ('cmd', 'seq', 'qpath', 'qkey', 'index', 'ms'):
                    value = intent.getStringExtra(key)
                    if value is not None:
                        data[key] = str(value)
                self.events.put(('cmd', data))
            except Exception:
                log('receive failed:', traceback.format_exc())

        self.receiver = BroadcastReceiver(on_receive, actions=[cmd_action, NOISY_ACTION])
        self.receiver.start()

    def _load_boot_command(self):
        """The app starts the service and also writes the first command to a
        file, so it is not lost if the broadcast arrives before we listen."""
        path = os.environ.get('PYTHON_SERVICE_ARGUMENT', '')
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            os.remove(path)
            self.events.put(('cmd', {k: str(v) for k, v in data.items()}))
        except Exception:
            log('boot command failed:', traceback.format_exc())

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
        cmd = data.get('cmd', '')
        if cmd == 'LOAD_PLAY':
            seq = int(data.get('seq') or 0)
            if seq and seq <= self.last_seq:
                return                       # already executed (boot file + broadcast)
            self.last_seq = max(self.last_seq, seq)
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
        elif cmd == 'SYNC':
            self.broadcast_state()
        elif cmd == 'QUIT':
            self.quit = True

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
            player.setDataSource(self.service, Uri.parse(song['uri']))
            player.prepareAsync()
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
            log('start failed:', e)
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
        self.error = message
        self.refresh()
        self.error = ''

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
            log('audio focus failed:', e)

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
    def broadcast_state(self):
        try:
            Intent = autoclass('android.content.Intent')
            intent = Intent(self.pkg + '.STATE')
            intent.setPackage(self.pkg)
            song = self.current_song()
            values = {
                'song_id': song['id'] if song else '',
                'index': self.index,
                'title': song['title'] if song else '',
                'artist': song['artist'] if song else '',
                'album': song.get('album', '') if song else '',
                'duration': (song.get('duration') or 0) if song else 0,
                'playing': '1' if (self.want_play and self.player is not None) else '0',
                'pos_ms': self.position_ms(),
                'dur_ms': self.duration_ms(),
                'ts': int(time.time() * 1000),
                'cover': self.cover_path or '',
                'error': self.error,
            }
            for key, value in values.items():
                intent.putExtra(key, str(value))
            self.service.sendBroadcast(intent)
        except Exception:
            log('state broadcast failed:', traceback.format_exc())

    def refresh(self):
        try:
            self.post_notification()
        except Exception:
            log('notification failed:', traceback.format_exc())
        self.broadcast_state()

    # ------------------------------------------------------------------
    # Notification + media session
    # ------------------------------------------------------------------
    def _cover_bitmap(self):
        path = self.cover_path
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

    def _update_session(self, song, playing, bitmap):
        if self.session is None:
            MediaSession = autoclass('android.media.session.MediaSession')
            self.session = MediaSession(self.service, 'KazeMusic')
            self.session.setActive(True)
        session = self.session

        MediaMetadata = autoclass('android.media.MediaMetadata')
        MetaBuilder = autoclass('android.media.MediaMetadata$Builder')
        meta = MetaBuilder()
        meta.putString(MediaMetadata.METADATA_KEY_TITLE, song['title'])
        meta.putString(MediaMetadata.METADATA_KEY_ARTIST, song['artist'])
        meta.putLong(MediaMetadata.METADATA_KEY_DURATION, self.duration_ms())
        if bitmap is not None:
            meta.putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, bitmap)
        session.setMetadata(meta.build())

        PlaybackState = autoclass('android.media.session.PlaybackState')
        StateBuilder = autoclass('android.media.session.PlaybackState$Builder')
        actions = (PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE |
                   PlaybackState.ACTION_PLAY_PAUSE | PlaybackState.ACTION_SKIP_TO_NEXT |
                   PlaybackState.ACTION_SKIP_TO_PREVIOUS)
        state = PlaybackState.STATE_PLAYING if playing else PlaybackState.STATE_PAUSED
        sb = StateBuilder()
        sb.setActions(actions)
        sb.setState(state, self.position_ms(), 1.0 if playing else 0.0)
        session.setPlaybackState(sb.build())
        return session.getSessionToken()

    def post_notification(self):
        song = self.current_song()
        if song is None:
            return
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

        def cs(text):
            return cast(CharSequence, String(text))

        playing = bool(self.want_play and self.player is not None)

        bitmap = None
        try:
            bitmap = self._cover_bitmap()
        except Exception as e:
            log('cover bitmap failed:', e)
        token = None
        try:
            token = self._update_session(song, playing, bitmap)
        except Exception as e:
            log('media session failed:', e)

        if sdk >= 26:
            channel = NotificationChannel(CHANNEL_ID, cs('KazeMusic Playback'),
                                          NotificationManager.IMPORTANCE_LOW)
            manager.createNotificationChannel(channel)
            builder = Builder(self.service, CHANNEL_ID)
        else:
            builder = Builder(self.service)

        flags = (1 << 26) | (1 << 27)               # IMMUTABLE | UPDATE_CURRENT
        open_intent = self.service.getPackageManager().getLaunchIntentForPackage(self.pkg)
        open_pending = PendingIntent.getActivity(self.service, 0, open_intent, flags)

        def button(cmd, code):
            intent = Intent(self.pkg + '.CMD')
            intent.setPackage(self.pkg)
            intent.putExtra('cmd', cmd)
            return PendingIntent.getBroadcast(self.service, code, intent, flags)

        icons = autoclass('android.R$drawable')
        builder.setContentTitle(cs(song['title'])) \
               .setContentText(cs(song['artist'])) \
               .setSmallIcon(self.service.getApplicationInfo().icon) \
               .setContentIntent(open_pending) \
               .setOngoing(True) \
               .setOnlyAlertOnce(True) \
               .setShowWhen(False) \
               .setVisibility(1)
        if bitmap is not None:
            builder.setLargeIcon(bitmap)

        builder.addAction(icons.ic_media_previous, cs('Previous'), button('PREV', 11))
        builder.addAction(icons.ic_media_pause if playing else icons.ic_media_play,
                          cs('Pause' if playing else 'Play'), button('TOGGLE', 12))
        builder.addAction(icons.ic_media_next, cs('Next'), button('NEXT', 13))
        builder.addAction(icons.ic_menu_close_clear_cancel, cs('Close'), button('QUIT', 14))

        MediaStyle = autoclass('android.app.Notification$MediaStyle')
        style = MediaStyle()
        try:
            style.setShowActionsInCompactView([0, 1, 2])
        except Exception as e:
            log('compact actions failed:', e)
        if token is not None:
            try:
                style.setMediaSession(token)
            except Exception as e:
                log('attaching media session failed:', e)
        builder.setStyle(style)

        notification = builder.build()
        if not self.foreground:
            self.service.startForeground(NOTIF_ID, notification)
            self.foreground = True
        else:
            manager.notify(NOTIF_ID, notification)

    # ------------------------------------------------------------------
    def _shutdown(self):
        log('shutting down')
        self.want_play = False
        self._release_player()
        self._abandon_focus()
        self.index = -1
        self.broadcast_state()                  # tells the app that playback ended
        for step in (
            lambda: self.receiver.stop(),
            lambda: self.session.release(),
            lambda: self.service.stopForeground(True),
            lambda: self.service.getSystemService(
                autoclass('android.content.Context').NOTIFICATION_SERVICE).cancel(NOTIF_ID),
            lambda: self.service.stopSelf(),
        ):
            try:
                step()
            except Exception:
                pass


def main():
    try:
        Engine().run()
    except Exception:
        log('fatal:', traceback.format_exc())


if not os.environ.get('KAZE_TEST'):      # tests import this file without starting the loop
    main()
