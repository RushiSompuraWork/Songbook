"""The book library: every book once, known by what it is, not where it is.

Redesign of 2026-09-22. Before, the card (books.json) and the
reader (reader.json) each kept their own record of a book, both by path, so
moving a folder left the progress behind. Now:

  - a book's id comes from its content: an audiobook from its files' names
    and lengths ("a-…"), a novel from the file's bytes ("t-…"). Moving or
    renaming keeps it; the path is only where it was last seen;
  - one record per book in library.json: what was heard (per chapter, by the
    chapter file's name, never its folder), where reading stopped (chapter
    and word), bookmarks, and when it was last used;
  - every process (the watcher, the card, the reader) changes the file under
    one lock, so none of them overwrites what another just wrote.

The first load moves the old books.json and reader.json records over.
"""

import contextlib
import hashlib
import json
import os
import threading
import time

import card

LIBRARY = "library.json"
VERSION = 2
MAX_BOOKS = 500
TEXT_HASH_BYTES = 64 << 20


def empty():
    return {"version": VERSION, "books": {}, "ids": {}, "playing": None}


# ---------------------------------------------------------------- ids

def audio_id(files):
    """An audiobook: its files' names and whole-second lengths, in order."""
    parts = []
    for f in files:
        try:
            length = int(float(f.get("duration") or f.get("Time") or 0))
        except (TypeError, ValueError):
            length = 0
        parts.append("%s:%d" % (os.path.basename(f["file"]), length))
    return "a-" + hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]


def text_id(path, data=None, migrating=False):
    """A novel: its bytes (the first 64 MB), remembered by path and stamp so
    a big EPUB is read once, not each time the book is opened."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    stamp = "%d:%d" % (st.st_mtime, st.st_size)
    data = data if data is not None else load()
    hit = data["ids"].get(path)
    if isinstance(hit, dict) and hit.get("stamp") == stamp:
        return hit["id"]
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(TEXT_HASH_BYTES))
    book_id = "t-" + h.hexdigest()[:16]
    if migrating:
        data["ids"][path] = {"stamp": stamp, "id": book_id}   # saved with the rest
        return book_id
    with edit() as lib:
        lib["ids"][path] = {"stamp": stamp, "id": book_id}
        if len(lib["ids"]) > MAX_BOOKS * 2:
            lib["ids"] = dict(list(lib["ids"].items())[-MAX_BOOKS:])
    return book_id


def chapter_key(file, start=0.0):
    """A chapter's name in the record: the file's name (and, inside one .m4b,
    where the chapter starts). No folder: moving the book keeps it."""
    base = os.path.basename(file)
    return "%s#%d" % (base, int(start)) if start else base


# ---------------------------------------------------------------- the file

def load():
    """The library, read only. Moves the old records over the first time.

    A library that is already here is never rebuilt. Moving the old records
    over is for the one case it was written for: there is no library yet.
    Rebuilding on a version that does not match would have thrown away every
    book's place, every bookmark and every chapter heard the next time this
    number was raised, because books.json and reader.json have already given
    up their records by then (found 2026-09-24).
    """
    data = card.load(LIBRARY, None)
    if not isinstance(data, dict) or not isinstance(data.get("books"), dict):
        data = migrate()
    elif data.get("version") != VERSION:
        # From another version of the card: keep everything, fill in what
        # this one expects. A change that really needs the records rewritten
        # belongs here, as a step of its own, never as a rebuild.
        data["version"] = VERSION
    for key, value in empty().items():
        if not isinstance(data.get(key), type(value)) and value is not None:
            data[key] = value
    return data


_editing = threading.local()          # the edit this thread is already inside


@contextlib.contextmanager
def edit():
    """Read, change and write library.json under a file lock, so the watcher,
    the card and the reader never overwrite each other's changes.

    An edit inside an edit uses the one already open. flock is held per open
    file, not per process, so asking for it twice in one thread would wait
    for a lock that only this thread can release -- which is a hang, not a
    guard (found 2026-09-23: text_id remembers its answer, and it is called
    with the library open).
    """
    inner = getattr(_editing, "data", None)
    if inner is not None:
        yield inner
        return
    import fcntl
    os.makedirs(card.STATE_DIR, exist_ok=True)
    with open(card.state_path("library.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load()
        _editing.data = data
        try:
            yield data
        finally:
            _editing.data = None
        books = data["books"]
        if len(books) > MAX_BOOKS:
            data["books"] = dict(sorted(books.items(), key=lambda kv: kv[1].get("t", 0))[-MAX_BOOKS:])
        card.save(LIBRARY, data)


def record(data, book_id):
    """The record of one book (created empty when new)."""
    rec = data["books"].setdefault(book_id, {})
    rec.setdefault("listen", {})
    rec["listen"].setdefault("ch", {})
    rec.setdefault("read", {})
    rec.setdefault("bookmarks", [])
    return rec


def get(book_id):
    """A copy of one book's record, for reading."""
    data = load()
    return record(data, book_id) if book_id in data["books"] else record(empty(), book_id)


