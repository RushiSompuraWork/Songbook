"""Audiobooks: a folder in the music folder that the card opens as a book.

There is no separate place for books (his design, 2026-09-22): a book is a
folder like any other, and the card recognises it. For certain when it holds
an .m4b file (its chapters come from the file itself, read with ffprobe);
by a guess otherwise: a genre tag of Audiobook or Speech, or every file 10
minutes or longer (Settings: "Counts as a book from") and numbered. And
every folder inside the Book folders chosen in Settings is a book.

Every chapter remembers where it was left and how far into it he got, so
going ahead and coming back starts it where it stopped. A chapter counts as
heard after 30 s, 1 min or the whole of it (Settings → Library); a chapter
skipped over stays as it was, unless "gray out skipped chapters" is on.
"""

import json
import os
import re
import subprocess
import threading
import time

import card
import library

# Progress lives in library.py (one record per book, by what the book is);
# this file decides what a book is, its chapters, and plays them.
CHAPTERS = "chapters.json"  # ffprobe's chapters of an .m4b, by path and mtime
GENRES = ("audiobook", "audiobooks", "audio book", "speech", "spoken", "spoken word")
LOCK = threading.Lock()


def listen_of(files):
    """What was heard of this book: {"ch": {chapter: {at, far}}, "last"}."""
    return library.get(library.audio_id(files))["listen"] if files else {"ch": {}}


def duration(rec):
    try:
        return float(rec.get("duration") or rec.get("Time") or 0)
    except ValueError:
        return 0.0


