"""Voices: the connector between the card and whatever says words out loud.

Design decision, 2026-09-23: the card never makes speech itself. It
only ever asks an outside program "say this text into this file". Which
program is his choice, so the plugin can be released without carrying anyone
else's model or licence, and works with none of them installed.

An engine here is a description, not code: how to check it is there, how to
list its voices, and how to speak one piece of text. Anything that can write
an audio file from text can be added, including a command he writes himself
(Settings → his own command, with places for the text, the file, the voice
and the speed).

Timings come out the same for every engine, because speech is made one
sentence at a time and each sentence's audio is measured (ffprobe). An engine
that reports its own word times (Kokoro, edge-tts) gives the finer highlight;
the rest light up a sentence at a time, which is what the reader shows by
default anyway.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time

import card

SPEAK_TIMEOUT = 300          # one sentence, at worst
MAX_SENTENCE = 2000          # characters handed to an engine at once
# Kokoro needs torch and a 330 MB model, which pip will not put in the system
# Python. The card keeps them in a Python of its own, made on request.
VENV = os.path.expanduser("~/.local/share/rushi.songbook/voices")
INSTALL = "voices-install.json"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=SPEAK_TIMEOUT, **kw)


def have(program):
    return bool(shutil.which(program))


def have_module(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def audio_seconds(path):
    """How long a piece of audio is, from ffprobe (0 when it cannot say)."""
    try:
        r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                 "default=nw=1:nk=1", "--", path])
        return float((r.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


# ---------------------------------------------------------------- the engines

def venv_python():
    path = os.path.join(VENV, "bin", "python")
    return path if os.path.exists(path) else ""


# Kokoro through ONNX Runtime rather than PyTorch. The same model and the
# same voices; measured side by side on the same passage on 2026-09-25 and he
# could not tell them apart (he thought the ONNX one a shade better, which was
# its 3 dB higher level, now levelled for both).
#
#   through PyTorch   1.9 GB   loads in 4.8 s   4.5x real time
#   through ONNX      0.7 GB   loads in 0.5 s   4.7x real time
#
# 765 MB of that was PyTorch, plus spaCy, transformers and sympy, to run an
# 82 MB model. The quick load matters as much as the size: a chapter is made
# in pieces so listening can start early, and 4.8 s of loading each time would
# have eaten the gain.
#
# What it gives up: the PyTorch build reports when each WORD is spoken, and
# ONNX reports phonemes only -- and the model that reports anything at all
# runs at 2.7x instead of 4.7x. Sentence times do not come from the engine at
# all: every sentence is made separately and measured, so lighting up the
# sentence being read (what he uses, and the default) is exact either way.
# Only word-by-word lighting is affected, and it falls back to spreading the
# words across the line by length, as it already does for espeak and piper.
MODEL = "kokoro-v1.0.onnx"
VOICES_BIN = "voices-v1.0.bin"
MODEL_BASE = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download"
              "/model-files-v1.0/")


def models_dir():
    return os.path.join(VENV, "models")


def kokoro_files():
    where = models_dir()
    return os.path.join(where, MODEL), os.path.join(where, VOICES_BIN)


def kokoro_ready():
    python = venv_python()
    model, voices_bin = kokoro_files()
    if not (os.path.exists(model) and os.path.exists(voices_bin)):
        return False
    if python and os.path.exists(os.path.join(VENV, "kokoro-ready")):
        return True
    if not python:
        return have_module("kokoro_onnx") and have_module("soundfile")
    try:
        r = subprocess.run([python, "-c", "import kokoro_onnx, soundfile"],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode == 0:
        try:
            open(os.path.join(VENV, "kokoro-ready"), "w").close()
        except OSError:
            pass
        return True
    return False


def kokoro_voices():
    return ["af_heart", "af_bella", "af_nicole", "am_michael", "am_puck",
            "bf_emma", "bm_george", "hf_alpha", "hm_omega"]


def voice_threads():
    """How many cores the voice may use, from Settings.

    Left to itself ONNX Runtime takes every core it can see. On this machine
    that was fourteen of sixteen -- for speech made 3.1x faster than it is
    listened to, where four cores managed 3.5x on a quarter of the work and
    two managed 1.9x on an eighth (measured 2026-09-27, see kokoro_say.py).
    A chapter begins playing while it is still being made, so staying ahead
    of the listening is all the speed that is wanted.

    1 gentle, 2 normal, 4 fast; his choice, in Settings -> Read aloud.
    """
    try:
        import card
        n = int(card.setting("voiceCores", 2) or 2)
    except Exception:
        n = 2
    return min(max(n, 1), 16)


def voice_env():
    """The child's environment, with the core limit added."""
    env = dict(os.environ)
    env["RMPC_VOICE_THREADS"] = str(voice_threads())
    return env