# ---------------------------------------------------------------- moving old records

def migrate():
    """books.json (the card's per-folder progress) and reader.json (the
    reader's places) into one library. A folder that has moved since is found
    again by its name among the books that exist now."""
    data = empty()
    if os.path.exists(card.state_path(LIBRARY)):
        # Something is there but could not be read. Moving the old records
        # over would write an emptier library on top of it; better to leave
        # the file alone and carry on with nothing this once.
        return data
    old = card.load("books.json", {})
    reader = card.load("reader.json", {})
    try:
        import books
        m = card.mpd()
    except Exception:
        m = None

    def files_at(folder):
        if m is None:
            return []
        try:
            return books.files_of(m, folder)
        except Exception:
            return []

    def find_moved(folder):
        """The same book somewhere else now: a folder of the same name."""
        name = os.path.basename(folder)
        for root in (card.setting("bookFolders", []) or []) + [""]:
            base = os.path.join(card.MUSIC_DIR, root)
            for dirpath, dirs, _names in os.walk(base):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                if os.path.basename(dirpath) == name:
                    return os.path.relpath(dirpath, card.MUSIC_DIR)
                if dirpath.count(os.sep) - base.count(os.sep) > 3:
                    dirs[:] = []
        return ""

    for folder, progress in (old.get("books") or {}).items():
        if not isinstance(progress, dict):
            continue
        where = folder
        files = files_at(folder)
        if not files:
            where = find_moved(folder)
            files = files_at(where) if where else []
        if not files:
            continue
        rec = record(data, audio_id(files))
        seen = rec["listen"]["ch"]
        for key, value in (progress.get("ch") or {}).items():
            file, _, start = key.partition("#")
            new = chapter_key(file, float(start or 0))
            if isinstance(value, dict) and value.get("far", 0) >= seen.get(new, {}).get("far", 0):
                seen[new] = {"at": int(value.get("at", 0)), "far": int(value.get("far", 0))}
        last = progress.get("last") or ""
        if last:
            file, _, start = last.partition("#")
            rec["listen"]["last"] = chapter_key(file, float(start or 0))
        rec.update(title=os.path.basename(where), path=where, kind="audio",
                   t=max(rec.get("t", 0), progress.get("t", 0)))
    playing = old.get("playing")
    if playing and files_at(playing):
        data["playing"] = playing

    last_old = reader.get("last")
    for key, place in (reader.get("books") or {}).items():
        if not isinstance(place, dict):
            continue
        book_id, path, kind = "", "", ""
        if key.startswith("file:"):
            path = key[5:]
            if not os.path.exists(path):
                # Moved: a book file of the same name in the Book folders.
                path = find_file(os.path.basename(path))
            if path:
                book_id, kind = text_id(path, data, migrating=True), "text"
        elif key.startswith("book:"):
            folder = key[5:]
            files = files_at(folder)
            if not files:
                folder = find_moved(folder)
                files = files_at(folder) if folder else []
            if files:
                book_id, path, kind = audio_id(files), folder, "audio"
        if not book_id:
            continue
        rec = record(data, book_id)
        rec.update(title=place.get("title") or rec.get("title") or os.path.basename(path),
                   path=path, kind=rec.get("kind") or kind, t=max(rec.get("t", 0), place.get("t", 0)))
        # The old place was a paragraph; the reader turns it into a word
        # the first time it opens the book (read["para"]).
        rec["read"].update(chapter=int(place.get("chapter", 0)), para=int(place.get("para", 0)),
                           done=list(place.get("read", [])))
        if key == last_old:
            reader["last"] = book_id
    if m is not None:
        m.close()
    card.save(LIBRARY, data)
    if isinstance(reader.get("last"), str) and not reader["last"].startswith(("a-", "t-")):
        reader.pop("last", None)
    reader.pop("books", None)                     # moved; prefs stay in reader.json
    card.save("reader.json", reader)
    return data


