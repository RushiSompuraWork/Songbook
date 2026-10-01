#!/usr/bin/env python3
"""The book reader: a terminal app in rmpc's way, for novels and audiobooks.

Design decision, 2026-09-22: only the text, in the middle, in the
terminal's own colours so every Omarchy theme applies; a dim line at the top
(book · chapter · %) and one at the bottom (keys, or the audio's time while a
book plays); C the chapters, A the text settings, z hides everything but the
text. While an audiobook plays, the sentence being spoken shows in reverse
(or the word, or the paragraph: the card's "A book lights up").

    reader.py                  the last book, or the list of books
    reader.py PATH             an .epub or .txt, or a book folder
    reader.py --pending        what the card asked to open (reader-open.json)
    reader.py --render 90x30 [--keys "C j"] PATH   draw once, as text (tests)

Books are read by booktext.py; audiobooks' chapters and places come from
books.py, so the reader and the card agree on where a book is.
"""

import bisect
import contextlib
import curses
import io
import json
import locale
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import card       # noqa: E402
import booktext   # noqa: E402
import books      # noqa: E402
import library    # noqa: E402
import speak      # noqa: E402

STATE = "reader.json"          # the settings and the last book; places live in library.py
PENDING = "reader-open.json"   # the card's "open this", picked up by a running reader
TEXT_EXT = (".epub", ".txt")
AUDIO_EXT = (".mp3", ".m4a", ".m4b", ".opus", ".ogg", ".flac", ".wav", ".aac")
WPM = 230                      # reading speed for "min left"
# His choice of defaults, 2026-09-22 (a screenshot of the Text panel).
# fontSize: the zoom he left the window at (None: foot.ini's own size).
DEFAULT_PREFS = {"width": 140, "spacing": 1, "justify": True, "layout": "pages",
                 "lineHeight": 1.2, "indent": 2, "animation": "slide", "fontSize": None}
WIDTHS = (40, 48, 56, 64, 72, 80, 90, 100, 110, 120, 140, 160, 180, 0)   # 0: the full window
LINE_HEIGHTS = (1.0, 1.2, 1.5, 1.7, 2.0)     # foot's own line height (books.launch_command)
INDENTS = (0, 2, 4)
ANIMATIONS = ("slide", "scroll", "none")
LAYOUTS = ("scroll", "pages")
HIGHLIGHTS = ("sentence", "word", "paragraph")


