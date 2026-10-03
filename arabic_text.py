# -*- coding: utf-8 -*-
"""
Arabic text support for Kivy (no third-party packages needed).

Kivy's SDL2 text renderer draws characters one by one, left to right.  It does
NOT join Arabic letters and does NOT reorder right-to-left text.  So Arabic
shows up as disconnected, reversed letters unless the text is prepared first.

This module prepares text for *display only*:

    display(text)        -> string that Kivy can draw as-is (joined + reordered)
    is_rtl(text)         -> True when the paragraph direction is right-to-left
    has_rtl(text)        -> True when the text contains any Arabic/Hebrew letter
    fix_mojibake(text)   -> repairs Arabic tags that were decoded with the
                            wrong charset (very common with old MP3 files)
    search_key(text)     -> normalised text used for searching

Always keep the ORIGINAL text for logic (search, storage, notifications).
Only pass the result of display() to Kivy widgets.
"""

import re
import unicodedata
from functools import lru_cache

# --------------------------------------------------------------------------
# Joining tables, built from the Unicode database (so there is nothing to
# mistype): base letter -> [isolated, final, initial, medial]
# --------------------------------------------------------------------------
_TAGS = {'isolated': 0, 'final': 1, 'initial': 2, 'medial': 3}

# Presentation forms that common fonts do not contain -> never use them.
_NO_GLYPH = {0xFB50, 0xFB51, 0xFBA4, 0xFBA5, 0xFBA6, 0xFBA7, 0xFBA8, 0xFBA9,
             0xFBAE, 0xFBAF, 0xFBB0, 0xFBB1, 0xFBDD, 0xFBE0, 0xFBE1, 0xFBE2,
             0xFBE3}

_FORMS = {}      # base char -> [iso, fin, ini, med]   (None when missing)
_LAM_ALEF = {}   # alef variant -> (isolated ligature, final ligature)

LAM = '\u0644'
TATWEEL = '\u0640'
_ALEFS = '\u0622\u0623\u0625\u0627'


def _build_tables():
    ranges = list(range(0xFB50, 0xFC00)) + list(range(0xFE70, 0xFF00))
    for cp in ranges:
        if cp in _NO_GLYPH:
            continue
        dec = unicodedata.decomposition(chr(cp))
        if not dec.startswith('<'):
            continue
        parts = dec.split()
        tag = parts[0].strip('<>')
        if tag not in _TAGS:
            continue
        codes = [int(x, 16) for x in parts[1:]]
        if len(codes) == 1:
            base = chr(codes[0])
            forms = _FORMS.setdefault(base, [None, None, None, None])
            forms[_TAGS[tag]] = chr(cp)
        elif len(codes) == 2 and codes[0] == 0x0644 and chr(codes[1]) in _ALEFS:
            entry = _LAM_ALEF.setdefault(chr(codes[1]), [None, None])
            if tag == 'isolated':
                entry[0] = chr(cp)
            elif tag == 'final':
                entry[1] = chr(cp)


try:
    _build_tables()
except Exception:          # unicodedata missing -> text is shown unshaped
    _FORMS.clear()
    _LAM_ALEF.clear()

# Diacritics (harakat), superscript alef - removed for display because simple
# renderers cannot position them; they are kept in the stored text.
_TASHKEEL = re.compile('[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]')


def _can_join_next(ch):
    """Does `ch` connect to the letter that follows it?"""
    if ch == TATWEEL:
        return True
    f = _FORMS.get(ch)
    return bool(f and f[2])


def _can_join_prev(ch):
    """Does `ch` accept a connection from the letter before it?"""
    if ch == TATWEEL:
        return True
    f = _FORMS.get(ch)
    return bool(f and f[1])


