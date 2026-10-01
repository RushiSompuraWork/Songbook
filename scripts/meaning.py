"""What a word means, for the reader.

Asked for 2026-09-25: choosing a word in a book and being told what it means,
in whichever language is set once, for every book. Offline first with the web
as the fallback was the order chosen then; online-only was acceptable too.

Offline is `sdcv`, the command-line StarDict reader: not installed here and
with no dictionaries, so it is tried and skipped. When he installs it
(`omarchy pkg add sdcv`) and puts dictionaries in ~/.stardict/dic, it answers
first, instantly and without the word leaving the machine.

Online is freedictionaryapi.com, which serves Wiktionary's own entries and
needs no account. Checked on 2026-09-25 with English, Hindi and Gujarati.

A word looked up is a word sent away, so: only the one word, never a sentence,
and what comes back is kept so the same word is not sent twice.

**Which one is asked first changed on 2026-10-01**: online when there is a
way out, the offline dictionaries when there is not. Online answers better and
a machine with no dictionaries installed has nothing to ask offline, so online
leads. Settings -> Look meanings up can pin it either way, and `offline` is the
setting for a word that must not leave the machine.

Whether there is a way out is decided by resolving the API's name -- one DNS
lookup, which the request would do anyway. Measured 2026-10-01: 0.2 ms to fail
with no network, 52 ms to resolve cold. A failure is remembered for half a
minute, so a book full of lookups behind a captive portal does not wait for a
timeout on every word.
"""

import json
import os
import re
import socket
import subprocess
import time
import urllib.parse

import card

CACHE = "meanings.json"
MOST = 400                      # words remembered
API = "https://freedictionaryapi.com/api/v1/entries/%s/%s"
HOST = urllib.parse.urlsplit(API).hostname or ""   # one name, not two to keep in step
WORD = re.compile(r"^[^\s\x00-\x1f]{1,60}$")
SOURCES = ("auto", "online", "offline")
WEB_TIMEOUT = 6                 # a word is small; waiting longer helps nobody
OFF_FOR = 30                    # how long a failure is believed


_no_way_out_until = 0.0


