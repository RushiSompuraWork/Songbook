"""The card's second tier: settings, the setup check, queue editing,
crossfade, synced lyrics and reading song lists for the Inbox.

Kept apart from card.py so that file stays about playback. Every function
here answers through card.out(), like the rest.
"""

import csv
import io
import glob
import json
import os
import re
import shutil
import sys
import time
import urllib.parse

import card

# ---------------------------------------------------------------- settings

# The card's keyboard shortcuts, every one changeable from the Shortcuts
# view. Navigation (arrows, Enter, Esc, typing) stays fixed.
KEY_DEFAULTS = {
    "download": "Ctrl+Space",
    "star": "Alt+Space",
    "playNext": "Shift+Return",
    "addToQueue": "Ctrl+Return",
    "remove": "Delete",
    "moveUp": "Alt+Up",
    "moveDown": "Alt+Down",
    "clearQueue": "Ctrl+Delete",
    "playPause": "Space",
    "guide": "?",
}
BAR_ACTIONS = ("playPause", "next", "rmpc", "card")

DEFAULTS = {
    "barDancer": False,       # the text dancer on the bar icon
    "notify": False,          # a notification when the song changes
    "pauseOnUnplug": True,    # pause when headphones go away
    "crossfade": 0,           # seconds MPD fades between songs
    "tourDone": False,        # the first-run tips were seen or skipped
    "keys": KEY_DEFAULTS,     # action -> key combination
    "barMiddle": "playPause", # middle click on the bar icon
    "barRight": "rmpc",       # right click on the bar icon
    "youtubeRelay": True,     # YouTube through the local relay (card.py)
    "youtubeLogin": "",       # browser whose YouTube sign-in yt-dlp uses; "" = none
    "radioRetestDays": 5,     # how long station lists and speed tests are kept
    # Phase 1 of the extra settings (2026-09-22):
    "consume": False,         # remove songs from Up next once played (MPD consume)
    "shuffleFolders": False,  # a folder played from the Library starts shuffled
    "downloadFormat": "mp3",  # mp3 (plays everywhere) or opus (as YouTube sends it)
    "downloadQuality": "best",  # best or small (MP3 only; Opus is kept as sent)
    "sponsorblock": False,    # cut parts SponsorBlock marks as not music, from downloads
    "radioHide": "none",      # none, bad (not working) or slow (slow and not working)
    "radioQuality": False,    # among stations as fast, higher bitrate first
    "youtubeQuality": "best", # best or saver (smaller streams)
    "searchResults": 6,       # YouTube results per search
    "danceStyle": -1,         # -1: a new style each song; 0..6: always that one
    "radioReportPlays": True, # tell Radio Browser a station was played, as it asks
    # Phase 2 (2026-09-22):
    "softPause": False,       # fade out before pausing, fade in on resume
    "downloadFolder": "",     # "" asks each time; else the folder downloads go into
    "hiddenFolders": [],      # Library folders not shown on the home page
    "folderSort": "name",     # name or recent (last played first)
    "radioCountries": [],     # country codes Discover leans towards
    "hiddenGenres": [],       # genres left out of the World page
    "youtubeMusic": False,    # search YouTube Music (songs only) instead of YouTube
    "barTitle": False,        # a short, still title beside the bar icon
    "notifyShows": "full",    # full (cover, title, artist), text (no cover) or title
    "lyricsAuto": False,      # the card opens on the lyrics while a song plays
    "lyricsOffset": 0,        # ms added to synced lyric times
    "lyricsSize": "normal",   # normal or large
    "textHighlight": "sentence",  # what lights up while a book's text is read along
    # Audiobook player, phase 1 (2026-09-22):
    "bookSkipBack": 10,       # seconds the ⟲ button goes back in a book
    "bookSkipForward": 30,    # seconds the ⟳ button goes ahead
    "bookRewind": 10,         # after a pause of 5 min or more a book goes back this far; 0 = off
    "bookFolders": [],        # folders whose folders (and own files) are books
    # Reading aloud (voices.py, speak.py), 2026-09-23:
    "voiceEngine": "",        # kokoro, piper, espeak, edge, custom, tone; "" = the first there is
    "voiceName": "",          # which voice of that engine
    "voiceSpeed": 100,        # percent: 50 to 300
    "voiceCores": 2,          # how many cores the voice may use: 1 gentle, 2 normal, 4 fast
    "voiceCommand": "",       # his own engine: a command with {text} {out} {voice} {speed}
    "spokenFolder": "Spoken",
    "spokenAheadOn": False,
    "spokenAhead": 2,
    "spokenTidy": False,
    "meaningLanguage": "en",  # what language a word's meaning is shown in
    # Where a word's meaning comes from: auto asks online when there is a way
    # out and the offline dictionaries when there is not (his choice,
    # 2026-10-01); offline never sends the word anywhere.
    "meaningSource": "auto",
    "sleepFade": True,        # the sleep timer fades the sound out instead of stopping
    # Audiobooks (books.py), 2026-09-22:
    "bookGraySkipped": False, # chapters skipped over also show as heard
}
RETEST_STEPS = (5, 10)
# Settings that take one of a few values. The dancer has 7 styles
# (danceStyles in CardService.qml); keep the two in step.
ENUMS = {
    "radioRetestDays": RETEST_STEPS,
    "downloadFormat": ("mp3", "opus"),
    "downloadQuality": ("best", "small"),
    "radioHide": ("none", "bad", "slow"),
    "youtubeQuality": ("best", "saver"),
    "searchResults": (6, 10, 15),
    "danceStyle": (-1, 0, 1, 2, 3, 4, 5, 6),
    "folderSort": ("name", "recent"),
    "notifyShows": ("full", "text", "title"),
    "lyricsOffset": (-1000, -500, 0, 500, 1000),
    "lyricsSize": ("normal", "large"),
    "textHighlight": ("sentence", "word", "paragraph"),
    "meaningSource": ("auto", "online", "offline"),
    "bookSkipBack": (10, 15, 30),
    "bookSkipForward": (10, 30),
    "bookRewind": (0, 5, 10, 30),
}
COUNTRY = re.compile(r"^[A-Z]{2}$")


def valid_folder_name(name):
    return (isinstance(name, str) and 0 < len(name) <= 120 and "/" not in name
            and not name.startswith(".") and "\n" not in name)


# Settings that hold a list: each item's check, and how many may be kept.
LISTS = {
    "hiddenFolders": (valid_folder_name, 200),
    "radioCountries": (lambda c: isinstance(c, str) and bool(COUNTRY.match(c)), 10),
    "hiddenGenres": (lambda g: g in card.GENRES, len(card.GENRES)),
    "bookFolders": (lambda p: valid_music_path(p), 50),
}
VOICE_ENGINES = ("", "kokoro", "piper", "espeak", "edge", "custom", "tone")


def valid_language(code):
    """A language code as a dictionary names one: en, hi, gu, pt."""
    return isinstance(code, str) and bool(re.match(r"^[a-z]{2,3}$", code))