def natural(name):
    """'2 - x' before '10 - x', as a person counts."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def files_of(m, folder):
    try:
        recs = [r for r in m.records("lsinfo", folder) if r["_type"] == "file"]
    except RuntimeError:
        # MPD indexes sound only, so a folder holding nothing but novels is
        # not in its database at all and asking for it is an error, not an
        # empty answer (his report, 2026-09-30: ~/Music/Book, one .epub).
        return []
    return sorted(recs, key=lambda r: natural(os.path.basename(r["file"])))


def guess(files):
    """Is this folder a book? Only when it says so itself: an .m4b, or an
    audiobook genre. Length is not a reason -- a long track is a long track,
    and a sixty-minute song is still a song (reported 2026-09-23). A folder of
    ordinary files is a book because it was chosen: Settings → Book folders."""
    if not files:
        return False
    if any(f["file"].lower().endswith(".m4b") for f in files):
        return True
    return any((f.get("Genre") or "").strip().lower() in GENRES for f in files)


def in_book_folders(folder):
    """Settings → Library → Audiobooks → Book folders: a chosen folder and
    every folder inside it open as books. What the card itself read aloud
    (Settings → Read aloud → the spoken folder) always counts, whatever the
    length of its chapters."""
    chosen = list(card.setting("bookFolders", []) or [])
    spoken = (card.setting("spokenFolder", "Spoken") or "Spoken").strip("/")
    if spoken:
        chosen.append(spoken)
    return any(folder == c or folder.startswith(c + "/") for c in chosen)


def is_book(folder, files, marks=None):
    """marks is no longer used (the per-folder "Open as a book" gave way to
    Book folders in Settings, 2026-09-22); kept so callers need not change."""
    if not files:
        return False
    return in_book_folders(folder) or guess(files)


def m4b_chapters(rel):
    """[{title, start, end}] from inside the file; [] when it has none."""
    full = os.path.join(card.MUSIC_DIR, rel)
    try:
        stamp = "%d:%d" % (os.path.getmtime(full), os.path.getsize(full))
    except OSError:
        return []
    cache = card.load(CHAPTERS, {})
    hit = cache.get(rel)
    if isinstance(hit, dict) and hit.get("stamp") == stamp:
        return hit["chapters"]
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", "--", full],
                           capture_output=True, text=True, timeout=30)
        raw = json.loads(r.stdout or "{}").get("chapters") or []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raw = []
    chapters = []
    for c in raw[:2000]:
        try:
            start, end = float(c["start_time"]), float(c["end_time"])
        except (KeyError, ValueError):
            continue
        title = " ".join(str((c.get("tags") or {}).get("title") or "").split())[:200]
        chapters.append({"title": title or "Chapter %d" % (len(chapters) + 1),
                         "start": start, "end": end})
    cache[rel] = {"stamp": stamp, "chapters": chapters}
    if len(cache) > 100:
        cache = dict(list(cache.items())[-100:])
    card.save(CHAPTERS, cache)
    return chapters


def clean_title(rec):
    if rec.get("Title"):
        return rec["Title"]
    base = os.path.splitext(os.path.basename(rec["file"]))[0]
    return " ".join(base.replace("_", " ").split())


def chapters(files):
    """One entry per chapter: {id, file, start, length, title}. A single
    .m4b with chapter marks is split by them; otherwise each file is one."""
    m4bs = [f for f in files if f["file"].lower().endswith(".m4b")]
    result = []
    if len(m4bs) == 1 and len(files) == 1:
        f = m4bs[0]
        for c in m4b_chapters(f["file"]):
            result.append({"id": library.chapter_key(f["file"], c["start"]), "file": f["file"],
                           "start": c["start"], "length": max(0.0, c["end"] - c["start"]),
                           "title": c["title"]})
    if not result:
        for f in files:
            result.append({"id": library.chapter_key(f["file"]), "file": f["file"], "start": 0.0,
                           "length": duration(f), "title": clean_title(f)})
    return result


# Within this of the end counts as the end: a chapter rarely runs to its last
# second before the next one starts, and nobody sits through the outro. Never
# more than a twentieth of a short chapter, so a two-minute one is not called
# finished six seconds in.
END_GRACE = 20


def is_finished(ch, far):
    """Has he reached the end of this chapter?

    It used to be "was he here for 30 or 60 seconds" (his design of
    2026-09-22, when nothing recorded how far he had got). A tick and a
    greyed-out row say *finished*, so a twelve-minute chapter sampled for a
    minute looked done -- and once the row began filling as well, the card
    contradicted itself: a tick beside an almost empty bar. The furthest
    point reached is written down now, so it can simply be asked (2026-09-25).
    """
    length = float(ch.get("length") or 0)
    if length <= 0:
        return False
    return far >= length - min(END_GRACE, length * 0.05)


def part_of(ch, far):
    """How far through the chapter, 0 to 1."""
    length = float(ch.get("length") or 0)
    return round(min(1.0, max(0.0, far) / length), 3) if length > 0 else 0


def book_view(folder, files, progress):
    chs = chapters(files)
    seen = progress.get("ch") or {}
    last = progress.get("last", "")
    far = [float((seen.get(c["id"]) or {}).get("far", 0) or 0) for c in chs]
    done = [is_finished(c, far[i]) for i, c in enumerate(chs)]
    touched = [far[i] > 0 for i in range(len(chs))]
    later = [any(touched[i + 1:]) for i in range(len(chs))]
    view = []
    for i, c in enumerate(chs):
        at = (seen.get(c["id"]) or {}).get("at", 0)
        # finished · started (part way through) · skipped (passed over, and
        # something after it was played) · new
        state_ = ("finished" if done[i] else "started" if touched[i]
                  else "skipped" if later[i] else "new")
        # A finished chapter fills the row completely: stopping twenty
        # seconds from the end is finished, and a row left at 99% would say
        # otherwise for ever.
        view.append(dict(c, at=at, state=state_,
                         part=1.0 if done[i] else part_of(c, far[i]),
                         current=c["id"] == last))
    return view


def summary(folder, files, progress):
    view = book_view(folder, files, progress)
    n = sum(1 for c in view if c["state"] == "finished")
    # How far through the whole book, counting seconds rather than chapters:
    # half of a long chapter is half of it, not nothing. Kept in the library,
    # so it is the same after a restart (his ask, 2026-09-25).
    seen = (progress.get("ch") or {})
    total = sum(max(0.0, float(c["length"] or 0)) for c in view)
    far = 0.0
    for c in view:
        got = float((seen.get(c["id"]) or {}).get("far", 0) or 0)
        far += min(got, float(c["length"] or 0) or got)
    return {"chapters": len(view), "finished": n,
            "part": round(min(1.0, far / total), 3) if total else 0}


# ---------------------------------------------------------------- commands

def cmd_book(folder):
    card.check_arg(folder)
    m = card.mpd()
    files = files_of(m, folder)
    m.close()
    card.out({"folder": folder, "title": os.path.basename(folder),
              "book": is_book(folder, files), "text": has_text(folder),
              "chapters": book_view(folder, files, listen_of(files)),
              "graySkipped": card.setting("bookGraySkipped", False) is True,
              "playing": library.playing_folder() == folder})


def pick(chs, progress, chapter_id):
    """Which chapter to start, and where in it."""
    seen = progress.get("ch") or {}
    ids = [c["id"] for c in chs]
    if chapter_id and chapter_id in ids:
        i = ids.index(chapter_id)
    elif progress.get("last") in ids:
        i = ids.index(progress["last"])
        at = (seen.get(ids[i]) or {}).get("at", 0)
        # Stopped at the very end: the next chapter, not the last seconds.
        if chs[i]["length"] and at >= chs[i]["length"] - 10 and i + 1 < len(chs):
            i += 1
    else:
        i = 0
    at = (seen.get(ids[i]) or {}).get("at", 0)
    if chs[i]["length"] and at >= chs[i]["length"] - 10:
        at = 0                     # finished before: from the top again
    return i, at


def cmd_play_book(folder, chapter_id=""):
    card.check_arg(folder)
    card.check_arg(chapter_id)
    card.new_fill_token()
    m = card.mpd()
    files = files_of(m, folder)
    if not files:
        m.close()
        card.fail("That book has no audio files")
    chs = chapters(files)
    book_id = library.audio_id(files)
    i, at = pick(chs, library.get(book_id)["listen"], chapter_id)
    with library.edit() as lib:
        lib["playing"] = folder
        lib["playing_id"] = None         # an ordinary book is its own folder
        rec = library.record(lib, book_id)
        rec.update(title=os.path.basename(folder), path=folder, kind="audio", t=time.time())
    ch = chs[i]
    m.raw("clear")
    m.raw("random", "0")           # chapters in order, whatever the music does
    for f in files:
        m.raw("add", f["file"])
    pos = [f["file"] for f in files].index(ch["file"])
    offset = ch["start"] + at
    m.raw("play", str(pos))
    if offset > 1:
        m.raw("seekcur", "%.1f" % offset)
    m.close()
    card.save("last.json", {"type": "book", "folder": folder, "title": os.path.basename(folder)})
    card.set_now("music")
    card.out({"ok": True, "chapter": ch["title"], "at": at})


def cmd_book_forget(folder, chapter_id):
    """A chapter back to not heard, from the start."""
    card.check_arg(folder)
    card.check_arg(chapter_id)
    m = card.mpd()
    files = files_of(m, folder)
    m.close()
    if files:
        with library.edit() as lib:
            library.record(lib, library.audio_id(files))["listen"]["ch"].pop(chapter_id, None)
    card.out({"ok": True})


def here(chs, path, elapsed):
    """The chapter playing now: the last one of this file that has started."""
    found = None
    for i, c in enumerate(chs):
        if c["file"] == path and c["start"] <= elapsed + 0.5:
            found = i
    return found


def now_playing(m, path, elapsed):
    """{"folder", "title", "chapter"} when the song playing is part of the book
    being read, else None. Used by card.status_view for the title and icon."""
    folder = playing_folder()
    if not folder or not path.startswith(folder + "/") or "/" in path[len(folder) + 1:]:
        return None
    info = {"folder": folder, "title": os.path.basename(folder), "chapter": "",
            "start": 0.0, "length": 0.0}
    if path.lower().endswith(".m4b"):
        chs = [dict(c, file=path) for c in chapters([{"file": path}])]
        i = here(chs, path, elapsed)
        if i is not None:
            # The card's time bar shows this chapter, not the whole file.
            info.update(chapter=chs[i]["title"], start=chs[i]["start"], length=chs[i]["length"])
    return info


def single_file(path, length, elapsed):
    """An .m4b playing on its own (not from a book folder): it plays
    book-style too, and its chapters are its own. Length alone no longer
    makes a book of anything."""
    if not path or card.is_stream(path):
        return None
    if not path.lower().endswith(".m4b"):
        return None
    m4b = True
    folder = os.path.dirname(path)
    info = {"folder": folder, "title": os.path.basename(folder) or os.path.basename(path),
            "chapter": "", "start": 0.0, "length": 0.0, "single": True}
    if m4b:
        chs = [dict(c, file=path) for c in chapters([{"file": path}])]
        i = here(chs, path, elapsed)
        if i is not None and len(chs) > 1:
            info.update(chapter=chs[i]["title"], start=chs[i]["start"], length=chs[i]["length"])
    return info


def cmd_book_step(direction):
    """Next or previous chapter of the book playing, where that chapter was
    left (as picking it from the list does). Works inside one .m4b too,
    where MPD's own next would skip the whole book."""
    step = 1 if direction in ("1", "+1", "next") else -1
    m = card.mpd()
    view = card.status_view(m)
    folder = playing_folder()
    path = (view.get("current") or {}).get("file", "")
    if not folder or not path.startswith(folder + "/"):
        if not path.lower().endswith(".m4b"):
            m.raw("next" if step > 0 else "previous")
            m.close()
            card.out({"ok": True})
            return
        folder = os.path.dirname(path)      # one .m4b on its own: its chapters
        files = [{"file": path}]
    else:
        files = files_of(m, folder)
    chs = chapters(files)
    if len(chs) <= 1:                       # no chapter marks: the next song
        m.raw("next" if step > 0 else "previous")
        m.close()
        card.out({"ok": True})
        return
    i = here(chs, path, float(view.get("elapsed") or 0))
    target = (i if i is not None else 0) + step
    if not 0 <= target < len(chs):
        m.close()
        card.out({"ok": True, "note": "That was the last chapter" if step > 0 else "First chapter"})
        return
    _, at = pick(chs, listen_of(files), chs[target]["id"])
    ch = chs[target]
    pos = [f["file"] for f in files].index(ch["file"])
    if ch["file"] != path:
        m.raw("play", str(pos))
    offset = ch["start"] + at
    if offset > 1 or ch["file"] == path:
        m.raw("seekcur", "%.1f" % offset)
    m.close()
    card.out({"ok": True, "chapter": ch["title"], "at": at})


