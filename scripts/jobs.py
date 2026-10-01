"""Everything the card is busy with, in one shape.

His ask, 2026-09-25: an indicator that says something is running, a line that
always says what kind, and a page showing what is being worked on now and
what is waiting behind it.

Five different things take a while -- a download, an import of a song list,
reading chapters aloud, installing a voice, testing radio stations -- and each
already keeps its own note of where it has got to. Nothing here writes; it
reads those notes and puts them in one shape:

    {"id", "kind", "title", "detail", "percent", "since", "stop"}

`percent` is -1 when the work cannot say how far along it is. `stop` is the
card command that ends it, or "" when it cannot be stopped.

**A job is only running if it says so recently.** A process that was killed
leaves its note behind saying "working", for ever. Every one of these writes
the time as it goes, and reading aloud has a lock that settles it outright;
anything that has not spoken for a minute is treated as gone (the stale
"making 38%" that hid a whole queue on 2026-09-23 came from trusting a file
that nobody was writing any more).
"""

import os
import time

import card

QUIET = 60          # not heard from in this long: the job is gone


def fresh(note, quiet=QUIET):
    at = note.get("at")
    try:
        return at is not None and time.time() - float(at) < quiet
    except (TypeError, ValueError):
        return False


def clock(seconds):
    seconds = int(max(0, seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60) if seconds < 3600 else \
        "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def downloading():
    note = card.load("download.json", {})
    if note.get("state") not in ("starting", "downloading") or not fresh(note):
        return None
    return {"id": "download", "kind": "download", "title": str(note.get("title", ""))[:80],
            "detail": "into " + str(note.get("folder", "")),
            "percent": int(note.get("percent") or 0), "since": float(note.get("at") or 0),
            "stop": "download-stop"}


def importing():
    note = card.load("import.json", {})
    if note.get("state") != "running" or not fresh(note):
        return None
    total, done = int(note.get("total") or 0), int(note.get("done") or 0)
    return {"id": "import", "kind": "import",
            "title": str(note.get("current") or "Adding songs")[:80],
            "detail": "%d of %d%s" % (done, total,
                                      " · %d could not be found" % int(note.get("failed") or 0)
                                      if note.get("failed") else ""),
            "percent": int(100 * done / total) if total else -1,
            "since": float(note.get("at") or 0), "stop": "import-stop"}


def speaking():
    import speak
    now = speak.busy()              # the lock, not the file: this one is certain
    if not now:
        return None
    return {"id": "speak", "kind": "speak",
            "title": str(now.get("title") or "Reading a chapter aloud")[:80],
            "detail": str(now.get("book") or "")[:60],
            "percent": int(now.get("percent") or 0), "since": float(now.get("at") or 0),
            "stop": "speak-stop"}


def installing():
    import voices
    note = card.load(voices.INSTALL, {})
    if note.get("state") != "working" or not fresh(note, 600):   # pip can be quiet a while
        return None
    return {"id": "install", "kind": "install",
            "title": "Installing " + str(note.get("engine") or "a voice"),
            "detail": (str(note.get("note") or "") + " " + str(note.get("doing") or "")).strip()[:70],
            "percent": int(note.get("percent", -1) or -1),
            "since": float(note.get("at") or 0), "stop": ""}


def testing():
    note = card.load("retest.json", {})
    if note.get("state") != "running" or not fresh(note, 300):
        return None
    total, done = int(note.get("total") or 0), int(note.get("done") or 0)
    return {"id": "retest", "kind": "retest", "title": "Testing radio stations",
            "detail": "%d of %d" % (done, total),
            "percent": int(100 * done / total) if total else -1,
            "since": float(note.get("at") or 0), "stop": ""}


def waiting():
    """What is queued behind what is running. Only reading aloud queues today;
    the others do one thing at a time."""
    import speak
    out = []
    for n, item in enumerate(speak.queue_of()):
        number = int(item.get("chapter", -1))
        out.append({"id": "speak:%s:%d" % (item.get("path", ""), number),
                    "kind": "speak", "place": n + 1,
                    "title": str(item.get("chapterTitle") or "Chapter %d" % (number + 1))[:80],
                    "detail": str(item.get("title") or "")[:60],
                    "path": item.get("path", ""), "chapter": number})
    return out


def running():
    out = []
    for look in (speaking, downloading, importing, installing, testing):
        try:
            one = look()
        except Exception:
            one = None
        if one:
            out.append(one)
    return out


def summary():
    """One line for the card: what is happening, and how many others."""
    now, queued = running(), waiting()
    if not now:
        return {"busy": False, "line": "", "running": [], "waiting": queued,
                "count": 0, "queued": len(queued)}
    first = now[0]
    said = {"speak": "Reading aloud", "download": "Downloading", "import": "Adding songs",
            "install": "Installing a voice", "retest": "Testing stations"}
    line = said.get(first["kind"], "Working")
    if first["percent"] >= 0:
        line += " · %d%%" % first["percent"]
    extra = len(now) - 1 + len(queued)
    if extra:
        line += " · %d more" % extra
    return {"busy": True, "line": line, "running": now, "waiting": queued,
            "count": len(now), "queued": len(queued)}


def cmd_jobs():
    card.out(summary())


def cmd_download_stop():
    """Stop a download part way. Its own process, so it is asked to go."""
    note = card.load("download.json", {})
    pid = note.get("pid")
    if isinstance(pid, int) and pid > 1:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    card.save("download.json", dict(note, state="stopped", at=time.time()))
    card.out({"ok": True})


COMMANDS = {
    "jobs": cmd_jobs,
    "download-stop": cmd_download_stop,
}