def valid_voice_name(name):
    # No control characters at all, not only line breaks: it ends up in a
    # folder's record and on an engine's command line (2026-09-24).
    return (isinstance(name, str) and len(name) <= 300
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def valid_music_path(path):
    """A folder inside the music folder, as MPD names it: 'Books/Fantasy'."""
    return (isinstance(path, str) and 0 < len(path) <= 300 and not path.startswith(("/", "."))
            and all(part not in ("", ".", "..") for part in path.split("/"))
            and "\n" not in path)


def clean_list(key, value):
    ok, most = LISTS[key]
    seen = []
    for item in value:
        if ok(item) and item not in seen:
            seen.append(item)
    return seen[:most]
CROSSFADE_STEPS = (0, 3, 6, 10)

# Modifiers, then one key. A letter or digit alone is refused: it would
# steal that letter from typing a search.
COMBO = re.compile(r"^((Ctrl|Alt|Shift|Meta)\+){0,3}"
                   r"([A-Za-z0-9]|F[1-9]|F1[0-2]|Space|Return|Delete|Backspace|"
                   r"Up|Down|Left|Right|Tab|Home|End|PageUp|PageDown|Insert|"
                   r"[?/,.;'\[\]\\=`-])$")


def valid_combo(combo):
    if not isinstance(combo, str) or not COMBO.match(combo):
        return False
    key = combo.split("+")[-1]
    has_mod = "+" in combo
    return has_mod or not (len(key) == 1 and key.isalnum())


def clean_keys(value):
    keys = dict(KEY_DEFAULTS)
    if isinstance(value, dict):
        for action, combo in value.items():
            if action in KEY_DEFAULTS and valid_combo(combo):
                keys[action] = combo
    return keys


# What counts as a usable value, once, for both directions (2026-09-27).
#
# There used to be two chains of `if key == ...`: one in `settings()` deciding
# whether a saved value could be trusted, one in `cmd_set_setting` deciding
# whether a new value could come in. They had to agree, and nothing made them:
# adding `voiceCores` meant writing `1..16` twice, and a rule written in only
# one of them either lets a bad value in or quietly drops a good one. Each
# rule is written once here, with the sentence to say when it is refused.
#
# `DEFAULTS` still gives the type -- every value must be the same kind as its
# default -- so a rule only has to say what else must be true of it.
# `keys` and the list settings are not here: they are cleaned rather than
# refused, item by item (`clean_keys`, `clean_list`).


def between(low, high):
    return lambda v: low <= v <= high


def one_of(allowed):
    return lambda v: v in allowed


def empty_or(ok):
    return lambda v: v == "" or ok(v)


RULES = {
    "barMiddle": (one_of(BAR_ACTIONS), "Unknown bar action"),
    "barRight": (one_of(BAR_ACTIONS), "Unknown bar action"),
    # BROWSERS is defined further down the file, so this looks at it when it
    # is asked rather than when the table is built.
    "youtubeLogin": (lambda v: v == "" or v in BROWSERS, "Unknown browser"),
    "downloadFolder": (empty_or(valid_folder_name), "Not a usable folder name"),
    "meaningLanguage": (valid_language, "A language code such as en, hi or gu"),
    "spokenAhead": (between(1, 10), "Chapters ahead: 1 to 10"),
    "voiceCores": (between(1, 16), "The voice may use 1 to 16 cores"),
    "voiceSpeed": (between(50, 300), "Speed from 50 to 300 percent"),
    "voiceEngine": (one_of(VOICE_ENGINES), "Unknown voice engine"),
    "voiceName": (valid_voice_name, "Not a usable voice name"),
    "voiceCommand": (valid_voice_name, "Not a usable command"),
    "spokenFolder": (valid_music_path, "Not a usable folder name"),
    "crossfade": (one_of(CROSSFADE_STEPS), "Crossfade must be one of %s" % (CROSSFADE_STEPS,)),
}
for _key, _allowed in ENUMS.items():
    RULES[_key] = (one_of(_allowed), "%s must be one of %s" % (_key, _allowed))

CLEANED = ("keys",)          # mended item by item, never refused whole


def usable(key, value):
    """Whether this value may be kept for this setting. The same answer on the
    way in and on the way out, because there is one rule."""
    if key not in DEFAULTS or type(value) is not type(DEFAULTS[key]):
        return False
    rule = RULES.get(key)
    try:
        return rule[0](value) if rule else True
    except Exception:
        return False


def why_not(key):
    """The sentence to say when it is refused."""
    rule = RULES.get(key)
    return rule[1] if rule else "Not a usable value for " + str(key)[:40]


def settings():
    saved = card.load("settings.json", {})
    merged = dict(DEFAULTS)
    for key, value in saved.items():
        if key == "keys":
            merged["keys"] = clean_keys(value)
        elif key in LISTS:
            if isinstance(value, list):
                merged[key] = clean_list(key, value)
        elif usable(key, value):
            merged[key] = value
    return merged


def cmd_settings():
    card.out({"settings": settings()})


def cmd_set_setting(key, value_json):
    """Only known keys, only the right type: the settings file is ours."""
    if key not in DEFAULTS:
        card.fail("Unknown setting: " + key[:40])
    try:
        value = json.loads(value_json)
    except ValueError:
        card.fail("Not a valid value for " + key)
    if type(value) is not type(DEFAULTS[key]):
        card.fail("Wrong kind of value for " + key)
    if key == "keys":
        bad = [a for a, c in value.items() if a not in KEY_DEFAULTS or not valid_combo(c)]
        if bad:
            card.fail("Not a usable shortcut for " + ", ".join(sorted(bad))[:80])
        combos = list(clean_keys(value).values())
        if len(combos) != len(set(combos)):
            card.fail("Two actions cannot share one shortcut")
        value = clean_keys(value)
    elif key in LISTS:
        ok, most = LISTS[key]
        if len(value) > most or not all(ok(v) for v in value):
            card.fail("Not a usable list for " + key)
        value = clean_list(key, value)
    elif not usable(key, value):
        card.fail(why_not(key))
    # The one thing that is stricter coming in than going out: a browser has
    # to be on this computer to be chosen, but a saved choice stays saved if
    # he uninstalls it for a while.
    if key == "youtubeLogin" and value != "" and value not in installed_browsers():
        card.fail("That browser was not found on this computer")
    if key == "consume":
        m = card.mpd()
        m.raw("consume", "1" if value else "0")
        m.close()
    if key == "crossfade":
        m = card.mpd()
        m.raw("crossfade", str(value))
        m.close()
    current = card.load("settings.json", {})
    current[key] = value
    card.save("settings.json", current)
    card.out({"settings": settings()})


# ---------------------------------------------------------------- youtube sign-in
#
# Signing in makes YouTube far less likely to answer "confirm you're not a
# bot", and lets age-restricted songs and private playlists play. The card
# never sees a password: yt-dlp reads the sign-in cookies straight from a
# browser the user is already signed in to, each time it runs, and the card
# never copies them into a file of its own.

# Browser -> (profile folders to look in, cookie file inside a profile,
# yt-dlp browser name). Zen is Firefox underneath, so yt-dlp reads it as
# Firefox given the profile folder.
BROWSERS = {
    "zen": (("~/.config/zen", "~/.zen", "~/.var/app/app.zen_browser.zen/.zen"),
            "cookies.sqlite", "firefox"),
    "firefox": (("~/.mozilla/firefox", "~/.config/mozilla/firefox"), "cookies.sqlite", "firefox"),
    "chromium": (("~/.config/chromium",), "Cookies", "chromium"),
    "chrome": (("~/.config/google-chrome",), "Cookies", "chrome"),
    "brave": (("~/.config/BraveSoftware/Brave-Browser",), "Cookies", "brave"),
    "vivaldi": (("~/.config/vivaldi",), "Cookies", "vivaldi"),
}
BROWSER_NAMES = {"zen": "Zen", "firefox": "Firefox", "chromium": "Chromium",
                 "chrome": "Google Chrome", "brave": "Brave", "vivaldi": "Vivaldi"}


def browser_profile(name):
    """The profile folder of `name` whose cookies changed most recently."""
    roots, cookie_file, _ = BROWSERS[name]
    best, best_time = "", 0
    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        for sub in os.listdir(root):
            prof = os.path.join(root, sub)
            for path in (os.path.join(prof, cookie_file),
                         os.path.join(prof, "Network", cookie_file)):
                try:
                    t = os.path.getmtime(path)
                except OSError:
                    continue
                if t > best_time:
                    best, best_time = prof, t
    return best


def installed_browsers():
    return [b for b in BROWSERS if browser_profile(b)]


def login_spec():
    """--cookies-from-browser value for the chosen browser, or "".

    Built only from BROWSERS and folders found on disk. A profile path with
    "::" or a line break would be misread by yt-dlp, so it is refused."""
    choice = settings().get("youtubeLogin", "")
    if not choice or choice not in BROWSERS:
        return ""
    profile = browser_profile(choice)
    if not profile or "::" in profile or "\n" in profile:
        return ""
    return "%s:%s" % (BROWSERS[choice][2], profile)


def cmd_yt_browsers():
    card.out({"browsers": [{"id": b, "name": BROWSER_NAMES[b]} for b in installed_browsers()],
              "chosen": settings().get("youtubeLogin", "")})


def cmd_yt_login_test():
    """Ask YouTube for one item of the watch history, which only answers
    when signed in. Nothing is changed and nothing is kept."""
    if not login_spec():
        card.out({"signedIn": False, "note": "Not signed in: pick a browser first"})
        return
    try:
        card.ytdlp("--flat-playlist", "--playlist-items", "1", "-J",
                   "--", "https://www.youtube.com/feed/history", timeout=60)
        card.out({"signedIn": True, "note": "Signed in: YouTube knows this account"})
    except Exception as e:
        text = str(e)
        if "sign in" in text.lower() or "login" in text.lower() or "authentication" in text.lower():
            text = "The browser is not signed in to YouTube; sign in there first"
        card.out({"signedIn": False, "note": text[:160]})


# ---------------------------------------------------------------- setup check

def cmd_setup_check():
    """One line per thing a new user might be missing, with how to fix it."""
    checks = []

    def add(name, ok, fix=""):
        checks.append({"name": name, "ok": bool(ok), "fix": "" if ok else fix})

    try:
        m = card.Mpd()
        m.raw("ping")
        m.close()
        add("MPD", True)
    except Exception:
        add("MPD", False, "Start it: systemctl --user enable --now mpd")
    add("Music folder", os.path.isdir(card.MUSIC_DIR),
        "Create %s, or set music_directory in mpd.conf" % card.MUSIC_DIR)
    add("yt-dlp", shutil.which("yt-dlp"),
        "For YouTube: sudo pacman -S yt-dlp")
    # YouTube changes often and an old yt-dlp is the usual reason YouTube
    # stops working, so its age is a check of its own.
    age = ytdlp_age_days()
    if age is not None:
        add("yt-dlp up to date", age <= 60,
            "yt-dlp is %d days old; YouTube may fail: sudo pacman -Syu yt-dlp" % age)
    add("ffmpeg", shutil.which("ffmpeg"),
        "For downloads: sudo pacman -S ffmpeg")
    add("Beat feed", bool(card.FIFO) and os.path.exists(card.FIFO),
        "Optional: add a fifo audio_output to mpd.conf so the dancer hears the beat")
    # The reader opens in a window of its own, through Omarchy's launcher. On
    # a machine without it the window simply never appeared and nothing said
    # why (2026-09-24).
    add("Reader window",
        shutil.which("omarchy-launch-or-focus") or shutil.which("omarchy-launch-or-focus-tui"),
        "The book reader needs Omarchy's launcher (omarchy-launch-or-focus)")
    add("Notifications", shutil.which("notify-send"),
        "Optional: for \u201cReady to read aloud\u201d when a chapter is made: "
        "sudo pacman -S libnotify")
    card.out({"checks": checks, "allOk": all(c["ok"] for c in checks[:2])})


def ytdlp_age_days():
    """yt-dlp versions are dates (2026.08.19)."""
    import datetime
    import subprocess
    try:
        v = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True,
                           timeout=10).stdout.strip()
        y, m, d = (int(x) for x in v.split(".")[:3])
        return (datetime.date.today() - datetime.date(y, m, d)).days
    except Exception:
        return None


