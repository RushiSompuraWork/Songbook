"""Reading a novel aloud: a chapter becomes an audiobook chapter.

Design decision, 2026-09-23. A chapter's sentences are spoken one
at a time by whichever engine he chose (voices.py), each sentence's audio is
measured, and the pieces are joined into one file with a timed text file
(.lrc) beside it, inside the music folder. That folder is then an ordinary
audiobook: the card plays it, rmpc sees it, the media keys and the sleep
timer work, and the reader lights up the sentence being spoken. The next
chapter is made while this one plays.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

import booktext
import card
import library
import voices

SPOKEN = "spoken.json"        # what is being made now, for the card and the reader
QUEUE = "speak-queue.json"    # the chapters he ticked, in the order they are made
RATE = "speak-rate.json"      # how fast this machine speaks, so "how long" is honest
DEFAULT_FOLDER = "Spoken"
BITRATE = "64k"
MAX_QUEUE = 500
WORDS_PER_SECOND = 2.5        # about 150 words a minute, which is reading-aloud pace
DEFAULT_RATE = 4.0            # speech seconds made per second of work (measured: Kokoro ~4.2)


def settings():
    return {"engine": card.setting("voiceEngine", "") or voices.chosen(),
            "voice": card.setting("voiceName", "") or "",
            "speed": float(card.setting("voiceSpeed", 100) or 100) / 100.0,
            "folder": card.setting("spokenFolder", DEFAULT_FOLDER) or DEFAULT_FOLDER,
            "ahead": int(card.setting("spokenAhead", 2) or 2)
            if card.setting("spokenAheadOn", False) else 0,
            "tidy": bool(card.setting("spokenTidy", False))}


# Control characters, and the marks that reorder text on screen: a title
# carrying one of these would name a file that reads as something other than
# it is. booktext.clean() already strips them from what is shown; a file name
# deserves the same (2026-09-24).
UNSAFE = re.compile(r"[/\\\x00-\x1f\x7f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def safe_name(text, limit=60):
    text = UNSAFE.sub(" ", str(text or ""))
    text = " ".join(text.split())[:limit].strip(" .")
    return text or "Book"


def book_folder(title, spoken_folder=DEFAULT_FOLDER):
    """Where a spoken book lives, inside the music folder so MPD sees it."""
    return "%s/%s" % (spoken_folder.strip("/"), safe_name(title))


def chapter_file(folder, index, title):
    return "%s/%03d %s.mp3" % (folder, index + 1, safe_name(title, 50))


def sentences_of(chapter):
    """A chapter's sentences, each with whether it starts a paragraph, so the
    spoken text reads like the book rather than one long block."""
    out = []
    for para in chapter["paras"]:
        first = True
        for sentence in para:
            text = " ".join(str(w) for w, _t in sentence).strip()
            if text:
                out.append((text, first))
                first = False
    return out


def lrc_time(seconds):
    return "[%02d:%05.2f]" % (int(seconds) // 60, seconds % 60)


def write_lrc(path, lines, words):
    """The timed text beside the audio: one line per sentence, an empty line
    where a paragraph starts, and each word's own time when the engine gave
    them (booktext reads all three)."""
    out = []
    for i, (start, text, para) in enumerate(lines):
        if para and i:
            out.append(lrc_time(start))      # an empty timed line: a new paragraph
        piece = lrc_time(start)
        if words and i < len(words) and words[i]:
            piece += "".join("<%02d:%05.2f>%s " % (int(t) // 60, t % 60, w) for w, t in words[i])
        else:
            piece += text
        out.append(piece.rstrip())
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


MADE_BY = ".spoken.json"       # which engine and voice made each chapter here


def made_by(folder):
    try:
        with open(os.path.join(card.MUSIC_DIR, folder, MADE_BY)) as f:
            marks = json.load(f)
        return marks if isinstance(marks, dict) else {}
    except (OSError, ValueError):
        return {}


def note_made(folder, name, opts, seconds=0):
    """Remember the voice a chapter was spoken with, so a chapter made with
    another voice (or the test hum) is spoken again rather than kept, and how
    long it came out, so listing a whole novel costs nothing."""
    marks = made_by(folder)
    marks[name] = {"engine": opts["engine"], "voice": opts["voice"],
                   "speed": round(opts["speed"], 2), "at": time.time(),
                   "seconds": int(seconds)}
    try:
        card.write_file(os.path.join(card.MUSIC_DIR, folder, MADE_BY), json.dumps(marks))
    except OSError:
        pass


def same_voice(folder, name, opts):
    mark = made_by(folder).get(os.path.basename(name))
    return bool(mark) and (mark.get("engine"), mark.get("voice"), mark.get("speed")) == \
        (opts["engine"], opts["voice"], round(opts["speed"], 2))


# ---------------------------------------------------------------- the queue
#
# His design, 2026-09-23: he ticks chapters in the card and they are made one
# at a time, in order. The queue is edited from three places at once (the
# card, the reader, and the job taking the next one), so it is only ever
# changed under a lock.


@contextlib.contextmanager
def queue_edit():
    import fcntl
    os.makedirs(card.STATE_DIR, exist_ok=True)
    with open(card.state_path("speak-queue.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        items = [i for i in card.load(QUEUE, []) if isinstance(i, dict) and i.get("path")]
        yield items
        card.save(QUEUE, items[:MAX_QUEUE])


def queue_of(path=""):
    """What is waiting: everything, or only this book's chapters."""
    items = [i for i in card.load(QUEUE, []) if isinstance(i, dict) and i.get("path")]
    return [i for i in items if not path or i["path"] == path]


