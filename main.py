# -*- coding: utf-8 -*-
import os
import json
import threading

from kivy.app import App
from kivy.core.window import Window
from kivy.core.text import LabelBase
from kivy.core.image import Image as CoreImage
from kivy.clock import Clock
from kivy.metrics import dp, sp
from kivy.animation import Animation
from kivy.utils import platform
from kivy.graphics import Color, Rectangle, RoundedRectangle
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.slider import Slider
from kivy.uix.widget import Widget
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup

from arabic_text import display, is_rtl, has_rtl, search_key
from audio_backend import create_player, scan_device_library, get_android_activity
from storage import Library

# ============================================================
# Fonts (needed for Arabic: Kivy's default Roboto has no Arabic letters)
# ============================================================
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FONT = 'Roboto'
ARABIC_FONT = 'ArabicUI'


def _register_arabic_font():
    candidates = [
        (os.path.join(APP_DIR, 'fonts', 'DejaVuSans.ttf'),
         os.path.join(APP_DIR, 'fonts', 'DejaVuSans-Bold.ttf')),
        ('/system/fonts/NotoNaskhArabic-Regular.ttf', '/system/fonts/NotoNaskhArabic-Bold.ttf'),
        ('/system/fonts/NotoSansArabic-Regular.ttf', '/system/fonts/NotoSansArabic-Bold.ttf'),
        ('/system/fonts/DroidSansArabic.ttf', '/system/fonts/DroidSansArabic.ttf'),
    ]
    for regular, bold in candidates:
        if os.path.exists(regular):
            LabelBase.register(name=ARABIC_FONT, fn_regular=regular,
                               fn_bold=bold if os.path.exists(bold) else regular)
            return ARABIC_FONT
    return DEFAULT_FONT


ARABIC_FONT = _register_arabic_font()

# ============================================================
# Color palette
# ============================================================
BG_COLOR      = (0.055, 0.055, 0.075, 1)
SURFACE_COLOR = (0.125, 0.125, 0.16, 1)
SURFACE_HOVER = (0.17, 0.17, 0.22, 1)
NAV_COLOR     = (0.09, 0.09, 0.12, 1)
ACCENT_COLOR  = (0.20, 0.85, 0.52, 1)
ACCENT_DARK   = (0.14, 0.60, 0.37, 1)
TEXT_MAIN     = (0.96, 0.96, 0.98, 1)
TEXT_MUTED    = (0.55, 0.55, 0.60, 1)
TEXT_DIM      = (0.38, 0.38, 0.42, 1)

CACHE_VERSION = 3          # bump when the song dictionaries change


def format_time(seconds):
    seconds = int(seconds or 0)
    m, s = divmod(seconds, 60)
    return '{}:{:02d}'.format(m, s)


def set_label_text(label, text, rtl=None):
    """Put (possibly Arabic) text into a Label: joins the letters, fixes the
    order, picks a font that has Arabic glyphs and aligns to the correct side."""
    text = '' if text is None else str(text)
    direction = is_rtl(text) if rtl is None else rtl
    label.font_name = ARABIC_FONT if has_rtl(text) else DEFAULT_FONT
    label.text = display(text, rtl)
    label.halign = 'right' if direction else 'left'
    # when the text is too long, cut its logical END (left side for RTL)
    label.shorten_from = 'left' if direction else 'right'


def _bind_text_size(label):
    label.bind(size=lambda i, v: setattr(i, 'text_size', i.size))


# ============================================================
# Reusable widgets
# ============================================================
class RoundedWidget(Widget):
    def __init__(self, bg_color=SURFACE_COLOR, radius=None, **kwargs):
        super().__init__(**kwargs)
        self.bg_color = bg_color
        self.radius = radius or [dp(16)]
        with self.canvas.before:
            self._color = Color(*self.bg_color)
            self.rect = RoundedRectangle(size=self.size, pos=self.pos, radius=self.radius)
        self.bind(size=self._update_rect, pos=self._update_rect)

    def _update_rect(self, instance, value):
        self.rect.pos = instance.pos
        self.rect.size = instance.size

    def set_color(self, color):
        self._color.rgba = color


class CustomRoundedButton(Button):
    def __init__(self, bg_color=SURFACE_COLOR, pressed_color=None, radius=None, **kwargs):
        super().__init__(**kwargs)
        self.background_normal = ''
        self.background_down = ''
        self.background_color = (0, 0, 0, 0)
        self.bg_color = bg_color
        self.pressed_color = pressed_color or SURFACE_HOVER
        self.radius = radius or [dp(14)]

        with self.canvas.before:
            self._color = Color(*self.bg_color)
            self.rect = RoundedRectangle(size=self.size, pos=self.pos, radius=self.radius)
        self.bind(size=self._update_rect, pos=self._update_rect)
        self.bind(state=self._on_state)

    def _update_rect(self, instance, value):
        self.rect.pos = instance.pos
        self.rect.size = instance.size

    def _on_state(self, instance, value):
        target = self.pressed_color if value == 'down' else self.bg_color
        Animation.cancel_all(self, '_color_rgba')
        Animation(_color_rgba=target, d=0.08).start(self)

    def _get_color_rgba(self):
        return list(self._color.rgba)

    def _set_color_rgba(self, value):
        self._color.rgba = value

    _color_rgba = property(_get_color_rgba, _set_color_rgba)

    def set_bg(self, color):
        self.bg_color = color
        Animation.cancel_all(self, '_color_rgba')
        self._color.rgba = color


class ModernTextInput(TextInput):
    """Rounded TextInput that can show Arabic correctly.

    Kivy's TextInput cannot join/reorder Arabic letters, so the real text is
    hidden (transparent) and a Label on top draws the properly shaped text.
    The stored `.text` is always the plain, logical text (good for searching).
    """

    def __init__(self, radius=None, **kwargs):
        visible_color = kwargs.pop('foreground_color', TEXT_MAIN)
        super().__init__(**kwargs)
        self.background_normal = ''
        self.background_active = ''
        self.background_color = (0, 0, 0, 0)
        self.foreground_color = (0, 0, 0, 0)
        self.radius = radius or [dp(14)]

        with self.canvas.before:
            self._bg_color = Color(*SURFACE_COLOR)
            self.rect = RoundedRectangle(size=self.size, pos=self.pos, radius=self.radius)

        self._overlay = Label(text='', color=visible_color, font_size=self.font_size,
                              halign='left', valign='middle', shorten=True,
                              shorten_from='left', size_hint=(None, None))
        self.add_widget(self._overlay)
        self.bind(size=self._update_rect, pos=self._update_rect)
        self.bind(text=self._sync_overlay, font_size=self._sync_overlay)
        self._update_rect()

    def _update_rect(self, *_):
        self.rect.pos = self.pos
        self.rect.size = self.size
        pad = self.padding
        left, top, right, bottom = pad[0], pad[1], pad[2], pad[3]
        self._overlay.pos = (self.x + left, self.y + bottom)
        self._overlay.size = (max(self.width - left - right, 1), max(self.height - top - bottom, 1))
        self._overlay.text_size = self._overlay.size

    def _sync_overlay(self, *_):
        self._overlay.font_size = self.font_size
        self._overlay.font_name = ARABIC_FONT if has_rtl(self.text) else DEFAULT_FONT
        self._overlay.text = display(self.text)


class ModernScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        with self.canvas.before:
            Color(*BG_COLOR)
            self.bg_rect = Rectangle(size=self.size, pos=self.pos)
        self.bind(size=self._update_bg, pos=self._update_bg)

    def _update_bg(self, instance, value):
        self.bg_rect.pos = instance.pos
        self.bg_rect.size = instance.size


def section_header(text):
    header = Label(text=text, font_size=sp(23), bold=True, color=TEXT_MAIN,
                   size_hint_y=None, height=dp(40), halign='left', valign='bottom')
    _bind_text_size(header)
    return header


def empty_state_label(text):
    # size_hint_y=None + fixed height: otherwise the label collapses to 0 height
    lbl = Label(text=text, font_size=sp(14), color=TEXT_MUTED, halign='center', valign='middle',
                size_hint_y=None, height=dp(200))
    _bind_text_size(lbl)
    return lbl


def card_popup(height, size_hint_x=0.86):
    """Popup with a rounded card as content. Returns (popup, card)."""
    popup = Popup(title='', separator_height=0, background='', background_color=(0, 0, 0, 0),
                  size_hint=(size_hint_x, None), height=height)
    card = BoxLayout(orientation='vertical', spacing=dp(14), padding=dp(18))
    with card.canvas.before:
        Color(*SURFACE_COLOR)
        card._bg = RoundedRectangle(size=card.size, pos=card.pos, radius=[dp(20)])
    card.bind(size=lambda i, v: setattr(i._bg, 'size', v),
              pos=lambda i, v: setattr(i._bg, 'pos', v))
    popup.content = card
    return popup, card


def popup_title(text, height=dp(30), size=sp(18)):
    lbl = Label(text='', font_size=size, bold=True, color=TEXT_MAIN,
                size_hint_y=None, height=height, valign='middle')
    _bind_text_size(lbl)
    set_label_text(lbl, text)
    return lbl


# ------------------------------------------------------------
# Cards that react to tap and long-press
# ------------------------------------------------------------
class PressCard(BoxLayout):
    """A rounded row. Tap -> on_press, hold -> on_long_press.
    A touch that moves (scrolling) never counts as a tap."""
    LONG_PRESS_SECONDS = 0.5

    def __init__(self, on_press=None, on_long_press=None, **kwargs):
        super().__init__(**kwargs)
        self._press_cb = on_press
        self._long_cb = on_long_press
        self._long_event = None
        self._long_fired = False
        self._touch_start = None
        with self.canvas.before:
            self._bg_color = Color(*SURFACE_COLOR)
            self._bg = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(14)])
        self.bind(pos=self._update_bg, size=self._update_bg)

    def _update_bg(self, *_):
        self._bg.pos = self.pos
        self._bg.size = self.size

    def _child_handles(self, touch):
        """Subclasses return True for touches that belong to a child widget."""
        return False

    def _cancel_long(self):
        if self._long_event is not None:
            self._long_event.cancel()
            self._long_event = None

    def _fire_long(self, dt):
        self._long_event = None
        if self._touch_start is None:
            return
        self._long_fired = True
        self._bg_color.rgba = SURFACE_COLOR
        if self._long_cb:
            self._long_cb()

    def on_touch_down(self, touch):
        if not self.collide_point(*touch.pos):
            return False
        if self._child_handles(touch):
            return super().on_touch_down(touch)
        touch.grab(self)
        self._touch_start = (touch.x, touch.y)
        self._long_fired = False
        self._bg_color.rgba = SURFACE_HOVER
        self._cancel_long()
        if self._long_cb:
            self._long_event = Clock.schedule_once(self._fire_long, self.LONG_PRESS_SECONDS)
        return True

    def on_touch_move(self, touch):
        if touch.grab_current is self:
            if self._touch_start is not None:
                moved = (abs(touch.x - self._touch_start[0]) > dp(12) or
                         abs(touch.y - self._touch_start[1]) > dp(12))
                if moved:
                    self._touch_start = None
                    self._cancel_long()
                    self._bg_color.rgba = SURFACE_COLOR
            return True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            self._cancel_long()
            self._bg_color.rgba = SURFACE_COLOR
            is_tap = (self._touch_start is not None and not self._long_fired
                      and self.collide_point(*touch.pos))
            self._touch_start = None
            if is_tap and self._press_cb:
                self._press_cb()
            return True
        return super().on_touch_up(touch)


class SongRow(PressCard):
    """One row in a song list: title / artist / duration + a favorite toggle."""

    def __init__(self, song, on_press_song, on_toggle_favorite, on_long_press, is_favorite, **kwargs):
        super().__init__(
            orientation='horizontal', size_hint_y=None, height=dp(64),
            padding=[dp(16), dp(6), dp(6), dp(6)], spacing=dp(8),
            on_press=lambda: on_press_song(song),
            on_long_press=(lambda: on_long_press(song)) if on_long_press else None,
            **kwargs)
        self.song = song

        rtl = is_rtl(song['title'])
        text_col = BoxLayout(orientation='vertical', spacing=dp(2))

        title = Label(text='', font_size=sp(15.5), color=TEXT_MAIN, bold=True,
                      valign='middle', shorten=True)
        _bind_text_size(title)
        set_label_text(title, song['title'], rtl=rtl)

        subtitle_text = song['artist']
        if song.get('duration'):
            subtitle_text += '  -  ' + format_time(song['duration'])
        subtitle = Label(text='', font_size=sp(12.5), color=TEXT_MUTED,
                         valign='middle', shorten=True)
        _bind_text_size(subtitle)
        set_label_text(subtitle, subtitle_text, rtl=rtl)

        text_col.add_widget(title)
        text_col.add_widget(subtitle)
        self.add_widget(text_col)

        self.fav_btn = Button(text='', font_size=sp(11), bold=True,
                              background_normal='', background_down='',
                              background_color=(0, 0, 0, 0),
                              size_hint=(None, 1), width=dp(84))
        self.fav_btn.bind(on_release=lambda *_: on_toggle_favorite(song))
        self.add_widget(self.fav_btn)
        self.refresh_favorite_state(is_favorite)

    def _child_handles(self, touch):
        return self.fav_btn.collide_point(*touch.pos)

    def refresh_favorite_state(self, is_favorite):
        self.fav_btn.text = 'Unfavorite' if is_favorite else 'Favorite'
        self.fav_btn.color = ACCENT_COLOR if is_favorite else TEXT_DIM