# ---------------------------------------------------------------- queue

def cmd_queue_delete(pos):
    if not pos.isdigit():
        card.fail("Bad position")
    m = card.mpd()
    m.raw("delete", pos)
    m.close()
    card.out({"ok": True})


def cmd_queue_clear_after():
    """One click in Up next: everything after the playing song goes."""
    m = card.mpd()
    st = m.dict("status")
    if "song" in st:
        m.raw("delete", "%d:" % (int(st["song"]) + 1))
    else:
        m.raw("clear")
    m.close()
    card.out({"ok": True})


def cmd_queue_move(src, dst):
    if not (src.isdigit() and dst.isdigit()):
        card.fail("Bad position")
    m = card.mpd()
    m.raw("move", src, dst)
    m.close()
    card.out({"ok": True})


# ---------------------------------------------------------------- lyrics
#
# From LRCLIB (lrclib.net): free, no account, community lyrics. Synced
# lyrics are LRC text, "[mm:ss.xx] line". Looked up in Python and cached,
# like album art, so the shell itself never touches the network.

LYRICS_DIR = os.path.expanduser("~/.cache/rushi.songbook/lyrics")
LRC_LINE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")


def parse_lrc(text):
    lines = []
    for raw in (text or "").splitlines():
        m = LRC_LINE.match(raw.strip())
        if m:
            t = int(m.group(1)) * 60 + float(m.group(2))
            lines.append({"t": round(t, 2), "line": m.group(3).strip()})
    return sorted(lines, key=lambda x: x["t"])


def split_artist_title(text):
    """'Artist - Song' is how stations and most uploads name things."""
    for sep in (" - ", " – ", " — "):
        if sep in text:
            a, t = text.split(sep, 1)
            return a.strip(), t.strip()
    return "", text.strip()


def clean_title(title):
    # "(Official Music Video)", "[Lyrics]", "| Label" add nothing to a match.
    title = re.sub(r"[\(\[][^\)\]]*(official|video|audio|lyric|visuali[sz]er|4k|hd)[^\)\]]*[\)\]]",
                   "", title, flags=re.I)
    title = title.split("|")[0]
    return re.sub(r"\s+", " ", title).strip(" -")


def prune(folder, keep):
    """Keep the newest files of a cache folder."""
    try:
        files = sorted((os.path.join(folder, n) for n in os.listdir(folder)),
                       key=os.path.getmtime)
    except OSError:
        return
    for f in files[:-keep]:
        try:
            os.remove(f)
        except OSError:
            pass


class Offline(Exception):
    """LRCLIB could not be reached: say so, and do not cache a miss."""