def queue_add(path, chapters, title="", names=None, listen=False):
    """Add chapters, keeping the order he ticked them in and never twice.

    `names` maps a chapter number to its name, so what is waiting can be read
    as "Poetry And Calligraphy" rather than "Chapter 56" (2026-09-25)."""
    added = []
    names = names or {}
    with queue_edit() as items:
        here = {(i["path"], int(i.get("chapter", -1))) for i in items}
        for c in chapters:
            if (path, int(c)) in here or len(items) >= MAX_QUEUE:
                continue
            here.add((path, int(c)))
            items.append({"path": path, "chapter": int(c), "title": title,
                          "chapterTitle": str(names.get(int(c)) or "")[:120],
                          "listen": bool(listen) and not added,   # only the first
                          "at": time.time()})
            added.append(int(c))
    return added


def queue_drop(path, chapters=None):
    """Untick chapters (all of this book's, with no list)."""
    gone = []
    with queue_edit() as items:
        want = None if chapters is None else {int(c) for c in chapters}
        keep = []
        for i in items:
            if i["path"] == path and (want is None or int(i.get("chapter", -1)) in want):
                gone.append(int(i.get("chapter", -1)))
            else:
                keep.append(i)
        items[:] = keep
    return gone


def queue_take():
    """The next chapter to make, removed from the queue."""
    taken = None
    with queue_edit() as items:
        if items:
            taken = items.pop(0)
    return taken


def note_rate(speech_seconds, work_seconds):
    """How fast this machine really speaks, averaged over the last chapters,
    so "about 25 minutes of work" is a measurement and not a guess."""
    if speech_seconds <= 0 or work_seconds <= 0.5:
        return
    past = card.load(RATE, {})
    old = float(past.get("rate") or 0) or None
    now = speech_seconds / work_seconds
    card.save(RATE, {"rate": round(now if old is None else old * 0.7 + now * 0.3, 3),
                     "at": time.time()})


def rate():
    return float(card.load(RATE, {}).get("rate") or 0) or DEFAULT_RATE


def ready_file(title, index, chapter_title, opts):
    """The chapter's audio if it is really there and was made with the voice
    now chosen, else "". This is what "spoken already" means everywhere."""
    folder = book_folder(title, opts["folder"])
    rel = chapter_file(folder, index, chapter_title)
    full = os.path.join(card.MUSIC_DIR, rel)
    if os.path.exists(full) and os.path.getsize(full) > 1024 \
            and same_voice(folder, os.path.basename(rel), opts):
        return rel
    return ""


def books_finished(length, heard):
    """Did he reach the end of this spoken chapter? The same rule the card
    uses for an audiobook, since a spoken chapter is one."""
    import books
    return books.is_finished({"length": length}, float(heard.get("far") or 0))


def chapter_rows(path, book=None, opts=None):
    """Every chapter of a book with what the card and the reader both need:
    whether it is spoken, waiting, being made now or heard already, and how
    long it is (or would take). No ffprobe: the length is remembered when the
    chapter is made, so a 600-chapter novel is listed at once."""
    opts = opts or settings()
    book = book or booktext.open_book(path)
    folder = book_folder(book["title"], opts["folder"])
    marks = made_by(folder)
    running = busy()
    making = int(running.get("chapter", -1)) if running.get("path") == path else -1
    waiting = {int(i.get("chapter", -1)): n
               for n, i in enumerate(queue_of(path))}
    book_id = library.text_id(path)
    record = library.get(book_id)
    done = set(record["read"].get("done", []))
    heard_at = record["listen"]["ch"]          # how far into each spoken chapter
    speed = rate()
    rows = []
    for i, c in enumerate(book["chapters"]):
        words = int(c.get("words") or 0)
        speech = words / WORDS_PER_SECOND
        rel = ready_file(book["title"], i, c["title"], opts)
        mark = marks.get(os.path.basename(rel)) if rel else None
        if i == making:
            state = "making"
        elif rel:
            state = "ready"
        elif i in waiting:
            state = "queued"
        else:
            state = "new"
        # How far he has got into this chapter, when it has been read aloud:
        # `at` is where he stopped, `far` the furthest he reached. Shown as
        # the row filling up rather than as a number (his idea, 2026-09-25).
        place = library.chapter_key(rel) if rel else ""
        heard = heard_at.get(place) or {}
        length = int((mark or {}).get("seconds") or speech)
        rows.append({"index": i, "title": c["title"], "words": words,
                     "state": state, "finished": i in done, "file": rel,
                     "seconds": length,
                     "at": int(heard.get("at") or 0), "far": int(heard.get("far") or 0),
                     "part": 1.0 if (i in done or books_finished(length, heard))
                     else (round(min(1.0, (heard.get("far") or 0) / length), 3) if length else 0),
                     "work": int(speech / speed), "place": waiting.get(i, -1),
                     "voice": (mark or {}).get("voice", "")})
    return rows


def claim():
    """Only one chapter is spoken at a time. The job holds a lock for as long
    as it runs; a second one finds it taken and stops, instead of racing it
    (three ran at once on 2026-09-23 and the fan with them)."""
    import fcntl
    os.makedirs(card.STATE_DIR, exist_ok=True)
    holder = open(card.state_path("speak.lock"), "w")
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder.close()
        return None
    return holder                      # kept open while the job runs