def reachable(host=None):
    """Is there a way out to ask? One name lookup, nothing sent anywhere.

    0.2 ms when there is no network and 52 ms to resolve cold (measured
    2026-10-01), so this costs nothing worth saving. A name that resolves is
    not a promise -- a captive portal resolves everything -- so a failed
    request writes the same verdict down, and both are believed for half a
    minute rather than retried per word.
    """
    if time.time() < _no_way_out_until:
        return False
    try:
        socket.getaddrinfo(host or HOST, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        no_way_out()
        return False


def no_way_out():
    global _no_way_out_until
    _no_way_out_until = time.time() + OFF_FOR


def source():
    """Settings -> Meanings from: auto, online, or offline."""
    got = str(card.setting("meaningSource", "auto") or "auto").strip().lower()
    return got if got in SOURCES else "auto"


def language():
    """Settings → Reading → Meanings in: a language code such as en, hi, gu."""
    got = str(card.setting("meaningLanguage", "en") or "en").strip().lower()
    return got if re.match(r"^[a-z]{2,3}$", got) else "en"


def tidy(word):
    """The word as a dictionary would have it: no quotes, brackets or the
    punctuation that came with it in the book."""
    word = str(word or "").strip()
    word = word.strip("\"'“”‘’()[]{}.,;:!?—–-…«»")
    return word if WORD.match(word) else ""


def from_sdcv(word, lang):
    """The offline dictionaries, when they are there."""
    try:
        r = subprocess.run(["sdcv", "--non-interactive", "--json-output", "--utf8-output",
                            "--", word], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0 or not (r.stdout or "").strip():
        return []
    try:
        found = json.loads(r.stdout)
    except ValueError:
        return []
    out = []
    for hit in found if isinstance(found, list) else []:
        text = " ".join(str(hit.get("definition") or "").split())
        if text:
            out.append({"part": str(hit.get("dict") or "")[:40], "definition": text[:600],
                        "example": ""})
    return out[:6]


def offline_ready():
    """Whether there is an offline dictionary to ask at all."""
    import shutil
    if not shutil.which("sdcv"):
        return False
    for folder in (os.path.expanduser("~/.stardict/dic"), "/usr/share/stardict/dic"):
        if os.path.isdir(folder) and os.listdir(folder):
            return True
    return False


def from_web(word, lang):
    """Wiktionary's entries, through freedictionaryapi.com."""
    from urllib import request as web
    url = API % (lang, urllib.parse.quote(word))
    req = web.Request(url, headers={"User-Agent": card.USER_AGENT})
    with web.urlopen(req, timeout=WEB_TIMEOUT) as r:
        data = card.read_json(r)
    out = []
    for entry in (data.get("entries") or [])[:6]:
        part = str(entry.get("partOfSpeech") or "")[:24]
        for sense in (entry.get("senses") or [])[:3]:
            text = " ".join(str(sense.get("definition") or "").split())
            if not text:
                continue
            examples = sense.get("examples") or []
            out.append({"part": part, "definition": text[:600],
                        "example": " ".join(str(examples[0]).split())[:200] if examples else ""})
    return out[:8]


def look(word, lang=None):
    """{"word", "language", "where", "entries"} -- or "why" when nothing."""
    lang = lang or language()
    clean = tidy(word)
    if not clean:
        return {"word": str(word)[:40], "language": lang, "entries": [],
                "why": "Not a word to look up"}
    kept = card.load(CACHE, {})
    key = "%s:%s" % (lang, clean.lower())
    hit = kept.get(key)
    if isinstance(hit, dict) and hit.get("entries"):
        return dict(hit, word=clean, language=lang, where=hit.get("where", "kept"))
    # Which to ask first, and what to fall back to. Online leads when there is
    # a way out (his choice, 2026-10-01); either can be pinned in Settings, and
    # "offline" is the one that never sends the word anywhere.
    want = source()
    online = want != "offline" and (want == "online" or reachable())
    tries = []
    if online:
        tries.append(("Wiktionary", lambda: from_web(clean, lang)))
    if want != "online":
        tries.append(("your dictionaries", lambda: from_sdcv(clean, lang)))
    entries, where, trouble = [], "", ""
    for name, ask in tries:
        try:
            entries = ask()
        except Exception as e:
            trouble = str(e)[:80]
            if name == "Wiktionary":
                no_way_out()     # a name that resolved but would not answer
            continue
        if entries:
            where = name
            break
    if not entries and (trouble or not tries):
        why = ("Could not look it up: %s" % trouble if trouble else
               "Meanings are set to offline only" if want == "offline" else
               "Nowhere to look it up")
        return {"word": clean, "language": lang, "entries": [], "why": why}
    answer = {"word": clean, "language": lang, "where": where, "entries": entries}
    if entries:
        kept[key] = {"entries": entries, "where": where, "at": time.time()}
        if len(kept) > MOST:
            kept = dict(sorted(kept.items(), key=lambda kv: kv[1].get("at", 0))[-MOST:])
        card.save(CACHE, kept)
    elif not entries:
        answer["why"] = "No entry for “%s” in %s" % (clean, lang)
        if not online and not offline_ready():
            # Only the offline dictionaries were asked and there are none
            # installed. "No entry" would read as the word not existing,
            # when the truth is that nothing was asked.
            answer["why"] = ("Offline, and no offline dictionaries yet: "
                             "omarchy pkg add sdcv, then dictionaries in ~/.stardict/dic")
    return answer


def cmd_meaning(word, lang=""):
    card.check_arg(word)
    card.check_arg(lang)
    card.out(look(word, lang.strip().lower() or None))


COMMANDS = {"meaning": cmd_meaning}