def lrclib(path, **params):
    from urllib import request as web
    from urllib.error import HTTPError
    url = "https://lrclib.net/api/%s?%s" % (path, urllib.parse.urlencode(params))
    req = web.Request(url, headers={"User-Agent": card.USER_AGENT})
    try:
        with web.urlopen(req, timeout=8) as r:
            return json.load(r)
    except HTTPError as e:
        if e.code == 404:
            return None  # a real "not found"
        raise Offline(str(e))
    except Exception as e:
        raise Offline(str(e))


def lyrics_for(artist, title, duration):
    hit = lyrics_try(artist, title, duration)
    bare = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title).strip()
    if not hit and bare and bare != title:
        # "a song (Some Anime Opening)" is often listed as just "a song".
        hit = lyrics_try(artist, bare, duration)
    return hit


def lyrics_try(artist, title, duration):
    if duration:
        hit = lrclib("get", artist_name=artist, track_name=title,
                     duration=int(round(duration)))
        if hit and (hit.get("syncedLyrics") or hit.get("plainLyrics")):
            return hit
    found = lrclib("search", q=(artist + " " + title).strip()) or []
    if not isinstance(found, list):
        return None
    # Prefer synced lyrics, then the closest length.
    found.sort(key=lambda h: (not h.get("syncedLyrics"),
                              abs((h.get("duration") or 0) - (duration or 0))))
    return found[0] if found else None


def cmd_lyrics():
    m = card.mpd()
    view = card.status_view(m)
    m.close()
    cur = view.get("current") or {}
    if not cur:
        card.out({"lyrics": None, "why": "Nothing playing"})
        return
    title, artist = cur.get("title", ""), cur.get("artist", "")
    # Words kept beside a local file (a book's text, a song's .lrc) come
    # first, see booktext.py. A book never asks the lyrics service: it
    # would only be sent a chapter name.
    path = cur.get("file", "")
    if path and not card.is_stream(path):
        import booktext
        book = view.get("book") or {}
        start, length = float(book.get("start") or 0), float(book.get("length") or 0)
        try:
            text = booktext.text_for(path, start if length else None,
                                     start + length if length else None)
        except Exception:
            text = None
        if text:
            card.out({"text": text, "title": title, "artist": artist})
            return
        if view.get("kind") == "book":
            card.out({"lyrics": None, "title": title, "artist": artist,
                      "why": "No text for this chapter yet (the reader, 󰊓, says how to add it)"})
            return
    duration = view.get("duration") or cur.get("duration") or 0
    if view.get("kind") == "radio":
        artist, title = split_artist_title(title)  # the station's "Artist - Song"
        duration = 0
    elif " - " in title and (not artist or artist.lower() in title.lower()):
        a, t = split_artist_title(title)
        artist, title = a or artist, t
    title = clean_title(title)
    if not title:
        card.out({"lyrics": None, "why": "No song name to look up"})
        return
    key = card.hashlib.sha1(("%s\n%s" % (artist, title)).lower().encode()).hexdigest()[:16]
    path = os.path.join(LYRICS_DIR, key + ".json")
    try:
        with open(path) as f:
            cached = json.load(f)
        # Found lyrics keep; a miss is asked again after three days.
        if cached.get("lyrics") or time.time() - os.path.getmtime(path) < 3 * 86400:
            card.out(cached)
            return
    except (OSError, ValueError):
        pass
    try:
        hit = lyrics_for(artist, title, duration)
    except Offline:
        card.out({"lyrics": None, "why": "Lyrics service unreachable", "artist": artist,
                  "title": title})
        return
    if not hit:
        answer = {"lyrics": None, "why": "No lyrics found", "artist": artist, "title": title}
    elif hit.get("instrumental"):
        answer = {"lyrics": None, "why": "Instrumental", "artist": artist, "title": title}
    else:
        answer = {"lyrics": {"synced": parse_lrc(hit.get("syncedLyrics")),
                             "plain": (hit.get("plainLyrics") or "")[:20000]},
                  "artist": hit.get("artistName") or artist,
                  "title": hit.get("trackName") or title}
    card.write_file(path, json.dumps(answer, ensure_ascii=False))
    prune(LYRICS_DIR, 600)
    card.out(answer)


# ---------------------------------------------------------------- song lists
#
# Other services export playlists as files; the Inbox reads them.
#   Exportify (Spotify)   CSV: "Track Name", "Artist Name(s)", ...
#   TuneMyMusic           CSV or TXT
#   Google Takeout        CSV with "Video ID" (a YouTube id: no search needed)
#   anything else         TXT, one "Artist - Song" per line, or M3U

LIST_EXT = (".csv", ".txt", ".m3u", ".m3u8")
AUDIO_EXT = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac")
MAX_LIST_BYTES = 2 << 20
MAX_SONGS = 2000
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

TITLE_COLS = ("track name", "title", "song", "song name", "track", "name")
ARTIST_COLS = ("artist name(s)", "artist name", "artist", "artists", "artist(s)")
VIDEO_COLS = ("video id", "videoid", "youtube id")
URL_COLS = ("url", "link", "youtube url")


def pick(header, names):
    for i, h in enumerate(header):
        if h in names:
            return i
    return -1


def read_csv(text):
    rows = list(csv.reader(io.StringIO(text)))
    # Takeout puts playlist details above the song table; find the header.
    start = 0
    for i, row in enumerate(rows[:20]):
        lowered = [c.strip().lower() for c in row]
        if pick(lowered, TITLE_COLS + VIDEO_COLS + URL_COLS) >= 0:
            start = i
            break
    header = [c.strip().lower() for c in rows[start]] if rows else []
    ti, ai = pick(header, TITLE_COLS), pick(header, ARTIST_COLS)
    vi, ui = pick(header, VIDEO_COLS), pick(header, URL_COLS)
    songs = []
    for row in rows[start + 1:]:
        def col(i):
            return row[i].strip() if 0 <= i < len(row) else ""
        song = {"title": col(ti), "artist": col(ai).split(",")[0].strip() if ai >= 0 else ""}
        if not song["artist"] and " - " in song["title"]:
            # TuneMyMusic's YouTube export leaves Artist empty and writes
            # "Artist - Song" as the title.
            song["artist"], song["title"] = split_artist_title(song["title"])
        vid = col(vi)
        url = col(ui)
        if VIDEO_ID.match(vid):
            song["url"] = "https://www.youtube.com/watch?v=" + vid
        elif url.startswith("https://") and card.is_youtube(url):
            song["url"] = url
        if song["title"] or song.get("url"):
            songs.append(song)
    return songs


def read_lines(text):
    songs, pending = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            pending = line.split(",", 1)[1].strip() if "," in line else ""
            continue
        if line.startswith("#"):
            continue
        name = pending or line
        pending = None
        if line.startswith("https://") and card.is_youtube(line):
            songs.append({"title": name if name != line else "", "artist": "", "url": line})
            continue
        # An M3U entry can be a file path; a song name can contain "/" too
        # ("cold/mess"). Only something that looks like a path is one.
        if name == line and (line.startswith(("/", "~", "./", "../"))
                             or line.lower().endswith(AUDIO_EXT)):
            name = os.path.splitext(os.path.basename(name))[0]
        artist, title = split_artist_title(name)
        songs.append({"title": title, "artist": artist})
    return songs