def _shape(text):
    """Replace Arabic letters by their contextual (joined) forms."""
    if not _FORMS:
        return text
    out = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        prev_ch = text[i - 1] if i > 0 else ''
        connect_prev = bool(prev_ch) and _can_join_next(prev_ch)

        # Lam + Alef ligature (لا)
        if ch == LAM and i + 1 < n and text[i + 1] in _LAM_ALEF:
            iso, fin = _LAM_ALEF[text[i + 1]]
            pick = fin if connect_prev else iso
            if pick:
                out.append(pick)
                i += 2
                continue

        forms = _FORMS.get(ch)
        if not forms:
            out.append(ch)
            i += 1
            continue

        next_ch = text[i + 1] if i + 1 < n else ''
        connect_next = bool(next_ch) and bool(forms[2]) and _can_join_prev(next_ch)
        connect_prev = connect_prev and bool(forms[1])

        if connect_prev and connect_next:
            idx = 3
        elif connect_prev:
            idx = 1
        elif connect_next:
            idx = 2
        else:
            idx = 0
        out.append(forms[idx] or forms[0] or ch)
        i += 1
    return ''.join(out)


# --------------------------------------------------------------------------
# Minimal bidirectional reordering (good for song titles / artist names)
# --------------------------------------------------------------------------
_MIRROR = {'(': ')', ')': '(', '[': ']', ']': '[', '{': '}', '}': '{',
           '<': '>', '>': '<', '\u00ab': '\u00bb', '\u00bb': '\u00ab'}


def _is_rtl_char(ch):
    cp = ord(ch)
    if 0x0660 <= cp <= 0x0669 or 0x06F0 <= cp <= 0x06F9:
        return False                      # Arabic-Indic digits are numbers
    return (0x0590 <= cp <= 0x08FF or 0xFB1D <= cp <= 0xFDFF
            or 0xFE70 <= cp <= 0xFEFF)


def _kind(ch):
    if _is_rtl_char(ch):
        return 'R'
    if ch.isdigit():
        return 'D'
    if ch.isalpha():
        return 'L'
    return 'N'


def has_rtl(text):
    return bool(text) and any(_is_rtl_char(c) for c in text)


def _base_is_rtl(text):
    for ch in text:
        k = _kind(ch)
        if k == 'R':
            return True
        if k == 'L':
            return False
    return False


def is_rtl(text):
    """True when the paragraph direction of `text` is right-to-left."""
    return _base_is_rtl(text or '')


def _reorder(text, base_rtl):
    kinds = [_kind(c) for c in text]
    n = len(text)

    # "influence" (how a char affects neighbouring neutrals) and "display"
    # direction (digits are always shown left-to-right).
    inf = [None] * n
    disp = [None] * n
    last_strong = 'R' if base_rtl else 'L'
    for i, k in enumerate(kinds):
        if k == 'R':
            inf[i] = disp[i] = 'R'
            last_strong = 'R'
        elif k == 'L':
            inf[i] = disp[i] = 'L'
            last_strong = 'L'
        elif k == 'D':
            inf[i] = last_strong
            disp[i] = 'L'

    base = 'R' if base_rtl else 'L'

    # Matched bracket pairs, e.g. "(Live)", take the paragraph direction so
    # both brackets end up on the correct sides of the enclosed text.
    paired = set()
    stack = []
    closing = {')': '(', ']': '[', '}': '{', '>': '<', '\u00bb': '\u00ab'}
    for i, c in enumerate(text):
        if c in '([{<\u00ab':
            stack.append((c, i))
        elif c in closing:
            for pos in range(len(stack) - 1, -1, -1):
                if stack[pos][0] == closing[c]:
                    paired.add(stack[pos][1])
                    paired.add(i)
                    del stack[pos:]
                    break

    for i in paired:
        disp[i] = base
        inf[i] = base

    for i, k in enumerate(kinds):
        if k != 'N' or i in paired:
            continue
        left = next((inf[j] for j in range(i - 1, -1, -1) if inf[j]), None)
        right = next((inf[j] for j in range(i + 1, n) if inf[j]), None)
        disp[i] = left if (left and left == right) else base

    # Group into runs of equal direction.
    runs = []
    for ch, d in zip(text, disp):
        if runs and runs[-1][0] == d:
            runs[-1][1].append(ch)
        else:
            runs.append([d, [ch]])

    pieces = []
    for d, chars in runs:
        if d == 'R':
            chars = [_MIRROR.get(c, c) for c in reversed(chars)]
        pieces.append(''.join(chars))
    if base_rtl:
        pieces.reverse()
    return ''.join(pieces)