def busy():
    """What is being spoken now, or {} when nothing is. A job that died (or
    was killed) leaves its progress behind saying "making"; the lock is what
    says whether anyone is really at work, and a missing lock file means
    nobody is."""
    now = card.load(SPOKEN, {})
    if now.get("state") not in ("starting", "making"):
        return {}
    import errno
    import fcntl
    try:
        holder = open(card.state_path("speak.lock"), "r+")
    except OSError:
        return {}                      # no lock file at all: nobody is speaking
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(holder, fcntl.LOCK_UN)
        return {}                      # it was free: the job is gone
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
            return now                 # someone holds it: really speaking
        return {}
    finally:
        holder.close()


def tell(text, body=""):
    """A desktop notification: making a chapter takes minutes, and he should
    not have to watch a screen to know it is done."""
    try:
        subprocess.Popen(["notify-send", "-a", "Music", "-u", "low", "-t", "6000",
                          "--", text[:120], body[:200]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def progress(**fields):
    card.save(SPOKEN, dict(fields, at=time.time()))


STOP = "speak.stop"          # its own file: the job rewrites the progress one


def stop_asked():
    return os.path.exists(card.state_path(STOP))


def clear_stop():
    try:
        os.remove(card.state_path(STOP))
    except OSError:
        pass


def wanted(token):
    """False once it was stopped. With no token (a direct call, or a test)
    there is nobody to stop it."""
    if stop_asked():
        return False
    if not token:
        return True
    now = card.load(SPOKEN, {})
    return now.get("token") == token


# How much speech goes in one piece when he is listening while it is made.
# Long enough that the joining is not the work, short enough that he waits
# well under a minute: at ~3.8x real time, 75 s of speech takes about 20 s.
PIECE_SECONDS = 75


def parts_folder(folder, rel):
    """Where the pieces of a chapter live while it is being made. A folder
    of its own under the book, so MPD does not count the pieces as chapters
    (it lists one level, and chapters come from the book's own folder)."""
    return "%s/parts/%s" % (folder, os.path.splitext(os.path.basename(rel))[0])


def make_chapter(path, index, token, book=None, piece_ready=None):
    """Speak one chapter of a book into the spoken folder. Returns its file.

    With `piece_ready`, the chapter is also written out in pieces as it goes
    and each one handed over the moment it is ready, so listening can start
    in about twenty seconds instead of waiting two and a half minutes for the
    whole chapter (his choice, 2026-09-25). The pieces all get the gain
    measured from the first, so the volume does not step between them."""
    opts = settings()
    if not opts["engine"]:
        raise RuntimeError("No voice engine yet: Settings → Read aloud")
    book = book or booktext.open_book(path)
    if not 0 <= index < len(book["chapters"]):
        raise RuntimeError("No such chapter")
    chapter = dict(book["chapters"][index])
    chapter["paras"] = booktext.chapter_paras(path, index)   # only this one
    folder = book_folder(book["title"], opts["folder"])
    rel = chapter_file(folder, index, chapter["title"])
    full = os.path.join(card.MUSIC_DIR, rel)
    if ready_file(book["title"], index, chapter["title"], opts):
        return rel                       # made earlier, with this voice
    os.makedirs(os.path.dirname(full), exist_ok=True)
    lines = sentences_of(chapter)
    if not lines:
        raise RuntimeError("That chapter has no words")
    engine = voices.engine(opts["engine"])
    work = full + ".parts"
    os.makedirs(work, exist_ok=True)
    pieces, times, word_times, at = [], [], [], 0.0
    # Pieces for listening to while it is made: where they go, how far the
    # last one reached, and the gain they all share.
    listen_rel = parts_folder(folder, rel)
    listen_dir = os.path.join(card.MUSIC_DIR, listen_rel)
    piece_at, piece_from, piece_no, gain = 0.0, 0, 0, None
    if piece_ready:
        shutil.rmtree(listen_dir, ignore_errors=True)
        os.makedirs(listen_dir, exist_ok=True)

    def hand_over(last=False):
        """Join the sentences made since the last piece and hand it over."""
        nonlocal piece_at, piece_from, piece_no, gain
        if not piece_ready or piece_from >= len(pieces):
            return
        if not last and at - piece_at < PIECE_SECONDS:
            return
        piece_no += 1
        name = "%03d.mp3" % piece_no
        # Written under another name and renamed when it is whole. ffmpeg
        # fills a file as it goes, and MPD told about a half-written piece
        # gives up on it and moves to the next -- which is a piece of the
        # book skipped, heard as a jump (found 2026-09-27).
        # Built in the working folder and moved in when whole: ffmpeg works
        # out the format from the name, so the half-built one has to end in
        # .mp3 too, and it must not sit where MPD is about to look.
        making = os.path.join(work, "piece-%s" % name)
        gain = join(pieces[piece_from:], making, gain)
        os.replace(making, os.path.join(listen_dir, name))
        piece_ready(listen_rel, name, at - piece_at, piece_no)
        piece_at, piece_from = at, len(pieces)

    try:
        # In batches, because an engine that loads a model (Kokoro: about
        # five seconds) must not be started once per sentence.
        size = max(1, voices.batch_size(opts["engine"]))
        for start in range(0, len(lines), size):
            if not wanted(token):
                raise RuntimeError("stopped")
            chunk = lines[start:start + size]
            outs = [os.path.join(work, "%04d%s" % (start + i, engine["ext"]))
                    for i in range(len(chunk))]
            said = voices.speak_many(opts["engine"], [t for t, _p in chunk], outs,
                                     opts["voice"], opts["speed"])
            for i, (text, para) in enumerate(chunk):
                seconds = voices.audio_seconds(outs[i])
                pieces.append(outs[i])
                times.append((at, text, para))
                words = said[i] if i < len(said) else []
                word_times.append([[w, round(at + t, 2)] for w, t in words] if words else [])
                at += seconds
            done = min(len(lines), start + size)
            progress(state="making", token=token, book=book["title"], path=path, chapter=index,
                     title=chapter["title"], done=done, total=len(lines),
                     percent=round(100 * done / len(lines)), seconds=round(at))
            hand_over()
        hand_over(last=True)
        join(pieces, full, gain)
        write_lrc(os.path.splitext(full)[0] + ".lrc", times, word_times)
        note_made(folder, os.path.basename(rel), opts, seconds=at)
    finally:
        for p in pieces:
            try:
                os.remove(p)
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)   # rmdir leaves it if anything is in it
    rescan(folder)
    return rel


# A spoken chapter should be as loud as his music, not ten decibels under it.
# Measured 2026-09-25: his songs average -15 dB, a chapter as the engine makes
# it -26 dB, which is why a book sounded quiet after a song. Engines differ
# too (PyTorch Kokoro ran 3 dB under the ONNX one), so the gain is worked out
# for each chapter rather than fixed, and a limiter catches the peaks.
LOUDNESS = -16.0          # dB mean: beside his music, and the usual mark for speech


def loudness_of(listing):
    """The mean level of the joined sentences, writing nothing."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "concat", "-safe", "0",
                        "-i", listing, "-af", "volumedetect", "-f", "null", "/dev/null"],
                       capture_output=True, text=True, timeout=600)
    found = re.search(r"mean_volume: (-?\d+(?:\.\d+)?) dB", r.stderr or "")
    return float(found.group(1)) if found else None


def join(pieces, out, gain=None):
    """One file from the sentences, as MP3 (small, and MPD plays it), levelled
    to sit beside his music.

    `gain` is measured when it is not given, and returned, so the pieces of a
    chapter being listened to as it is made all get the same one -- measuring
    each separately would step the volume between them."""
    listing = out + ".list"
    with open(listing, "w") as f:
        for p in pieces:
            f.write("file '%s'\n" % p.replace("'", "'\\''"))
    try:
        try:
            if gain is None:
                mean = loudness_of(listing)
                gain = max(0.0, min(24.0, LOUDNESS - mean)) if mean is not None else 0.0
            # level=false, or the limiter puts the gain straight back on and
            # the peak sits at full scale whatever the limit says.
            level = (["-af", "volume=%.1fdB,alimiter=limit=0.82:level=false" % gain]
                     if gain > 0.5 else [])
            r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing]
                               + level
                               + ["-c:a", "libmp3lame", "-b:a", BITRATE, "-ac", "1", out],
                               capture_output=True, text=True, timeout=600)
        except FileNotFoundError:
            raise card.missing("ffmpeg", "It joins the spoken sentences into a chapter: "
                                         "sudo pacman -S ffmpeg") from None
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError((r.stderr or "ffmpeg failed").strip().splitlines()[-1][:200])
        return gain
    finally:
        try:
            os.remove(listing)
        except OSError:
            pass


def rescan(folder):
    """Let MPD see the new chapter, so it can be played straight away."""
    try:
        m = card.Mpd()           # not card.mpd(): that one prints and exits
        m.raw("update", folder)
        for _ in range(60):
            if "updating_db" not in m.dict("status"):
                break
            time.sleep(0.5)
        m.close()
    except (Exception, SystemExit):
        pass                 # no MPD right now: it will see the file next time


# ---------------------------------------------------------------- commands

def cmd_voices():
    """Which engines are here, and the chosen one's voices. The test hum is
    left out unless it is the one chosen: it is not a voice."""
    now = card.setting("voiceEngine", "") or voices.chosen()
    engines = [e for e in voices.ready_engines() if not e["hidden"] or e["id"] == now]
    card.out({"engines": engines, "engine": now, "voices": voices.voices_of(now)[:300],
              "voice": card.setting("voiceName", "") or "",
              "speed": card.setting("voiceSpeed", 100)})


def book_arg(path):
    """A book to read aloud, said the same way everywhere."""
    card.check_arg(path)
    full = os.path.expanduser(path)
    if not os.path.isabs(full):
        full = os.path.join(card.MUSIC_DIR, path)
    if not (os.path.isfile(full) and full.lower().endswith((".epub", ".txt"))):
        card.fail("Not a book to read aloud")
    return full


def chapter_arg(chapters):
    """"58" or "58,59,60" -- what the ticked boxes send."""
    want = []
    for part in str(chapters).split(","):
        part = part.strip()
        if not part.isdigit():
            card.fail("Bad chapter")
        want.append(int(part))
    if not want:
        card.fail("No chapters")
    return want[:MAX_QUEUE]


def start_job():
    """Wake the worker if nobody is at it. Safe to call whenever the queue
    grows: a job already running takes the new chapters itself, and the lock
    turns away a second one."""
    # Asking for something new cancels an earlier stop, whether or not a job
    # is still winding down. Clearing it only when nothing was running meant
    # a request made just after a Stop was quietly killed by that stop: the
    # chapter joined the queue, the job that picked it up saw the flag and
    # gave up, and the card said "done" having made nothing (2026-09-27).
    clear_stop()
    if busy():
        return ""
    token = "%d-%d" % (os.getpid(), time.time())
    progress(state="starting", token=token, waiting=len(queue_of()))
    subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "card.py"), "speak-run", token],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return token


def cmd_speak(path, chapters="0", listen=""):
    """Tick chapters to be read aloud. They join the queue and are made one
    at a time; chapters already spoken with this voice are left alone.

    With `listen`, the first of them starts playing as its first piece is
    ready -- about twenty seconds -- rather than when the whole chapter is
    made (his choice, 2026-09-25)."""
    full = book_arg(path)
    want = chapter_arg(chapters)
    opts = settings()
    if not opts["engine"]:
        card.fail("No voice engine yet: Settings → Read aloud")
    try:
        book = booktext.open_book(full)
    except Exception as e:
        card.fail("Could not open that book: %s" % str(e)[:120])
    want = [c for c in want if 0 <= c < len(book["chapters"])]
    if not want:
        card.fail("No such chapter")
    # Chapters already spoken are not made again, but they are still an
    # answer: the card plays them straight away.
    ready = [c for c in want
             if ready_file(book["title"], c, book["chapters"][c]["title"], opts)]
    running = busy()
    mine = int(running.get("chapter", -1)) if running.get("path") == full else -1
    todo = [c for c in want if c not in ready and c != mine]
    card.check_arg(listen)
    added = queue_add(full, todo, book["title"],
                      {c: book["chapters"][c]["title"] for c in todo},
                      listen=listen in ("listen", "1", "true", "yes"))
    token = start_job() if added or todo else ""
    speech = sum(int(book["chapters"][c].get("words") or 0)
                 for c in added) / WORDS_PER_SECOND
    card.out({"ok": True, "token": token, "added": added, "ready": ready,
              "waiting": len(queue_of()), "work": int(speech / rate()),
              "folder": book_folder(book["title"], opts["folder"])})


def cmd_speak_drop(path, chapters=""):
    """Untick chapters: take them out of the queue again."""
    full = book_arg(path)
    want = chapter_arg(chapters) if chapters else None
    card.out({"ok": True, "dropped": queue_drop(full, want),
              "waiting": len(queue_of())})


def cmd_speak_run(token):
    """The work: chapter after chapter out of the queue, until it is empty."""
    card.check_arg(token)
    holder = claim()
    if holder is None:
        return                         # another job is already at it
    try:
        run_queue(token)
    finally:
        holder.close()


def carry_over(path, rel, listener):
    """Move where he got to from the pieces onto the finished chapter.

    While it was being made he was listening to 001.mp3, 002.mp3 and so on,
    and the watcher wrote his place against those. The chapter is one file
    now, so the same place has to be said in its terms, or coming back to it
    would start from the beginning (2026-09-25)."""
    if not listener.seconds:
        return
    book_id = library.text_id(path)
    keys = ["%03d.mp3" % (n + 1) for n in range(len(listener.seconds))]
    with library.edit() as lib:
        seen = library.record(lib, book_id)["listen"]["ch"]
        reached = 0.0
        before = 0.0
        for n, key in enumerate(keys):
            got = seen.pop(key, None)          # the piece's own record goes
            if got:
                far = float(got.get("far") or 0)
                reached = max(reached, before + min(far, listener.seconds[n]))
            before += listener.seconds[n]
        if reached <= 0:
            return
        chapter_key = library.chapter_key(rel)
        was = seen.get(chapter_key) or {}
        seen[chapter_key] = {"at": int(reached),
                             "far": int(max(float(was.get("far") or 0), reached))}
        lib["books"][book_id]["listen"]["last"] = chapter_key


def sweep_parts(folder):
    """Clear the pieces of chapters that are finished and not being played."""
    root = os.path.join(card.MUSIC_DIR, folder, "parts")
    if not os.path.isdir(root):
        return
    playing = ""
    try:
        m = card.Mpd()
        playing = (card.status_view(m).get("current") or {}).get("file", "")
        m.close()
    except Exception:
        pass
    for name in os.listdir(root):
        where = "%s/parts/%s" % (folder, name)
        if playing.startswith(where + "/"):
            continue                          # he is listening to these
        chapter = os.path.join(card.MUSIC_DIR, folder, name + ".mp3")
        if os.path.exists(chapter):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
    try:
        os.rmdir(root)
        rescan(folder)
    except OSError:
        pass


class Listener:
    """Plays the pieces of a chapter as they are made.

    The first one clears the queue and starts; the rest are added behind it.
    It stops adding if he has gone off to play something else -- the chapter
    is still finished and written, he simply is not listening to it."""

    def __init__(self, book_folder="", book_id=""):
        self.book = book_folder    # MPD is told to look here, not at the new
        self.book_id = book_id     # folder, which it does not know yet
        self.folder = ""
        self.playing = False
        self.gave_up = False
        self.trouble = ""
        self.seconds = []          # how long each piece is, in order

    def piece(self, rel_folder, name, seconds, number):
        self.folder = rel_folder
        self.seconds.append(seconds)
        if self.gave_up:
            return
        try:
            m = card.Mpd()
        except Exception as e:
            self.trouble = "no MPD: %s" % str(e)[:60]
            return
        try:
            # The parent, not the piece's own folder: MPD refuses to update a
            # directory it has never seen ("No such directory"), and the
            # pieces live in one it has only just been given (2026-09-27).
            m.raw("update", self.book or rel_folder)
            for _ in range(120):
                if "updating_db" not in m.dict("status"):
                    break
                time.sleep(0.05)
            where = rel_folder + "/" + name
            if not self.playing:
                card.new_fill_token()
                m.raw("clear")
                m.raw("random", "0")
                m.raw("add", where)
                m.raw("play", "0")
                self.playing = True
                card.set_now("music")
                # So the watcher writes down where he gets to. Without this
                # it records nothing at all while the pieces play, and
                # stopping half way through loses the place (2026-09-27).
                library.set_playing(rel_folder, self.book_id)
            else:
                # Still on this chapter? If he has moved on, leave him be.
                view = card.status_view(m)
                now = (view.get("current") or {}).get("file", "")
                if not now.startswith(rel_folder + "/"):
                    self.gave_up = True
                    return
                m.raw("add", where)
        except Exception as e:
            # Said, not swallowed: a piece that cannot be played silently is
            # exactly what this feature must not do.
            self.trouble = "%s: %s" % (type(e).__name__, str(e)[:80])
        finally:
            m.close()


def run_queue(token):
    """One chapter at a time, in the order he ticked them. A chapter that
    cannot be made is reported and the rest carry on: one bad chapter in a
    queue of twenty must not throw the other nineteen away."""
    made, failed, why = 0, 0, "nothing left to make"
    while True:
        if not wanted(token):
            why = "stopped" if stop_asked() else "another job took over"
            break
        item = queue_take()
        if item is None:
            break
        path, index = item["path"], int(item.get("chapter", -1))
        try:
            book = booktext.open_book(path)
        except Exception as e:
            failed += 1
            progress(state="error", token=token, path=path, chapter=index,
                     book=item.get("title", ""), message=str(e)[:200],
                     waiting=len(queue_of()))
            continue
        if not 0 <= index < len(book["chapters"]):
            continue
        title = book["chapters"][index]["title"]
        folder = book_folder(book["title"], settings()["folder"])
        sweep_parts(folder)          # pieces of chapters already finished
        progress(state="making", token=token, book=book["title"], path=path, chapter=index,
                 title=title, done=0, total=0, percent=0, folder=folder,
                 waiting=len(queue_of()))
        started = time.time()
        listener = (Listener(folder, library.text_id(path))
                    if item.get("listen") else None)
        try:
            rel = make_chapter(path, index, token, book=book,
                               piece_ready=listener.piece if listener else None)
        except Exception as e:
            if str(e) == "stopped":
                why = "stopped part way through"
                break
            failed += 1
            progress(state="error", token=token, book=book["title"], path=path,
                     chapter=index, title=title, message=str(e)[:200],
                     waiting=len(queue_of()))
            tell("Could not read it aloud", "%s · %s" % (book["title"][:50], str(e)[:90]))
            continue
        made += 1
        if listener:
            carry_over(path, rel, listener)
        seconds = (made_by(folder).get(os.path.basename(rel)) or {}).get("seconds") or 0
        note_rate(seconds, time.time() - started)
        left = len(queue_of())
        progress(state="ready", token=token, book=book["title"], path=path, chapter=index,
                 folder=folder, file=rel, percent=100, title=title,
                 total=len(book["chapters"]), waiting=left)
        tell("Ready to read aloud",
             "%s · %s%s" % (book["title"][:50], title[:50],
                            "  (%d more waiting)" % left if left else ""))
    # Why it ended, not only that it did: "done" having made nothing was
    # impossible to tell apart from "done" having finished the queue
    # (2026-09-27).
    progress(state="done", token=token, made=made, failed=failed, why=why,
             waiting=len(queue_of()))


def spoken_chapters(book, opts=None):
    """The chapters of this book that really are spoken, in order, as
    (index, file). One listing of the folder, not one check per chapter: a
    535-chapter novel is asked about every time the reader draws."""
    opts = opts or settings()
    folder = book_folder(book["title"], opts["folder"])
    marks = made_by(folder)
    want = (opts["engine"], opts["voice"], round(opts["speed"], 2))
    out = []
    for i, c in enumerate(book["chapters"]):
        rel = chapter_file(folder, i, c["title"])
        mark = marks.get(os.path.basename(rel))
        if not mark or (mark.get("engine"), mark.get("voice"),
                        round(float(mark.get("speed") or 1), 2)) != want:
            continue
        full = os.path.join(card.MUSIC_DIR, rel)
        if os.path.exists(full) and os.path.getsize(full) > 1024:
            out.append((i, rel))
    return out


def keep_ahead(path, index, book=None, opts=None):
    """Tick the next few chapters, so listening does not run out.

    Off unless he asks for it (his choice, 2026-09-25: a toggle, off by
    default, with the number his to set). Chapters already spoken or already
    waiting are not asked for again."""
    opts = opts or settings()
    ahead = int(opts.get("ahead") or 0)
    if ahead <= 0:
        return []
    try:
        book = book or booktext.open_book(path)
    except Exception:
        return []
    ready = {i for i, _rel in spoken_chapters(book, opts)}
    waiting = {int(i.get("chapter", -1)) for i in queue_of(path)}
    want = [i for i in range(index + 1, min(index + 1 + ahead, len(book["chapters"])))
            if i not in ready and i not in waiting]
    if not want:
        return []
    added = queue_add(path, want, book["title"],
                      {c: book["chapters"][c]["title"] for c in want})
    if added:
        start_job()
    return added


def cmd_play_spoken(path, chapter="0"):
    """Play a novel's spoken chapter -- as the novel, not as a folder.

    His design, 2026-09-23: reading a chapter aloud used to switch the reader
    to the Spoken folder, which holds only the chapters made so far. The book
    lost its other 500 chapters and its progress split into two records. The
    audio belongs to the novel, so it is played under the novel's own name
    and its own place is what is written down.
    """
    full = book_arg(path)
    card.check_arg(chapter)
    if not chapter.isdigit():
        card.fail("Bad chapter")
    index = int(chapter)
    try:
        book = booktext.open_book(full)
    except Exception as e:
        card.fail("Could not open that book: %s" % str(e)[:120])
    opts = settings()
    ready = spoken_chapters(book, opts)
    here = [i for i, _rel in ready]
    if index not in here:
        card.fail("That chapter has not been read aloud yet")
    folder = book_folder(book["title"], opts["folder"])
    book_id = library.text_id(full)
    rel = dict(ready)[index]
    at = float(library.get(book_id)["listen"]["ch"]
               .get(library.chapter_key(rel), {}).get("at", 0) or 0)
    card.new_fill_token()
    m = card.mpd()
    m.raw("clear")
    m.raw("random", "0")             # a book is read in order, whatever music does
    for _i, f in ready:
        m.raw("add", f)
    m.raw("play", str(here.index(index)))
    if at > 1:
        m.raw("seekcur", "%.1f" % at)
    m.close()
    with library.edit() as lib:
        lib["playing"] = folder
        lib["playing_id"] = book_id      # so listening is recorded as the novel
        rec = library.record(lib, book_id)
        rec.update(title=book["title"], path=full, kind="text", t=time.time())
        rec["read"]["chapter"] = index
    card.save("last.json", {"type": "book", "folder": folder, "title": book["title"]})
    card.set_now("music")
    ahead = keep_ahead(full, index, book=book, opts=opts)
    card.out({"ok": True, "chapter": book["chapters"][index]["title"], "at": at,
              "file": rel, "folder": folder, "ahead": ahead})


def cmd_speak_status(path=""):
    """What is being made, and what is waiting behind it. Both the card and
    the reader read this one answer, so they never disagree."""
    if path:
        card.check_arg(path)
    now = dict(card.load(SPOKEN, {}))
    if now.get("state") in ("starting", "making") and not busy():
        now = {"state": "idle"}        # its job is gone: do not show a stale bar
    waiting = queue_of()
    speed = rate()
    now["queue"] = [{"chapter": int(i.get("chapter", -1)), "title": i.get("title", ""),
                     "path": i.get("path", "")} for i in waiting[:100]]
    now["waiting"] = len(waiting)
    if path:
        full = os.path.expanduser(path)
        if not os.path.isabs(full):
            full = os.path.join(card.MUSIC_DIR, path)
        now["mine"] = [int(i.get("chapter", -1)) for i in waiting if i.get("path") == full]
    now["rate"] = round(speed, 2)
    card.out(now)


def cmd_speak_stop():
    """Stop means stop: the chapter being made now, and everything ticked
    behind it. Half a queue left running would be a nasty surprise."""
    os.makedirs(card.STATE_DIR, exist_ok=True)
    open(card.state_path(STOP), "w").close()
    dropped = 0
    with queue_edit() as items:
        dropped = len(items)
        items[:] = []
    now = card.load(SPOKEN, {})
    now["state"] = "stop"
    now["waiting"] = 0
    card.save(SPOKEN, now)
    card.out({"ok": True, "dropped": dropped})


def cmd_book_chapters(path, want="0"):
    """Every chapter of a book, with whether it is spoken, waiting, being
    made or heard -- what the card's tick list and the reader's chapter panel
    both draw. `want` is how many chapters the caller can show at once, so a
    600-chapter novel does not have to be sent whole."""
    full = book_arg(path)
    card.check_arg(want)
    try:
        book = booktext.open_book(full)
    except Exception as e:
        card.fail("Could not open that book: %s" % str(e)[:120])
    rows = chapter_rows(full, book=book)
    speed = rate()
    waiting = [r for r in rows if r["state"] == "queued"]
    card.out({"ok": True, "title": book["title"], "path": full,
              "chapters": rows, "total": len(rows),
              "ready": sum(1 for r in rows if r["state"] == "ready"),
              "waiting": len(waiting),
              "work": int(sum(r["words"] for r in waiting) / WORDS_PER_SECOND / speed),
              "voice": settings()["voice"], "engine": settings()["engine"],
              "folder": book_folder(book["title"], settings()["folder"])})


def cmd_voice_install(engine_id):
    """Put a voice engine in place, when that needs no root (Kokoro)."""
    card.check_arg(engine_id)
    if engine_id != "kokoro":
        card.fail("That one is installed with its own command (Settings → Read aloud)")
    # Pressing it twice must not start a second one: two pips in one venv is
    # how a half-installed voice happens.
    import jobs
    now = card.load(voices.INSTALL, {})
    if now.get("state") == "working" and jobs.fresh(now, 600):
        card.fail("It is already installing · %d%%" % int(now.get("percent") or 0))
    enough, why = voices.room_for(voices.ROOM_NEEDED)
    if not enough:
        card.fail(why)
    card.save(voices.INSTALL, {"state": "working", "engine": engine_id, "at": time.time(),
                               "percent": 0, "note": "Starting…"})
    subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "card.py"), "voice-install-run", engine_id],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    card.out({"ok": True})


def cmd_voice_install_run(engine_id):
    if engine_id == "kokoro":
        voices.install_kokoro()


def cmd_voice_install_status():
    card.out(card.load(voices.INSTALL, {}))


def cmd_spoken_relevel(which=""):
    """Bring chapters made before the levelling up to the same loudness.

    They were made at whatever the engine gave, about ten decibels under his
    music. Remaking them would cost minutes each; this is one pass of ffmpeg
    over the audio that is already there (2026-09-25)."""
    card.check_arg(which)
    opts = settings()
    root = os.path.join(card.MUSIC_DIR, opts["folder"].strip("/"))
    running = busy()
    done, left = [], []
    for folder, dirs, names in os.walk(root) if os.path.isdir(root) else []:
        dirs[:] = [d for d in dirs if not d.endswith(".parts")]
        rel = os.path.relpath(folder, card.MUSIC_DIR)
        if which and rel != which:
            continue
        for name in sorted(names):
            if not name.lower().endswith(".mp3"):
                continue
            full = os.path.join(folder, name)
            if running.get("folder") == rel:
                left.append(name)
                continue                     # being made, or being played from
            mean = mean_level(full)
            if mean is None or mean >= LOUDNESS - 1.0:
                continue                     # already where it should be
            gain = max(0.0, min(24.0, LOUDNESS - mean))
            tmp = full + ".level.mp3"
            try:
                r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", full,
                                    "-af", "volume=%.1fdB,alimiter=limit=0.82:level=false" % gain,
                                    "-c:a", "libmp3lame", "-b:a", BITRATE, "-ac", "1", tmp],
                                   capture_output=True, text=True, timeout=600)
                if r.returncode == 0 and os.path.getsize(tmp) > 1024:
                    os.replace(tmp, full)
                    done.append(name)
                else:
                    os.remove(tmp)
            except (OSError, subprocess.SubprocessError):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        rescan(rel)
    card.out({"ok": True, "levelled": len(done), "skipped": len(left), "names": done[:20]})


def mean_level(path):
    """The mean level of a finished file, or None."""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect",
                            "-f", "null", "/dev/null"], capture_output=True, text=True,
                           timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"mean_volume: (-?\d+(?:\.\d+)?) dB", r.stderr or "")
    return float(found.group(1)) if found else None


def cmd_spoken_clean(which=""):
    """Remove spoken chapters that were not made with the voice now chosen
    (the test hum, an older voice, or ones made before the card kept a
    record). They are simply spoken again when next opened."""
    card.check_arg(which)
    opts = settings()
    root = os.path.join(card.MUSIC_DIR, opts["folder"].strip("/"))
    running = busy()
    keep_folder = (running.get("folder") or "") if running else ""
    gone, kept = [], []
    for folder, dirs, names in os.walk(root) if os.path.isdir(root) else []:
        dirs[:] = [d for d in dirs if not d.endswith(".parts")]   # a job's workroom
        rel = os.path.relpath(folder, card.MUSIC_DIR)
        if which and rel != which:
            continue
        here = []                        # kept in THIS folder: names repeat between books
        for name in sorted(names):
            if not name.lower().endswith(".mp3"):
                continue
            if rel == keep_folder and int(running.get("chapter", -1)) >= 0 \
                    and name.startswith("%03d " % (int(running["chapter"]) + 1)):
                continue                       # the one being made right now
            if same_voice(rel, name, opts):
                here.append(name)
                continue
            for path in (os.path.join(folder, name),
                         os.path.join(folder, os.path.splitext(name)[0] + ".lrc")):
                try:
                    os.remove(path)
                except OSError:
                    pass
            gone.append(name)
        kept.extend(here)
        old_marks = made_by(rel)
        marks = {k: v for k, v in old_marks.items() if k in here}
        if marks != old_marks:           # a folder with nothing of ours is left alone
            try:
                if marks:
                    card.write_file(os.path.join(folder, MADE_BY), json.dumps(marks))
                else:
                    os.remove(os.path.join(folder, MADE_BY))
            except OSError:
                pass
        rescan(rel)
    card.out({"ok": True, "removed": len(gone), "kept": len(kept), "names": gone[:20]})


COMMANDS = {
    "voices": cmd_voices,
    "spoken-clean": cmd_spoken_clean,
    "spoken-relevel": cmd_spoken_relevel,
    "voice-install": cmd_voice_install,
    "voice-install-run": cmd_voice_install_run,
    "voice-install-status": cmd_voice_install_status,
    "speak": cmd_speak,
    "speak-drop": cmd_speak_drop,
    "speak-run": cmd_speak_run,
    "book-chapters": cmd_book_chapters,
    "play-spoken": cmd_play_spoken,
    "speak-status": cmd_speak_status,
    "speak-stop": cmd_speak_stop,
}