def read_song_list(path):
    if os.path.getsize(path) > MAX_LIST_BYTES:
        raise ValueError("That file is too big for a song list")
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    songs = read_csv(text) if path.lower().endswith(".csv") else read_lines(text)
    for s in songs:
        s["title"] = s["title"][:200]
        s["artist"] = s["artist"][:200]
    return songs[:MAX_SONGS]


def cmd_import_read(path):
    path = os.path.realpath(os.path.expanduser(path))
    if not path.lower().endswith(LIST_EXT) or not os.path.isfile(path):
        card.fail("Not a song list (CSV, TXT or M3U)")
    try:
        songs = read_song_list(path)
    except (OSError, ValueError) as e:
        card.fail(e)
    card.out({"file": path, "songs": songs, "count": len(songs)})


INBOX_DIRS = ("~/Downloads",)
INBOX_DAYS = 14


def cmd_inbox():
    """Song lists and music files that arrived lately and were not handled."""
    handled = set(card.load("inbox.json", []))
    cutoff = time.time() - INBOX_DAYS * 86400
    lists, files = [], []
    for d in INBOX_DIRS:
        base = os.path.expanduser(d)
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            low = name.lower()
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path) or st.st_mtime < cutoff or path in handled:
                continue
            if low.endswith(AUDIO_EXT):
                files.append({"path": path, "name": name})
            elif low.endswith(LIST_EXT) and st.st_size <= MAX_LIST_BYTES:
                lists.append({"path": path, "name": name})
    card.out({"lists": lists, "files": files})


def cmd_inbox_done(path):
    handled = card.load("inbox.json", [])
    if path not in handled:
        handled.append(path)
    card.save("inbox.json", handled[-500:])
    card.out({"ok": True})


# ---------------------------------------------------------------- music folder
#
# MPD decides where music lives (music_directory in mpd.conf). Changing it
# from the card edits that one line, keeps a dated backup next to the file,
# restarts the MPD user service and rescans. The card asks before calling
# this; it never runs on its own.

def conf_path():
    return os.path.expanduser(os.environ.get("MPD_CONF", "~/.config/mpd/mpd.conf"))


def rewrite_music_dir(text, value):
    """Replace the top-level music_directory line, or add one at the top."""
    lines, depth, done = text.splitlines(keepends=True), 0, False
    out = []
    for line in lines:
        bare = line.split("#", 1)[0].strip()
        if depth == 0 and not done and bare.startswith("music_directory"):
            out.append('music_directory     "%s"\n' % value)
            done = True
        else:
            out.append(line)
        depth += bare.count("{") - bare.count("}")
    if not done:
        out.insert(0, 'music_directory     "%s"\n' % value)
    return "".join(out)


def links_everywhere(folder, depth=4, limit=4000):
    """A link inside the folder that leads to / or to home (a Wine prefix's
    dosdevices/z: is one) would make MPD scan the whole disk, when mpd.conf
    follows symlinks. Look a few levels down, briefly."""
    home = os.path.realpath(os.path.expanduser("~"))
    seen = 0
    for top, dirs, files in os.walk(folder):
        if top[len(folder):].count(os.sep) >= depth:
            dirs[:] = []
        for name in dirs + files:
            seen += 1
            path = os.path.join(top, name)
            if os.path.islink(path):
                target = os.path.realpath(path)
                if target in ("/", home) or folder.startswith(target + os.sep) or target == folder:
                    return path
        if seen > limit:
            break
    return ""


def cmd_set_music_dir(path):
    import subprocess
    card.check_arg(path)
    if '"' in path:
        card.fail("A folder name with a double quote cannot go in mpd.conf")
    full = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(full):
        card.fail("No such folder: " + path[:80])
    risky = links_everywhere(full)
    if risky:
        card.fail("Not used: %s links to your whole disk (a Wine prefix?), so MPD would scan "
                  "everything. Pick a folder with just music." % risky.replace(full + "/", "")[:80])
    conf = conf_path()
    if not os.path.isfile(conf):
        card.fail("mpd.conf not found at " + conf)
    home = os.path.expanduser("~")
    value = "~" + full[len(home):] if full == home or full.startswith(home + os.sep) else full
    with open(conf, encoding="utf-8") as f:
        text = f.read()
    backup = "%s.bak.%s" % (conf, time.strftime("%Y%m%d-%H%M%S"))
    card.write_file(backup, text)
    # Keep the last few, not one for every change ever made.
    for stale in sorted(glob.glob(conf + ".bak.*"))[:-5]:
        try:
            os.remove(stale)
        except OSError:
            pass
    # follow: plenty of people keep mpd.conf as a link into their dotfiles,
    # and swapping the link out would leave the real file behind.
    card.write_file(conf, rewrite_music_dir(text, value), follow=True)
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "mpd"]).returncode == 0
    if not active:
        card.out({"ok": True, "restarted": False, "backup": backup,
                  "note": "Saved. Restart MPD yourself to use the new folder."})
        return
    subprocess.run(["systemctl", "--user", "restart", "mpd"], timeout=30)
    for _ in range(40):
        try:
            m = card.Mpd()
            m.raw("update")
            # Wait for the rescan (up to 20 s) so the card shows the new
            # folders, not the old ones. A longer scan finishes later and
            # the card refreshes when MPD reports it.
            for _ in range(80):
                if "updating_db" not in m.dict("status"):
                    break
                time.sleep(0.25)
            m.close()
            break
        except Exception:
            time.sleep(0.25)
    card.out({"ok": True, "restarted": True, "backup": backup, "musicDir": full})


def cmd_reset_settings():
    """Every setting and shortcut back to its default."""
    try:
        os.remove(card.state_path("settings.json"))
    except OSError:
        pass
    try:
        m = card.Mpd()
        m.raw("crossfade", "0")
        m.close()
    except Exception:
        pass
    card.out({"settings": settings()})


# ---------------------------------------------------------------- headphones
#
# Pause when headphones go away: the default output was headphones (a
# Headphones port, or a Bluetooth device) and now is not, or its port shows
# "not available". Driven by `pactl subscribe`, so it costs nothing between
# changes. Only runs while the setting is on.

def output_state():
    import subprocess
    try:
        info = json.loads(subprocess.run(["pactl", "-f", "json", "info"], capture_output=True,
                                         text=True, timeout=5).stdout or "{}")
        sinks = json.loads(subprocess.run(["pactl", "-f", "json", "list", "sinks"],
                                          capture_output=True, text=True, timeout=5).stdout or "[]")
    except Exception:
        return None
    return is_headphones(info.get("default_sink_name", ""), sinks)


def is_headphones(default_name, sinks):
    for s in sinks:
        if s.get("name") != default_name:
            continue
        props = s.get("properties") or {}
        if props.get("device.api") == "bluez5" or props.get("device.bus") == "bluetooth":
            return True
        active = s.get("active_port")
        for port in s.get("ports") or []:
            if port.get("name") == active:
                return (str(port.get("type", "")).lower() in ("headphones", "headset")
                        and port.get("availability") != "not available")
    return False


def die_with_parent():
    """pactl should never outlive the watcher: ask the kernel to send it
    SIGTERM when its parent goes, however the parent goes."""
    import ctypes
    import signal
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def should_pause(was, now):
    return was is True and now is False