class SongListView(ScrollView):
    """Scrollable list of songs. Rows are created in small batches so the UI
    stays responsive even with thousands of songs."""
    BATCH = 20

    def __init__(self, on_press_song, on_toggle_favorite, on_long_press, is_favorite_fn,
                 empty_text, **kwargs):
        super().__init__(bar_width=dp(3), bar_color=ACCENT_COLOR,
                         bar_inactive_color=(1, 1, 1, 0.08), **kwargs)
        self._on_press_song = on_press_song
        self._on_toggle_favorite = on_toggle_favorite
        self._on_long_press = on_long_press
        self._is_favorite_fn = is_favorite_fn
        self.empty_text = empty_text

        self.lst = BoxLayout(orientation='vertical', size_hint_y=None, spacing=dp(10))
        self.lst.bind(minimum_height=self.lst.setter('height'))
        self.add_widget(self.lst)

        self.rows = {}
        self._pending = []
        self._event = None

    def set_songs(self, songs):
        self._cancel()
        self.lst.clear_widgets()
        self.rows = {}
        self.scroll_y = 1
        if not songs:
            self.lst.add_widget(empty_state_label(self.empty_text))
            return
        self._pending = list(songs)
        self._add_batch(0)

    def _cancel(self):
        if self._event is not None:
            self._event.cancel()
            self._event = None
        self._pending = []

    def _add_batch(self, dt):
        self._event = None
        batch, self._pending = self._pending[:self.BATCH], self._pending[self.BATCH:]
        for song in batch:
            row = SongRow(song, self._on_press_song, self._on_toggle_favorite,
                          self._on_long_press, self._is_favorite_fn(song['id']))
            self.lst.add_widget(row)
            self.rows[song['id']] = row
        if self._pending:
            self._event = Clock.schedule_once(self._add_batch, 0)

    def sync_favorites(self):
        for song_id, row in self.rows.items():
            row.refresh_favorite_state(self._is_favorite_fn(song_id))


# ============================================================
# Screens
# ============================================================
class SongsScreen(ModernScreen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.all_songs = []
        self.list_view = None
        self._search_event = None
        self.on_rescan = None

        self.main = BoxLayout(orientation='vertical', padding=[dp(20), dp(24), dp(20), dp(8)], spacing=dp(12))

        header_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(40), spacing=dp(10))
        header_row.add_widget(section_header('All Songs'))
        rescan_btn = CustomRoundedButton(text='Rescan', font_size=sp(13), bold=True,
                                         bg_color=SURFACE_COLOR, color=ACCENT_COLOR, radius=[dp(12)],
                                         size_hint=(None, None), size=(dp(84), dp(36)))
        rescan_btn.bind(on_release=lambda *_: self.on_rescan and self.on_rescan())
        header_row.add_widget(rescan_btn)
        self.main.add_widget(header_row)

        self.search_input = ModernTextInput(
            hint_text='Search songs...', multiline=False, size_hint_y=None, height=dp(44),
            font_size=sp(14), padding=[dp(14), dp(12), dp(14), dp(12)],
            foreground_color=TEXT_MAIN, hint_text_color=TEXT_MUTED, cursor_color=ACCENT_COLOR,
            radius=[dp(14)]
        )
        self.search_input.bind(text=self._on_search_change)
        self.main.add_widget(self.search_input)

        self.body_holder = BoxLayout(orientation='vertical')
        self.main.add_widget(self.body_holder)
        self.add_widget(self.main)

    def setup(self, on_press_song, on_toggle_favorite, on_long_press, is_favorite_fn):
        self.on_press_song = on_press_song
        self.list_view = SongListView(
            on_press_song=self._press_callback,
            on_toggle_favorite=on_toggle_favorite,
            on_long_press=on_long_press,
            is_favorite_fn=is_favorite_fn,
            empty_text='No songs found on this device yet.\nAdd some music, then tap "Rescan".')
        self._filtered = []

    def _press_callback(self, song):
        self.on_press_song(song, self._filtered)

    def populate(self, songs):
        self.all_songs = songs
        self._update_list()

    def _on_search_change(self, instance, value):
        if self._search_event is not None:
            self._search_event.cancel()
        self._search_event = Clock.schedule_once(lambda dt: self._update_list(), 0.25)

    def _update_list(self):
        self._search_event = None
        if self.list_view is None:
            return
        query = search_key(self.search_input.text)
        if query:
            self._filtered = [s for s in self.all_songs if query in s.get('_key', '')]
            self.list_view.empty_text = 'No matching songs found.'
        else:
            self._filtered = self.all_songs
            self.list_view.empty_text = 'No songs found on this device yet.\nAdd some music, then tap "Rescan".'
        self.body_holder.clear_widgets()
        self.body_holder.add_widget(self.list_view)
        self.list_view.set_songs(self._filtered)

    def sync_favorites(self):
        if self.list_view is not None:
            self.list_view.sync_favorites()

    def show_message(self, title, text):
        self.body_holder.clear_widgets()
        box = BoxLayout(orientation='vertical', spacing=dp(10), padding=[0, dp(30), 0, 0])
        t = Label(text=title, font_size=sp(17), bold=True, color=TEXT_MAIN,
                  size_hint_y=None, height=dp(30), halign='center', valign='middle')
        _bind_text_size(t)
        body = Label(text=text, font_size=sp(14), color=TEXT_MUTED, halign='center', valign='top')
        _bind_text_size(body)
        box.add_widget(t)
        box.add_widget(body)
        self.body_holder.add_widget(box)

    def show_error(self, message):
        self.body_holder.clear_widgets()
        box = BoxLayout(orientation='vertical', spacing=dp(8))
        title = Label(text='Something went wrong', font_size=sp(16), bold=True, color=TEXT_MAIN,
                      size_hint_y=None, height=dp(28), halign='left', valign='middle')
        _bind_text_size(title)
        box.add_widget(title)

        hint = Label(text='Copy the text below and send it back for a fix:', font_size=sp(12.5),
                     color=TEXT_MUTED, size_hint_y=None, height=dp(22), halign='left', valign='middle')
        _bind_text_size(hint)
        box.add_widget(hint)

        error_box = TextInput(
            text=message, readonly=True, multiline=True, font_size=sp(12),
            background_normal='', background_active='', background_color=SURFACE_COLOR,
            foreground_color=TEXT_MAIN, cursor_color=ACCENT_COLOR, padding=[dp(12), dp(12)],
        )
        box.add_widget(error_box)
        self.body_holder.add_widget(box)