def find_file(name):
    for root in card.setting("bookFolders", []) or []:
        for dirpath, dirs, names in os.walk(os.path.join(card.MUSIC_DIR, root)):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if name in names:
                return os.path.join(dirpath, name)
    return ""


def touch(book_id, **fields):
    """Note that a book was used (and where it is now)."""
    with edit() as lib:
        rec = record(lib, book_id)
        rec.update(fields)
        rec["t"] = time.time()


# ---------------------------------------------------------------- reading time
#
# Minutes read or listened, by day and by book (weread's idea). A day counts
# for the streak at a minute or more, as their 有效阅读 rule does.

DAY_SECONDS = 60
KEEP_DAYS = 400


def add_time(book_id, seconds):
    """Add time spent on a book, to today and to that book."""
    seconds = int(max(0, min(seconds, 3600)))
    if seconds <= 0:
        return
    today = time.strftime("%Y-%m-%d")
    with edit() as lib:
        stats = lib.setdefault("stats", {})
        days = stats.setdefault("days", {})
        days[today] = int(days.get(today, 0)) + seconds
        if len(days) > KEEP_DAYS:
            stats["days"] = dict(sorted(days.items())[-KEEP_DAYS:])
        rec = record(lib, book_id)
        rec["seconds"] = int(rec.get("seconds", 0)) + seconds
        rec["t"] = time.time()


def reading_time(data=None):
    """{"today", "week", "streak"} in seconds and days."""
    data = data if data is not None else load()
    days = (data.get("stats") or {}).get("days") or {}
    today = time.strftime("%Y-%m-%d")
    week = 0
    for back in range(7):
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - back * 86400))
        week += int(days.get(day, 0))
    streak = 0
    for back in range(0, KEEP_DAYS):
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - back * 86400))
        if int(days.get(day, 0)) >= DAY_SECONDS:
            streak += 1
        elif back > 0 or int(days.get(day, 0)) > 0:
            break                       # today may still be empty and the streak stand
        else:
            continue
    return {"today": int(days.get(today, 0)), "week": week, "streak": streak}


# ---------------------------------------------------------------- bookmarks

MAX_MARKS = 300


def add_mark(book_id, mark):
    """A bookmark: {kind: read|listen, chapter, word|at, title, note, t}."""
    with edit() as lib:
        marks = record(lib, book_id)["bookmarks"]
        marks.append(dict(mark, t=time.time()))
        del marks[:-MAX_MARKS]


def drop_mark(book_id, at):
    with edit() as lib:
        marks = record(lib, book_id)["bookmarks"]
        if 0 <= at < len(marks):
            marks.pop(at)


def note_mark(book_id, at, note):
    with edit() as lib:
        marks = record(lib, book_id)["bookmarks"]
        if 0 <= at < len(marks):
            marks[at]["note"] = str(note)[:200]


def playing_folder():
    return load().get("playing") or ""


def playing_id():
    """Which book the playing folder belongs to, when it is not the folder's
    own: a novel's spoken chapters live in the Spoken folder but are the
    novel's, and its listening belongs in the novel's one record."""
    return load().get("playing_id") or ""


def set_playing(folder, book_id=""):
    with edit() as lib:
        lib["playing"] = folder or None
        lib["playing_id"] = book_id or None