# ---------------------------------------------------------------- the music folder
#
# MPD reports its own database, and two kinds of change never reach it:
#
#   - a folder with no sound in it. MPD indexes sound, so ~/Music/Books with
#     one .epub is not in its database at all and no rescan will put it there;
#   - the old place of a folder that was moved. Measured 2026-09-30: a create
#     is seen in 5.6 s and a delete in 6.4 s, but a move adds the destination
#     and leaves the source behind for ever (still there after 32 s).
#
# So the card watches the music folder itself. One watch on the folder, not on
# the tree: the folders he makes are the ones he can see when he opens it. A
# blocked read costs nothing while nothing changes, which is why this is a
# watch and not a tick.

IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_CLOEXEC = 0x00080000
FOLDER_MASK = IN_CREATE | IN_DELETE | IN_MOVED_FROM | IN_MOVED_TO | IN_DELETE_SELF | IN_MOVE_SELF
TEXT_ONLY = (".epub", ".txt")
SETTLE = 0.8            # wait for a burst to finish before acting on it
LEAST_APART = 2.0       # never ask MPD to look more often than this


def _inotify():
    """(fd, add_watch) from libc, or None where inotify is not available."""
    import ctypes
    import ctypes.util
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fd = libc.inotify_init1(IN_CLOEXEC)
    except (OSError, AttributeError):
        return None
    if fd < 0:
        return None
    return fd, libc


def watch_music_folder(emit, once=False):
    """Tell the card when the music folder changes, and MPD when it should look.

    Never puts a name from the filesystem into an MPD command: a file may be
    called anything at all, and MPD's protocol is line-based, so a name with a
    newline in it would be two commands. A bare `update` says everything
    needed and carries nothing from outside.
    """
    import select
    made = _inotify()
    if made is None:
        return False                     # the Settings button is the fallback
    fd, libc = made
    path = card.MUSIC_DIR.encode()
    if libc.inotify_add_watch(fd, path, FOLDER_MASK) < 0:
        os.close(fd)
        return False
    asked = 0.0
    try:
        while True:
            data = os.read(fd, 8192)     # blocks; costs nothing until something happens
            names, overflow = _events(data)
            # Wait for the rest of a burst -- copying an album in is one change
            # to him, not two hundred -- then act once.
            while True:
                if not select.select([fd], [], [], SETTLE)[0]:
                    break
                more, spilled = _events(os.read(fd, 8192))
                names += more
                overflow = overflow or spilled
            emit({"foldersChanged": True})
            # Sound may have moved, so MPD has something to learn -- unless
            # every name was a book, which MPD does not keep.
            sound = overflow or not names or any(
                not n.lower().endswith(TEXT_ONLY) for n in names)
            if sound and time.time() - asked >= LEAST_APART:
                asked = time.time()
                try:
                    m = card.Mpd()
                    m.raw("update")
                    m.close()
                except Exception:
                    pass                 # it is not there; the next change asks again
            if once:
                return True
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _events(data):
    """The names in one read, and whether the kernel dropped any."""
    import struct
    names, overflow, at = [], False, 0
    while at + 16 <= len(data):
        _wd, mask, _cookie, length = struct.unpack_from("iIII", data, at)
        at += 16
        raw = data[at:at + length].split(b"\0", 1)[0]
        at += length
        if mask & IN_Q_OVERFLOW:
            # The kernel gave up counting. What changed is unknown, so the only
            # safe answer is to treat everything as changed.
            overflow = True
        if raw:
            names.append(raw.decode("utf-8", "replace"))
    return names, overflow


def folder_loop(emit):
    """The watch, kept up if it ever falls over (the music folder itself being
    moved away ends it)."""
    while True:
        if not watch_music_folder(emit):
            return                       # no inotify here: nothing to keep up
        time.sleep(2)


def unplug_loop(emit):
    """Runs inside `card.py watch` as a thread. The setting is read at the
    moment headphones go, so switching it needs no restart."""
    import subprocess
    if not shutil.which("pactl"):
        return
    was = output_state()
    proc = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True,
                            preexec_fn=die_with_parent)
    for line in proc.stdout:
        if "sink" not in line and "server" not in line and "card" not in line:
            continue
        now = output_state()
        if should_pause(was, now) and settings().get("pauseOnUnplug"):
            try:
                m = card.Mpd()
                if m.dict("status").get("state") == "play":
                    m.raw("pause", "1")
                    emit({"paused": "headphones unplugged"})
                m.close()
            except Exception:
                pass
        if now is not None:
            was = now


# ---------------------------------------------------------------- long files
#
# A long file plays as an audiobook, always (his choice, 2026-09-22): an
# audiobook, a podcast or a two-hour mix, as long as Settings → Library →
# Audiobooks → "Counts as a book from" (10 minutes unless changed), from the
# music folder, carries on where it was left, even after other songs or a
# restart. The watcher notes the place every half minute while one plays and
# on pause; when that file starts again from the beginning, it seeks there.

POSITIONS = "positions.json"


# Where a long file had got to is worth remembering whatever it is -- a
# lecture, a mix, a set. It is not a claim that the file is a book: that rule
# was removed on 2026-09-23 because a 60-minute song is a song.
RESUME_MINUTES = 20


def is_long(view):
    cur = view.get("current") or {}
    return (not card.is_stream(cur.get("file", ""))
            and float(view.get("duration") or 0) >= RESUME_MINUTES * 60)


def note_position(view):
    """Write down where a long file is; forget it once it is nearly done."""
    if not is_long(view):
        return
    path = view["current"]["file"]
    at, length = float(view.get("elapsed") or 0), float(view["duration"])
    kept = card.load(POSITIONS, {})
    if at >= length - 60:
        if kept.pop(path, None) is not None:
            card.save(POSITIONS, kept)
    elif at >= 60:
        kept[path] = {"at": int(at), "t": time.time()}
        if len(kept) > 200:
            kept = dict(sorted(kept.items(), key=lambda kv: kv[1]["t"])[-200:])
        card.save(POSITIONS, kept)