def quiet(fn, *args):
    """Run a card command without its JSON reaching the screen; its answer."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(*args)
    except SystemExit:
        pass
    except Exception as e:
        return {"error": str(e)}
    lines = buf.getvalue().strip().splitlines()
    try:
        return json.loads(lines[-1]) if lines else {}
    except ValueError:
        return {}


def load_state():
    s = card.load(STATE, {})
    if not isinstance(s, dict):
        s = {}
    s.setdefault("books", {})
    prefs = dict(DEFAULT_PREFS)
    prefs.update({k: v for k, v in (s.get("prefs") or {}).items() if k in DEFAULT_PREFS})
    s["prefs"] = prefs
    return s


# ---------------------------------------------------------------- books

class TextBook:
    """An .epub or .txt, read chapter by chapter.

    A novel has no sound of its own, but chapters read aloud (speak.py) are
    its sound: they belong to this book, are listed among its chapters and
    play under its name. The reader never switches to the Spoken folder --
    that folder holds only the chapters made so far, and opening it used to
    lose the other five hundred (his report, 2026-09-23).
    """
    audio = False

    def __init__(self, path):
        self.path = path
        try:
            data = booktext.open_book(path)
        except Exception as e:
            # A broken zip, bad XML, a missing part: said plainly, not as
            # Python's own words.
            raise ValueError("damaged, or not a real %s"
                             % os.path.splitext(path)[1].lstrip(".").upper()) from e
        self.title = data["title"]
        # A book too large to read whole keeps what was read (booktext's
        # budget); he should be told rather than wonder where the rest went.
        self.partial = bool(data.get("partial"))
        self.chapters = [{"title": c["title"], "id": str(i)} for i, c in enumerate(data["chapters"])]
        # Its words per chapter, for "how far through the book" and time left:
        # counted when the book was read, so no text is loaded to know it.
        self.weights = [int(c.get("words") or 0) for c in data["chapters"]]
        self._plain = {}          # chapters whose words are in hand, a few at a time
        self.key = library.text_id(path)
        self._timed = {}
        self.folder = ""            # where its spoken chapters live
        self.spoken = {}            # chapter -> the audio made for it
        self.lengths = {}           # ... and how long it came out
        self.refresh_spoken()

    def refresh_spoken(self):
        """Which chapters have been read aloud with the voice now chosen."""
        opts = speak.settings()
        self.folder = speak.book_folder(self.title, opts["folder"])
        marks = speak.made_by(self.folder)
        want = (opts["engine"], opts["voice"], round(opts["speed"], 2))
        spoken, lengths = {}, {}
        for i, c in enumerate(self.chapters):
            rel = speak.chapter_file(self.folder, i, c["title"])
            mark = marks.get(os.path.basename(rel))
            if not mark or (mark.get("engine"), mark.get("voice"),
                            round(float(mark.get("speed") or 1), 2)) != want:
                continue
            full = os.path.join(card.MUSIC_DIR, rel)
            if os.path.exists(full) and os.path.getsize(full) > 1024:
                spoken[i] = rel
                lengths[i] = int(mark.get("seconds") or 0)
        for i in set(self._timed) - set(spoken):
            self._timed.pop(i, None)         # a chapter cleaned away: forget its timings
        self.spoken, self.lengths = spoken, lengths

    def chapter_of_file(self, name):
        """Which chapter MPD is playing, from the file it is playing."""
        base = os.path.basename(name)
        for i, rel in self.spoken.items():
            if os.path.basename(rel) == base:
                return i
        return None

    def paras(self, i):
        """The chapter's words. Spoken chapters bring their own timings (the
        .lrc beside the audio), so the sentence being read lights up."""
        rel = self.spoken.get(i)
        if rel:
            if i not in self._timed:
                try:
                    text = booktext.text_for(rel)
                except Exception:
                    text = None
                self._timed[i] = (text or {}).get("paras") or None
            if self._timed[i]:
                return self._timed[i]
        return self.plain(i)

    KEEP_CHAPTERS = 3         # the one being read, and a step either way

    def plain(self, i):
        """One chapter's words, read when it is wanted. The whole book used
        to be held: 180 MB to show 224 words (2026-09-24)."""
        if not 0 <= i < len(self.chapters):
            return []
        if i not in self._plain:
            self._plain[i] = booktext.chapter_paras(self.path, i)
            for old_i in sorted(self._plain)[:-self.KEEP_CHAPTERS]:
                if old_i != i:
                    self._plain.pop(old_i, None)
        return self._plain[i]


class AudioBook:
    """A book folder with audio: chapters are books.py's, the text for each
    comes from booktext.text_for (a .lrc/.srt/.vtt/.txt beside it, or a
    read-along EPUB), timed so the reader can follow the voice."""
    audio = True

    def __init__(self, folder, files):
        self.folder = folder
        self.files = files
        self.title = os.path.basename(folder)
        self.chapters = books.chapters(files)
        self.weights = [max(1, int(c["length"])) for c in self.chapters]
        self.key = library.audio_id(files)
        self._cache = {}

    def paras(self, i):
        if not 0 <= i < len(self.chapters):
            return []
        if i not in self._cache:
            ch = self.chapters[i]
            span = (ch["start"], ch["start"] + ch["length"]) if ch["file"].lower().endswith(".m4b") \
                and ch["length"] else (None, None)
            try:
                text = booktext.text_for(ch["file"], *span)
            except Exception:
                text = None
            self._cache[i] = text["paras"] if text else []
        return self._cache[i]


def music_dir():
    return os.path.realpath(card.MUSIC_DIR)


def open_target(target):
    """A path (absolute, ~, or inside the music folder) -> a book, or None."""
    if not target:
        return None
    full = os.path.expanduser(target)
    if not os.path.isabs(full):
        full = os.path.join(card.MUSIC_DIR, target)
    full = os.path.realpath(full)
    if os.path.isfile(full) and full.lower().endswith(TEXT_EXT):
        return TextBook(full)
    if os.path.isdir(full):
        rel = os.path.relpath(full, music_dir())
        if not rel.startswith(".."):
            try:
                m = card.Mpd()
                files = books.files_of(m, rel)
                m.close()
            except Exception:
                files = []
            if files:
                return AudioBook(rel, files)
        texts = sorted(n for n in os.listdir(full) if n.lower().endswith(TEXT_EXT))
        if texts:
            return TextBook(os.path.join(full, texts[0]))
    return None


def resolve(target):
    """Where a target is on disk, or "" when it is gone."""
    full = os.path.expanduser(target)
    if not os.path.isabs(full):
        full = os.path.join(card.MUSIC_DIR, target)
    return os.path.realpath(full) if os.path.exists(full) else ""


def shelf(state):
    """Books to offer: recent ones first (if still there), then every book in
    the Book folders: each .epub or .txt on its own, each folder with sound."""
    found, seen = [], set()
    # Chapters read aloud belong to the novel they came from, so the spoken
    # folder is never a book of its own (his choice, 2026-09-23: one entry,
    # one progress, one place he left off).
    spoken_root = (card.setting("spokenFolder", speak.DEFAULT_FOLDER)
                   or speak.DEFAULT_FOLDER).strip("/")
    spoken_path = os.path.realpath(os.path.join(card.MUSIC_DIR, spoken_root))

    def is_spoken(where):
        return where == spoken_path or where.startswith(spoken_path + os.sep)

    def add(title, target, kind, rec=None, book_id=""):
        where = resolve(target)
        if where and is_spoken(os.path.realpath(where)):
            return
        # A folder is a book to listen to only if it has sound of its own
        # (an older entry could point at a whole shelf of novels).
        if kind == "listen" and where and not any(
                n.lower().endswith(AUDIO_EXT) for n in os.listdir(where)):
            return
        if where and where not in seen:
            seen.add(where)
            rec = rec or {}
            read = rec.get("read") or {}
            percent = float(read.get("percent") or rec.get("percent") or 0)
            found.append({"title": title, "target": target, "kind": kind, "id": book_id,
                          "where": os.path.basename(os.path.dirname(where)),
                          "percent": percent, "seconds": int(rec.get("seconds", 0)),
                          "t": float(rec.get("t", 0)),
                          "state": "finished" if percent >= 0.98 else
                                   "reading" if percent > 0.001 else "new"})

    def kind_of(target):
        return "read" if target.lower().endswith(TEXT_EXT) else "listen"

    # Recent books come from the library's own records (library.py), which
    # every part of the card shares, so a book heard in the card is here too.
    records = library.load()["books"]
    for book_id, rec in sorted(records.items(), key=lambda kv: -kv[1].get("t", 0)):
        target = rec.get("path")
        if target:
            add(rec.get("title") or os.path.basename(target),
                target, "read" if rec.get("kind") == "text" else kind_of(target), rec, book_id)
    for folder in card.setting("bookFolders", []) or []:
        for root, dirs, names in os.walk(os.path.join(card.MUSIC_DIR, folder)):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                             and not is_spoken(os.path.realpath(os.path.join(root, d))))
            rel = os.path.relpath(root, card.MUSIC_DIR)
            for n in sorted(names):
                if n.lower().endswith(TEXT_EXT):
                    add(os.path.splitext(n)[0], rel + "/" + n, "read")
            if any(n.lower().endswith(AUDIO_EXT) for n in names):
                add(os.path.basename(root), rel, "listen")
            if len(found) >= 300:
                break
    # The same title twice (a book in two formats): say where each one is.
    titles = [b["title"] for b in found]
    for b in found:
        b["label"] = b["title"] + ("  · " + b["where"] if titles.count(b["title"]) > 1 else "")
    return found[:300]


# ---------------------------------------------------------------- layout

def cells(text):
    """How many terminal columns a word takes: 2 for a wide character (Chinese,
    Japanese, Korean, most emoji), 0 for combining marks, else 1."""
    import unicodedata
    n = 0
    for c in text:
        if unicodedata.combining(c):
            continue
        n += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return n


def wrap(text, width):
    """A definition broken into lines that fit."""
    words, lines, line = str(text or "").split(), [], ""
    for word in words:
        if line and cells(line) + 1 + cells(word) > width:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word) if line else word
    if line:
        lines.append(line)
    return lines or [""]


def cut(text, width):
    """The start of `text` that fits in `width` columns. A title in Chinese
    is twice as wide as its letters suggest, and would push a panel's border
    off the screen if it were cut by character count."""
    if width <= 0:
        return ""
    out, n = [], 0
    for c in text:
        w = cells(c)
        if n + w > width:
            break
        out.append(c)
        n += w
    return "".join(out)


def split_long(word, width):
    """A word wider than the line (a long link, a run of dashes) is broken
    into line-wide pieces instead of cut off; the pieces keep its place."""
    if cells(word) <= width:
        return [word]
    pieces, cur = [], ""
    for c in word:
        if cells(cur + c) > width:
            pieces.append(cur)
            cur = ""
        cur += c
    return pieces + ([cur] if cur else [])


def layout(paras, width, spacing, justify, indent=0):
    """Paragraphs -> screen lines. Each line: {"p": para, "words": [(x, text,
    (s, w))]} so a sentence, word or paragraph can be lit by its place.
    indent: columns the first line of each paragraph starts in by."""
    lines = []
    for pi, para in enumerate(paras):
        words = [(piece, (si, wi)) for si, s in enumerate(para) for wi, (w, _t) in enumerate(s)
                 for piece in split_long(str(w), max(1, width - indent))]
        cur, used, first = [], indent, True
        for text, idx in words:
            need = cells(text) + (1 if cur else 0)
            if cur and used + need > width:
                lines.append(place_line(cur, width - (indent if first else 0), justify, pi,
                                        indent if first else 0))
                cur, used, first = [], 0, False
                need = cells(text)
            cur.append((text, idx))
            used += need
        if cur:
            lines.append(place_line(cur, width, False, pi, indent if first else 0))
        for _ in range(spacing):
            lines.append({"p": pi, "words": []})
    while lines and not lines[-1]["words"]:
        lines.pop()
    return lines


def place_line(words, width, justify, pi, start=0):
    gaps = len(words) - 1
    extra = width - sum(cells(t) for t, _ in words) - gaps
    out, x = [], start
    for n, (text, idx) in enumerate(words):
        out.append((x, text, idx))
        x += cells(text) + 1
        if justify and gaps and n < gaps:
            x += extra // gaps + (1 if n < extra % gaps else 0)
    return {"p": pi, "words": out}


def minutes(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return "0m"
    return "%dh %dm" % (seconds // 3600, seconds // 60 % 60) if seconds >= 3600 else "%dm" % (seconds // 60)


def progress_bar(part, width):
    done = max(0, min(width, int(round(width * float(part or 0)))))
    return "━" * done + "─" * (width - done)


def clock(secs):
    secs = max(0, int(secs))
    h, m, s = secs // 3600, secs // 60 % 60, secs % 60
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


# ---------------------------------------------------------------- the app

class Reader:
    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.state = load_state()
        self.prefs = self.state["prefs"]
        self.book = None
        self.ci = 0                 # chapter shown
        self.top = 0                # first line on screen (scroll layout)
        self.lines = []
        self.times = []             # word start times of the chapter, sorted
        self.index = []             # ... and their (p, s, w)
        self.active = None          # (p, s, w) being spoken
        self.zen = False
        self.overlay = None         # "chapters", "text", "help", "library", "menu"
        self.sel = 0
        self.follow = True
        self.input = None            # typing a search or a note
        self.hits, self.marks = [], []
        self.sort = "recent"         # the book list: recent, title or progress
        self.filter = "all"          # all, reading, finished or new
        self.query = ""              # what the book list is filtered by
        self.chapter_query = ""      # ... and the chapter list
        self.chapter_shown = []      # the chapters the panel is really showing
        self.chapter_typed = ""      # a chapter number being typed
        self.chapter_typed_at = 0
        self.chapter_from = -1       # one end of a run of chapters to tick
        # Choosing a word or a sentence in the text, to look up, mark or hear
        # from (his ask, 2026-09-25). None when he is simply reading.
        self.pick = None             # (line, word on that line)
        self.pick_kind = "word"      # "word", "sentence" or "line"
        self.menu = None             # what a right click offers, when it is open
        self.menu_at = (0, 0)        # where it was opened, so it sits there
        self.panel_box = None        # the last pop-up drawn: (y, x, w, h)
        self.clicks = (0.0, -1, -1, 0)   # when, where, and how many in a row
        self.meaning = None          # what the last word turned out to mean
        self.read_seconds = 0.0      # time on this book, not yet written down
        self.last_tick = time.time()
        self.last_key = time.time()
        self.message, self.message_at = "", 0
        self.waiting_for = -1        # the chapter this reader asked for, to play when ready
        self.polled_at = 0.0         # when MPD was last asked (see poll)
        self._spoken, self._spoken_at = ({}, False), 0.0
        self.waiting = 0             # how many chapters are queued behind it
        self.said_error = None       # so one failure is reported once
        self.making = None           # whatever is being spoken now, for the top line
        self.status = {}
        self.playing_here = False
        self.saved_at = 0
        self.place = None
        self.dirty = False
        self.quit = False
        self.reopen = False
        self.mpd = None
        self.terminal = ""

    # -- books and chapters

    def open(self, target):
        try:
            book = open_target(target)
        except Exception as e:
            self.say("Could not open: %s" % str(e)[:80])
            return False
        if not book or not book.chapters:
            self.say("No book there: an .epub, a .txt, or a book folder")
            return False
        self.save()
        self.book = book
        place = library.get(book.key)["read"]
        self.ci = min(max(0, int(place.get("chapter", 0))), len(book.chapters) - 1)
        self.load_chapter(word=place.get("word"), para=int(place.get("para", 0)))
        self.state["last"] = book.key
        self.dirty = True
        if getattr(book, "partial", False):
            self.say("This book is very large: %d chapters were read, not all of it"
                     % len(book.chapters))
        # A folder holding a novel is remembered as the novel itself, so the
        # list of books does not send every novel of a shelf to the first one.
        self.remember(book.path if isinstance(book, TextBook) else target)
        self.overlay = None
        return True

    def remember(self, target):
        library.touch(self.book.key, title=self.book.title, path=target,
                      kind="text" if isinstance(self.book, TextBook) else "audio")

    def load_chapter(self, para=0, word=None):
        paras = self.book.paras(self.ci)
        self.relayout(paras)
        # Where each paragraph starts, counted in words from the chapter's
        # first word: the place is kept as a word, which no width, font or
        # line height can move (the old place was a paragraph).
        self.para_start, total = [], 0
        for p in paras:
            self.para_start.append(total)
            total += sum(len(s) for s in p)
        self.chapter_words = total
        self.times, self.index = [], []
        for pi, para_ in enumerate(paras):
            for si, s in enumerate(para_):
                for wi, (_w, t) in enumerate(s):
                    if t is not None and t >= 0:
                        self.times.append(float(t))
                        self.index.append((pi, si, wi))
        order = sorted(range(len(self.times)), key=lambda k: self.times[k])
        self.times = [self.times[k] for k in order]
        self.index = [self.index[k] for k in order]
        self.active = None
        self.set_top(self.line_of_word(word) if word is not None else self.line_of_para(para))
        self.dirty = True

    def relayout(self, paras=None):
        if paras is None and self.book:
            paras = self.book.paras(self.ci)
        want = self.prefs["width"] or (self.cols - 6)          # 0: as wide as the window
        width = max(20, min(want, self.cols - 4))
        self.lines = layout(paras or [], width, self.prefs["spacing"], self.prefs["justify"],
                            self.prefs.get("indent", 0))
        self.left = max(2, (self.cols - width) // 2)

    def word_of_line(self, n):
        """The word the line at n starts with, counted from the chapter's start."""
        if not self.lines or n >= len(self.lines):
            return 0
        line = self.lines[min(n, len(self.lines) - 1)]
        if not line["words"]:
            return self.para_start[line["p"]] if line["p"] < len(self.para_start) else 0
        si, wi = line["words"][0][2]
        paras = self.book.paras(self.ci) if self.book else []
        para = paras[line["p"]] if line["p"] < len(paras) else []
        start = self.para_start[line["p"]] if line["p"] < len(self.para_start) else 0
        return start + sum(len(s) for s in para[:si]) + wi

    def line_of_word(self, word):
        """The line holding that word: the place, whatever the layout now is."""
        word = max(0, int(word or 0))
        best = 0
        for n, line in enumerate(self.lines):
            if line["words"] and self.word_of_line(n) <= word:
                best = n
            elif line["words"]:
                break
        return best

    def line_of_para(self, para):
        for n, line in enumerate(self.lines):
            if line["p"] >= para and line["words"]:
                return n
        return 0

    def go_chapter(self, i):
        if not self.book or not 0 <= i < len(self.book.chapters):
            return
        if self.playing_here:
            if self.book.audio:
                quiet(books.cmd_play_book, self.book.folder, self.book.chapters[i]["id"])
            elif i in self.book.spoken:
                quiet(speak.cmd_play_spoken, self.book.path, str(i))
            else:
                quiet(card.cmd_pause)       # nothing to play there yet: stop, do not drift on
                self.say("Chapter %d has not been read aloud yet · space adds it" % (i + 1))
        self.mark_read()
        self.ci = i
        self.load_chapter()
        self.follow = True

    def mark_read(self):
        """A chapter scrolled to its end counts as read."""
        if not self.book or self.book.audio:
            return
        if self.top + self.body_height() >= len(self.lines):
            with library.edit() as lib:
                read = library.record(lib, self.book.key)["read"]
                done = set(read.get("done", []))
                done.add(self.ci)
                read["done"] = sorted(done)

    # -- screen geometry

    def body_top(self):
        return 0 if self.zen else 2

    def body_height(self):
        return max(1, self.rows - (0 if self.zen else 4))

    def page_count(self):
        return max(1, -(-len(self.lines) // self.body_height()))

    def last_top(self):
        """The furthest the page can go.

        Paging: the first line of the last page. It is NOT "a screenful from
        the end" -- that pulled the last page back so the screen was full,
        which repeated the bottom of the page before it and showed the same
        lines twice (reported 2026-09-30: with a few words left, show those and
        nothing carried over from the page before).
        A page is a page; the last one is as short as what is left.
        """
        h = self.body_height()
        if self.prefs["layout"] == "pages":
            return max(0, (self.page_count() - 1) * h)
        return max(0, len(self.lines) - h)

    def set_top(self, line):
        """Show this line, never further than the last page allows.

        It does not snap to a page boundary: where he stopped is where he
        starts again, to the word, and snapping would move it (a test has
        said so since the places were written).
        """
        self.top = max(0, min(int(line), self.last_top()))

    # -- listening

    def poll(self, gap=0.24):
        """While this book plays in MPD: which chapter, which word. A novel
        counts as playing when MPD is on one of its spoken chapters -- the
        audio belongs to the book, so the book is what is open.

        Holding a key down used to ask MPD once per keypress; the answer
        cannot change that fast, so it is asked four times a second at most.
        """
        if not self.book or not (self.book.audio or self.book.spoken):
            self.playing_here = False
            return
        now = time.time()
        if now - self.polled_at < gap:
            return
        self.polled_at = now
        try:
            if self.mpd is None:
                self.mpd = card.Mpd()
            view = card.status_view(self.mpd)
        except Exception:
            self.mpd = None
            self.playing_here = False
            return
        self.status = view
        cur = (view.get("current") or {}).get("file", "")
        here = cur.startswith(self.book.folder + "/") and view.get("state") in ("play", "pause")
        self.playing_here = here
        if not here:
            self.active = None
            return
        elapsed = float(view.get("elapsed") or 0)
        i = books.here(self.book.chapters, cur, elapsed) if self.book.audio \
            else self.book.chapter_of_file(cur)
        if i is not None and i != self.ci:
            self.ci = i
            self.load_chapter()
            self.follow = True
        offset = float(card.setting("lyricsOffset", 0) or 0) / 1000
        k = bisect.bisect_right(self.times, elapsed + 0.15 - offset) - 1
        active = self.index[k] if k >= 0 else None
        if active != self.active:
            self.active = active
            if self.follow and active:
                self.bring_into_view(active[0], active[1])

    def bring_into_view(self, p, s):
        target = None
        for n, line in enumerate(self.lines):
            if line["p"] == p and any(idx[0] == s for _x, _t, idx in line["words"]):
                target = n
                break
        if target is None:
            return
        h = self.body_height()
        if self.prefs["layout"] == "pages":
            self.top = (target // h) * h
        elif not self.top + 1 <= target < self.top + h - 2:
            self.top = max(0, target - h // 3)

    # -- choosing a word or a sentence

    def picked(self, li, wn, line, idx):
        """Is this word part of what he has chosen?"""
        if self.pick is None:
            return False
        pl, pw = self.pick
        if self.pick_kind == "word":
            return li == pl and wn == pw
        if self.pick_kind == "line":
            return li == pl
        here = self.pick_word()
        return bool(here) and line["p"] == here[0] and idx[0] == here[1]

    def pick_word(self):
        """(paragraph, sentence, word) of what is chosen, or None."""
        if self.pick is None:
            return None
        pl, pw = self.pick
        if not 0 <= pl < len(self.lines):
            return None
        line = self.lines[pl]
        if not 0 <= pw < len(line["words"]):
            return None
        _x, _text, idx = line["words"][pw]
        return (line["p"], idx[0], idx[1])

    def pick_text(self):
        """The word he chose, or the whole sentence it is in."""
        here = self.pick_word()
        if not here:
            return ""
        p, s_, w = here
        paras = self.book.paras(self.ci)
        if not (0 <= p < len(paras) and 0 <= s_ < len(paras[p])):
            return ""
        if self.pick_kind == "line":
            line = self.lines[self.pick[0]]
            return " ".join(text for _x, text, _i in line["words"])
        sentence = paras[p][s_]
        if self.pick_kind == "sentence":
            return " ".join(str(word) for word, _t in sentence)
        return str(sentence[w][0]) if 0 <= w < len(sentence) else ""

    def start_pick(self):
        """Choose the first word on the line he is reading from."""
        if not self.lines:
            return
        at = max(0, min(self.top, len(self.lines) - 1))
        for n in range(at, min(len(self.lines), at + self.body_height())):
            if self.lines[n]["words"]:
                self.pick = (n, 0)
                self.say("Choosing · ← → a word · w the sentence · "
                         "d meaning · m mark · space read from here · right click for more · esc")
                return

    def move_pick(self, by=0, lines=0):
        """Along the words, and up and down the lines."""
        if self.pick is None:
            return
        pl, pw = self.pick
        if lines:
            want = pl
            while True:
                want += lines
                if not 0 <= want < len(self.lines):
                    return
                if self.lines[want]["words"]:
                    break
            pw = min(pw, len(self.lines[want]["words"]) - 1)
            self.pick = (want, pw)
        elif by:
            pw += by
            while True:
                words = self.lines[pl]["words"] if 0 <= pl < len(self.lines) else []
                if 0 <= pw < len(words):
                    break
                pl += 1 if pw >= len(words) else -1
                if not 0 <= pl < len(self.lines):
                    return
                pw = 0 if by > 0 else len(self.lines[pl]["words"]) - 1
            self.pick = (pl, pw)
        self.bring_pick_into_view()

    def bring_pick_into_view(self):
        if self.pick is None:
            return
        line = self.pick[0]
        h = self.body_height()
        if self.prefs["layout"] == "pages":
            self.top = (line // h) * h
        elif not self.top <= line < self.top + h:
            self.top = max(0, line - h // 3)
        self.dirty = True

    # -- the mouse
    #
    # Asked for 2026-09-30: choosing a word, a sentence or a line with the
    # mouse, and a right click offering what can be done with it -- reading
    # aloud from there, a word's meaning, and whatever else fits. The choosing
    # already existed for the keyboard (s, then the arrows), so the mouse sets
    # the same pick and every action that already worked keeps working. One
    # click a word, two a sentence, three the line -- as a terminal does it.

    PICK_KINDS = ("word", "sentence", "line")

    def at_point(self, y, x):
        """(line, word) under the pointer, or None when that is not text."""
        y0, h = self.body_top(), self.body_height()
        if not self.lines or not (y0 <= y < y0 + h):
            return None
        li = self.top + (y - y0)
        if not 0 <= li < len(self.lines):
            return None
        words = self.lines[li]["words"]
        if not words:
            return None
        col = x - self.left
        nearest, best = 0, None
        for wn, (wx, text, _idx) in enumerate(words):
            if wx <= col < wx + cells(text):
                return (li, wn)
            # Past the end of the line, or in the gap between two words: the
            # nearest word, so a click never does nothing.
            gap = abs(col - wx)
            if best is None or gap < best:
                nearest, best = wn, gap
        return (li, nearest)

    def pick_at(self, y, x, kind="word"):
        """Choose what is under the pointer."""
        where = self.at_point(y, x)
        if where is None:
            return False
        self.pick, self.pick_kind = where, kind
        self.say("Chose the %s · right click for what can be done with it" % kind)
        self.dirty = True
        return True

    # -- what a right click offers

    def menu_items(self):
        """The menu, built for what is chosen. Only what can actually be done
        with it: a dictionary takes one word, so the meaning is always the
        word under the pointer even when a sentence is chosen."""
        items = []
        word = ""
        here = self.pick_word()
        if here:
            p, s_, w = here
            paras = self.book.paras(self.ci) if self.book else []
            if 0 <= p < len(paras) and 0 <= s_ < len(paras[p]):
                sentence = paras[p][s_]
                if 0 <= w < len(sentence):
                    word = str(sentence[w][0])
        chosen = self.pick_text()
        items.append(("Read aloud from here", "read"))
        if word:
            items.append(("Meaning of \u201c%s\u201d" % cut(word, 22), "meaning"))
        if chosen:
            items.append(("Copy the %s" % self.pick_kind, "copy"))
            items.append(("Find \u201c%s\u201d in the book" % cut(chosen, 18), "find"))
        items.append(("Bookmark here", "mark"))
        for kind in self.PICK_KINDS:
            if kind != self.pick_kind:
                items.append(("Choose the %s instead" % kind, "kind:" + kind))
        return items

    def open_menu(self, y, x):
        """Right click: choose what is under the pointer if nothing is, then
        offer what can be done with it."""
        if self.pick is None and not self.pick_at(y, x):
            return
        self.menu = self.menu_items()
        if not self.menu:
            return
        self.menu_at, self.sel, self.overlay = (y, x), 0, "menu"
        self.dirty = True

    def menu_do(self, action):
        """One item of the menu. The pick stays where it is unless the action
        itself puts it away, so several things can be done to one word."""
        self.overlay, self.menu = None, None
        if action == "read":
            self.read_from_here()
        elif action == "meaning":
            self.look_up()
        elif action == "mark":
            self.add_mark()
        elif action == "copy":
            self.copy_pick()
        elif action == "find":
            text = self.pick_text()
            if text:
                self.input = {"kind": "search", "text": text[:60]}
        elif action.startswith("kind:"):
            self.pick_kind = action.split(":", 1)[1]
            self.say("Choosing the %s" % self.pick_kind)
        self.dirty = True

    def copy_pick(self):
        """What he chose, onto the clipboard. wl-copy is Wayland's, and it is
        already here (Omarchy's own clipboard watch uses wl-paste)."""
        text = self.pick_text()
        if not text:
            return
        try:
            proc = subprocess.Popen(["wl-copy", "--"], stdin=subprocess.PIPE)
            proc.communicate(text.encode()[:100000], timeout=5)
            self.say("Copied the %s" % self.pick_kind)
        except (OSError, subprocess.SubprocessError):
            # Nothing to be done about it and nothing lost; say so plainly
            # rather than showing Python's words about wl-copy.
            self.say("Could not copy: wl-copy is not here")

    def menu_row(self, y, x):
        """Which menu row is under the pointer, or None."""
        if self.overlay != "menu" or not self.panel_box:
            return None
        y0, x0, width, height = self.panel_box
        if not (y0 < y < y0 + height - 1 and x0 <= x < x0 + width):
            return None
        n = y - y0 - 1
        return n if 0 <= n < len(self.menu or []) else None

    def mouse(self, y, x, bstate):
        """One mouse event: one click a word, two a sentence, three the line.

        Both ways of learning that are needed, and which one applies is the
        terminal's business, not ours. Measured here on 2026-09-30 in a real
        terminal: ncurses gives ONE event per run of clicks, already counted --
        BUTTON1_CLICKED, then DOUBLE, then TRIPLE. Counting the events myself
        therefore saw a double click as a single one. Where a terminal reports
        only plain presses instead, the count below does the work.
        """
        double = bstate & getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
        triple = bstate & getattr(curses, "BUTTON1_TRIPLE_CLICKED", 0)
        left = bstate & (getattr(curses, "BUTTON1_PRESSED", 0)
                         | getattr(curses, "BUTTON1_CLICKED", 0)) or double or triple
        right = bstate & (getattr(curses, "BUTTON3_PRESSED", 0)
                          | getattr(curses, "BUTTON3_CLICKED", 0))
        if right:
            self.open_menu(y, x)
            return
        if not left:
            return
        if self.overlay == "menu":
            row = self.menu_row(y, x)
            if row is not None:
                self.sel = row
                self.menu_do((self.menu or [])[row][1])
            else:
                self.overlay, self.menu = None, None      # clicked away
                self.dirty = True
            return
        if self.overlay is not None:
            return                       # another pop-up owns the screen
        if triple or double:
            self.clicks = (0.0, -1, -1, 0)      # counted for us; start afresh after
            self.pick_at(y, x, "line" if triple else "sentence")
            return
        when, ly, lx, n = self.clicks
        near = y == ly and abs(x - lx) <= 2
        n = n + 1 if (time.time() - when < 0.4 and near) else 1
        self.clicks = (time.time(), y, x, n)
        self.pick_at(y, x, self.PICK_KINDS[min(n, 3) - 1])

    def stop_pick(self):
        self.pick, self.meaning = None, None
        if self.overlay == "menu":
            self.overlay, self.menu = None, None
        self.dirty = True

    def look_up(self):
        """What the chosen word means (meaning.py: his dictionaries if he has
        any, else Wiktionary, in the language he set)."""
        here = self.pick_word()
        if not here:
            return
        p, s_, w = here
        paras = self.book.paras(self.ci)
        word = ""
        if 0 <= p < len(paras) and 0 <= s_ < len(paras[p]):
            sentence = paras[p][s_]
            if 0 <= w < len(sentence):
                word = str(sentence[w][0])
        if not word:
            return
        import meaning
        self.meaning = quiet(meaning.cmd_meaning, word)
        self.overlay = "meaning"
        self.dirty = True

    def lit(self, p, idx):
        if not self.active:
            return False
        ap, as_, aw = self.active
        mode = card.setting("textHighlight", "sentence")
        if mode == "paragraph":
            return p == ap
        if mode == "word":
            return p == ap and idx == (as_, aw)
        return p == ap and idx[0] == as_

    # -- places

    def note_zoom(self):
        """Ctrl + / Ctrl − in foot zoom the window; the cells get wider, which
        the terminal reports in pixels. The size it adds up to is kept, so
        the next window (any book) opens at the same zoom."""
        width = cell_width()
        if not (width and getattr(self, "base_cell", 0) and getattr(self, "base_size", 0)):
            return
        size = round(self.base_size * width / self.base_cell * 2) / 2
        keep = None if abs(size - books.foot_font_size()) < 0.25 else size
        if 4 <= size <= 40 and self.prefs.get("fontSize") != keep:
            self.prefs["fontSize"] = keep
            self.dirty = True

    # -- time spent
    #
    # Count the time on this book: while it plays, or while he is still
    # turning pages. Written down once a minute (library.add_time), so the
    # library screen can show today, this week and the streak.

    IDLE = 120
    FLUSH = 60

    # How long to wait for a key before looking around again. Following
    # spoken audio moves a highlight from word to word and needs the quarter
    # second; with nothing playing, a reader open on the desk was waking four
    # times a second to ask MPD the same question -- measured 2026-09-27 at
    # 0.4% of a core for a window nobody was touching. A key still wakes it
    # at once whatever this says; the wait is only a ceiling.
    QUICK, CALM, RESTFUL = 250, 500, 1000

    def pace(self):
        # Only what is playing *here* matters: `follow` stays on between
        # books, and MPD playing something else is no reason to hurry.
        if self.playing_here and self.status.get("state") == "play":
            return self.QUICK
        if self.playing_here:
            return self.CALM          # paused on this book: nothing is moving
        if self.waiting_for >= 0 or self.speaking_now():
            return self.CALM
        if time.time() - self.last_key < 2:
            return self.CALM          # he is still moving about
        return self.RESTFUL

    def speaking_now(self):
        """Whether a chapter is being made for this book right now."""
        now, fresh = self.read_speaking()
        return bool(fresh and now.get("state") in ("making", "queued"))

    def tick(self):
        now = time.time()
        gap, self.last_tick = min(5.0, max(0.0, now - self.last_tick)), now
        if self.book and (self.playing_here or now - self.last_key < self.IDLE):
            self.read_seconds += gap
        if self.read_seconds >= self.FLUSH and self.book:
            self.flush_time()

    def flush_time(self):
        if self.book and self.read_seconds >= 1:
            library.add_time(self.book.key, int(self.read_seconds))
            self.read_seconds = 0.0

    def save(self, force=False):
        self.note_zoom()
        if force:
            self.flush_time()
        if not self.book:
            return
        word = self.word_of_line(self.top)
        if (self.place or {}).get("chapter") != self.ci or (self.place or {}).get("word") != word:
            self.place = {"chapter": self.ci, "word": word}
            self.dirty = True
        if self.dirty and (force or time.time() - self.saved_at > 3):
            with library.edit() as lib:
                rec = library.record(lib, self.book.key)
                rec["read"].update(chapter=self.ci, word=word,
                                   percent=round(self.book_pct(), 4),
                                   chapters=len(self.book.chapters))
                rec["read"].pop("para", None)         # the old place, now a word
                rec["t"] = time.time()
            self.state["prefs"] = self.prefs
            card.save(STATE, self.state)
            self.saved_at, self.dirty = time.time(), False

    def check_pending(self):
        path = card.state_path(PENDING)
        try:
            with open(path) as f:
                req = json.load(f)
            os.remove(path)
        except (OSError, ValueError):
            return
        target = req.get("target") if isinstance(req, dict) else None
        if target:
            self.open(target)
        else:
            self.open_last()

    def open_last(self):
        last = self.state.get("last")
        target = (library.get(last).get("path") if last else "") or None
        if not (target and self.open(target)):
            self.overlay, self.sel = "library", 0
            self.shelf = shelf(self.state)

    # -- keys

    def say(self, text):
        self.message, self.message_at = text, time.time()

    def scroll(self, n):
        h = self.body_height()
        if self.prefs["layout"] == "pages":
            n = h if n > 0 else -h
        before = self.top
        self.set_top(self.top + n)
        if self.top == before and n > 0 and self.book and self.ci + 1 < len(self.book.chapters):
            self.go_chapter(self.ci + 1)       # past the end: the next chapter
        elif self.top == before and n < 0 and self.ci > 0 and self.top == 0:
            self.go_chapter(self.ci - 1)
            self.set_top(self.last_top())
        if self.playing_here and n:
            self.follow = False
        self.mark_read()

    def key(self, k):
        """k: a curses key code, or a one-character string."""
        self.last_key = time.time()
        ch = k if isinstance(k, str) else (chr(k) if 0 <= k < 256 else "")
        if self.input is not None:
            return self.type_key(k, ch)
        if self.overlay:
            return self.overlay_key(k, ch)
        if self.book is None:
            # Nothing open (the book was empty or has gone): only the list of
            # books and the keys make sense.
            if ch == "?":
                self.overlay = "help"
            else:
                self.overlay, self.sel, self.shelf = "library", 0, shelf(self.state)
            return
        h = self.body_height()
        if self.pick is not None:
            return self.pick_key(k, ch)
        audio = self.book is not None and self.book.audio and self.playing_here
        # Skipping back and forward is about sound, not about the format: a
        # novel being read aloud skips exactly like an audiobook.
        listening = self.playing_here
        # No key closes the reader (his choice, 2026-09-22): the window closes
        # like any other, with Super + W, and run() saves the place then.
        if k in (curses.KEY_DOWN,) or ch == "j":
            self.scroll(1)
        elif k in (curses.KEY_UP,) or ch == "k":
            self.scroll(-1)
        elif ch == " ":
            # Space plays and pauses, as it does everywhere else (his choice,
            # 2026-09-23). Paging is f / b and the page keys.
            self.play_pause()
        elif k in (curses.KEY_NPAGE,) or ch == "f":
            self.scroll(h - 1)
        elif k in (curses.KEY_PPAGE,) or ch == "b":
            self.scroll(-(h - 1))
        elif ch == "g" or k == curses.KEY_HOME:
            self.top = 0
        elif ch == "G" or k == curses.KEY_END:
            self.set_top(self.last_top())
            self.mark_read()
        elif ch in ("]", "J"):
            # Chapter forward sits next to j / k (he asked for it, 2026-09-23:
            # the chapter keys were far from everything else).
            if audio:
                quiet(books.cmd_book_step, "next")
            else:
                self.go_chapter(self.ci + 1)
        elif ch in ("[", "K"):
            if audio:
                quiet(books.cmd_book_step, "previous")
            else:
                self.go_chapter(self.ci - 1)
        elif k == curses.KEY_RIGHT or ch == "l":
            if listening:
                quiet(card.cmd_mpd, "seekcur", "+%d" % (card.setting("bookSkipForward", 30) or 30))
            else:
                self.scroll(h - 1)
        elif k == curses.KEY_LEFT or ch == "h":
            if listening:
                quiet(card.cmd_mpd, "seekcur", "-%d" % (card.setting("bookSkipBack", 10) or 10))
            else:
                self.scroll(-(h - 1))
        elif ch == "p":
            self.play_pause()           # p still works: it is what he learned first
        elif ch == ".":
            self.follow = True
            if self.active:
                self.bring_into_view(self.active[0], self.active[1])
        elif ch in ("C", "c"):
            self.overlay, self.sel = "chapters", self.ci
        elif ch in ("A", "a"):
            self.overlay, self.sel = "text", 0
            self.line_height_before = self.prefs.get("lineHeight", 1.0)
        elif ch == "L":
            self.open_shelf()
        elif ch == "/":
            self.input = {"kind": "search", "text": ""}
        elif ch == "m":
            self.add_mark()
        elif ch in ("B", "M"):
            self.marks = library.get(self.book.key)["bookmarks"]
            self.overlay, self.sel = "marks", max(0, len(self.marks) - 1)
        elif ch == "?":
            self.overlay = "help"
        elif ch == "s":
            self.start_pick()
        elif ch == "z":
            self.zen = not self.zen
            self.relayout()
        elif ch in ("+", "="):
            self.change_pref("width", 1)
        elif ch in ("-", "_"):
            self.change_pref("width", -1)

    def pick_key(self, k, ch):
        """While a word or a sentence is chosen."""
        if k == 27 or ch in ("\x1b", "s", "q"):
            self.stop_pick()
        elif k == curses.KEY_RIGHT or ch == "l":
            self.move_pick(by=1)
        elif k == curses.KEY_LEFT or ch == "h":
            self.move_pick(by=-1)
        elif k == curses.KEY_DOWN or ch == "j":
            self.move_pick(lines=1)
        elif k == curses.KEY_UP or ch == "k":
            self.move_pick(lines=-1)
        elif ch == "w":
            self.pick_kind = "sentence" if self.pick_kind == "word" else "word"
            self.say("Choosing the %s" % self.pick_kind)
        elif ch == "d":
            self.look_up()
        elif ch == "m":
            self.add_mark()
        elif ch == " ":
            self.read_from_here()
        self.dirty = True

    def read_from_here(self):
        """Read the book aloud from the sentence he chose.

        When the chapter has been read aloud already, it is a seek: every
        sentence's time is in the .lrc beside it. When it has not, the making
        starts and playing begins at its first piece -- from the top of the
        chapter, because speech has to be made in order, and he is told so.
        """
        here = self.pick_word()
        if not here or not self.book:
            return
        p, s_, _w = here
        if self.book.audio or self.ci in getattr(self.book, "spoken", {}):
            when = self.sentence_time(p, s_)
            if not self.playing_here:
                answer = (quiet(speak.cmd_play_spoken, self.book.path, str(self.ci))
                          if not self.book.audio
                          else quiet(books.cmd_play_book, self.book.folder,
                                     self.book.chapters[self.ci]["id"]))
                if answer.get("error"):
                    self.say(answer["error"])
                    self.stop_pick()
                    return
            if when is not None:
                quiet(card.cmd_mpd, "seekcur", "%.1f" % when)
                self.say("Reading from here")
            self.follow = True
            self.stop_pick()
            return
        answer = quiet(speak.cmd_speak, self.book.path, str(self.ci), "listen")
        if answer.get("error"):
            # The message replaces the choosing, as it does when the reading
            # starts: a highlighted sentence left under an error looks like it
            # is about to be read and is not (found 2026-09-30, by the test
            # that had been saying so all along).
            self.say(answer["error"])
            self.stop_pick()
            return
        self.waiting_for = self.ci
        self.say("Reading this chapter aloud · it starts from the beginning")
        self.stop_pick()

    def sentence_time(self, p, s_):
        """When a sentence is spoken, from the times already in hand."""
        paras = self.book.paras(self.ci)
        if not (0 <= p < len(paras) and 0 <= s_ < len(paras[p])):
            return None
        for word, when in paras[p][s_]:
            if when is not None and when >= 0:
                return float(when)
        return None

    def open_shelf(self):
        self.shelf = shelf(self.state)
        self.overlay, self.sel = "library", 0

    # -- searching, bookmarks

    def visible_shelf(self):
        """The book list as the sort, the filter and the search leave it."""
        books_ = [b for b in self.shelf
                  if (self.filter == "all" or b["state"] == self.filter)
                  and (not self.query or self.query.lower() in b["title"].lower())]
        if self.sort == "title":
            books_.sort(key=lambda b: b["title"].lower())
        elif self.sort == "progress":
            books_.sort(key=lambda b: -b["percent"])
        else:
            books_.sort(key=lambda b: -b["t"])
        return books_

    def search_book(self, query, most=200):
        """Every place the words appear, over the whole book."""
        query = query.strip().lower()
        self.hits = []
        if not query or not self.book:
            return
        for ci in range(len(self.book.chapters)):
            paras = self.book.paras(ci)
            word_at = 0
            for para in paras:
                words = [w for s in para for w, _t in s]
                text = " ".join(words).lower()
                start = text.find(query)
                if start >= 0:
                    before = len(text[:start].split())
                    plain = " ".join(words)
                    self.hits.append({"chapter": ci, "word": word_at + before,
                                      "title": self.book.chapters[ci]["title"],
                                      "snippet": plain[max(0, start - 30):start + 60].strip()})
                    if len(self.hits) >= most:
                        return
                word_at += len(words)

    def jump_to(self, chapter, word):
        """Go to a place without playing anything (a search hit, a bookmark)."""
        if not 0 <= chapter < len(self.book.chapters):
            return
        if chapter != self.ci:
            self.ci = chapter
            self.load_chapter(word=word)
        else:
            self.set_top(self.line_of_word(word))
        self.follow = False
        self.save()

    def add_mark(self):
        if not self.book:
            return
        if self.playing_here:
            st = self.status
            book = st.get("book") or {}
            at = float(st.get("elapsed") or 0) - float(book.get("start") or 0)
            mark = {"kind": "listen", "chapter": self.ci, "at": int(at),
                    "title": self.book.chapters[self.ci]["title"], "note": ""}
            where = clock(at)
        elif self.pick is not None:
            # A bookmark on what he chose, with the words themselves as the
            # note, so the list of marks reads as the book does.
            line = self.lines[self.pick[0]]
            word = self.word_of_line(self.pick[0]) + self.pick[1]
            snippet = self.pick_text() or " ".join(t for _x, t, _i in line["words"])
            mark = {"kind": "read", "chapter": self.ci, "word": word,
                    "title": self.book.chapters[self.ci]["title"], "note": snippet[:200]}
            where = snippet[:30]
        else:
            word = self.word_of_line(self.top)
            line = self.lines[self.top] if self.top < len(self.lines) else {"words": []}
            snippet = " ".join(t for _x, t, _i in line["words"])[:60]
            mark = {"kind": "read", "chapter": self.ci, "word": word,
                    "title": self.book.chapters[self.ci]["title"], "note": snippet}
            where = snippet[:30]
        library.add_mark(self.book.key, mark)
        self.marks = library.get(self.book.key)["bookmarks"]
        self.say("Bookmarked · " + where + " · type a note, or Esc")
        self.input = {"kind": "note", "text": "", "mark": len(self.marks) - 1}

    def use_mark(self, mark):
        if mark.get("kind") == "listen" and self.book.audio:
            ch = self.book.chapters[min(mark.get("chapter", 0), len(self.book.chapters) - 1)]
            quiet(books.cmd_play_book, self.book.folder, ch["id"])
            quiet(card.cmd_mpd, "seekcur", "%.1f" % (ch["start"] + float(mark.get("at", 0))))
        else:
            self.jump_to(int(mark.get("chapter", 0)), int(mark.get("word", 0)))

    # -- typing a search or a note

    def type_key(self, k, ch):
        box = self.input
        if k == 27:
            self.input = None
        elif k in (10, 13, curses.KEY_ENTER):
            text, kind = box["text"].strip(), box["kind"]
            self.input = None
            if kind == "search" and self.overlay == "library":
                self.query = text
                self.sel = 0
            elif kind == "search":
                self.search_book(text)
                self.overlay, self.sel = "hits", 0
                if not self.hits:
                    self.say("Not found: " + text[:40])
                    self.overlay = None
            elif kind == "chapters":
                self.chapter_query = text
                shown = [r["i"] for r in self.chapter_rows()
                         if not text or text.lower() in ("%d %s" % (r["i"] + 1, r["title"])).lower()]
                if shown and self.sel not in shown:
                    self.sel = shown[0]
            elif kind == "note":
                library.note_mark(self.book.key, box.get("mark", -1), text)
                self.marks = library.get(self.book.key)["bookmarks"]
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            box["text"] = box["text"][:-1]
        elif ch and ch.isprintable():
            box["text"] = (box["text"] + ch)[:120]

    def play_pause(self):
        """Space: play or pause what is in front of him.

        A chapter already read aloud plays like any other. One that is not
        made yet joins the queue (his design, 2026-09-23: the card is where
        chapters are ticked, and the reader adds one without leaving it)."""
        if not self.book:
            return
        if self.playing_here:
            quiet(card.cmd_pause)
            return
        if self.book.audio:
            quiet(books.cmd_play_book, self.book.folder, self.book.chapters[self.ci]["id"])
            self.follow = True
            return
        if self.ci in self.book.spoken:
            answer = quiet(speak.cmd_play_spoken, self.book.path, str(self.ci))
            if answer.get("error"):
                self.say(answer["error"])
                return
            self.follow = True
            self.load_chapter()             # now with the timings from its .lrc
            self.say("Reading aloud")
        else:
            self.queue_chapter(self.ci)

    def queue_run(self, first, last):
        """Tick every chapter between two, skipping the ones already spoken
        or already waiting: those are not work to ask for again."""
        if not self.book or self.book.audio:
            return
        lo, hi = min(first, last), max(first, last)
        waiting = {int(q.get("chapter", -1)) for q in speak.queue_of(self.book.path)}
        want = [i for i in range(lo, hi + 1)
                if i not in self.book.spoken and i not in waiting]
        if not want:
            self.say("Nothing to add between %d and %d" % (lo + 1, hi + 1))
            return
        answer = quiet(speak.cmd_speak, self.book.path, ",".join(str(i) for i in want))
        if answer.get("error"):
            self.say(answer["error"])
            return
        added = answer.get("added") or []
        self.waiting_for = added[0] if added else -1
        work = int(answer.get("work") or 0)
        self.say("%d chapters added%s" % (
            len(added), " · about %d min of work" % max(1, round(work / 60)) if work else ""))

    def queue_chapter(self, i):
        """Tick a chapter to be read aloud, from the reader. The same queue
        the card uses, so both show the same thing."""
        if not self.book or self.book.audio or not 0 <= i < len(self.book.chapters):
            return
        if i in self.book.spoken:
            self.say("Chapter %d is ready · space plays it" % (i + 1))
            return
        answer = quiet(speak.cmd_speak, self.book.path, str(i))
        if answer.get("error"):
            self.say(answer["error"])       # no engine, or the book has gone
            return
        self.waiting_for = i
        work = int(answer.get("work") or 0)
        left = int(answer.get("waiting") or 0)
        self.say("Chapter %d added%s%s" % (
            i + 1,
            " · about %d min of work" % max(1, round(work / 60)) if work else "",
            " · %d waiting" % left if left > 1 else ""))

    # -- reading a novel aloud
    #
    # A chapter is spoken into the spoken folder and belongs to this book:
    # the reader stays on the novel, lists all its chapters, and plays the
    # made ones under the novel's own name and its own progress.

    def read_speaking(self, gap=0.5):
        """What is being made and what is waiting: only read, never acted on,
        so drawing the screen can ask too.

        It is three small pieces of work -- a file, the lock, the queue --
        and the loop, the drawing and the pacing all want the answer within
        the same breath, so the answer is held for half a second.
        """
        if time.time() - self._spoken_at < gap:
            return self._spoken
        now = card.load(speak.SPOKEN, {})
        fresh = time.time() - float(now.get("at", 0) or 0) < 90
        self.making = speak.busy() or None        # the lock, not a left-behind file
        self.waiting = len(speak.queue_of())      # behind the one being made
        self._spoken, self._spoken_at = (now, fresh), time.time()
        return self._spoken

    def spoken_check(self):
        """While chapters are being made: show how far, and play the one he
        is waiting for when it is ready."""
        now, fresh = self.read_speaking()
        if now.get("state") == "error" and fresh and now.get("path") == getattr(self.book, "path", None):
            if self.said_error != now.get("at"):
                self.said_error = now.get("at")
                self.say("Could not read chapter %d aloud: %s"
                         % (int(now.get("chapter", 0)) + 1, str(now.get("message"))[:60]))
            return
        if now.get("state") != "ready" or not fresh:
            return
        if not self.book or self.book.audio or now.get("path") != self.book.path:
            return
        chapter = int(now.get("chapter", -1))
        if chapter not in self.book.spoken:
            self.book.refresh_spoken()      # it has just appeared
            if chapter == self.ci:
                self.load_chapter()
            self.dirty = True
        if chapter == self.waiting_for:
            self.waiting_for = -1
            if chapter != self.ci:
                self.ci = chapter
                self.load_chapter()
            answer = quiet(speak.cmd_play_spoken, self.book.path, str(chapter))
            if not answer.get("error"):
                self.follow = True
                self.say("Reading aloud")

    def change_pref(self, name, step):
        p = self.prefs
        if name == "width":
            i = min(range(len(WIDTHS)), key=lambda n: abs(WIDTHS[n] - p["width"]))
            p["width"] = WIDTHS[max(0, min(len(WIDTHS) - 1, i + step))]
        elif name == "spacing":
            p["spacing"] = (p["spacing"] + step) % 3
        elif name == "lineHeight":
            cur = p.get("lineHeight", 1.0)
            i = LINE_HEIGHTS.index(cur) if cur in LINE_HEIGHTS else 0
            p["lineHeight"] = LINE_HEIGHTS[(i + step) % len(LINE_HEIGHTS)]
        elif name == "indent":
            cur = p.get("indent", 0)
            i = INDENTS.index(cur) if cur in INDENTS else 0
            p["indent"] = INDENTS[(i + step) % len(INDENTS)]
        elif name == "animation":
            cur = p.get("animation", "slide")
            p["animation"] = ANIMATIONS[(ANIMATIONS.index(cur) + 1) % len(ANIMATIONS)
                                        if cur in ANIMATIONS else 0]
        elif name == "justify":
            p["justify"] = not p["justify"]
        elif name == "layout":
            p["layout"] = LAYOUTS[(LAYOUTS.index(p["layout"]) + 1) % 2]
            h = self.body_height()
            if p["layout"] == "pages":
                self.top = (self.top // h) * h
        elif name == "highlight":
            cur = card.setting("textHighlight", "sentence")
            nxt = HIGHLIGHTS[(HIGHLIGHTS.index(cur) + step) % 3] if cur in HIGHLIGHTS else "sentence"
            settings = card.load("settings.json", {})
            settings["textHighlight"] = nxt         # the card's setting: one choice for both
            card.save("settings.json", settings)
        para = self.lines[self.top]["p"] if self.lines and self.top < len(self.lines) else 0
        self.relayout()
        self.top = self.line_of_para(para) if p["layout"] == "scroll" else self.top
        self.dirty = True

    TEXT_ROWS = ("width", "lineHeight", "spacing", "indent", "justify", "layout", "highlight",
                 "animation")

    def overlay_key(self, k, ch):
        up = k == curses.KEY_UP or ch == "k"
        down = k == curses.KEY_DOWN or ch == "j"
        enter = k in (10, 13, curses.KEY_ENTER)
        if self.overlay == "menu":
            items = self.menu or []
            if k == 27 or ch == "q":
                self.overlay, self.menu = None, None   # the pick stays chosen
            elif up:
                self.sel = (self.sel - 1) % max(1, len(items))
            elif down:
                self.sel = (self.sel + 1) % max(1, len(items))
            elif enter and 0 <= self.sel < len(items):
                self.menu_do(items[self.sel][1])
            self.dirty = True
            return
        if self.overlay == "meaning":
            self.overlay, self.meaning = None, None
            return
        if k == 27 or (self.overlay == "chapters" and ch in ("C", "c")) \
                or (self.overlay == "text" and ch in ("A", "a")) or (self.overlay == "help" and ch == "?"):
            if self.overlay == "text" and self.prefs.get("lineHeight", 1.0) != \
                    getattr(self, "line_height_before", self.prefs.get("lineHeight", 1.0)):
                self.reopen = True          # line height lives in the terminal: a new window
            self.overlay = None
            return
        if self.overlay == "chapters":
            # The list is what is on screen: with a search on, moving must
            # not walk through the chapters it is hiding.
            shown = [r["i"] for r in (self.chapter_shown or self.chapter_rows())]
            n = len(self.book.chapters)
            at = shown.index(self.sel) if self.sel in shown else 0
            step = max(1, self.rows - 8)
            if ch.isdigit():
                # Typing a number jumps to that chapter: 600 chapters are not
                # something to scroll through one line at a time.
                self.chapter_typed = (self.chapter_typed + ch)[-6:]
                self.chapter_typed_at = time.time()
                want = int(self.chapter_typed) - 1
                if 0 <= want < n:
                    self.sel = want
                return
            self.chapter_typed = ""
            if up:
                self.sel = shown[max(0, at - 1)] if shown else self.sel
            elif down:
                self.sel = shown[min(len(shown) - 1, at + 1)] if shown else self.sel
            elif k == curses.KEY_PPAGE or ch == "b":
                self.sel = shown[max(0, at - step)] if shown else self.sel
            elif k == curses.KEY_NPAGE or ch == "f":
                self.sel = shown[min(len(shown) - 1, at + step)] if shown else self.sel
            elif ch == "g" or k == curses.KEY_HOME:
                self.sel = shown[0] if shown else 0
            elif ch == "G" or k == curses.KEY_END:
                self.sel = shown[-1] if shown else n - 1
            elif ch == "/":
                self.input = {"kind": "chapters", "text": self.chapter_query}
            elif ch == "v":
                # "From here": mark one end of a run. A terminal cannot tell
                # shift + space from space, so the run is marked instead.
                self.chapter_from = -1 if self.chapter_from == self.sel else self.sel
                self.say("From chapter %d · move, then space" % (self.sel + 1)
                         if self.chapter_from >= 0 else "Run cleared")
            elif ch == " ":
                # Tick this chapter to be read aloud, without leaving the list.
                if self.chapter_from >= 0:
                    self.queue_run(self.chapter_from, self.sel)
                    self.chapter_from = -1
                else:
                    self.queue_chapter(self.sel)
                self.dirty = True
            elif ch == "." and self.ci in shown:
                self.sel = self.ci               # back to the one being read
            elif enter:
                self.go_chapter(self.sel)
                if not self.book.audio and self.sel in self.book.spoken and self.playing_here:
                    pass                          # go_chapter already moved the sound
                self.overlay = None
        elif self.overlay == "text":
            if up:
                self.sel = max(0, self.sel - 1)
            elif down:
                self.sel = min(len(self.TEXT_ROWS) - 1, self.sel + 1)
            elif k == curses.KEY_RIGHT or ch == "l" or enter:
                self.change_pref(self.TEXT_ROWS[self.sel], 1)
            elif k == curses.KEY_LEFT or ch == "h":
                self.change_pref(self.TEXT_ROWS[self.sel], -1)
        elif self.overlay == "library":
            books_ = self.visible_shelf()
            n = len(books_)
            if up:
                self.sel = max(0, self.sel - 1)
            elif down:
                self.sel = min(max(0, n - 1), self.sel + 1)
            elif ch == "/":
                self.input = {"kind": "search", "text": self.query}
            elif ch == "s":
                order = ("recent", "title", "progress")
                self.sort = order[(order.index(self.sort) + 1) % 3]
            elif ch == "f":
                kinds = ("all", "reading", "finished", "new")
                self.filter = kinds[(kinds.index(self.filter) + 1) % 4]
                self.sel = 0
            elif enter and n:
                if not self.open(books_[self.sel]["target"]):
                    self.shelf = shelf(self.state)   # it may have moved: the list again
                    self.sel = 0
        elif self.overlay == "hits":
            if up:
                self.sel = max(0, self.sel - 1)
            elif down:
                self.sel = min(max(0, len(self.hits) - 1), self.sel + 1)
            elif enter and self.hits:
                self.jump_to(self.hits[self.sel]["chapter"], self.hits[self.sel]["word"])
                self.overlay = None
        elif self.overlay == "marks":
            if up:
                self.sel = max(0, self.sel - 1)
            elif down:
                self.sel = min(max(0, len(self.marks) - 1), self.sel + 1)
            elif ch == "d" and self.marks:
                library.drop_mark(self.book.key, self.sel)
                self.marks = library.get(self.book.key)["bookmarks"]
                self.sel = min(self.sel, max(0, len(self.marks) - 1))
            elif ch == "n" and self.marks:
                self.input = {"kind": "note", "text": self.marks[self.sel].get("note", ""),
                              "mark": self.sel}
            elif enter and self.marks:
                self.use_mark(self.marks[self.sel])
                self.overlay = None

    # -- drawing (to curses, or to text for --render)

    def draw(self, scr):
        scr.clear()
        if self.book is None and self.overlay not in ("library", "help"):
            # Nothing open (a damaged book, one that moved): the list of
            # books, with the reason in it.
            self.overlay, self.sel, self.shelf = "library", 0, shelf(self.state)
        rows, cols = self.rows, self.cols
        if self.book and self.overlay != "library":
            self.draw_text(scr)
            if not self.zen:
                self.draw_top(scr)
                self.draw_bottom(scr)
        if self.overlay == "chapters":
            self.draw_chapters(scr)
        elif self.overlay == "text":
            self.draw_text_box(scr)
        elif self.overlay == "meaning":
            self.draw_meaning(scr)
        elif self.overlay == "help":
            self.draw_help(scr)
        elif self.overlay == "library":
            self.draw_library(scr)
        elif self.overlay == "hits":
            self.draw_hits(scr)
        elif self.overlay == "marks":
            self.draw_marks(scr)
        elif self.overlay == "menu":
            self.draw_menu(scr)
        if self.input is not None:
            box = self.input
            label = {"search": "search: ", "chapters": "chapter: ",
                     "note": "note: "}.get(box["kind"], "search: ")
            scr.put(self.rows - 1, 0, " " * self.cols, "")
            scr.put(self.rows - 1, 1, (label + box["text"] + "▏")[:self.cols - 2], "accent")

    def draw_text(self, scr):
        y0, h = self.body_top(), self.body_height()
        if not self.lines:
            if self.book.audio:
                msg = ["No text for this chapter yet.", "",
                       "To read along, put the book's text in its folder:",
                       "a .lrc, .srt, .vtt or .txt with the same name as the chapter,",
                       "or a read-along EPUB (EPUB 3 with Media Overlays).",
                       "", "Reading any book aloud (text to speech) comes later."]
            else:
                msg = ["This chapter is empty."]
            for n, line in enumerate(msg):
                scr.put(y0 + max(0, h // 2 - len(msg) // 2) + n,
                        max(0, (self.cols - len(line)) // 2), line, "dim" if n else "")
            return
        for n in range(h):
            li = self.top + n
            if li >= len(self.lines):
                break
            line = self.lines[li]
            for wn, (x, text, idx) in enumerate(line["words"]):
                lit = self.lit(line["p"], idx)
                if self.picked(li, wn, line, idx):
                    scr.put(y0 + n, self.left + x, text, "accent")
                    continue
                scr.put(y0 + n, self.left + x, text, "reverse" if lit else "")
                # Lit words run together into one bar, as a lit line should.
                if lit and self.lit(line["p"], self.next_idx(line, idx)):
                    scr.put(y0 + n, self.left + x + cells(text), " ", "reverse")
            # The place kept: a small accent mark beside the paragraph
            # (not in text-only mode: there, nothing but the words).
            if not self.zen and (li == self.top or (self.active and line["p"] == self.active[0]
                                                    and line["words"])):
                if self.active and line["p"] == self.active[0] and line["words"]:
                    scr.put(y0 + n, self.left - 2, "▌", "accent")
                elif not self.active and line["words"] and line["p"] == self.lines[self.top]["p"]:
                    scr.put(y0 + n, self.left - 2, "▌", "accent")

    @staticmethod
    def next_idx(line, idx):
        found = False
        for _x, _t, other in line["words"]:
            if found:
                return other
            found = other == idx
        return (-1, -1)

    def pct(self):
        """How far through the chapter, 0 to 1."""
        if not self.lines:
            return 0.0
        return min(1.0, (self.top + self.body_height()) / max(1, len(self.lines)))

    def book_pct(self):
        """How far through the whole book, from each chapter's share of it."""
        weights = getattr(self.book, "weights", None) or []
        total = sum(weights)
        if not total or self.ci >= len(weights):
            return self.pct()
        done = sum(weights[:self.ci]) + weights[self.ci] * self.pct()
        return min(1.0, done / total)

    def words_left(self):
        """Words to the end of the chapter (for "min left")."""
        return sum(len(t.split()) for line in self.lines[self.top:] for _x, t, _i in line["words"])

    def draw_top(self, scr):
        ch = self.book.chapters[self.ci]["title"] if self.book.chapters else ""
        left = " %s · %s · %d of %d · %d%%" % (self.book.title, ch, self.ci + 1,
                                               len(self.book.chapters), round(100 * self.book_pct()))
        # What is being made stays in sight while it is made, with what is
        # waiting behind it: the queue must not be something he guesses at.
        right, style = "C  A  z ", "dim"
        if self.making:
            right = " 󰔊 %d · %d%%%s " % (int(self.making.get("chapter", 0)) + 1,
                                         int(self.making.get("percent", 0)),
                                         " · %d waiting" % self.waiting if self.waiting else "")
            style = "accent"
        elif self.waiting:
            right = " 󰔊 %d waiting " % self.waiting
        scr.put(0, 0, left[:max(0, self.cols - len(right) - 2)], "dim")
        scr.put(0, max(0, self.cols - len(right)), right[:self.cols], style)
        scr.put(1, 0, "─" * self.cols, "dim")

    def draw_bottom(self, scr):
        y = self.rows - 1
        scr.put(y - 1, 0, "─" * self.cols, "dim")
        if time.time() - self.message_at < 4 and self.message:
            scr.put(y, 1, self.message[:self.cols - 2], "")
            return
        if self.playing_here:
            st = self.status
            book = st.get("book") or {}
            start, length = float(book.get("start") or 0), float(book.get("length") or 0)
            elapsed = float(st.get("elapsed") or 0)
            total = length or float(st.get("duration") or 0)
            here = elapsed - start if length else elapsed
            mark = "⏵" if st.get("state") == "play" else "⏸"
            left = " %s %s / %s  " % (mark, clock(here), clock(total))
            right = "  %d min left " % max(0, round((total - here) / 60))
            back = "" if self.follow else "  ↓ back to reading point (.)"
            bar_w = max(0, self.cols - len(left) - len(right) - len(back))
            done = int(bar_w * here / total) if total else 0
            scr.put(y, 0, left, "")
            scr.put(y, len(left), "━" * done, "accent")
            scr.put(y, len(left) + done, "─" * (bar_w - done), "dim")
            scr.put(y, len(left) + bar_w, back + right, "dim")
            return
        hints = " ? keys · / search · m mark · L books · z text only"
        if self.prefs["layout"] == "pages":
            info = "page %d / %d · %d min left " % (self.top // self.body_height() + 1,
                                                    self.page_count(),
                                                    max(1, round(self.words_left() / WPM)))
        else:
            words = self.words_left()
            info = "%d min left in chapter " % max(1, round(words / WPM)) if words else ""
        scr.put(y, 0, hints[:max(0, self.cols - len(info) - 1)], "dim")
        scr.put(y, self.cols - len(info), info, "dim")

    def panel(self, scr, title, rows, width, height=None, at=None):
        """A Claude-style pop-up: a rounded box, a dim title."""
        height = height or len(rows) + 2
        width = min(width, self.cols - 4)
        height = min(height, self.rows - 2)
        if at is None:
            x0, y0 = (self.cols - width) // 2, max(1, (self.rows - height) // 2)
        else:
            # Anchored, for the menu a right click opens: beside the pointer,
            # and pulled back on screen rather than off the edge of it.
            ay, ax = at
            x0 = max(2, min(ax, self.cols - width - 2))
            y0 = max(1, min(ay + 1, self.rows - height - 1))
        # A blank border keeps the text behind from running into the frame.
        for y in range(max(0, y0 - 1), min(self.rows, y0 + height + 1)):
            scr.put(y, max(0, x0 - 2), " " * (width + 4), "")
        scr.put(y0, x0, "╭" + "─" * (width - 2) + "╮", "dim")
        scr.put(y0, x0 + 2, " %s " % title, "dim")
        for y in range(y0 + 1, y0 + height - 1):
            scr.put(y, x0, "│", "dim")
            scr.put(y, x0 + width - 1, "│", "dim")
        scr.put(y0 + height - 1, x0, "╰" + "─" * (width - 2) + "╯", "dim")
        for n, (text, style) in enumerate(rows[:height - 2]):
            scr.put(y0 + 1 + n, x0 + 2, cut(text, width - 4), style)
        # Where it landed, so a click on a row can be matched to that row
        # rather than to a second guess at this arithmetic.
        self.panel_box = (y0, x0, width, height)

    def chapter_states(self):
        if self.book.audio:
            try:
                view = books.book_view(self.book.folder, self.book.files,
                                       books.listen_of(self.book.files))
                return [c["state"] for c in view]
            except Exception:
                return ["new"] * len(self.book.chapters)
        read = set(library.get(self.book.key)["read"].get("done", []))
        last = max(read) if read else -1
        return ["finished" if i in read else "skipped" if i < last else "new"
                for i in range(len(self.book.chapters))]

    # What each mark in the chapter list means. A spoken chapter must not
    # look like one from a real audiobook (he asked for this, 2026-09-23).
    CHAPTER_MARKS = {"finished": "✓", "started": "·", "ready": "♪", "making": "◐",
                     "queued": "☑", "skipped": "○", "new": "○"}

    def chapter_rows(self):
        """Every chapter with its number, its mark and how long it is: what
        the panel draws and what the search looks through."""
        states = self.chapter_states()
        novel = not self.book.audio
        queued, making = set(), -1
        if novel:
            # What is really being made: the lock says so, not the progress
            # file, which a job that has finished leaves behind saying
            # "making" (2026-09-23).
            now = speak.busy()
            if now.get("path") == self.book.path:
                making = int(now.get("chapter", -1))
            queued = {int(i.get("chapter", -1)) for i in speak.queue_of(self.book.path)}
        rows = []
        for i, c in enumerate(self.book.chapters):
            if novel:
                kind = "making" if i == making else "ready" if i in self.book.spoken \
                    else "queued" if i in queued else states[i]
                if kind == "ready" and states[i] == "finished":
                    kind = "finished"
                # How long it is, or -- for one not made yet -- how long it
                # would be, so he can see what he is asking for.
                seconds = self.book.lengths.get(i, 0)
                guess = not seconds
                if guess:
                    seconds = int(self.book.weights[i] / speak.WORDS_PER_SECOND)
            else:
                kind = states[i]
                seconds, guess = int(self.book.chapters[i].get("length") or 0), False
            rows.append({"i": i, "title": c["title"], "kind": kind, "seconds": seconds,
                         "guess": guess,
                         "dim": states[i] == "skipped"
                                or (states[i] == "finished" and i != self.ci)})
        return rows

    def draw_chapters(self, scr):
        all_rows = self.chapter_rows()
        shown = [r for r in all_rows
                 if not self.chapter_query
                 or self.chapter_query.lower() in ("%d %s" % (r["i"] + 1, r["title"])).lower()]
        self.chapter_shown = shown
        width = min(70, self.cols - 4)
        height = min(self.rows - 4, len(shown) + 2)
        body = max(1, height - 2)
        pos = next((n for n, r in enumerate(shown) if r["i"] == self.sel), 0)
        first = max(0, min(pos - body // 2, len(shown) - body))
        number = max(2, len(str(len(all_rows))))
        rows = []
        for r in shown[first:first + body]:
            i = r["i"]
            mark = "●" if i == self.ci else self.CHAPTER_MARKS.get(r["kind"], "○")
            pointer = "❯" if i == self.sel else " "
            length = ("~" if r.get("guess") else "") + clock(r["seconds"]) \
                if r["seconds"] else ""
            # "❯ ♪ " + number + two spaces, then the title, then the length.
            room = (width - 4) - (4 + number + 2) - (len(length) + 2 if length else 0)
            title = cut(r["title"], max(1, room))
            line = "%s %s %*d  %s%s" % (pointer, mark, number, i + 1, title,
                                        " " * max(0, room - cells(title)))
            if length:
                line += "  " + length
            style = "accent" if i == self.sel else "dim" if r["dim"] or r["kind"] == "heard" else ""
            rows.append((line, style))
        if not shown:
            rows = [("  nothing matching “%s”" % self.chapter_query[:30], "dim")]
            height = 3
        title = "Chapters · %d" % len(all_rows)
        if self.chapter_query:
            title = "Chapters · %d of %d · %s" % (len(shown), len(all_rows), self.chapter_query[:20])
        self.panel(scr, title, rows, width, height)
        if not self.book.audio:
            ready = sum(1 for r in all_rows if r["kind"] == "ready")
            making = sum(1 for r in all_rows if r["kind"] == "making")
            waiting = sum(1 for r in all_rows if r["kind"] == "queued")
            foot = "♪ %d spoken%s%s · %s · / search · number jumps" % (
                ready, " · 1 being made" if making else "",
                " · %d waiting" % waiting if waiting else "",
                "space adds to chapter %d" % (self.chapter_from + 1)
                if self.chapter_from >= 0 else "space adds · v a run")
            y = min(self.rows - 2, (self.rows - height) // 2 + height)
            scr.put(y, max(0, (self.cols - width) // 2), foot[:width], "dim")

    def draw_text_box(self, scr):
        p = self.prefs
        foot = getattr(self, "terminal", "") == "foot.desktop"
        values = {"width": "%d" % p["width"] if p["width"] else "full",
                  "lineHeight": ("%.1f" % p.get("lineHeight", 1.0)) + ("" if foot else " (foot only)"),
                  "spacing": ("none", "1 line", "2 lines")[p["spacing"]],
                  "indent": "%d" % p.get("indent", 0) if p.get("indent") else "off",
                  "animation": p.get("animation", "slide"),
                  "justify": "on" if p["justify"] else "off",
                  "layout": p["layout"],
                  "highlight": card.setting("textHighlight", "sentence")}
        names = {"width": "Width", "lineHeight": "Line height", "spacing": "Paragraphs",
                 "indent": "Indent", "justify": "Justify", "layout": "Layout",
                 "highlight": "Lights up", "animation": "Animation"}
        rows = []
        for n, key in enumerate(self.TEXT_ROWS):
            pointer = "❯" if n == self.sel else " "
            rows.append(("%s %-10s ‹ %s ›" % (pointer, names[key], values[key]),
                         "accent" if n == self.sel else ""))
        rows.append(("", ""))
        if p.get("lineHeight", 1.0) != getattr(self, "line_height_before", p.get("lineHeight", 1.0)):
            rows.append(("  the window reopens for the new line height", "dim"))
        rows.append(("  ← → change · esc close", "dim"))
        self.panel(scr, "Text", rows, 50)

    # What a dictionary calls a part of speech, short enough for the margin.
    PARTS = {"adjective": "adj", "adverb": "adv", "preposition": "prep",
             "conjunction": "conj", "pronoun": "pron", "interjection": "excl",
             "determiner": "det", "numeral": "num", "particle": "part",
             "proper noun": "name", "verb": "verb", "noun": "noun", "name": "name"}

    def draw_menu(self, scr):
        """What a right click offers, beside the pointer."""
        rows = []
        for n, (label, _action) in enumerate(self.menu or []):
            rows.append(("%s %s" % ("\u203a" if n == self.sel else " ", label),
                         "accent" if n == self.sel else ""))
        width = max([len(r[0]) for r in rows] + [18]) + 6
        self.panel(scr, self.pick_kind, rows, width, at=self.menu_at)

    def draw_meaning(self, scr):
        got = self.meaning or {}
        width = min(74, self.cols - 4)
        rows = []
        entries = got.get("entries") or []
        if not entries:
            rows.append((got.get("why") or "Nothing found", "dim"))
        for n, e in enumerate(entries[:6]):
            if n:
                rows.append(("", ""))
            part = str(e.get("part") or "")
            part = self.PARTS.get(part.lower(), part)
            # The panel trims to width - 4, and the label takes six more.
            room = max(10, width - 10)
            for m, piece in enumerate(wrap(str(e.get("definition") or ""), room)):
                rows.append((("%-5s " % part[:5] if m == 0 else "      ") + piece,
                             "" if m == 0 else "dim"))
            example = str(e.get("example") or "").strip()
            if example:
                for piece in wrap(example, room - 2)[:2]:
                    rows.append(("        " + piece, "dim"))
        title = "%s · %s" % (got.get("word", ""), got.get("language", ""))
        if got.get("where"):
            title += " · " + got["where"]
        self.panel(scr, title, rows, width, min(self.rows - 4, len(rows) + 2))

    def draw_help(self, scr):
        keys = [("space", "play / pause · read this chapter aloud"),
                ("↑ ↓  j k", "scroll"), ("f  b", "page down / up"),
                ("J  K", "chapter ahead / back  (also ] and [)"),
                ("← →", "skip back / ahead while listening, else page"),
                ("g  G", "start / end of chapter"),
                (".", "back to where it is reading"),
                ("C", "chapters · number jumps, / searches"),
                ("  space  v", "in chapters: add one · a whole run (v, move, space)"),
                ("A", "text"), ("+ −", "text width"), ("z", "text only"),
                ("L", "books"), ("/", "search the book"),
                ("s", "choose a word · ← → move, w the sentence"),
                ("  d  m  space", "chosen: its meaning · mark it · read from there"),
                ("m  B", "bookmark · bookmarks"), ("esc", "close a panel"),
                ("Super + W", "close the reader"),
                ("", ""),
                ("click", "choose a word · twice the sentence · three times the line"),
                ("right click", "what can be done with it"),
                ("wheel", "scroll"),
                ("", ""),
                ("shift + drag", "the terminal's own selection, while the mouse is the reader's")]
        rows = [("%-12s %s" % k if k[0] or k[1] else "", "") for k in keys]
        self.panel(scr, "Keys and the mouse", rows, 68)

    def draw_hits(self, scr):
        height = min(self.rows - 4, len(self.hits) + 2)
        first = max(0, min(self.sel - (height - 2) // 2, len(self.hits) - (height - 2)))
        rows = []
        for i in range(first, min(len(self.hits), first + height - 2)):
            hit = self.hits[i]
            pointer = "❯" if i == self.sel else " "
            rows.append(("%s %-22s %s" % (pointer, hit["title"][:22], hit["snippet"]),
                         "accent" if i == self.sel else ""))
        self.panel(scr, "Found · %d" % len(self.hits), rows, min(90, self.cols - 6), height)

    def draw_marks(self, scr):
        if not self.marks:
            self.panel(scr, "Bookmarks", [("None yet. Press m while reading.", "dim")], 50)
            return
        height = min(self.rows - 4, len(self.marks) + 3)
        first = max(0, min(self.sel - (height - 3) // 2, len(self.marks) - (height - 3)))
        rows = []
        for i in range(first, min(len(self.marks), first + height - 3)):
            mark = self.marks[i]
            pointer = "❯" if i == self.sel else " "
            where = clock(mark.get("at", 0)) if mark.get("kind") == "listen" else ""
            rows.append(("%s %s %s %s" % (pointer, "󰂺" if mark.get("kind") == "listen" else "󰂽",
                                          (mark.get("title") or "")[:20] + (" " + where if where else ""),
                                          (mark.get("note") or "")[:40]),
                         "accent" if i == self.sel else ""))
        rows.append(("  enter goes there · n note · d delete", "dim"))
        self.panel(scr, "Bookmarks · %d" % len(self.marks), rows, min(80, self.cols - 6), height)

    def draw_library(self, scr):
        if not self.shelf:
            rows = [("No books yet.", ""), ("", ""),
                    ("Choose Book folders in the music card's", "dim"),
                    ("Settings → Library → Audiobooks, or open", "dim"),
                    ("an .epub or .txt: reader.py FILE", "dim")]
            if time.time() - self.message_at < 30 and self.message:
                rows = [(self.message, "")] + [("", "")] + rows
            self.panel(scr, "Books", rows, 60)
            return
        books_ = self.visible_shelf()
        self.sel = min(self.sel, max(0, len(books_) - 1))
        height = min(self.rows - 4, len(books_) + 4)
        first = max(0, min(self.sel - (height - 4) // 2, max(0, len(books_) - (height - 4))))
        stats = library.reading_time()
        rows = [(" today %s · this week %s · %d day streak"
                 % (minutes(stats["today"]), minutes(stats["week"]), stats["streak"]), "dim"), ("", "")]
        width = min(86, self.cols - 6)
        for i in range(first, min(len(books_), first + height - 4)):
            b = books_[i]
            pointer = "❯" if i == self.sel else " "
            icon = "󰂽" if b["kind"] == "read" else "󰂺"
            bar = progress_bar(b["percent"], 10)
            tail = "%s %3d%%" % (bar, round(100 * b["percent"])) if b["percent"] > 0.001 else " " * 15
            name = b.get("label", b["title"])[:max(10, width - 22)]
            rows.append(("%s %s %-*s %s" % (pointer, icon, max(10, width - 22), name, tail),
                         "accent" if i == self.sel else "dim" if b["state"] == "finished" else ""))
        if not books_:
            rows.append(("  Nothing matches" if self.query or self.filter != "all" else "  No books", "dim"))
        rows.append(("", ""))
        rows.append(("  / search · s sort: %s · f show: %s%s"
                     % (self.sort, self.filter, "  · " + self.query if self.query else ""), "dim"))
        if time.time() - self.message_at < 4 and self.message:
            rows[-1] = ("  " + self.message[:width - 4], "")
        self.panel(scr, "Books · %d" % len(books_), rows, width, height)


# ---------------------------------------------------------------- screens

def safe(text):
    """Nothing reaches the terminal that can drive it.

    `booktext.clean` already does this to a book's words. What it never saw is
    everything else drawn beside them: a title taken from a file's name, a
    chapter name, a song's tags, a message. Those come from whoever made the
    file, and a book called "quiet\x1b]0;...\x07.txt" retitled the window the
    moment the reader opened it -- found 2026-10-01, going through what
    changes when a stranger runs this.

    It is done here, where the screen is written, because that is the one way
    in: a new row drawn somewhere else cannot forget it.
    """
    return booktext.CONTROL.sub("", text) if text else text


class CursesScreen:
    def __init__(self, win):
        self.win = win
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLUE, -1)      # the theme's accent
        self.styles = {"": curses.A_NORMAL, "dim": curses.A_DIM, "reverse": curses.A_REVERSE,
                       "accent": curses.color_pair(1)}

    def clear(self):
        self.win.erase()

    def put(self, y, x, text, style):
        rows, cols = self.win.getmaxyx()
        if not 0 <= y < rows or x >= cols or x < 0:
            return
        try:
            self.win.addnstr(y, x, safe(text),
                             max(0, cols - x - (1 if y == rows - 1 else 0)),
                             self.styles.get(style, 0))
        except curses.error:
            pass


class TextScreen:
    """For --render and tests: the screen as lines, and which cells are lit."""

    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.clear()

    def clear(self):
        self.grid = [[" "] * self.cols for _ in range(self.rows)]
        self.marks = [[""] * self.cols for _ in range(self.rows)]

    def put(self, y, x, text, style):
        if not 0 <= y < self.rows:
            return
        for i, c in enumerate(safe(text)):
            if 0 <= x + i < self.cols:
                self.grid[y][x + i] = c
                self.marks[y][x + i] = style

    def text(self):
        return "\n".join("".join(r).rstrip() for r in self.grid)

    def lit_text(self):
        return " ".join("".join(c for c, m in zip(r, mk) if m == "reverse").strip()
                        for r, mk in zip(self.grid, self.marks)).split()


# ---------------------------------------------------------------- running

KEYNAMES = {"<down>": curses.KEY_DOWN, "<up>": curses.KEY_UP, "<left>": curses.KEY_LEFT,
            "<right>": curses.KEY_RIGHT, "<enter>": 10, "<esc>": 27, "<pgdn>": curses.KEY_NPAGE,
            "<pgup>": curses.KEY_PPAGE, "<space>": " ", "<home>": curses.KEY_HOME,
            "<end>": curses.KEY_END}


def render(size, keys, target, save=False):
    """Draw once, as text. Saves the place only when asked (the tests, which
    use their own state folder): run from the command line it must never
    change the real reader's settings or places."""
    cols, rows = (int(v) for v in size.lower().split("x"))
    app = Reader(rows, cols)
    if not (target and app.open(target)):
        if not app.book:
            app.open_last() if not target else None
    app.poll()
    app.read_speaking()
    for k in (keys or "").split():
        app.key(KEYNAMES.get(k, k))
        app.poll()
        app.read_speaking()
    scr = TextScreen(rows, cols)
    app.draw(scr)
    if save:
        app.save(force=True)              # as closing the reader would
    return app, scr


# ---------------------------------------------------------------- page turns
#
# His wish (2026-09-22): a page change you can see. The old page slides out
# to the left while the new one comes in from the right (backwards the other
# way), over about a tenth of a second; in the scroll layout a page down
# scrolls in a few smooth steps instead. Text → Animation: slide or none.
# Only the text moves; the top and bottom lines stay.

# A page turn: how many frames, and how long each one is allowed. The frames
# are eased rather than evenly spaced -- a constant speed is what made it feel
# mechanical, and a smooth turn was asked for on 2026-09-30. The loop keeps to
# a clock, so on a slow
# terminal it drops frames instead of dragging the turn out.
TURN_FRAMES = 14
TURN_DELAY = 0.009          # 14 x 9 ms = about 125 ms end to end


def ease(t):
    """Smoothstep: eases in, then out, so the turn sets off and settles.

    Ease-out cubic was tried first and was wrong for so few frames: it was 73%
    across by frame 5 of 14 and the last four frames all landed on the same
    column, which reads as a stall at the end rather than a glide.
    """
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def paced(start, step, frames, last):
    """Sleep until this frame is due; say whether it is already too late to
    bother drawing it. The last frame is always drawn -- it is the new page."""
    due = start + TURN_DELAY * step
    now = time.monotonic()
    if now < due:
        time.sleep(due - now)
        return True
    return last or now < due + TURN_DELAY


def snapshot(app):
    scr = TextScreen(app.rows, app.cols)
    app.draw(scr)
    return scr


def draw_row(scr, y, chars, marks):
    start = 0
    for i in range(1, len(chars) + 1):
        if i == len(chars) or marks[i] != marks[start]:
            scr.put(y, start, "".join(chars[start:i]), marks[start])
            start = i


def slide(win, scr, app, before, after, direction):
    """The new page slides in from the side, eased."""
    y0, h, cols = app.body_top(), app.body_height(), app.cols
    start, was = time.monotonic(), -1
    for step in range(1, TURN_FRAMES + 1):
        last = step == TURN_FRAMES
        if not paced(start, step, TURN_FRAMES, last):
            continue
        off = min(cols, int(round(cols * ease(step / TURN_FRAMES))))
        if off == was and not last:
            continue                     # nothing would move: not a frame
        was = off
        scr.clear()
        for y in range(app.rows):
            if y0 <= y < y0 + h:
                if direction > 0:
                    chars = before.grid[y][off:] + after.grid[y][:off]
                    marks = before.marks[y][off:] + after.marks[y][:off]
                else:
                    chars = after.grid[y][cols - off:] + before.grid[y][:cols - off]
                    marks = after.marks[y][cols - off:] + before.marks[y][:cols - off]
            else:
                chars, marks = after.grid[y], after.marks[y]
            draw_row(scr, y, chars, marks)
        win.refresh()


def wipe(win, scr, app, before, after, direction):
    """The page moves up (or down) out of the way and the new one follows it
    in -- the way a terminal moves, for when a sideways slide is not wanted.
    Settings: Animation, scroll."""
    y0, h = app.body_top(), app.body_height()
    start, was = time.monotonic(), -1
    for step in range(1, TURN_FRAMES + 1):
        last = step == TURN_FRAMES
        if not paced(start, step, TURN_FRAMES, last):
            continue
        k = min(h, int(round(h * ease(step / TURN_FRAMES))))
        if k == was and not last:
            continue
        was = k
        scr.clear()
        for y in range(app.rows):
            if y0 <= y < y0 + h:
                n = y - y0
                if direction > 0:
                    src, row = (before, n + k) if n + k < h else (after, n + k - h)
                else:
                    src, row = (before, n - k) if n - k >= 0 else (after, n - k + h)
                chars, marks = src.grid[y0 + row], src.marks[y0 + row]
            else:
                chars, marks = after.grid[y], after.marks[y]
            draw_row(scr, y, chars, marks)
        win.refresh()


def smooth_scroll(win, scr, app, old_top, new_top):
    """Scrolling, line by line and eased, instead of jumping."""
    span = abs(new_top - old_top)
    frames = max(2, min(TURN_FRAMES, span))
    start = time.monotonic()
    for step in range(1, frames + 1):
        last = step == frames
        if not paced(start, step, frames, last):
            continue
        app.top = old_top + int(round((new_top - old_top) * ease(step / frames)))
        app.draw(scr)
        win.refresh()
    app.top = new_top


TURN_KEYS = {" ", "f", "b", "[", "]", "h", "l"}


def turns_page(app, k):
    ch = chr(k) if isinstance(k, int) and 0 <= k < 256 else ""
    return (app.book is not None and app.overlay is None and not app.playing_here
            and app.prefs.get("animation", "slide") in ("slide", "scroll")
            and (ch in TURN_KEYS or k in (curses.KEY_NPAGE, curses.KEY_PPAGE,
                                         curses.KEY_LEFT, curses.KEY_RIGHT)))


def reopen_window(app):
    """Line height lives in the terminal: open a new window on the same book
    and let this one close."""
    import shlex
    target = library.get(app.book.key).get("path", "") if app.book else ""
    card.save(PENDING, {"target": target, "at": time.time()})
    command = shlex.join(books.launch_command())
    subprocess.Popen(["sh", "-c", "sleep 1; exec " + command], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def cell_width():
    """Pixels per column, from the terminal (0 when it does not say)."""
    try:
        import fcntl
        import struct
        import termios
        rows, cols, xpx, _ypx = struct.unpack("HHHH", fcntl.ioctl(sys.stdout.fileno(),
                                                                 termios.TIOCGWINSZ, b"\0" * 8))
        return xpx / cols if cols and xpx else 0
    except (OSError, ValueError, ImportError):
        return 0


def run(win, target, pending):
    curses.curs_set(0)
    win.timeout(250)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | getattr(curses, "REPORT_MOUSE_POSITION", 0))
    rows, cols = win.getmaxyx()
    app = Reader(rows, cols)
    # The window closing (Super + W) ends this process with a hang-up, and a
    # stop ends it with a terminate: the place is saved first, either way.
    import signal
    # Speaking runs in its own process; the reader never waits for it, so let
    # the system clear them away rather than leaving dead children behind.
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    except (AttributeError, ValueError):
        pass

    def closing(_signum, _frame):
        try:
            app.save(force=True)
        finally:
            os._exit(0)
    signal.signal(signal.SIGHUP, closing)
    signal.signal(signal.SIGTERM, closing)
    app.terminal = books.default_terminal()
    # The size this window opened at, and its cell width then: zooming is
    # measured against these (note_zoom).
    app.base_size = app.prefs.get("fontSize") or books.foot_font_size()
    app.base_cell = cell_width()
    scr = CursesScreen(win)
    if pending:
        app.check_pending()
    elif target:
        if not app.open(target):
            app.open_last()
    else:
        app.open_last()
    while not app.quit:
        app.check_pending()
        app.poll()
        app.spoken_check()
        app.tick()
        app.draw(scr)
        win.refresh()
        win.timeout(app.pace())
        k = win.getch()
        if k == -1:
            app.save()
            continue
        if k == curses.KEY_RESIZE:
            app.rows, app.cols = win.getmaxyx()
            para = app.lines[app.top]["p"] if app.lines and app.top < len(app.lines) else 0
            app.relayout()
            app.top = app.line_of_para(para)
            continue
        if k == curses.KEY_MOUSE:
            try:
                _id, _x, _y, _z, bstate = curses.getmouse()
            except curses.error:
                continue
            if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
                app.key(curses.KEY_UP)
                app.key(curses.KEY_UP)
                app.key(curses.KEY_UP)
            elif bstate & getattr(curses, "BUTTON5_PRESSED", 0x200000):
                app.key(curses.KEY_DOWN)
                app.key(curses.KEY_DOWN)
                app.key(curses.KEY_DOWN)
            else:
                app.mouse(_y, _x, bstate)
            if app.dirty:
                app.draw(scr)
                win.refresh()
                app.dirty = False
            app.save()
            continue
        if turns_page(app, k):
            before, where = snapshot(app), (app.ci, app.top)
            app.key(k)
            if (app.ci, app.top) != where and app.overlay is None:
                forward = (app.ci, app.top) > where
                if app.ci != where[0] or app.prefs["layout"] == "pages":
                    move = wipe if app.prefs.get("animation") == "scroll" else slide
                    move(win, scr, app, before, snapshot(app), 1 if forward else -1)
                else:
                    smooth_scroll(win, scr, app, where[1], app.top)
        else:
            app.key(k)
        app.save()
        if app.reopen:
            app.save(force=True)
            reopen_window(app)
            return
    app.save(force=True)


def main(argv):
    locale.setlocale(locale.LC_ALL, "")
    args = list(argv)
    if "--render" in args:
        i = args.index("--render")
        size = args[i + 1]
        del args[i:i + 2]
        keys = ""
        if "--keys" in args:
            j = args.index("--keys")
            keys = args[j + 1]
            del args[j:j + 2]
        _app, scr = render(size, keys, args[0] if args else "")
        print(scr.text())
        return
    pending = "--pending" in args
    args = [a for a in args if a != "--pending"]
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(run, args[0] if args else "", pending)


if __name__ == "__main__":
    main(sys.argv[1:])