class FavoritesScreen(ModernScreen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.main = BoxLayout(orientation='vertical', padding=[dp(20), dp(24), dp(20), dp(8)], spacing=dp(14))
        self.main.add_widget(section_header('Favorite Tracks'))
        self.body_holder = BoxLayout(orientation='vertical')
        self.main.add_widget(self.body_holder)
        self.add_widget(self.main)
        self.list_view = None

    def setup(self, on_press_song, on_toggle_favorite, on_long_press, is_favorite_fn):
        self.list_view = SongListView(
            on_press_song=on_press_song, on_toggle_favorite=on_toggle_favorite,
            on_long_press=on_long_press, is_favorite_fn=is_favorite_fn,
            empty_text='No favorites yet.\nTap "Favorite" next to a song in All Songs.')
        self.body_holder.add_widget(self.list_view)

    def populate(self, songs):
        if self.list_view is not None:
            self.list_view.set_songs(songs)


class PlaylistsScreen(ModernScreen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        main = BoxLayout(orientation='vertical', padding=[dp(20), dp(24), dp(20), dp(8)], spacing=dp(14))

        top_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(40), spacing=dp(10))
        top_row.add_widget(section_header('Your Playlists'))
        add_btn = CustomRoundedButton(text='New Playlist', font_size=sp(13), bold=True,
                                      bg_color=ACCENT_COLOR, pressed_color=ACCENT_DARK,
                                      color=(0, 0, 0, 1), radius=[dp(12)],
                                      size_hint=(None, None), size=(dp(130), dp(38)))
        add_btn.bind(on_release=lambda *_: self._prompt_new_playlist())
        top_row.add_widget(add_btn)
        main.add_widget(top_row)

        self.body_holder = BoxLayout(orientation='vertical')
        main.add_widget(self.body_holder)
        self.add_widget(main)
        self._on_create_callback = None

    def set_create_callback(self, cb):
        self._on_create_callback = cb

    def _prompt_new_playlist(self):
        popup, card = card_popup(height=dp(250))
        card.add_widget(popup_title('Create Playlist'))

        text_input = ModernTextInput(
            hint_text='Playlist name', multiline=False, size_hint_y=None, height=dp(48),
            font_size=sp(15), padding=[dp(14), dp(14), dp(14), dp(14)],
            foreground_color=TEXT_MAIN, hint_text_color=TEXT_MUTED, cursor_color=ACCENT_COLOR,
        )
        text_input._bg_color.rgba = SURFACE_HOVER
        card.add_widget(text_input)

        btn_row = BoxLayout(orientation='horizontal', spacing=dp(12), size_hint_y=None, height=dp(46))
        cancel_btn = CustomRoundedButton(text='Cancel', font_size=sp(14), bold=True,
                                         bg_color=SURFACE_HOVER, color=TEXT_MAIN, radius=[dp(14)])
        create_btn = CustomRoundedButton(text='Create', font_size=sp(14), bold=True,
                                         bg_color=ACCENT_COLOR, pressed_color=ACCENT_DARK,
                                         color=(0, 0, 0, 1), radius=[dp(14)])
        btn_row.add_widget(cancel_btn)
        btn_row.add_widget(create_btn)
        card.add_widget(btn_row)

        cancel_btn.bind(on_release=lambda *_: popup.dismiss())

        def do_create(*_):
            name = text_input.text.strip()
            if name and self._on_create_callback:
                self._on_create_callback(name)
            popup.dismiss()

        create_btn.bind(on_release=do_create)
        text_input.bind(on_text_validate=do_create)
        popup.open()
        Clock.schedule_once(lambda dt: setattr(text_input, 'focus', True), 0.2)

    def populate(self, playlists, on_open_playlist, on_delete_playlist):
        self.body_holder.clear_widgets()
        scroll = ScrollView(bar_width=dp(3), bar_color=ACCENT_COLOR, bar_inactive_color=(1, 1, 1, 0.08))
        lst = BoxLayout(orientation='vertical', size_hint_y=None, spacing=dp(10))
        lst.bind(minimum_height=lst.setter('height'))

        if not playlists:
            lst.add_widget(empty_state_label(
                'No playlists yet.\nTap "New Playlist" to create one,\nthen hold a song to add it.'))
        else:
            for name, song_ids in playlists.items():
                row = PressCard(orientation='horizontal', size_hint_y=None, height=dp(68),
                                padding=[dp(16), 0, dp(16), 0],
                                on_press=lambda n=name: on_open_playlist(n),
                                on_long_press=lambda n=name: on_delete_playlist(n))
                rtl = is_rtl(name)
                text_col = BoxLayout(orientation='vertical', spacing=dp(2))
                title = Label(text='', font_size=sp(16), bold=True, color=TEXT_MAIN,
                              valign='middle', shorten=True)
                _bind_text_size(title)
                set_label_text(title, name, rtl=rtl)
                count = Label(text='{} song(s)'.format(len(song_ids)), font_size=sp(12.5),
                              color=TEXT_MUTED, valign='middle', halign='right' if rtl else 'left')
                _bind_text_size(count)
                text_col.add_widget(title)
                text_col.add_widget(count)
                row.add_widget(text_col)
                lst.add_widget(row)

        scroll.add_widget(lst)
        self.body_holder.add_widget(scroll)


class CoverArt(Widget):
    """Square rounded picture. Shows a music note until a cover is set."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        radius = [dp(28)]
        with self.canvas:
            self._bg_color = Color(0.15, 0.15, 0.19, 1)
            self._bg = RoundedRectangle(pos=self.pos, size=self.size, radius=radius)
            self._img_color = Color(1, 1, 1, 0)
            self._img = RoundedRectangle(pos=self.pos, size=self.size, radius=radius)
        self._note = Label(text='\u266A', font_name=ARABIC_FONT, font_size=sp(110), color=TEXT_DIM)
        self.add_widget(self._note)
        self.bind(pos=self._relayout, size=self._relayout)

    def _relayout(self, *_):
        self._bg.pos = self.pos
        self._bg.size = self.size
        self._img.pos = self.pos
        self._img.size = self.size
        self._note.pos = self.pos
        self._note.size = self.size

    def set_cover(self, path):
        texture = None
        if path:
            try:
                texture = CoreImage(path).texture
                w, h = texture.size
                side = min(w, h)
                if w != h:   # centre-crop to a square
                    texture = texture.get_region((w - side) // 2, (h - side) // 2, side, side)
            except Exception as e:
                print('Could not load cover:', e)
                texture = None
        if texture is not None:
            self._img.texture = texture
            self._img_color.a = 1
            self._note.opacity = 0
        else:
            self._img_color.a = 0
            self._note.opacity = 1


class NowPlayingScreen(ModernScreen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.on_play_pause = None
        self.on_next = None
        self.on_prev = None
        self.on_seek = None
        self.on_toggle_favorite = None
        self._seeking = False
        self._seek_touch = None

        root = BoxLayout(orientation='vertical', padding=[dp(28), dp(20), dp(28), dp(18)], spacing=dp(16))

        header = Label(text='NOW PLAYING', font_size=sp(11), bold=True, color=TEXT_MUTED,
                       size_hint_y=None, height=dp(18))
        root.add_widget(header)

        self.album_wrap = FloatLayout(size_hint_y=1)
        self.cover = CoverArt(size_hint=(None, None))
        self.album_wrap.add_widget(self.cover)
        self.album_wrap.bind(size=self._place_cover, pos=self._place_cover)
        root.add_widget(self.album_wrap)

        meta_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(56), spacing=dp(10))
        meta_col = BoxLayout(orientation='vertical', spacing=dp(4))
        self.title_lbl = Label(text='', font_size=sp(20), bold=True, color=TEXT_MAIN,
                               valign='middle', shorten=True)
        _bind_text_size(self.title_lbl)
        self.artist_lbl = Label(text='', font_size=sp(14), color=ACCENT_COLOR,
                                valign='middle', shorten=True)
        _bind_text_size(self.artist_lbl)
        set_label_text(self.title_lbl, 'Nothing playing')
        set_label_text(self.artist_lbl, 'Pick a song to start')
        meta_col.add_widget(self.title_lbl)
        meta_col.add_widget(self.artist_lbl)
        meta_row.add_widget(meta_col)

        self.fav_toggle = CustomRoundedButton(text='Favorite', font_size=sp(12), bold=True,
                                              bg_color=SURFACE_COLOR, color=TEXT_MUTED, radius=[dp(14)],
                                              size_hint=(None, None), size=(dp(96), dp(40)),
                                              pos_hint={'center_y': 0.5})
        self.fav_toggle.bind(on_release=lambda *_: self.on_toggle_favorite and self.on_toggle_favorite())
        meta_row.add_widget(self.fav_toggle)
        root.add_widget(meta_row)

        progress_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(28), spacing=dp(10))
        self.time_cur = Label(text='0:00', font_size=sp(11), color=TEXT_MUTED, size_hint_x=None, width=dp(38))
        self.slider = Slider(value=0, min=0, max=100, value_track=True, value_track_color=ACCENT_COLOR,
                             cursor_size=(dp(14), dp(14)))
        self.slider.bind(on_touch_down=self._start_seek, on_touch_up=self._end_seek)
        self.time_total = Label(text='0:00', font_size=sp(11), color=TEXT_MUTED, size_hint_x=None, width=dp(38))
        progress_row.add_widget(self.time_cur)
        progress_row.add_widget(self.slider)
        progress_row.add_widget(self.time_total)
        root.add_widget(progress_row)

        controls_row = BoxLayout(orientation='horizontal', size_hint_y=None, height=dp(64), spacing=dp(16))
        self.btn_prev = CustomRoundedButton(text='Prev', font_size=sp(14), bold=True, bg_color=SURFACE_COLOR,
                                            color=TEXT_MAIN, radius=[dp(18)], size_hint_x=0.28)
        self.btn_play = CustomRoundedButton(text='Play', font_size=sp(15), bold=True,
                                            bg_color=ACCENT_COLOR, pressed_color=ACCENT_DARK,
                                            color=(0, 0, 0, 1), radius=[dp(18)], size_hint_x=0.44)
        self.btn_next = CustomRoundedButton(text='Next', font_size=sp(14), bold=True, bg_color=SURFACE_COLOR,
                                            color=TEXT_MAIN, radius=[dp(18)], size_hint_x=0.28)
        self.btn_prev.bind(on_release=lambda *_: self.on_prev and self.on_prev())
        self.btn_play.bind(on_release=lambda *_: self.on_play_pause and self.on_play_pause())
        self.btn_next.bind(on_release=lambda *_: self.on_next and self.on_next())
        controls_row.add_widget(self.btn_prev)
        controls_row.add_widget(self.btn_play)
        controls_row.add_widget(self.btn_next)
        root.add_widget(controls_row)

        self.add_widget(root)

    def _place_cover(self, *_):
        side = max(min(self.album_wrap.width, self.album_wrap.height), dp(120))
        self.cover.size = (side, side)
        self.cover.center = self.album_wrap.center

    # --- seeking: finish the seek wherever the finger is released -------------
    def _start_seek(self, instance, touch):
        if instance.collide_point(*touch.pos):
            self._seeking = True
            self._seek_touch = touch

    def _end_seek(self, instance, touch):
        if self._seeking and touch is self._seek_touch:
            self._seeking = False
            self._seek_touch = None
            if self.on_seek:
                self.on_seek(instance.value)

    def is_seeking(self):
        return self._seeking

    def update_track_info(self, song, is_favorite):
        if song is None:
            set_label_text(self.title_lbl, 'Nothing playing')
            set_label_text(self.artist_lbl, 'Pick a song to start')
            self.fav_toggle.text = 'Favorite'
            self.fav_toggle.set_bg(SURFACE_COLOR)
            self.fav_toggle.color = TEXT_MUTED
            return
        rtl = is_rtl(song['title'])
        set_label_text(self.title_lbl, song['title'], rtl=rtl)
        set_label_text(self.artist_lbl, song['artist'], rtl=rtl)
        self.fav_toggle.text = 'Unfavorite' if is_favorite else 'Favorite'
        self.fav_toggle.color = ACCENT_COLOR if is_favorite else TEXT_MUTED

    def update_playback_state(self, is_playing):
        self.btn_play.text = 'Pause' if is_playing else 'Play'

    def update_progress(self, position, duration):
        if not self._seeking:
            self.slider.max = max(duration, 1)
            self.slider.value = min(position, self.slider.max)
        self.time_cur.text = format_time(position)
        self.time_total.text = format_time(duration)


# ============================================================
# Main app
# ============================================================
class KazeMusicApp(App):
    def build(self):
        self.title = 'KazeMusic'
        Window.clearcolor = BG_COLOR

        self.library_store = Library(self.user_data_dir)
        self._ui_event = None
        self._scanning = False
        self._audio_permission = True
        self.all_songs = []

        if platform == 'android':
            try:
                get_android_activity()          # cache it while on the main thread
            except Exception as e:
                print('Activity not available:', e)

        # On Android the music is played by a foreground service (service.py);
        # self.player only sends it commands and mirrors its state.
        self.player = create_player(self.user_data_dir)
        self.player.on_change = self._request_ui_refresh
        self.player.on_error = lambda msg: Clock.schedule_once(lambda dt: self._show_playback_error(msg), 0)

        root = BoxLayout(orientation='vertical')

        self.sm = ScreenManager(transition=SlideTransition(duration=0.18))
        self.songs_screen = SongsScreen(name='songs')
        self.playlists_screen = PlaylistsScreen(name='playlists')
        self.favorites_screen = FavoritesScreen(name='favorites')
        self.player_screen = NowPlayingScreen(name='nowplaying')

        self.songs_screen.setup(self._play_song_from_list, self._toggle_favorite,
                                self._show_add_to_playlist, self.library_store.is_favorite)
        self.songs_screen.on_rescan = lambda: self._load_library(force=True)
        self.favorites_screen.setup(self._play_song_from_favorites, self._toggle_favorite,
                                    self._show_add_to_playlist, self.library_store.is_favorite)
        self.playlists_screen.set_create_callback(self._create_playlist)
        self.player_screen.on_play_pause = self._toggle_play_pause
        self.player_screen.on_next = self._play_next
        self.player_screen.on_prev = self._play_prev
        self.player_screen.on_seek = self._seek_to
        self.player_screen.on_toggle_favorite = self._toggle_current_favorite

        for scr in (self.songs_screen, self.playlists_screen, self.favorites_screen, self.player_screen):
            self.sm.add_widget(scr)
        root.add_widget(self.sm)

        nav_container = BoxLayout(size_hint=(1, None), height=dp(64))
        with nav_container.canvas.before:
            Color(*NAV_COLOR)
            nav_container._bg_rect = RoundedRectangle(
                size=nav_container.size, pos=nav_container.pos,
                radius=[dp(20), dp(20), 0, 0])

        def _update_nav_bg(instance, _value):
            instance._bg_rect.size = instance.size
            instance._bg_rect.pos = instance.pos

        nav_container.bind(size=_update_nav_bg, pos=_update_nav_bg)

        nav_content = BoxLayout(orientation='horizontal', padding=[dp(10), dp(8), dp(10), dp(8)], spacing=dp(6))
        nav_container.add_widget(nav_content)

        tabs = [('Songs', 'songs'), ('Playlists', 'playlists'), ('Favorites', 'favorites'), ('Player', 'nowplaying')]
        self.nav_buttons = {}
        for label, screen_name in tabs:
            btn = CustomRoundedButton(text=label, font_size=sp(13), bold=True,
                                      bg_color=(0, 0, 0, 0), pressed_color=(1, 1, 1, 0.06),
                                      color=TEXT_DIM, radius=[dp(16)])
            btn.bind(on_release=lambda *_, sn=screen_name: self._go_to(sn))
            self.nav_buttons[screen_name] = btn
            nav_content.add_widget(btn)
        self._set_active_tab('songs')

        root.add_widget(nav_container)

        Window.bind(on_keyboard=self._on_keyboard)
        self.player.start()
        Clock.schedule_interval(self._tick, 0.25)
        Clock.schedule_once(lambda dt: self._start_library_load(), 0.2)

        return root

    # ------------------------------------------------------------
    # Small UI helpers
    # ------------------------------------------------------------
    def _toast(self, text, seconds=1.6):
        popup = Popup(title='', separator_height=0, background='', background_color=(0, 0, 0, 0),
                      size_hint=(0.8, None), height=dp(64), auto_dismiss=True,
                      overlay_color=(0, 0, 0, 0))
        card = BoxLayout(padding=dp(12))
        with card.canvas.before:
            Color(*SURFACE_HOVER)
            card._bg = RoundedRectangle(size=card.size, pos=card.pos, radius=[dp(16)])
        card.bind(size=lambda i, v: setattr(i._bg, 'size', v),
                  pos=lambda i, v: setattr(i._bg, 'pos', v))
        lbl = Label(text='', font_size=sp(14), color=TEXT_MAIN, halign='center', valign='middle',
                    shorten=True, shorten_from='right')
        _bind_text_size(lbl)
        lbl.font_name = ARABIC_FONT if has_rtl(text) else DEFAULT_FONT
        lbl.text = display(text)
        card.add_widget(lbl)
        popup.content = card
        popup.open()
        Clock.schedule_once(lambda dt: popup.dismiss(), seconds)

    def _show_playback_error(self, msg):
        self.player_screen.update_playback_state(False)
        popup = Popup(
            title='Playback Error',
            content=Label(text=msg, font_size=sp(14), color=TEXT_MAIN, halign='center', valign='middle'),
            size_hint=(0.8, 0.4),
            background='', background_color=(0, 0, 0, 0.9),
            separator_height=0,
        )
        popup.content.bind(size=lambda i, v: setattr(i, 'text_size', i.size))
        popup.open()

    def _show_add_to_playlist(self, song):
        playlists = list(self.library_store.get_playlists().keys())
        rows = max(len(playlists), 1)
        list_height = min(dp(50) * rows + dp(8) * (rows - 1), dp(260))
        popup, card = card_popup(height=dp(250) + list_height)

        card.add_widget(popup_title('Add to playlist'))
        song_lbl = Label(text='', font_size=sp(13), color=TEXT_MUTED, size_hint_y=None, height=dp(22),
                         valign='middle', shorten=True)
        _bind_text_size(song_lbl)
        set_label_text(song_lbl, song['title'])
        card.add_widget(song_lbl)

        scroll = ScrollView(size_hint_y=None, height=list_height, bar_width=dp(3), bar_color=ACCENT_COLOR)
        box = BoxLayout(orientation='vertical', size_hint_y=None, spacing=dp(8))
        box.bind(minimum_height=box.setter('height'))
        if not playlists:
            hint = Label(text='No playlists yet.\nCreate one in the Playlists tab first.',
                         font_size=sp(13), color=TEXT_MUTED, halign='center', valign='middle',
                         size_hint_y=None, height=dp(100))
            _bind_text_size(hint)
            box.add_widget(hint)
        for name in playlists:
            btn = CustomRoundedButton(text=display(name), font_size=sp(15), bold=True,
                                      bg_color=SURFACE_HOVER, color=TEXT_MAIN, radius=[dp(14)],
                                      size_hint_y=None, height=dp(50), shorten=True)
            btn.font_name = ARABIC_FONT if has_rtl(name) else DEFAULT_FONT
            btn.bind(size=lambda i, v: setattr(i, 'text_size', (i.width - dp(24), i.height)))
            btn.halign = 'right' if is_rtl(name) else 'left'
            btn.valign = 'middle'
            btn.shorten_from = 'left' if is_rtl(name) else 'right'

            def choose(_btn, n=name):
                added = self.library_store.add_to_playlist(n, song['id'])
                self._refresh_playlists_screen()
                popup.dismiss()
                self._toast(('Added to: ' if added else 'Already in: ') + n)

            btn.bind(on_release=choose)
            box.add_widget(btn)
        scroll.add_widget(box)
        card.add_widget(scroll)

        cancel = CustomRoundedButton(text='Cancel', font_size=sp(14), bold=True, bg_color=SURFACE_HOVER,
                                     color=TEXT_MAIN, radius=[dp(14)], size_hint_y=None, height=dp(44))
        cancel.bind(on_release=lambda *_: popup.dismiss())
        card.add_widget(cancel)
        popup.open()

    def _confirm_delete_playlist(self, name):
        popup, card = card_popup(height=dp(230))
        card.add_widget(popup_title('Delete playlist?'))
        lbl = Label(text='', font_size=sp(15), color=TEXT_MUTED, valign='middle', shorten=True)
        _bind_text_size(lbl)
        set_label_text(lbl, name)
        card.add_widget(lbl)
        row = BoxLayout(orientation='horizontal', spacing=dp(12), size_hint_y=None, height=dp(46))
        cancel = CustomRoundedButton(text='Cancel', font_size=sp(14), bold=True, bg_color=SURFACE_HOVER,
                                     color=TEXT_MAIN, radius=[dp(14)])
        delete = CustomRoundedButton(text='Delete', font_size=sp(14), bold=True, bg_color=(0.85, 0.25, 0.25, 1),
                                     pressed_color=(0.6, 0.15, 0.15, 1), color=(1, 1, 1, 1), radius=[dp(14)])
        cancel.bind(on_release=lambda *_: popup.dismiss())

        def do_delete(*_):
            self.library_store.remove_playlist(name)
            self._refresh_playlists_screen()
            popup.dismiss()

        delete.bind(on_release=do_delete)
        row.add_widget(cancel)
        row.add_widget(delete)
        card.add_widget(row)
        popup.open()

    def _on_keyboard(self, window, key, *args):
        """Android back button: go back to the song list, then send the app to
        the background (so the music keeps playing instead of the app closing)."""
        if key != 27:
            return False
        if self.sm.current != 'songs':
            self._go_to('songs')
            return True
        if platform == 'android':
            try:
                from jnius import autoclass
                autoclass('org.kivy.android.PythonActivity').mActivity.moveTaskToBack(True)
                return True
            except Exception:
                return False
        return False

    # ------------------------------------------------------------
    # Player -> UI (the player may notify from any thread)
    # ------------------------------------------------------------
    def _request_ui_refresh(self):
        """Safe to call from any thread.  Several calls are merged into one."""
        if self._ui_event is None:
            self._ui_event = Clock.schedule_once(self._ui_refresh, 0)

    def _ui_refresh(self, dt=None):
        self._ui_event = None
        song = self.player.current_song
        self.player_screen.update_track_info(
            song, bool(song and self.library_store.is_favorite(song['id'])))
        self.player_screen.update_playback_state(self.player.is_playing())
        self.player_screen.cover.set_cover(self.player.cover_path)
        if song is None:
            self.player_screen.update_progress(0, 0)

    # ------------------------------------------------------------
    # Library loading / caching
    # ------------------------------------------------------------
    def _get_cache_path(self):
        return os.path.join(self.user_data_dir, 'library_cache.json')

    def _load_cached_library(self):
        cache_path = self._get_cache_path()
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get('version') != CACHE_VERSION:
                return None
            songs = data.get('songs')
            return songs if isinstance(songs, list) else None
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, AttributeError):
            return None

    def _save_cached_library(self, songs):
        clean = [{k: v for k, v in s.items() if not k.startswith('_')} for s in songs]
        tmp = self._get_cache_path() + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'version': CACHE_VERSION, 'songs': clean}, f, ensure_ascii=False)
            os.replace(tmp, self._get_cache_path())
        except Exception as e:
            print('Could not save the library cache:', e)

    def _start_library_load(self):
        if platform == 'android':
            try:
                from android.permissions import request_permissions
                perms = [
                    'android.permission.READ_MEDIA_AUDIO',
                    'android.permission.READ_EXTERNAL_STORAGE',
                    'android.permission.POST_NOTIFICATIONS',
                ]
                request_permissions(perms, self._on_permissions_result)
            except Exception:
                import traceback
                self._handle_fatal_error('requesting permissions', traceback.format_exc())
        else:
            self._load_library()

    def _on_permissions_result(self, permissions, grant_results):
        audio_perms = ('android.permission.READ_MEDIA_AUDIO', 'android.permission.READ_EXTERNAL_STORAGE')
        try:
            self._audio_permission = any(
                bool(g) for p, g in zip(permissions, grant_results) if p in audio_perms)
        except Exception:
            self._audio_permission = True

        def _resume(dt):
            if not self._audio_permission:
                self.songs_screen.show_message(
                    'Permission needed',
                    'KazeMusic needs access to your music files.\n\n'
                    'Open Settings > Apps > KazeMusic > Permissions,\n'
                    'allow "Music and audio", then open the app again.')
                return
            try:
                self._load_library()
            except Exception:
                import traceback
                self._handle_fatal_error('loading your library', traceback.format_exc())
        Clock.schedule_once(_resume, 0)

    def _load_library(self, force=False):
        if self._scanning:
            return
        cached = None if force else self._load_cached_library()
        if cached is not None and not self.all_songs:
            self._on_library_scanned(cached)      # instant start, refreshed below

        activity = None
        if platform == 'android':
            try:
                activity = get_android_activity()      # resolve on the main thread
            except Exception:
                activity = None
        self._scanning = True
        if force:
            self._toast('Scanning...')
        threading.Thread(target=self._scan_worker, args=(activity,), daemon=True).start()

    def _scan_worker(self, activity):
        try:
            songs = scan_device_library(activity)
        except Exception:
            import traceback
            trace_text = traceback.format_exc()
            Clock.schedule_once(
                lambda dt, t=trace_text: self._handle_fatal_error('scanning your music library', t), 0)
            return
        Clock.schedule_once(lambda dt: self._on_scan_finished(songs), 0)

    @staticmethod
    def _signature(songs):
        return [(s['id'], s['title'], s['artist']) for s in songs]

    def _on_scan_finished(self, songs):
        self._scanning = False
        changed = self._signature(songs) != self._signature(self.all_songs)
        self._save_cached_library(songs)
        if changed or not self.all_songs:
            self._on_library_scanned(songs)

    def _on_library_scanned(self, songs):
        for s in songs:
            s['_key'] = search_key('{} {} {}'.format(s.get('title', ''), s.get('artist', ''), s.get('album', '')))
        self.all_songs = songs
        self._refresh_songs_screen()
        self._refresh_favorites_screen()
        self._refresh_playlists_screen()

    def _handle_fatal_error(self, context_text, trace_text=None):
        self._scanning = False
        if trace_text is None:
            import traceback
            trace_text = traceback.format_exc()
        try:
            log_path = os.path.join(self.user_data_dir, 'kazemusic_crash.log')
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write('--- error while {} ---\n{}\n'.format(context_text, trace_text))
        except Exception:
            pass

        message = 'Error while {}:\n\n{}'.format(context_text, trace_text)
        self.songs_screen.show_error(message)
        self._go_to('songs')

    # ------------------------------------------------------------
    # Screen refreshing
    # ------------------------------------------------------------
    def _refresh_songs_screen(self):
        self.songs_screen.populate(self.all_songs)

    def _favorite_songs(self):
        fav_ids = set(self.library_store.get_favorite_ids())
        return [s for s in self.all_songs if s['id'] in fav_ids]

    def _refresh_favorites_screen(self):
        self.favorites_screen.populate(self._favorite_songs())

    def _refresh_playlists_screen(self):
        self.playlists_screen.populate(self.library_store.get_playlists(),
                                       self._open_playlist, self._confirm_delete_playlist)

    def _create_playlist(self, name):
        self.library_store.create_playlist(name)
        self._refresh_playlists_screen()

    def _open_playlist(self, name):
        song_ids = self.library_store.get_playlists().get(name, [])
        by_id = {s['id']: s for s in self.all_songs}
        songs = [by_id[i] for i in song_ids if i in by_id]
        if not songs:
            self._toast('This playlist is empty. Hold a song to add it.')
            return
        self._play_song_from_list(songs[0], songs)

    # ------------------------------------------------------------
    # Favorites
    # ------------------------------------------------------------
    def _toggle_favorite(self, song):
        is_fav = self.library_store.toggle_favorite(song['id'])
        self.songs_screen.sync_favorites()           # keep every list in sync
        self._refresh_favorites_screen()
        current = self.player.current_song
        if current and current['id'] == song['id']:
            self.player_screen.update_track_info(current, is_fav)

    def _toggle_current_favorite(self):
        song = self.player.current_song
        if song:
            self._toggle_favorite(song)

    # ------------------------------------------------------------
    # Playback (the player does the real work; see audio_backend / service.py)
    # ------------------------------------------------------------
    def _play_song_from_list(self, song, context_list=None):
        songs = context_list if context_list is not None else self.all_songs
        index = next((i for i, s in enumerate(songs) if s['id'] == song['id']), -1)
        if index < 0:
            songs, index = [song], 0
        self.player.set_queue_and_play(songs, index)
        self._go_to('nowplaying')

    def _play_song_from_favorites(self, song):
        self._play_song_from_list(song, self._favorite_songs())

    def _toggle_play_pause(self):
        self.player.toggle()

    def _play_next(self):
        self.player.next()

    def _play_prev(self):
        self.player.prev()

    def _seek_to(self, seconds):
        self.player.seek_to(seconds)

    def _tick(self, dt):
        if self.player.current_song:
            try:
                pos = self.player.get_position()
                dur = self.player.get_duration()
                if pos is not None and dur is not None and dur > 0:
                    self.player_screen.update_progress(pos, dur)
            except Exception:
                pass

    # ------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------
    def _go_to(self, screen_name):
        order = ['songs', 'playlists', 'favorites', 'nowplaying']
        cur_i = order.index(self.sm.current)
        new_i = order.index(screen_name)
        self.sm.transition.direction = 'left' if new_i > cur_i else 'right'
        self.sm.current = screen_name
        self._set_active_tab(screen_name)

    def _set_active_tab(self, active_name):
        for name, btn in self.nav_buttons.items():
            is_active = (name == active_name)
            btn.color = ACCENT_COLOR if is_active else TEXT_DIM
            btn.set_bg((1, 1, 1, 0.05) if is_active else (0, 0, 0, 0))

    def on_pause(self):
        return True

    def on_resume(self):
        self.player.sync()
        # The user may have just granted the permission in Android settings
        if platform == 'android' and not self.all_songs and not self._scanning:
            self._start_library_load()

    def on_stop(self):
        # Android: only detaches from the service - the music keeps playing.
        # Desktop: stops the music.
        self.player.close()


if __name__ == '__main__':
    KazeMusicApp().run()