def resume_position(m, view, prev):
    """A long file just started from the top: seek to where it stopped.
    Returns a notice for the card, or None."""
    cur = view.get("current") or {}
    path = cur.get("file", "")
    if (view.get("state") != "play" or path == prev.get("file") or not is_long(view)
            or float(view.get("elapsed") or 0) > 10):
        return None
    import books
    book = books.playing_folder()
    if book and path.startswith(book + "/"):
        return None                  # a book keeps its own place, per chapter
    hit = card.load(POSITIONS, {}).get(path)
    if not isinstance(hit, dict) or not isinstance(hit.get("at"), int):
        return None
    at = hit["at"]
    if not 60 <= at < float(view["duration"]) - 60:
        return None
    m.raw("seekcur", str(at))
    return "Carried on from %d:%02d:%02d" % (at // 3600, at // 60 % 60, at % 60)


# What the watcher last saw. The position thread cannot share the watcher's
# connection -- it is sitting in `idle` -- but it can share what it knows, and
# skip connecting at all when nothing is playing (2026-09-24).
_seen = {"view": None, "at": 0.0}


def note_view(view):
    """The watcher, telling this file what it just saw."""
    _seen["view"], _seen["at"] = view, time.time()


def stale():
    """Whether what the watcher last saw is too old to trust. The watcher may
    have died; then this looks for itself, as it always did."""
    return not _seen["view"] or time.time() - _seen["at"] > 120


def worth_a_look():
    """Whether a connection is worth making. MPD reports a change of state,
    so "playing" and "how long the file is" are current even when the elapsed
    time in that view is minutes old; the elapsed time is what we connect
    for."""
    if stale():
        return True
    view = _seen["view"]
    return view.get("state") == "play" and is_long(view)


def note_loop():
    """Write down where playback got to (one thread of the watcher).

    Two threads used to do this: one every ten seconds for a book, one every
    thirty for a long file. They asked the same question of the same server
    and differed only in what they wrote down, so they are one tick now --
    two threads and two connections a minute became one and one
    (2026-09-27). The tick still sleeps ten seconds, and still connects only
    when there is something it would write.
    """
    import books
    n = 0
    while True:
        time.sleep(10)
        n += 1
        try:
            book = bool(books.playing_folder())
            long_file = (n % 3 == 0) and worth_a_look()
            if not (book or long_file):
                continue
            m = card.Mpd()
            try:
                view = card.status_view(m)
                if view.get("state") == "play":
                    if book:
                        books.note(m, view)
                    if long_file:
                        note_position(view)
            finally:
                m.close()
        except Exception:
            pass


def cmd_unplug_watch():
    def emit(obj):
        card.out(obj)
        sys.stdout.flush()
    unplug_loop(emit)


# ---------------------------------------------------------------- inbox: files

def inside(path, folders):
    real = os.path.realpath(path)
    return any(real.startswith(os.path.realpath(os.path.expanduser(d)) + os.sep) for d in folders)


def cmd_inbox_move(path, folder):
    """Move a music file that arrived in Downloads into a music folder."""
    card.check_arg(path)
    if not (inside(path, INBOX_DIRS) and path.lower().endswith(AUDIO_EXT)
            and os.path.isfile(path) and not os.path.islink(path)):
        card.fail("Only music files from Downloads can be moved in")
    folder = card.safe_folder(folder)
    dest_dir = os.path.join(card.MUSIC_DIR, folder)
    os.makedirs(dest_dir, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(path))
    dest, n = os.path.join(dest_dir, base + ext), 1
    while os.path.exists(dest):          # never overwrite a song already there
        n += 1
        dest = os.path.join(dest_dir, "%s (%d)%s" % (base, n, ext))
    shutil.move(path, dest)
    try:
        m = card.Mpd()
        m.raw("update", folder)
        m.close()
    except Exception:
        pass
    card.out({"ok": True, "moved": os.path.basename(dest), "folder": folder})


# ---------------------------------------------------------------- inbox: lists
#
# An import works through a song list in the background: each song is found
# on YouTube (or taken straight from its link) and either queued to play or
# downloaded into a folder. Songs are spaced IMPORT_GAP seconds apart, as
# yt-dlp's guide advises, so YouTube does not rate-limit the connection.
# Progress is in import.json; starting another import or `import-stop`
# ends the current one at the next song.

IMPORT_GAP = 6


def import_state():
    return card.load("import.json", {})


def cmd_import_start(mode, folder, source):
    """mode: play | download. source: JSON list of songs, or playlist:<url>."""
    if mode not in ("play", "download"):
        card.fail("Unknown import mode")
    if mode == "download":
        folder = card.safe_folder(folder)
    if source.startswith("playlist:"):
        url = source[len("playlist:"):]
        if not (url.startswith("https://") and card.is_youtube(url)):
            card.fail("Only a YouTube playlist link can be saved")
        try:
            data = json.loads(card.ytdlp("-J", "--flat-playlist", "--", url, timeout=60))
        except Exception as e:
            card.fail(e)
        songs = [{"title": e.get("title") or "", "artist": "",
                  "url": card.yt_entry_view(e)["url"]} for e in data.get("entries") or []]
    else:
        try:
            songs = json.loads(source)
        except ValueError:
            card.fail("Not a song list")
        if not isinstance(songs, list):
            card.fail("Not a song list")
        clean = []
        for s in songs[:MAX_SONGS]:
            if not isinstance(s, dict):
                continue
            song = {"title": str(s.get("title", ""))[:200], "artist": str(s.get("artist", ""))[:200]}
            url = str(s.get("url", ""))
            if url.startswith("https://") and card.is_youtube(url):
                song["url"] = url
            if song["title"] or song.get("url"):
                clean.append(song)
        songs = clean
    if not songs:
        card.fail("No songs to import")
    token = "%x" % card.random.getrandbits(64)
    card.save("import-job.json", {"mode": mode, "folder": folder, "songs": songs})
    card.save("import.json", {"state": "running", "mode": mode, "folder": folder, "at": time.time(),
                              "total": len(songs), "done": 0, "failed": 0,
                              "current": "", "token": token})
    import subprocess
    subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), "card.py"),
                      "import-worker", token],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    card.out({"ok": True, "total": len(songs)})


def song_target(song):
    """A YouTube link as it is; otherwise a one-result YouTube search."""
    if song.get("url"):
        return song["url"]
    words = (song.get("artist", "") + " " + song.get("title", "")).strip()
    return "ytsearch1:" + words


def watch_url(target):
    if not target.startswith("ytsearch"):
        return target
    data = json.loads(card.ytdlp("-J", "--flat-playlist", "--", target, timeout=40))
    entries = data.get("entries") or []
    if not entries:
        raise RuntimeError("not found on YouTube")
    return card.yt_entry_view(entries[0])["url"]


def has_mutagen():
    """yt-dlp needs mutagen to put a cover into an Opus file."""
    import importlib.util
    return importlib.util.find_spec("mutagen") is not None


def download_args():
    """yt-dlp options for Settings → Library: format, quality, cuts.

    MP3 plays everywhere; quality 0 is best, 5 about 40% smaller. Opus is
    kept exactly as YouTube sends it (no re-encoding, so no quality choice)
    and gets a cover only when mutagen is installed: without it yt-dlp
    reports the whole download as failed (checked 2026-09-22)."""
    st = settings()
    if st["downloadFormat"] == "opus":
        args = ["-x", "--audio-format", "opus", "--embed-metadata"]
        if has_mutagen():
            args.append("--embed-thumbnail")
    else:
        args = ["-x", "--audio-format", "mp3",
                "--audio-quality", "0" if st["downloadQuality"] == "best" else "5",
                "--embed-metadata", "--embed-thumbnail"]
    if st["sponsorblock"]:
        # Asks sponsor.ajay.app which parts of the video are not music.
        args += ["--sponsorblock-remove", "music_offtopic"]
    return args


def download_one(target, folder):
    import subprocess
    template = os.path.join(card.MUSIC_DIR, folder, "%(title)s.%(ext)s")
    cmd = (["yt-dlp", "--quiet", "--no-warnings", "--no-playlist"] + card.auth_args()
           + download_args() + ["-o", template, "--", target])
    return subprocess.run(cmd, capture_output=True, timeout=600).returncode == 0