# ---------------------------------------------------------------- listening
#
# The watcher (card.py watch) calls note() on pause and when the song
# changes, and a thread of it every 10 s while a book plays: where in which
# chapter, and the furthest point reached, which decides "heard".

def note(m, view):
    folder = library.playing_folder()
    if not folder:
        return
    cur = view.get("current") or {}
    path = cur.get("file", "")
    if not path.startswith(folder + "/") or "/" in path[len(folder) + 1:]:
        if path:                             # something else plays now
            library.set_playing(None)
        return
    elapsed = float(view.get("elapsed") or 0)
    files = files_of(m, folder)
    chs = chapters(files)
    i = here(chs, path, elapsed)
    if i is None:
        return
    ch = chs[i]
    at = int(max(0.0, elapsed - ch["start"]))
    # A novel read aloud is recorded as the novel, not as the Spoken folder:
    # one book, one record, one place he left off.
    book_id = library.playing_id() or library.audio_id(files)
    old_listen = library.get(book_id)["listen"]
    old = old_listen["ch"].get(ch["id"]) or {}
    new = {"at": at, "far": max(int(old.get("far", 0)), at)}
    if new == old and old_listen.get("last") == ch["id"]:
        return                               # nothing new: no write
    with library.edit() as lib:
        rec = library.record(lib, book_id)
        seen = rec["listen"]["ch"]
        prev = seen.get(ch["id"]) or {}
        seen[ch["id"]] = {"at": at, "far": max(int(prev.get("far", 0)), at)}
        rec["listen"]["last"] = ch["id"]
        if not library.playing_id():
            rec.update(title=os.path.basename(folder), path=folder, kind="audio",
                       t=time.time())
        else:
            rec["t"] = time.time()          # the novel keeps its own name and path
    tidy_spoken(folder, chs, i)


