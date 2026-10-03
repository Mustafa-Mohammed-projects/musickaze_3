# -*- coding: utf-8 -*-
"""
Cover-art lookup, shared by the app and the playback service.

IMPORTANT: this module must NOT import kivy - the playback service runs in a
separate process where Kivy is not initialised.
"""

import os
import re
import threading


def _to_bytes(data):
    """pyjnius may hand back bytes, bytearray or a list of signed ints."""
    if data is None:
        return None
    try:
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        if isinstance(data, memoryview):
            return data.tobytes()
        return bytes(bytearray(b & 0xFF for b in data))
    except Exception:
        return None


def _sniff_image_ext(raw):
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png'
    if raw[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return '.webp'
    if raw[:3] == b'GIF':
        return '.gif'
    return None


class CoverLoader:
    """Finds the picture of a song in a background thread.

        loader.request(song, callback)

    callback(song, path_or_None) is called immediately (cached / known miss)
    or from the worker thread - NOT through Kivy's Clock, so it also works
    while the app is in the background.  The callback must be thread-safe.
    Only the newest request is processed (fast skipping through songs
    never builds up a queue).
    """

    MAX_SIDE = 900          # pixels, larger pictures are scaled down
    EXTS = ('.jpg', '.png', '.webp', '.gif')

    def __init__(self, cache_dir, context_provider=None):
        """context_provider: callable returning the Android Context (service or
        activity).  None -> desktop mode (looks for cover.jpg next to songs)."""
        self.context_provider = context_provider
        self.cache_dir = os.path.join(cache_dir, 'covers')
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except OSError:
            pass
        self._cond = threading.Condition()
        self._job = None
        self._thread = None
        self._misses = set()

    # ---------------- public ----------------
    def request(self, song, callback):
        cached = self._cached_path(song)
        if cached:
            callback(song, cached)
            return
        if song['id'] in self._misses:
            callback(song, None)
            return
        context = None
        if self.context_provider is not None:
            try:
                context = self.context_provider()
            except Exception as e:
                print('No context for cover loading:', e)
        with self._cond:
            self._job = (song, callback, context)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
            self._cond.notify()

    # ---------------- worker ----------------
    def _run(self):
        while True:
            with self._cond:
                while self._job is None:
                    self._cond.wait()
                song, callback, context = self._job
                self._job = None
            path = None
            try:
                if self.context_provider is not None:
                    path = self._extract_android(song, context)
                else:
                    path = self._find_desktop(song)
            except Exception as e:
                print('Cover extraction failed:', e)
            if not path:
                self._misses.add(song['id'])
            try:
                callback(song, path)
            except Exception as e:
                print('Cover callback failed:', e)

    # ---------------- cache ----------------
    def _base(self, song):
        safe = re.sub(r'[^0-9A-Za-z_-]', '_', str(song['id']))
        return os.path.join(self.cache_dir, safe)

    def _cached_path(self, song):
        base = self._base(song)
        for ext in self.EXTS:
            if os.path.exists(base + ext):
                return base + ext
        return None

    # ---------------- desktop ----------------
    def _find_desktop(self, song):
        path = song.get('path')
        if not path:
            return None
        folder = os.path.dirname(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        names = [stem, 'cover', 'folder', 'album', 'front']
        for name in names:
            for ext in ('.jpg', '.jpeg', '.png', '.webp'):
                candidate = os.path.join(folder, name + ext)
                if os.path.exists(candidate):
                    return candidate
        return None

    # ---------------- android ----------------
    def _extract_android(self, song, context):
        from jnius import autoclass

        if context is None or not song.get('uri'):
            return None
        Uri = autoclass('android.net.Uri')
        uri = Uri.parse(song['uri'])
        base = self._base(song)

        # 1) Picture embedded in the audio file (ID3 / MP4 / FLAC ...)
        try:
            MMR = autoclass('android.media.MediaMetadataRetriever')
            retriever = MMR()
            try:
                retriever.setDataSource(context, uri)
                raw = _to_bytes(retriever.getEmbeddedPicture())
            finally:
                try:
                    retriever.release()
                except Exception:
                    pass
            if raw:
                path = self._store_bytes(raw, base)
                if path:
                    return path
        except Exception as e:
            print('Embedded picture failed:', e)

        sdk = autoclass('android.os.Build$VERSION').SDK_INT

        # 2) System thumbnail (Android 10+) - also finds folder.jpg style art
        if sdk >= 29:
            try:
                Size = autoclass('android.util.Size')
                bitmap = context.getContentResolver().loadThumbnail(
                    uri, Size(self.MAX_SIDE, self.MAX_SIDE), None)
                if bitmap is not None:
                    dst = base + '.jpg'
                    ok = self._write_bitmap(bitmap, dst)
                    try:
                        bitmap.recycle()
                    except Exception:
                        pass
                    if ok:
                        return dst
            except Exception as e:
                print('Thumbnail failed:', e)

        # 3) Legacy album-art table (Android 9 and older)
        if sdk < 29 and song.get('album_id'):
            try:
                albums = autoclass('android.provider.MediaStore$Audio$Albums')
                cursor = context.getContentResolver().query(
                    albums.EXTERNAL_CONTENT_URI, ['album_art'], '_id=?',
                    [str(song['album_id'])], None)
                art_path = None
                if cursor is not None:
                    try:
                        if cursor.moveToFirst():
                            art_path = cursor.getString(0)
                    finally:
                        cursor.close()
                if art_path and os.path.exists(art_path):
                    with open(art_path, 'rb') as f:
                        path = self._store_bytes(f.read(), base)
                    if path:
                        return path
            except Exception as e:
                print('Legacy album art failed:', e)
        return None

    def _store_bytes(self, raw, base):
        """Save picture bytes (scaled down when large). Returns the file path."""
        ext = _sniff_image_ext(raw)
        if ext is None:
            return None
        tmp = base + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(raw)
        dst = base + '.jpg'
        try:
            if self._downscale_file(tmp, dst):
                return dst
        except Exception as e:
            print('Downscale failed, keeping original:', e)
        # Could not re-encode: keep the original bytes with the right extension
        final = base + ext
        try:
            os.replace(tmp, final)
            return final
        except OSError:
            return None
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _downscale_file(self, src, dst):
        from jnius import autoclass
        BitmapFactory = autoclass('android.graphics.BitmapFactory')
        Options = autoclass('android.graphics.BitmapFactory$Options')

        bounds = Options()
        bounds.inJustDecodeBounds = True
        BitmapFactory.decodeFile(src, bounds)
        width, height = bounds.outWidth, bounds.outHeight
        if width <= 0 or height <= 0:
            return False
        sample = 1
        while max(width, height) // (sample * 2) >= self.MAX_SIDE:
            sample *= 2
        opts = Options()
        opts.inSampleSize = sample
        bitmap = BitmapFactory.decodeFile(src, opts)
        if bitmap is None:
            return False
        try:
            return self._write_bitmap(bitmap, dst)
        finally:
            try:
                bitmap.recycle()
            except Exception:
                pass
            try:
                os.remove(src)
            except OSError:
                pass

    def _write_bitmap(self, bitmap, dst):
        from jnius import autoclass
        FileOutputStream = autoclass('java.io.FileOutputStream')
        CompressFormat = autoclass('android.graphics.Bitmap$CompressFormat')
        stream = FileOutputStream(dst)
        try:
            ok = bitmap.compress(CompressFormat.JPEG, 90, stream)
            stream.flush()
        finally:
            stream.close()
        return bool(ok)