@lru_cache(maxsize=4096)
def _display_cached(text, force_rtl):
    if not has_rtl(text):
        if force_rtl is True:
            return _reorder(text, True) if text else text
        return text
    cleaned = _TASHKEEL.sub('', text)
    shaped = _shape(cleaned)
    base_rtl = _base_is_rtl(cleaned) if force_rtl is None else force_rtl
    return _reorder(shaped, base_rtl)


def display(text, rtl=None):
    """Return `text` ready to be drawn by Kivy.

    rtl=None  -> paragraph direction is detected from the first strong letter
    rtl=True/False forces the paragraph direction (used to keep the second
                   line of a list row consistent with the first one).
    """
    if not text:
        return ''
    return _display_cached(str(text), rtl)


# --------------------------------------------------------------------------
# Repair of wrongly decoded tags (mojibake)
# --------------------------------------------------------------------------
_ASCII_LETTER = re.compile('[A-Za-z]')


def _arabic_count(s):
    return sum(1 for c in s if 0x0600 <= ord(c) <= 0x06FF)


def fix_mojibake(text):
    """Old MP3 tags written in Windows-1256 (or UTF-8) are often read by
    Android as Latin-1, giving text such as 'ÇáÍÈ'.  Detect and repair it."""
    if not text:
        return text
    high = [c for c in text if ord(c) >= 0x80]
    if len(high) < 2 or any(ord(c) > 0xFF for c in text):
        return text
    try:
        raw = text.encode('latin-1')
    except UnicodeEncodeError:
        return text

    # 1) UTF-8 bytes shown as Latin-1 (strict decoding => almost no false hits)
    try:
        fixed = raw.decode('utf-8')
        if _arabic_count(fixed) > 0:
            return fixed
    except UnicodeDecodeError:
        pass

    # 2) Windows-1256 bytes shown as Latin-1 (only when no Latin letters, to
    #    avoid damaging real French/Spanish/etc. titles such as "Café")
    if not _ASCII_LETTER.search(text):
        try:
            fixed = raw.decode('cp1256')
            non_ascii = [c for c in fixed if ord(c) >= 0x80]
            if non_ascii and all(0x0600 <= ord(c) <= 0x06FF for c in non_ascii):
                return fixed
        except UnicodeDecodeError:
            pass
    return text


# --------------------------------------------------------------------------
# Search normalisation
# --------------------------------------------------------------------------
_SEARCH_MAP = str.maketrans({
    '\u0622': '\u0627', '\u0623': '\u0627', '\u0625': '\u0627',   # آ أ إ -> ا
    '\u0649': '\u064A',                                           # ى -> ي
    '\u0629': '\u0647',                                           # ة -> ه
    '\u0624': '\u0648', '\u0626': '\u064A',                       # ؤ -> و ئ -> ي
    '\u06CC': '\u064A', '\u06A9': '\u0643',                       # ی -> ي ک -> ك
})
_ARABIC_DIGITS = str.maketrans('\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669'
                               '\u06F0\u06F1\u06F2\u06F3\u06F4\u06F5\u06F6\u06F7\u06F8\u06F9',
                               '01234567890123456789')


def search_key(text):
    """Lower-case text without diacritics/tatweel and with letter variants
    unified, so 'احمد' finds 'أحمد' and 'اغنيه' finds 'أغنية'."""
    if not text:
        return ''
    t = _TASHKEEL.sub('', str(text)).replace(TATWEEL, '')
    t = t.translate(_SEARCH_MAP).translate(_ARABIC_DIGITS)
    return t.casefold().strip()