def tidy_spoken(folder, chs, playing):
    """Remove a chapter the card read aloud, once it has really been heard.

    Off unless he asks for it (Settings → Read aloud). Only chapters the card
    made itself, never a real audiobook; only ones behind the one playing, so
    nothing is taken out from under MPD; and only when heard by the same rule
    the card greys them out with. They can always be made again.
    """
    if not card.setting("spokenTidy", False):
        return
    spoken = (card.setting("spokenFolder", "Spoken") or "Spoken").strip("/")
    if not (folder == spoken or folder.startswith(spoken + "/")):
        return
    listen = library.get(library.playing_id() or "")["listen"]["ch"]
    for n, ch in enumerate(chs):
        if n >= playing:
            continue                     # this one, or still to come
        far = int((listen.get(ch["id"]) or {}).get("far", 0))
        if not is_finished(ch, far):
            continue          # only once he has really reached the end of it
        base = os.path.join(card.MUSIC_DIR, ch["file"])
        for path in (base, os.path.splitext(base)[0] + ".lrc"):
            try:
                os.remove(path)
            except OSError:
                pass


def playing_folder():
    return library.playing_folder()


# Where a book got to is written down by extras.note_loop, which does the
# same for long files: one tick of the watcher instead of two threads
# (2026-09-27). note() above is what it calls.


# ---------------------------------------------------------------- the reader

READER_APP = "songbook.reader"


def text_files(folder):
    """Books to read that sit directly in a folder: .epub and .txt files."""
    try:
        names = sorted(os.listdir(os.path.join(card.MUSIC_DIR, folder)))
    except OSError:
        return []
    return [n for n in names if n.lower().endswith((".epub", ".txt"))
            and os.path.isfile(os.path.join(card.MUSIC_DIR, folder, n))]