def cmd_import_worker(token):
    job = card.load("import-job.json", {})
    state = import_state()
    if state.get("token") != token:
        return
    songs, mode, folder = job.get("songs", []), job.get("mode"), job.get("folder", "")
    if mode == "download":
        folder = card.safe_folder(folder)   # checked here too, not only where it started
    started = False
    for i, song in enumerate(songs):
        state = import_state()
        if state.get("token") != token:
            return  # stopped, or another import took over
        name = (song.get("artist", "") + " - " + song.get("title", "")).strip(" -") or "song"
        state.update({"current": name[:120], "at": time.time()})
        card.save("import.json", state)
        ok = False
        try:
            target = song_target(song)
            if mode == "play":
                stream, meta = card.yt_stream(watch_url(target))
                card.check_arg(stream)
                card.remember_title(stream, meta)
                m = card.Mpd()
                if not started:
                    card.new_fill_token()
                    m.raw("clear")
                    song_id = m.dict("addid", stream).get("Id")
                    m.raw("playid", song_id)
                    card.set_now("youtube")
                    started = True
                else:
                    m.raw("add", stream)
                m.close()
                ok = True
            else:
                ok = download_one(target, folder)
        except Exception:
            ok = False
        state = import_state()
        if state.get("token") != token:
            return
        state["done"] = state.get("done", 0) + 1
        if not ok:
            state["failed"] = state.get("failed", 0) + 1
        card.save("import.json", state)
        if i < len(songs) - 1:
            time.sleep(IMPORT_GAP)
    state = import_state()
    if state.get("token") == token:
        state.update({"state": "done", "current": "", "at": time.time()})
        card.save("import.json", state)
        if mode == "download":
            try:
                m = card.Mpd()
                m.raw("update", folder)
                m.close()
            except Exception:
                pass


def cmd_import_stop():
    state = import_state()
    if state.get("state") == "running":
        state.update({"state": "stopped", "token": "", "current": "", "at": time.time()})
        card.save("import.json", state)
    card.out({"ok": True})


def cmd_rescan():
    """Settings -> "Look for new songs and books". Asks MPD to look, and says
    how many folders are there now.

    MPD's answer is not the whole answer: it indexes sound, so a folder of
    novels is never in its database and it will report no change at all for
    one. The card reloads the list itself when this returns, which is what
    makes the button find books (found 2026-09-30: it only asked MPD, so
    pressing it after adding a book folder did nothing whatever).
    """
    try:
        m = card.mpd()
        m.raw("update")
        folders = len([f for f in card.folder_counts(m) if f["name"] != card.FAV])
        m.close()
    except Exception as e:
        card.fail(str(e)[:120])
        return
    card.out({"ok": True, "folders": folders})


def saved_paths():
    """What "Clear saved data" removes: covers, lyrics, radio lists and speed
    tests, and resolved YouTube streams. Settings, stars and Favorites stay."""
    import booktext
    return [card.ART_DIR, LYRICS_DIR, booktext.CACHE_DIR] + [
        card.state_path(n) for n in ("lists.json", "quality.json", "streams.json", "chapters.json")]


def cmd_saved_size():
    total = 0
    for path in saved_paths():
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                total += sum(os.path.getsize(os.path.join(root, f)) for f in files
                             if not os.path.islink(os.path.join(root, f)))
        elif os.path.isfile(path):
            total += os.path.getsize(path)
    card.out({"bytes": total})


def cmd_clear_saved():
    for path in saved_paths():
        if os.path.islink(path):
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            os.remove(path)
    card.out({"ok": True})


# ---------------------------------------------------------------- starred stations
#
# Settings → Radio: starred stations go out as an M3U playlist in Downloads
# (any player opens it, and the other computer can import it) and come back
# in from any .m3u there. A playlist file is text anyone could have written,
# so only web addresses are taken, names are cut short and the list has a
# limit; a station is still checked again when it is played.

STARS_FILE = "radio-stars.m3u"
MOST_STARS = 500


def m3u_name(text):
    return " ".join(str(text).split())[:120]


def cmd_stars_export():
    stars = card.load("stars.json", [])
    lines = ["#EXTM3U"]
    for s in stars:
        url = str(s.get("url", ""))
        if url.startswith(("http://", "https://")) and "\n" not in url and "\r" not in url:
            lines += ["#EXTINF:-1," + m3u_name(s.get("name", "")), url]
    folder = os.path.expanduser(INBOX_DIRS[0])
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, STARS_FILE)
    # Not follow: a link planted in Downloads is not something to write
    # through, and this file is ours to replace.
    card.write_file(path, "\n".join(lines) + "\n")
    card.out({"ok": True, "path": path, "count": (len(lines) - 1) // 2})


def m3u_files():
    folder = os.path.expanduser(INBOX_DIRS[0])
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names
            if n.lower().endswith((".m3u", ".m3u8")) and os.path.isfile(os.path.join(folder, n))]


def cmd_stars_files():
    card.out({"files": [{"path": p, "name": os.path.basename(p)} for p in m3u_files()[:30]]})


def read_m3u(path):
    stations, name = [], ""
    with open(path, errors="replace") as f:
        text = f.read(512 << 10)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            name = m3u_name(line.split(",", 1)[1]) if "," in line else ""
        elif line.startswith(("http://", "https://")) and len(line) <= 2000:
            stations.append({"uuid": "", "name": name or "Imported station", "url": line,
                             "country": "", "tags": "", "votes": 0, "clicks": 0, "bitrate": 0})
            name = ""
    return stations


def cmd_stars_import(path):
    """Only a playlist sitting in Downloads, picked from cmd_stars_files."""
    if os.path.realpath(path) not in [os.path.realpath(p) for p in m3u_files()]:
        card.fail("Pick a playlist from Downloads")
    found = read_m3u(path)
    stars = card.load("stars.json", [])
    have = {s.get("url") for s in stars}
    added = [s for s in found if s["url"] not in have]
    seen, fresh = set(), []
    for s in added:
        if s["url"] not in seen:
            seen.add(s["url"])
            fresh.append(s)
    before = len(stars)
    stars = (stars + fresh)[:max(MOST_STARS, before)]
    card.save("stars.json", stars)
    card.out({"ok": True, "added": len(stars) - before, "stars": stars})


COMMANDS = {
    "settings": cmd_settings,
    "set-setting": cmd_set_setting,
    "setup-check": cmd_setup_check,
    "queue-delete": cmd_queue_delete,
    "queue-move": cmd_queue_move,
    "queue-clear-after": cmd_queue_clear_after,
    "set-music-dir": cmd_set_music_dir,
    "reset-settings": cmd_reset_settings,
    "unplug-watch": cmd_unplug_watch,
    "lyrics": cmd_lyrics,
    "import-read": cmd_import_read,
    "inbox": cmd_inbox,
    "inbox-done": cmd_inbox_done,
    "inbox-move": cmd_inbox_move,
    "import-start": cmd_import_start,
    "import-worker": cmd_import_worker,
    "import-stop": cmd_import_stop,
    "yt-browsers": cmd_yt_browsers,
    "rescan": cmd_rescan,
    "saved-size": cmd_saved_size,
    "clear-saved": cmd_clear_saved,
    "yt-login-test": cmd_yt_login_test,
    "stars-export": cmd_stars_export,
    "stars-files": cmd_stars_files,
    "stars-import": cmd_stars_import,
}
import books  # noqa: E402  (books.py imports card, as this file does)
import speak  # noqa: E402  (reading a novel aloud)
import jobs                                    # noqa: E402  (after card, like the rest)
import meaning                                 # noqa: E402
COMMANDS.update(meaning.COMMANDS)
COMMANDS.update(jobs.COMMANDS)
COMMANDS.update(books.COMMANDS)
COMMANDS.update(speak.COMMANDS)