def kokoro_speak(text, out, voice, speed):
    """Kokoro: offline, natural, Apache-2.0. It runs in the card's own Python
    (kokoro_say.py), which also prints when each word starts."""
    python = venv_python() or sys.executable
    text_file = out + ".txt"
    with open(text_file, "w") as f:
        f.write(text)
    try:
        r = run([python, os.path.join(os.path.dirname(os.path.abspath(__file__)), "kokoro_say.py"),
                 text_file, out, voice or "af_heart", str(speed or 1.0)], env=voice_env())
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError((r.stderr or "Kokoro failed").strip().splitlines()[-1][:200])
        try:
            return json.loads((r.stdout or "[]").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return []
    finally:
        try:
            os.remove(text_file)
        except OSError:
            pass


def kokoro_speak_many(texts, outs, voice, speed):
    """A batch in one run of kokoro_say.py: the model is loaded once."""
    import tempfile
    python = venv_python() or sys.executable
    job = {"voice": voice or "af_heart", "speed": float(speed or 1.0),
           "items": [{"text": t, "out": o} for t, o in zip(texts, outs)]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(job, f)
        job_file = f.name
    try:
        r = subprocess.run([python, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "kokoro_say.py"), "--job", job_file],
                           capture_output=True, text=True, timeout=SPEAK_TIMEOUT * 10,
                           env=voice_env())
        if r.returncode != 0:
            raise RuntimeError((r.stderr or "Kokoro failed").strip().splitlines()[-1][:200])
        try:
            return json.loads((r.stdout or "[]").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return [[] for _ in texts]
    finally:
        try:
            os.remove(job_file)
        except OSError:
            pass


def piper_voices():
    found = []
    for base in ("~/.local/share/piper-voices", "~/.local/share/piper", "/usr/share/piper-voices"):
        folder = os.path.expanduser(base)
        for root, _dirs, names in os.walk(folder) if os.path.isdir(folder) else []:
            for n in names:
                if n.endswith(".onnx"):
                    found.append(os.path.join(root, n))
    return sorted(found)[:100]


def piper_speak(text, out, voice, speed):
    """Piper: offline, quick, small. Length-scale is how it takes speed."""
    cmd = ["piper", "--output_file", out]
    if voice:
        cmd += ["--model", voice]
    if speed and float(speed) != 1.0:
        cmd += ["--length-scale", "%.3f" % (1.0 / float(speed))]
    r = run(cmd, input=text)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError((r.stderr or "piper failed").strip().splitlines()[-1][:200])
    return []


def espeak_speak(text, out, voice, speed):
    """espeak-ng: tiny, instant, robotic; always a fallback."""
    cmd = ["espeak-ng", "-w", out, "-s", str(int(175 * float(speed or 1.0)))]
    if voice:
        cmd += ["-v", voice]
    r = run(cmd, input=text)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError((r.stderr or "espeak-ng failed").strip()[:200])
    return []


ESPEAK_VARIANTS = os.path.join("/usr/share/espeak-ng-data/voices", "!v")


def espeak_voices():
    """What can be given to espeak-ng's -v: a language, or a language and a
    voice variant ("en-us+f3" is a woman's voice). English first, with its
    variants, then every other language."""
    try:
        r = run(["espeak-ng", "--voices"])
    except (OSError, subprocess.SubprocessError):
        return []
    languages = []
    for line in (r.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            languages.append(parts[1])
    try:
        variants = sorted(n for n in os.listdir(ESPEAK_VARIANTS) if not n.startswith("."))
    except OSError:
        variants = []
    women = [v for v in variants if re.match(r"^f[1-5]$", v)]
    named = [v for v in variants if v[0].isupper() or v in ("anika", "aunty", "belinda")]
    out = []
    for lang in ("en-us", "en-gb", "en-gb-scotland", "hi"):
        if lang in languages:
            out.append(lang)
            out += ["%s+%s" % (lang, v) for v in women + named[:12]]
    out += [lang for lang in languages if lang not in out]
    return out[:400]


def edge_voices():
    try:
        r = run(["edge-tts", "--list-voices"])
    except (OSError, subprocess.SubprocessError):
        return []
    return re.findall(r"^([a-z]{2}-[A-Z]{2}-\w+Neural)", r.stdout or "", re.M)[:300]


def edge_speak(text, out, voice, speed):
    """Microsoft's Edge voices: good, but the text goes over the network. Its
    subtitles give each word's time."""
    subs = out + ".vtt"
    pct = int(round((float(speed or 1.0) - 1.0) * 100))
    cmd = ["edge-tts", "--text", text, "--write-media", out, "--write-subtitles", subs,
           "--rate", "%+d%%" % pct]
    if voice:
        cmd += ["--voice", voice]
    r = run(cmd)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError((r.stderr or "edge-tts failed").strip().splitlines()[-1][:200])
    words = []
    try:
        import booktext
        for cue in booktext.parse_subs(booktext.read_text(subs)):
            for w in cue["text"].split():
                words.append([w, round(float(cue["t"]), 2)])
        os.remove(subs)
    except Exception:
        words = []
    return words


def custom_speak(text, out, voice, speed):
    """His own command (Settings → Read aloud → Command). The text goes in a
    file, never on the command line, and the parts are given as a list, never
    through a shell."""
    template = card.setting("voiceCommand", "") or ""
    if not template.strip():
        raise RuntimeError("No command set (Settings → Read aloud)")
    text_file = out + ".txt"
    with open(text_file, "w") as f:
        f.write(text)
    try:
        # shlex, not split(): a command at "/home/me/my tts/say.sh" is one
        # word in quotes, and split() tore it in half (2026-09-24). Still no
        # shell -- the parts are passed as a list, and the text in a file.
        import shlex
        try:
            parts = shlex.split(template)
        except ValueError as e:
            raise RuntimeError("That command cannot be read: %s" % e) from None
        cmd = []
        for part in parts:
            cmd.append(part.replace("{text}", text_file).replace("{out}", out)
                       .replace("{voice}", voice or "").replace("{speed}", str(speed or 1.0)))
        r = run(cmd)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError((r.stderr or "the command failed").strip().splitlines()[-1][:200])
    finally:
        try:
            os.remove(text_file)
        except OSError:
            pass
    return []


def tone_speak(text, out, voice, speed):
    """A stand-in used by the tests and for trying the machinery out: ffmpeg
    hums for about as long as the words would take. Not a voice."""
    seconds = max(0.4, len(text.split()) / (2.5 * float(speed or 1.0)))
    hz = 180 + (len(text) % 5) * 40
    r = run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=%d:duration=%.2f" % (hz, seconds),
             "-ac", "1", "-ar", "24000", out])
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("ffmpeg could not make the test sound")
    return []


ENGINES = [
    {"id": "kokoro", "name": "Kokoro", "ext": ".wav", "words": False,
     "about": "offline · natural · the best of the small voices",
     "install": "the card can put it in place · about 700 MB, no root needed",
     "ready": kokoro_ready, "setup": True, "batch": 20,
     "voices": kokoro_voices, "speak": kokoro_speak, "speak_many": kokoro_speak_many},
    {"id": "piper", "name": "Piper", "ext": ".wav", "words": False,
     "about": "offline · quick · small voices",
     "install": "omarchy pkg add piper-tts, then a voice from rhasspy/piper-voices",
     "ready": lambda: have("piper"), "voices": piper_voices, "speak": piper_speak},
    {"id": "espeak", "name": "espeak-ng", "ext": ".wav", "words": False,
     "about": "offline · instant · robotic",
     "install": "omarchy pkg add espeak-ng",
     "ready": lambda: have("espeak-ng"), "voices": espeak_voices, "speak": espeak_speak},
    {"id": "edge", "name": "Edge voices", "ext": ".mp3", "words": True,
     "about": "Microsoft's voices · the text is sent over the internet",
     "install": "pip install edge-tts",
     "ready": lambda: have("edge-tts"), "voices": edge_voices, "speak": edge_speak},
    {"id": "custom", "name": "Your own command", "ext": ".wav", "words": False,
     "about": "any program: {text} {out} {voice} {speed}",
     "install": "Settings → Read aloud → Command",
     "ready": lambda: bool((card.setting("voiceCommand", "") or "").strip()),
     "voices": lambda: [], "speak": custom_speak},
    # Not a voice: it hums, to try the machinery. Hidden in the card unless
    # it is the one chosen, so it cannot be picked by mistake.
    {"id": "tone", "name": "Test hum (not a voice)", "ext": ".wav", "words": False,
     "about": "hums instead of speaking · for testing only",
     "install": "already here (ffmpeg)", "hidden": True,
     "ready": lambda: have("ffmpeg"), "voices": lambda: [], "speak": tone_speak},
]


def engine(engine_id):
    for e in ENGINES:
        if e["id"] == engine_id:
            return e
    return None


def ready_engines():
    """Every engine, whether it is installed, and how to get it."""
    out = []
    for e in ENGINES:
        try:
            ready = bool(e["ready"]())
        except Exception:
            ready = False
        out.append({"id": e["id"], "name": e["name"], "about": e["about"],
                    "install": e["install"], "ready": ready, "words": e["words"],
                    "hidden": bool(e.get("hidden")), "setup": bool(e.get("setup"))})
    return out


PY_DIR = os.path.expanduser("~/.local/share/rushi.songbook/python")
PY_RELEASES = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
PY_WANT = "cpython-3.12"


def py_kind():
    """The build for this machine. It was x86_64 only, so on an ARM machine
    the card would have fetched the wrong Python or found none at all
    (2026-09-24); those builds are published too."""
    import platform
    arch = platform.machine()
    known = {"x86_64": "x86_64", "amd64": "x86_64",
             "aarch64": "aarch64", "arm64": "aarch64"}
    if arch not in known:
        raise RuntimeError("no Kokoro build for this kind of machine (%s); "
                           "espeak-ng or piper work anywhere" % arch)
    return "%s-unknown-linux-gnu-install_only.tar.gz" % known[arch]


def own_python():
    """A Python the voices can use. Kokoro needs 3.10 to 3.12, and a system
    Python is often newer (3.14 here), so one is fetched: a single folder
    under ~/.local/share/rushi.songbook, no root, deletable."""
    mine = os.path.join(PY_DIR, "bin", "python3.12")
    if os.path.exists(mine):
        return mine
    if (3, 10) <= sys.version_info[:2] <= (3, 12):
        return sys.executable
    return fetch_python()


# Installing a voice, as one job with a shape (2026-09-27). It used to say
# only "Installing…" and pip's last line: for a 340 MB download over an
# ordinary connection that is several minutes of a sentence that never
# changes, and if he left the screen it vanished. Every step now reports a
# share of one whole, so the work bar fills and can be watched from anywhere.
#
#   making the Python            0 -> 4
#   the speech engine (pip)      4 -> 30
#   the voice model (310 MB)    30 -> 92
#   the voice styles (27 MB)    92 -> 99
#
# Shares, not guesses at time: the two downloads are most of it and report
# their own bytes, so the figure moves steadily instead of jumping.
STAGES = {"python": (0, 4), "engine": (4, 30), MODEL: (30, 92), VOICES_BIN: (92, 99)}
ROOM_NEEDED = 1700 << 20        # the venv, the engine and the voice, with room to unpack


def say_install(stage, note, part=0.0, doing="", state="working"):
    """Write where the install has got to: which stage, and how far through
    the whole job that puts it."""
    low, high = STAGES.get(stage, (0, 99))
    percent = int(low + (high - low) * min(1.0, max(0.0, part)))
    card.save(INSTALL, {"at": time.time(), "state": state, "engine": "kokoro",
                        "note": note, "doing": doing, "stage": stage,
                        "percent": 100 if state == "done" else percent})


def in_words(n):
    """A size the way a download page writes it: 27 MB, not 26.9 MiB."""
    return "%d MB" % round(n / (1 << 20)) if n < (1 << 30) else "%.1f GB" % (n / (1 << 30))


def left_in_words(seconds):
    if seconds <= 0 or seconds > 6 * 3600:
        return ""
    if seconds < 90:
        return "under a minute left"
    return "about %d minutes left" % round(seconds / 60.0)


def room_for(bytes_needed, where=VENV):
    """Whether there is space, in his words rather than an OSError later.

    A download that runs out of disk half way leaves a broken half and says
    something about errno; asking first costs nothing.
    """
    look = where
    while look and not os.path.isdir(look):
        look = os.path.dirname(look)
    try:
        free = shutil.disk_usage(look or os.path.expanduser("~")).free
    except OSError:
        return True, ""
    if free >= bytes_needed:
        return True, ""
    return False, ("Not enough room: %s free, about %s needed. Free some space and try again."
                   % (in_words(free), in_words(bytes_needed)))


def fetch_one(url, target, stage, what, size_hint=""):
    """Download one file, saying how far along it is, and carrying on from
    where it stopped if there is a part of it already.

    Resuming matters here: the model is 310 MB, and starting again from the
    beginning after a dropped connection is the difference between waiting
    and giving up.
    """
    import urllib.error
    import urllib.request
    part = target + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    request = urllib.request.Request(url)
    if have:
        request.add_header("Range", "bytes=%d-" % have)
    try:
        r = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as e:
        if have and e.code in (416, 400, 403):    # it will not resume: start again
            have = 0
            r = urllib.request.urlopen(urllib.request.Request(url), timeout=60)
        else:
            raise
    with r:
        resumed = r.status == 206 and have
        if not resumed:
            have = 0
        total = have + int(r.headers.get("Content-Length") or 0)
        mode = "ab" if resumed else "wb"
        got, started, said = have, time.time(), 0.0
        if resumed:
            say_install(stage, "Carrying on with " + what, have / float(total or 1),
                        "%s of %s already" % (in_words(have), in_words(total)))
        with open(part, mode) as f:
            while True:
                block = r.read(1 << 20)
                if not block:
                    break
                f.write(block)
                got += len(block)
                if time.time() - said < 0.5:
                    continue
                said = time.time()
                gone = max(0.1, said - started)
                rate = (got - have) / gone
                doing = "%s of %s" % (in_words(got), in_words(total or got))
                if rate > 0 and total:
                    words = left_in_words((total - got) / rate)
                    if words:
                        doing += " · " + words
                say_install(stage, "Getting " + what, got / float(total or got or 1), doing)
    if total and os.path.getsize(part) < total:
        raise RuntimeError("the download stopped part way")
    # Named only once it is whole: a download cut off half way must not look
    # like a voice that is ready.
    os.replace(part, target)


def plainly(e):
    """A sentence he can act on, instead of the machine's own words."""
    text = str(e)
    low = text.lower()
    if "name or service not known" in low or "temporary failure" in low \
            or "nodename nor servname" in low:
        return "No internet connection. Try again when you are online."
    if "timed out" in low or "timeout" in low:
        return "The download timed out. It carries on from where it stopped if you try again."
    if "no space left" in low:
        return "The disk is full. Free some space and try again."
    if "certificate" in low:
        return "Could not check the download's certificate. Try again, or check the clock."
    if "connection reset" in low or "incompleteread" in low or "stopped part way" in low:
        return "The connection dropped. It carries on from where it stopped if you try again."
    return text[:200]


def fetch_python():
    """Download a self-contained Python 3.12, checking it against the
    checksum published beside it."""
    import hashlib
    import tarfile
    import tempfile
    import urllib.request
    say_install("python", "This system's Python is too new for the voice; getting a 3.12…")
    with urllib.request.urlopen(PY_RELEASES, timeout=30) as r:
        release = json.load(r)
    kind = py_kind()
    asset = next((a for a in release.get("assets", [])
                  if PY_WANT in a["name"] and a["name"].endswith(kind)), None)
    if not asset:
        raise RuntimeError("no Python 3.12 build to download")
    # The checksums are published in one SHA256SUMS file listing every build.
    # This looked for "<name>.sha256", which has never existed, so `want` was
    # always empty and the check below never ran: the download was taken on
    # trust every time (found 2026-09-24). It refuses to go on without one now.
    sums = next((a for a in release["assets"] if a["name"] == "SHA256SUMS"), None)
    if not sums:
        raise RuntimeError("no checksums published with that Python; refusing to install it")
    with urllib.request.urlopen(sums["browser_download_url"], timeout=60) as r:
        listing = r.read(8 << 20).decode("utf-8", "replace")
    want = ""
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset["name"]:
            want = parts[0]
            break
    if not want:
        raise RuntimeError("that Python build is not in the published checksums")
    with tempfile.TemporaryDirectory() as work:
        tar = os.path.join(work, "python.tar.gz")
        with urllib.request.urlopen(asset["browser_download_url"], timeout=600) as r, \
                open(tar, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        h = hashlib.sha256()
        with open(tar, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if h.hexdigest() != want:
            raise RuntimeError("the Python download did not match its checksum")
        with tarfile.open(tar) as t:
            t.extractall(work, filter="data")       # nothing outside the folder
        shutil.rmtree(PY_DIR, ignore_errors=True)
        os.makedirs(os.path.dirname(PY_DIR), exist_ok=True)
        shutil.move(os.path.join(work, "python"), PY_DIR)
    mine = os.path.join(PY_DIR, "bin", "python3.12")
    if not os.path.exists(mine):
        raise RuntimeError("the Python download had no python3.12 in it")
    return mine


def install_kokoro():
    """Make the card's own Python and put Kokoro in it. No sudo, nothing
    outside ~/.local/share/rushi.songbook; it can be deleted any time."""
    os.makedirs(os.path.dirname(VENV), exist_ok=True)
    enough, why = room_for(ROOM_NEEDED)
    if not enough:
        say_install("python", why, state="error")
        return False
    try:
        python = own_python()
    except Exception as e:
        say_install("python", plainly(e), state="error")
        return False
    pip = [os.path.join(VENV, "bin", "pip"), "install", "--upgrade",
           "--timeout", "120", "--retries", "5"]
    steps = [("python", "Step 1 of 3 · making a Python for the voice…",
              [python, "-m", "venv", VENV]),
             ("engine", "Step 2 of 3 · getting the speech engine (about 50 MB)…",
              pip + ["kokoro-onnx", "soundfile"])]
    for stage, note, cmd in steps:
        say_install(stage, note)
        if not step(stage, note, cmd):
            return False
    if not fetch_model():
        return False
    ok = kokoro_ready()
    if ok:
        say_install(VOICES_BIN, "The voice is ready", 1.0, state="done")
    else:
        say_install(VOICES_BIN, "The voice did not come out working. Try installing it again.",
                    1.0, state="error")
    return ok


def fetch_model():
    """The voice itself: the model and the voice styles beside it."""
    where = models_dir()
    os.makedirs(where, exist_ok=True)
    for name, said, step_of in ((MODEL, "310 MB", "Step 3 of 3"),
                                (VOICES_BIN, "27 MB", "Step 3 of 3")):
        target = os.path.join(where, name)
        if os.path.exists(target) and os.path.getsize(target) > 1 << 20:
            continue
        what = "the voice (%s)" % said
        say_install(name, "%s · getting %s…" % (step_of, what))
        try:
            fetch_one(MODEL_BASE + name, target, name, what)
        except Exception as e:
            part = target + ".part"
            kept = os.path.getsize(part) if os.path.exists(part) else 0
            say_install(name, plainly(e), state="error",
                        doing="%s kept" % in_words(kept) if kept else "")
            return False
    return True


def step(stage, note, cmd):
    """Run one step of the install, telling the card what it is doing as it
    goes (pip's own last line: what it is downloading, and how big)."""
    import threading
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            bufsize=1)
    last = [note]

    def watch():
        for line in proc.stdout:
            line = " ".join(line.split())
            if line and not line.startswith("Requirement already"):
                last[0] = line[:150]

    reader = threading.Thread(target=watch, daemon=True)
    reader.start()
    # pip does not say how far along it is, so this creeps towards the end of
    # the stage's share rather than sitting still: it is honest about being a
    # guess, because it never reaches the end until the step really does.
    started = time.time()
    while proc.poll() is None:
        crept = min(0.9, (time.time() - started) / 90.0)
        say_install(stage, note, crept, last[0])
        time.sleep(1)
    reader.join(timeout=5)
    if proc.returncode != 0:
        say_install(stage, plainly(last[0]), state="error")
        return False
    say_install(stage, note, 1.0)
    return True


def chosen():
    """The engine to use: his choice if it is there, else the first that is."""
    want = card.setting("voiceEngine", "") or ""
    for e in ready_engines():
        if e["id"] == want and e["ready"]:
            return want
    for e in ready_engines():
        if e["ready"] and e["id"] != "tone":
            return e["id"]
    return ""


def voices_of(engine_id):
    e = engine(engine_id)
    if not e:
        return []
    try:
        return [str(v) for v in e["voices"]()]
    except Exception:
        return []


def speak_many(engine_id, texts, outs, voice="", speed=1.0):
    """Say several pieces at once when the engine can (Kokoro: one model
    load for the batch); otherwise one after another, as before."""
    e = engine(engine_id)
    if not e:
        raise RuntimeError("No such voice engine: %s" % str(engine_id)[:40])
    clean = [" ".join(str(t).split())[:MAX_SENTENCE] for t in texts]
    if e.get("speak_many"):
        words = e["speak_many"](clean, outs, voice, float(speed or 1.0))
        for out in outs:
            if not os.path.exists(out) or os.path.getsize(out) < 64:
                raise RuntimeError("%s made no sound" % e["name"])
        return [w or [] for w in (words or [[] for _ in clean])]
    return [speak(engine_id, t, o, voice, speed) for t, o in zip(clean, outs)]


def batch_size(engine_id):
    e = engine(engine_id)
    return int((e or {}).get("batch", 1))


def speak(engine_id, text, out, voice="", speed=1.0):
    """Say one piece of text into one file. Returns [[word, second], …] when
    the engine says where its words are, else []."""
    e = engine(engine_id)
    if not e:
        raise RuntimeError("No such voice engine: %s" % str(engine_id)[:40])
    text = " ".join(str(text).split())[:MAX_SENTENCE]
    if not text:
        raise RuntimeError("Nothing to say")
    words = e["speak"](text, out, voice, float(speed or 1.0))
    if not os.path.exists(out) or os.path.getsize(out) < 64:
        raise RuntimeError("%s made no sound" % e["name"])
    return words or []