def holds_text(folder, depth=3):
    """Is there a novel (.epub or .txt) in this folder or just below it?

    MPD does not know a folder that has no sound in it, so the card has to
    look on disk to find one: without this a folder of novels appears
    nowhere -- not on the home page, and not in Settings -> Book folders,
    which is built from the same list (his report, 2026-09-30).
    """
    base = os.path.join(card.MUSIC_DIR, folder)
    try:
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if any(n.lower().endswith((".epub", ".txt")) for n in names):
                return True
            if root.count(os.sep) - base.count(os.sep) >= depth:
                dirs[:] = []
    except OSError:
        return False
    return False


def has_subfolders(folder):
    try:
        base = os.path.join(card.MUSIC_DIR, folder)
        return any(os.path.isdir(os.path.join(base, n)) and not n.startswith(".")
                   for n in os.listdir(base))
    except OSError:
        return False


def has_text(folder):
    """A book folder with something to read: a book file or timed text."""
    try:
        names = os.listdir(os.path.join(card.MUSIC_DIR, folder))
    except OSError:
        return False
    return any(n.lower().endswith((".epub", ".txt", ".lrc", ".srt", ".vtt")) for n in names)


def cmd_reader_open(target=""):
    """Open the reader (reader.py) on a book, or on the last one read.
    The target travels in a file, not on the command line: the launcher
    joins its words into one string, and book names have spaces."""
    target = card.check_arg(target)
    if target:
        full = os.path.realpath(os.path.join(card.MUSIC_DIR, os.path.expanduser(target)))
        inside = full.startswith(os.path.realpath(card.MUSIC_DIR) + os.sep)
        is_file = os.path.isfile(full) and full.lower().endswith((".epub", ".txt"))
        if not (is_file or (inside and os.path.isdir(full))):
            card.fail("Not a book: an .epub, a .txt, or a folder in the music folder")
    card.save("reader-open.json", {"target": target, "at": time.time()})
    try:
        subprocess.Popen(launch_command(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except FileNotFoundError:
        # Without the launcher the window simply never appeared, and the card
        # showed Python's own words about it (2026-09-24).
        card.fail("The reader needs Omarchy's launcher (omarchy-launch-or-focus), "
                  "which is not on this system")
    card.out({"ok": True})


FOOT_LINE = 1.32         # foot's own line height, as a share of the font size (JetBrains Mono)
LINE_HEIGHTS = (1.0, 1.2, 1.5, 1.7, 2.0)


def default_terminal():
    try:
        r = subprocess.run(["xdg-terminal-exec", "--print-id"], capture_output=True, text=True,
                           timeout=3)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def foot_font():
    """(family, size) from foot.ini's font=Name:size=9; ("monospace", 11) if unset."""
    try:
        with open(os.path.expanduser("~/.config/foot/foot.ini")) as f:
            for line in f:
                m = re.match(r"\s*font\s*=\s*([^:,\n]+)(?::size=([0-9.]+))?", line)
                if m:
                    return m.group(1).strip(), float(m.group(2) or 11)
    except (OSError, ValueError):
        pass
    return "monospace", 11.0


def foot_font_size():
    return foot_font()[1]


def launch_command():
    """How the reader's window opens. In foot (his terminal) it opens with the
    line height chosen in the reader (Text → Line height), which a terminal
    app cannot set from inside; any other terminal opens as Omarchy's TUIs do."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reader.py")
    import shlex
    prefs = card.load("reader.json", {}).get("prefs") or {}
    ratio = prefs.get("lineHeight", 1.2)
    ratio = ratio if ratio in LINE_HEIGHTS else 1.2
    if default_terminal() == "foot.desktop":
        family, size = foot_font()
        zoom = prefs.get("fontSize")
        extra = []
        # The zoom he left the reader at (reader.note_zoom), for every book.
        if isinstance(zoom, (int, float)) and not isinstance(zoom, bool) and 4 <= zoom <= 40:
            size = float(zoom)
            extra += ["-o", "main.font=%s:size=%g" % (family, size)]
        if ratio != 1.0:
            extra += ["-o", "main.line-height=%.1f" % (size * FOOT_LINE * ratio)]
        # One string, because the launcher runs it with eval: every part quoted.
        command = "uwsm-app -- " + shlex.join(["foot", "--app-id=" + READER_APP] + extra
                                              + ["-e", "python3", script, "--pending"])
        return ["omarchy-launch-or-focus", READER_APP, command]
    return ["omarchy-launch-or-focus-tui", "--app-id=" + READER_APP, "python3", script, "--pending"]


COMMANDS = {
    "book": cmd_book,
    "play-book": cmd_play_book,
    "book-forget": cmd_book_forget,
    "book-step": cmd_book_step,
    "reader-open": cmd_reader_open,
}
