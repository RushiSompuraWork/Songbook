#!/usr/bin/env python3
"""Backend for the Songbook card.

The QML side stays thin: every action is one call to this script, and every
answer is one line of JSON on stdout. It speaks MPD's protocol directly,
asks Radio Browser for stations, and drives yt-dlp for YouTube.

State lives in ~/.local/state/rushi.songbook/:
  last.json      what to resume when the queue is empty
  stars.json     starred stations
  titles.json    YouTube stream url -> title (MPD only sees the raw url)
  now.json       what kind of thing is playing: music, radio or youtube
  download.json  progress of the current download
  streams.json   video id -> resolved stream, reused until it nearly expires
  relay.json     port and pid of the YouTube relay (see below)
"""

import hashlib
import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

def mpd_conf():
    """The few settings the card needs from the user's own mpd.conf, so it
    works with any MPD setup, not only the author's."""
    conf = {}
    path = os.path.expanduser(os.environ.get("MPD_CONF", "~/.config/mpd/mpd.conf"))
    try:
        in_output, output = False, {}
        for raw in open(path, encoding="utf-8", errors="replace"):
            line = raw.split("#", 1)[0].strip()
            if line.startswith("audio_output"):
                in_output, output = True, {}
                continue
            if in_output and line.startswith("}"):
                in_output = False
                if output.get("type") == "fifo" and "path" in output:
                    conf.setdefault("fifo", output["path"])
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            key, value = parts[0], parts[1].strip().strip('"')
            (output if in_output else conf).setdefault(key, value)
    except OSError:
        pass
    return conf


# This folder on the import path, once. It used to be added inside every
# function that reaches extras.py, which in a process that runs for days (the
# watcher) grew sys.path by one entry per call -- 500 calls, 506 entries, and
# every later import searching all of them (2026-09-24).
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CONF = mpd_conf()
# MPD_HOST may be a host name or a socket path, as for mpc and rmpc.
MPD_HOST = os.environ.get("MPD_HOST") or CONF.get("bind_to_address") or "127.0.0.1"
MPD_PORT = int(os.environ.get("MPD_PORT") or CONF.get("port") or "6600")
MUSIC_DIR = os.path.expanduser(os.environ.get("RMPC_CARD_MUSIC_DIR")
                               or CONF.get("music_directory") or "~/Music")
FIFO = os.path.expanduser(CONF.get("fifo", ""))
STATE_DIR = os.path.expanduser("~/.local/state/rushi.songbook")
USER_AGENT = "rushi.songbook/2.0"
# all.api is the round-robin name; de1/de2 are the servers alive in 2026-09
# (fi1 no longer resolves).
RADIO_FALLBACK = ["all.api.radio-browser.info", "de1.api.radio-browser.info",
                  "de2.api.radio-browser.info"]
YT_HOSTS = ("googlevideo.com", "youtube.com", "youtu.be")


# ---------------------------------------------------------------- output

def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def fail(msg):
    out({"error": str(msg)})
    sys.exit(0)


# ---------------------------------------------------------------- state

def state_path(name):
    return os.path.join(STATE_DIR, name)


# Files whose contents cannot be made again: where he is in every book, what
# he starred, what he chose. These keep one older copy, because a write cut
# short leaves an unreadable file, and an unreadable file used to read as
# "nothing yet" -- which the next save then wrote over for good (2026-09-24).
PRECIOUS = ("library.json", "stars.json", "settings.json", "reader.json")


def load(name, default):
    try:
        with open(state_path(name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        if name not in PRECIOUS:
            return default
    # It is there but unreadable: the copy from before the last save.
    try:
        with open(state_path(name) + ".bak") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_file(path, data, precious=False, follow=False):
    """Write a file so nobody can read half of it.

    Every file this card writes goes through here. It used to be written out
    wherever it was needed -- four times, three of them differently, two of
    them able to let two writers land in one file (2026-09-24).

      - its own temporary name per writer and per thread, so two writers
        cannot interleave;
      - the mode of the file being replaced is kept, not the umask's;
      - `follow`: write through a symlink to the file it points at. os.replace
        swaps out the link itself, so someone whose mpd.conf is a link into
        their dotfiles would have quietly stopped updating the real file.
        Left off everywhere else: a link where we keep our own files is not
        something to write through;
      - `precious`: the bytes are on disk before the name changes, the folder
        entry too, and the file it replaces is kept as .bak;
      - a write that does not finish takes its temporary file with it;
      - a new file under our own folder is readable by its owner and nobody
        else. What is kept there is what he read, what he listened to and
        every word he looked up, and the umask that made these 644 is the
        system's, not a decision anybody took (found 2026-10-01, going
        through what changes when a stranger runs this on a shared machine).
    """
    if follow and os.path.islink(path):
        path = os.path.realpath(path)
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    mine = os.path.realpath(folder).startswith(os.path.realpath(STATE_DIR))
    if mine:
        try:
            os.chmod(folder, 0o700)    # others cannot even list it
        except OSError:
            pass
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    binary = isinstance(data, (bytes, bytearray))
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = None
    try:
        with open(tmp, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8"})) as f:
            f.write(data)
            if precious:
                f.flush()
                os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        elif mine:
            os.chmod(tmp, 0o600)       # a new file of ours: the owner only
        if precious and os.path.exists(path):
            try:
                os.replace(path, path + ".bak")
            except OSError:
                pass
        os.replace(tmp, path)
        if precious:
            # The new name itself, on disk: without this a power cut can
            # leave the folder still pointing at what was there before.
            try:
                fd = os.open(folder, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save(name, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    # Its own temporary file per writer: the watcher, the card and the reader
    # can save the same file at once, and a shared ".tmp" mixed their halves.
    write_file(state_path(name), json.dumps(data, ensure_ascii=False),
               precious=name in PRECIOUS)


def sweep_temps(older_than=3600):
    """Clear temporary files left by a writer that was killed outright, where
    even the `finally` above never ran. Called once when the watcher starts."""
    now = time.time()
    try:
        names = os.listdir(STATE_DIR)
    except OSError:
        return
    for name in names:
        if not name.endswith(".tmp"):
            continue
        path = os.path.join(STATE_DIR, name)
        try:
            if now - os.path.getmtime(path) > older_than:
                os.remove(path)
        except OSError:
            pass


def set_now(kind, **extra):
    save("now.json", dict(kind=kind, **extra))


# ---------------------------------------------------------------- mpd

class Mpd:
    def __init__(self):
        if MPD_HOST.startswith(("/", "~", "@")):
            path = os.path.expanduser(MPD_HOST)
            if path.startswith("@"):
                path = "\0" + path[1:]  # abstract socket
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect(path)
        else:
            self.sock = socket.create_connection((MPD_HOST, MPD_PORT), timeout=5)
        self.f = self.sock.makefile("rwb")
        self.f.readline()

    def quote(self, arg):
        s = str(arg)
        # MPD's protocol is one command per line. A line break inside an
        # argument (say, in a station url from Radio Browser) would start a
        # second command, so refuse it outright.
        if "\n" in s or "\r" in s or "\0" in s:
            raise ValueError("line break in MPD argument")
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def raw(self, cmd, *args):
        line = cmd + "".join(" " + self.quote(a) for a in args) + "\n"
        self.f.write(line.encode())
        self.f.flush()
        pairs = []
        while True:
            l = self.f.readline().decode("utf-8", "replace").rstrip("\n")
            if l == "OK":
                return pairs
            if l.startswith("ACK"):
                raise RuntimeError(l)
            if not l:
                raise RuntimeError("mpd closed the connection")
            k, _, v = l.partition(": ")
            pairs.append((k, v))

    def binary(self, cmd, uri, limit=5 << 20):
        """Fetch a binary answer (readpicture, albumart), chunk by chunk."""
        data, offset = b"", 0
        while True:
            self.f.write(("%s %s %d\n" % (cmd, self.quote(uri), offset)).encode())
            self.f.flush()
            size, chunk, kind = 0, b"", ""
            while True:
                l = self.f.readline().decode("utf-8", "replace").rstrip("\n")
                if l == "OK":
                    break
                if l.startswith("ACK") or not l:
                    return b"", ""
                k, _, v = l.partition(": ")
                if k == "size":
                    size = int(v)
                elif k == "type":
                    kind = v
                elif k == "binary":
                    chunk = self.f.read(int(v))
                    self.f.readline()
            if not chunk:
                return data, kind
            data += chunk
            offset += len(chunk)
            if offset >= size or offset > limit:
                return (data, kind) if offset <= limit else (b"", "")

    def dict(self, cmd, *args):
        return dict(self.raw(cmd, *args))

    def records(self, cmd, *args, starts=("file", "directory", "playlist")):
        recs, cur = [], None
        for k, v in self.raw(cmd, *args):
            if k in starts:
                cur = {"_type": k, k: v}
                recs.append(cur)
            elif cur is not None:
                cur[k] = v
        return recs

    def close(self):
        try:
            self.raw("close")
        except Exception:
            pass
        self.sock.close()


def check_arg(value):
    """Refuse bad input before anything changes. Mpd.quote refuses it too,
    but by then a `clear` may already have emptied the queue."""
    s = str(value)
    if "\n" in s or "\r" in s or "\0" in s:
        fail("Refused: line break in %r" % s[:60])
    return s


def mpd():
    try:
        return Mpd()
    except OSError as e:
        fail("MPD unreachable: %s" % e)


def is_stream(path):
    return "://" in (path or "")


def is_youtube(path):
    host = urllib.parse.urlparse(path or "").hostname or ""
    return any(host == h or host.endswith("." + h) for h in YT_HOSTS)


def song_view(rec, titles):
    path = rec.get("file", "")
    known = titles.get(path) if is_stream(path) else None
    base = os.path.splitext(os.path.basename(path))[0]
    title = rec.get("Title") or (known or {}).get("title") or base
    return {
        "file": path,
        "title": (known or {}).get("title") or title,
        "artist": rec.get("Artist") or (known or {}).get("artist", ""),
        "album": rec.get("Album", ""),
        "duration": float(rec.get("duration") or rec.get("Time") or 0),
        "pos": int(rec.get("Pos", -1)),
        "source": (known or {}).get("source", ""),
    }


# ---------------------------------------------------------------- radio

MOST_ANSWER = 8 << 20           # what any web answer may be, read into memory


def read_json(r, most=MOST_ANSWER):
    """A web answer, with a ceiling on how much of it is believed.

    `json.load(r)` reads whatever the far end chooses to send. The station
    directory, the lyrics server and the dictionary are other people's
    machines, and a hostile or broken one answering with a gigabyte would be
    held in memory in full -- on the machine of whoever installed this, not
    mine (found 2026-10-01, going through what changes when strangers run it).

    Measured the same day: the largest real answer is a 200-station search at
    234 KB, lyrics 57 KB, a word 10 KB. Eight megabytes is thirty-five times
    the largest and still a ceiling.
    """
    raw = r.read(most + 1)
    if len(raw) > most:
        raise ValueError("the answer was larger than %d bytes; not reading it" % most)
    return json.loads(raw.decode("utf-8", "replace"))


def radio_hosts():
    hosts = []
    try:
        infos = socket.getaddrinfo("all.api.radio-browser.info", 443,
                                   proto=socket.IPPROTO_TCP)
        for info in infos:
            try:
                name = socket.gethostbyaddr(info[4][0])[0]
                if name not in hosts:
                    hosts.append(name)
            except OSError:
                pass
    except OSError:
        pass
    random.shuffle(hosts)
    return hosts + [h for h in RADIO_FALLBACK if h not in hosts]


def radio(path, **params):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    last = None
    # Twice over the servers: a name lookup can fail once right after the
    # shell or the network comes up, and succeed a second later.
    for host in radio_hosts()[:3] + [None] + radio_hosts()[:2]:
        if host is None:
            time.sleep(1.0)
            continue
        url = "https://%s/json/%s%s" % (host, path, ("?" + query) if query else "")
        # Imported here so only the network commands pay for it, and under
        # another name: `import urllib.request` inside a function makes
        # `urllib` local to it and breaks the urllib.parse call above.
        from urllib import request as web
        req = web.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with web.urlopen(req, timeout=6) as r:
                return read_json(r)
        except Exception as e:
            last = e
    # Keep the message human: "[Errno -2] Name or service not known" means
    # no internet (or no DNS) more often than a broken service.
    reason = str(getattr(last, "reason", last) or "")
    if "Errno -2" in reason or "Errno -3" in reason or "Name or service" in reason:
        raise RuntimeError("Can't reach Radio Browser: no internet connection?")
    raise RuntimeError("Radio Browser is not answering right now; try again soon")


def station_view(s):
    tags = [t for t in (s.get("tags") or "").split(",") if t][:2]

    def count(key):
        try:
            return max(0, int(s.get(key) or 0))
        except (TypeError, ValueError):
            return 0
    return {
        "uuid": s.get("stationuuid", ""),
        "name": (s.get("name") or "").strip() or "Unnamed station",
        "url": s.get("url_resolved") or s.get("url") or "",
        "country": s.get("country") or s.get("countrycode") or "",
        "tags": ", ".join(tags),
        # Radio Browser's reviews: votes are people who liked it, clicks
        # people who played it. Used to order stations of equal speed.
        "votes": count("votes"),
        "clicks": count("clickcount"),
        "bitrate": count("bitrate"),
    }


def stations(**params):
    params.setdefault("hidebroken", "true")
    params.setdefault("limit", 40)
    raw = radio("stations/search", **params)
    seen, result = set(), []
    for s in raw:
        # HLS playlists (.m3u8) are hit-and-miss in MPD; plain streams are not.
        if s.get("hls") == 1 or (s.get("url_resolved") or "").endswith(".m3u8"):
            continue
        v = station_view(s)
        if v["url"] and v["url"] not in seen:
            seen.add(v["url"])
            result.append(v)
    return result


# ---------------------------------------------------------------- station speed
#
# Stations differ a lot in how fast they start: measured 2026-09-21 on 40
# lofi stations, the first 32 KB (about two seconds of audio, what a
# player wants before it starts) took from 0.7 s to 5 s, and two were not
# plain streams at all. So every list is tested and sorted: good, then ok,
# then untested, slow, and not working; within a tier, by reviews.
#
# Testing means opening the stream and reading 32 KB, then hanging up. It
# happens for stations nobody chose yet, so it refuses any address inside
# this computer or the local network: a station listing is written by
# strangers and must not make this machine knock on the router or on MPD.

TIER_GOOD, TIER_OK = 1.5, 3.0      # seconds to the first 32 KB
PROBE_BYTES = 32 << 10
PROBE_LIMIT = 6.0
TIER_RANK = {"good": 0, "ok": 1, "": 2, "slow": 3, "bad": 4}
DAY = 24 * 3600


def retest_days():
    """Settings → Test radio speed: how long lists and results are kept."""
    try:
        import extras
        return extras.settings().get("radioRetestDays", 5)
    except Exception:
        return 5


def quality_ttl(tier):
    # A station that did not answer gets another chance after a day, so one
    # bad evening does not hide it for a week.
    days = retest_days()
    return DAY if tier == "bad" else days * DAY
NOT_AUDIO = ("text/", "application/vnd.apple.mpegurl", "application/x-mpegurl",
             "audio/x-mpegurl", "audio/mpegurl", "audio/x-scpls", "application/pls",
             "application/json", "application/xml")


def allow_address(text):
    """Is this one address out on the public internet? The gate the station
    tests are held to, and the one place a test replaces to reach its own
    server on this machine."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(str(text).split("%")[0])
    except ValueError:
        return False
    return bool(ip.is_global) and not ip.is_multicast


def checked_addresses(host, port):
    """Look the name up ONCE, make sure every address it gives is public, and
    hand back all of them, in order, to try.

    A name is looked up by whoever owns it, and the answer may differ from
    one moment to the next. Checking the name and then connecting by name
    asks twice, so a name can answer "somewhere public" to the check and
    "your router" to the connection half a second later, and the check was
    for nothing. Everything after this connects to the address that was
    checked, and only tells the far end which name it wanted (2026-09-24).
    """
    if not host:
        raise PermissionError("no host")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        raise PermissionError("no such host") from None
    addresses = []
    for info in infos:
        if info[4][0] not in addresses:
            addresses.append(info[4][0])
    if not addresses or not all(allow_address(a) for a in addresses):
        raise PermissionError("not a public address")
    # All of them, not the first: a name commonly gives an IPv6 and an IPv4
    # address, and pinning to whichever came first would have called every
    # station on a machine with no route to it dead (2026-09-24). Each one
    # was checked, so trying the next is no less careful.
    return addresses


def public_host(host):
    """True when every address `host` resolves to is on the public internet.
    Kept for anything that only wants the answer; what actually connects uses
    checked_addresses, which keeps the addresses it checked."""
    try:
        checked_addresses(host, None)
        return True
    except PermissionError:
        return False


def pinned_get(url, timeout, hops=3):
    """A GET that connects to an address checked a moment before, by number,
    while telling the far end the name it wanted -- so the certificate is
    still checked against the name. Redirects are followed by hand, each one
    checked the same way. Returns the open response; the caller closes it."""
    import http.client
    import ssl

    class PinnedHTTPS(http.client.HTTPSConnection):
        def __init__(self, name, address, port, **kw):
            super().__init__(name, port, **kw)
            self.address = address
            self.ssl_context = ssl.create_default_context()

        def connect(self):
            sock = socket.create_connection((self.address, self.port), self.timeout)
            # server_hostname is the name, so the certificate is checked
            # against it even though we dialled a number.
            self.sock = self.ssl_context.wrap_socket(sock, server_hostname=self.host)

    class PinnedHTTP(http.client.HTTPConnection):
        def __init__(self, name, address, port, **kw):
            super().__init__(name, port, **kw)
            self.address = address

        def connect(self):
            self.sock = socket.create_connection((self.address, self.port), self.timeout)

    seen = url
    for _hop in range(hops + 1):
        p = urllib.parse.urlparse(seen)
        if p.scheme not in ("http", "https"):
            raise PermissionError("not a web address")
        port = p.port or (443 if p.scheme == "https" else 80)
        addresses = checked_addresses(p.hostname, port)
        kind = PinnedHTTPS if p.scheme == "https" else PinnedHTTP
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        r, trouble = None, None
        for address in addresses:
            conn = kind(p.hostname, address, port, timeout=timeout)
            try:
                # Host comes from the name, not the number, as http.client
                # builds it, so the far end sees the name it is known by.
                conn.request("GET", path, headers={"User-Agent": USER_AGENT,
                                                   "Icy-MetaData": "0",
                                                   "Accept": "*/*"})
                r = conn.getresponse()
                break
            except OSError as e:
                conn.close()
                trouble = e          # that address did not answer; try the next
        if r is None:
            raise trouble or PermissionError("no address answered")
        if r.status in (301, 302, 303, 307, 308):
            where = r.getheader("Location") or ""
            r.close()
            conn.close()
            if not where:
                raise PermissionError("a redirect to nowhere")
            seen = urllib.parse.urljoin(seen, where)
            continue          # and the next address is checked in its turn
        r.conn = conn         # so the caller can hang up on both
        return r
    raise PermissionError("too many redirects")


def probe_station(url):
    """{"tier": good|ok|slow|bad, "secs": time to 32 KB}."""
    p = urllib.parse.urlparse(url or "")
    if p.scheme not in ("http", "https"):
        return {"tier": "bad", "secs": 0}
    t0 = time.time()
    r = None
    try:
        r = pinned_get(url, PROBE_LIMIT)
        ctype = (r.getheader("Content-Type") or "").lower()
        got = 0
        if not ctype.startswith(NOT_AUDIO):
            while got < PROBE_BYTES and time.time() - t0 < PROBE_LIMIT:
                block = r.read(8192)
                if not block:
                    break
                got += len(block)
        r.close()
        r.conn.close()
    except Exception:
        try:
            if r is not None:
                r.close()
                r.conn.close()
        except Exception:
            pass
        return {"tier": "bad", "secs": round(time.time() - t0, 2)}
    secs = round(time.time() - t0, 2)
    if got < PROBE_BYTES:
        return {"tier": "bad", "secs": secs}   # a playlist, a web page, or too slow
    tier = "good" if secs <= TIER_GOOD else "ok" if secs <= TIER_OK else "slow"
    return {"tier": tier, "secs": secs}


def known_quality():
    """url -> tier for stations tested recently enough."""
    now = time.time()
    return {url: q["tier"] for url, q in load("quality.json", {}).items()
            if isinstance(q, dict) and q.get("tier") in TIER_RANK
            and now - q.get("at", 0) < quality_ttl(q.get("tier"))}


def probe_many(urls, deadline=8.0, enough=None, force=False):
    """Test the stations not tested lately, all at once. Returns url -> tier
    for everything known afterwards; any still running at the deadline stay
    untested this time. `enough(tiers)` may end the wait early.

    Daemon threads, not a thread pool: a pool's threads keep the process
    alive until the slowest test gives up, and the card waits for the
    process to end before it shows anything."""
    import queue
    known = {} if force else known_quality()
    todo = [u for u in dict.fromkeys(urls) if u and u not in known][:60]
    results = {}
    if todo:
        jobs, done = queue.Queue(), queue.Queue()
        for u in todo:
            jobs.put(u)

        def worker():
            while True:
                try:
                    u = jobs.get_nowait()
                except queue.Empty:
                    return
                done.put((u, probe_station(u)))
        for _ in range(min(16, len(todo))):
            threading.Thread(target=worker, daemon=True).start()
        end = time.time() + deadline
        while len(results) < len(todo):
            try:
                u, q = done.get(timeout=max(0.01, end - time.time()))
            except queue.Empty:
                break
            results[u] = q
            if enough and enough(dict(known, **{k: v["tier"] for k, v in results.items()})):
                break
        stored = load("quality.json", {})
        now = time.time()
        for u, q in results.items():
            stored[u] = dict(q, at=now)
        if len(stored) > 2000:
            stored = dict(sorted(stored.items(), key=lambda kv: kv[1].get("at", 0))[-1500:])
        save("quality.json", stored)
        known.update({u: q["tier"] for u, q in results.items()})
    return {u: known[u] for u in urls if u in known}


def rank(found, tiers):
    """Good first, then ok, untested, slow, not working; by reviews within
    (or by bitrate first, with Settings → Radio → Prefer higher quality).
    Settings → Radio → Hide can leave out not-working, or slow too."""
    by_quality = setting("radioQuality", False)
    hide = setting("radioHide", "none")
    hidden = {"bad"} if hide == "bad" else {"bad", "slow"} if hide == "slow" else set()
    for s in found:
        s["tier"] = tiers.get(s["url"], "")
    kept = [s for s in found if s["tier"] not in hidden]
    return sorted(kept, key=lambda s: (TIER_RANK[s["tier"]],
                                       -s.get("bitrate", 0) if by_quality else 0,
                                       -s.get("votes", 0), -s.get("clicks", 0)))


def cmd_probe_stations(urls_json):
    urls = [u for u in json.loads(urls_json) if isinstance(u, str)][:60]
    out({"quality": probe_many(urls)})


def count_click(uuid):
    """Tell Radio Browser a station was played, without making anyone wait.
    Settings → Privacy and data can turn this off."""
    if not uuid or not setting("radioReportPlays", True):
        return
    subprocess.Popen([sys.executable, __file__, "click-run", uuid],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


STATION_UUID = re.compile(r"^[0-9a-fA-F-]{8,40}$")


def cmd_click_run(uuid):
    # It becomes part of a url path, so it may only look like a station id.
    if not STATION_UUID.match(str(uuid)):
        return
    try:
        radio("url/" + uuid)
    except Exception:
        pass


# ---------------------------------------------------------------- youtube

def setting(key, default=None):
    try:
        import extras
        return extras.settings().get(key, default)
    except Exception:
        return default


def auth_args():
    """yt-dlp options for the YouTube sign-in chosen in Settings, or none.

    The browser spec is built by extras.login_spec() from what it found on
    disk, never from typed text, and yt-dlp reads the cookies from that
    browser itself each run: the card never copies them anywhere.
    """
    try:
        import extras
        spec = extras.login_spec()
    except Exception:
        spec = ""
    return ["--cookies-from-browser", spec] if spec else []


def missing(program, what):
    """A plain sentence when a program is not installed. Without this the card
    showed Python's own words -- "[Errno 2] No such file or directory:
    'yt-dlp'" -- which says nothing about what to do (2026-09-24)."""
    return RuntimeError("%s is not installed. %s" % (program, what))


import contextlib


@contextlib.contextmanager
def cookie_turn(wait=30):
    """One yt-dlp at a time, while it is reading the browser's cookies.

    Signed in, every yt-dlp run opens the browser's cookie store, and they
    get in each other's way: one run takes 9 seconds, four at once take 28
    each (measured 2026-09-24), which is how a download in the background
    made a song being resolved time out. Waiting a turn is faster than
    racing. If the wait runs out -- something is holding it far too long --
    it goes ahead anyway rather than failing.
    """
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    held = None
    try:
        held = open(state_path("ytdlp.lock"), "w")
        end = time.time() + wait
        while True:
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= end:
                    break
                time.sleep(0.4)
    except OSError:
        held = None
    try:
        yield
    finally:
        if held is not None:
            held.close()


# Signed in, a run has to wait for its turn at the cookies as well as do its
# own work, so the clock it is given is longer.
COOKIE_TIMEOUT = 120
# ... except when MPD is waiting at the other end. The relay is read by the
# player itself: if it sits waiting for a turn, the music sits with it, and
# nothing he presses seems to take (2026-09-27). It gives up quickly instead,
# and MPD is told plainly that the piece could not be fetched.
HURRIED_WAIT = 5
HURRIED_TIMEOUT = 45


def ytdlp(*args, timeout=60, hurry=False):
    auth = auth_args()
    cmd = ["yt-dlp", "--no-warnings", "--quiet"] + auth + list(args)
    if not auth:
        return run_ytdlp(cmd, HURRIED_TIMEOUT if hurry else timeout)
    if hurry:
        with cookie_turn(wait=HURRIED_WAIT):
            return run_ytdlp(cmd, HURRIED_TIMEOUT)
    with cookie_turn():
        return run_ytdlp(cmd, max(timeout, COOKIE_TIMEOUT))


def run_ytdlp(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "YouTube took longer than %d seconds to answer. If a download is "
            "running, let it finish and try again; signing out of YouTube "
            "(Settings → YouTube sign-in) also makes this quicker."
            % timeout) from None
    except FileNotFoundError:
        raise missing("yt-dlp", "It is what plays and downloads from YouTube: "
                                "sudo pacman -S yt-dlp") from None
    if r.returncode != 0:
        err = (r.stderr or "yt-dlp failed").strip()
        if "not a bot" in err or "429" in err:
            raise RuntimeError("YouTube is limiting requests right now; signing in "
                               "(Settings) usually helps, or try again in a few minutes")
        if "cookie" in err.lower() and auth_args():
            raise RuntimeError("Could not read the browser's YouTube sign-in; "
                               "check it in Settings → YouTube sign-in")
        raise RuntimeError(err.splitlines()[-1])
    return r.stdout


def yt_entry_view(e):
    vid = e.get("id") or ""
    url = e.get("url") or e.get("webpage_url") or ""
    if not url.startswith("http") and vid:
        url = "https://www.youtube.com/watch?v=" + vid
    return {
        "url": url,
        "title": e.get("title") or "YouTube video",
        "artist": e.get("channel") or e.get("uploader") or "",
        "duration": float(e.get("duration") or 0),
    }


def stream_expiry(stream):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(stream).query)
    try:
        return int(q.get("expire", ["0"])[0])
    except ValueError:
        return 0


VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Headers yt-dlp says the stream expects; nothing else is ever sent upstream.
STREAM_HEADERS = ("User-Agent", "Accept", "Accept-Language", "Sec-Fetch-Mode",
                  "Origin", "Referer")


def video_id(url):
    """The 11-character id of a single YouTube video, or ""."""
    p = urllib.parse.urlparse(url or "")
    host = p.hostname or ""
    vid = ""
    if host == "youtu.be":
        vid = p.path.lstrip("/").split("/")[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if p.path == "/watch":
            vid = urllib.parse.parse_qs(p.query).get("v", [""])[0]
        elif p.path.startswith(("/shorts/", "/live/", "/embed/")):
            vid = p.path.split("/")[2] if p.path.count("/") >= 2 else ""
    return vid if VIDEO_ID.match(vid) else ""


def yt_resolve(url, fresh=False, hurry=False):
    """Resolve a watch url to its direct audio stream.

    Every resolve is a request YouTube counts, and too many in a row get the
    connection rate-limited (HTTP 429, then "confirm you're not a bot"), which
    is what made playback slow. So a stream resolved in the last few hours is
    reused from streams.json instead of asking again.

    Returns {"url", "headers", "meta"}.
    """
    saver = setting("youtubeQuality", "best") == "saver"
    key = (video_id(url) or url) + (":saver" if saver else "")
    cache = load("streams.json", {})
    hit = None if fresh else cache.get(key)
    if hit and stream_expiry(hit.get("url", "")) - time.time() > 600:
        return hit
    fmt = "bestaudio[abr<=70]/worstaudio" if saver else "bestaudio"
    try:
        data = json.loads(ytdlp("-f", fmt, "-J", "--no-playlist", "--", url, hurry=hurry))
        stream = data.get("url")
    except Exception:
        # Could not ask just now -- another yt-dlp has the cookies, or
        # YouTube is being slow. For the relay that means MPD gets an error
        # and says "Failed to decode", and the song simply does not start
        # (2026-09-27). The link we already have usually still works, and a
        # link that has truly expired is refreshed by the 403 that follows.
        if hurry and cache.get(key):
            return cache[key]
        raise
    if not stream:
        if hurry and cache.get(key):
            return cache[key]
        raise RuntimeError("no audio stream for " + url)
    headers = data.get("http_headers") or {}
    entry = {
        "url": stream,
        "headers": {k: str(v) for k, v in headers.items() if k in STREAM_HEADERS},
        "meta": {"title": data.get("title") or "YouTube video",
                 "artist": data.get("channel") or data.get("uploader") or "",
                 "source": url},
    }
    cache[key] = entry
    if len(cache) > 200:
        cache = dict(list(cache.items())[-150:])
    save("streams.json", cache)
    return entry


def yt_stream(url, fresh=False):
    """What to hand MPD for a YouTube video, and its title.

    Through the relay when it is running (a stable local link that never
    expires), otherwise the direct stream as before.
    """
    entry = yt_resolve(url, fresh)
    vid = video_id(url)
    relay = relay_address() if vid else ""
    if relay:
        return "%s/yt/%s" % (relay, vid), entry["meta"]
    return entry["url"], entry["meta"]


def is_playlist_link(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return "list" in q or "/playlist" in url


def new_fill_token():
    """Mark that the queue changed hands; a running playlist fill stops."""
    token = "%x" % random.getrandbits(64)
    save("fill.json", {"token": token})
    return token


def fill_still_wanted(token):
    return load("fill.json", {}).get("token") == token


def remember_title(stream, meta):
    titles = load("titles.json", {})
    titles[stream] = meta
    # Stream urls expire within hours; keep the map from growing forever.
    if len(titles) > 400:
        titles = dict(list(titles.items())[-300:])
    save("titles.json", titles)


# ---------------------------------------------------------------- youtube relay
#
# Since mid-2026 YouTube throttles a stream request that has no byte range
# and then drops the connection: measured 2026-09-21, a plain request for a
# 2.7 MB song was cut after about a minute at 1.9 MB, so the song stopped
# partway. MPD opens streams exactly that way. Requests for bounded pieces
# ("bytes=0-1048575") come through at full speed.
#
# So the watcher also runs a tiny web server on 127.0.0.1 only. MPD is given
# http://127.0.0.1:<port>/yt/<video id>; the relay asks YouTube for 1 MiB
# pieces one after another and passes them on. A link that expired is
# resolved again in the middle of a song, so a queued song never goes stale.
#
# It serves nothing but /yt/<11-character id>, fetches nothing but the
# stream yt-dlp resolved for that id (https, a googlevideo.com host), and
# passes on no header from the caller except a parsed byte range.

RELAY_CHUNK = 1 << 20
RELAY_MAX_CLIENTS = 6


def relay_setting():
    try:
        import extras
        return extras.settings().get("youtubeRelay", True)
    except Exception:
        return True


def relay_address():
    """http://127.0.0.1:<port> when the relay is up and wanted, else ""."""
    info = load("relay.json", {})
    port, pid = info.get("port"), info.get("pid")
    if not isinstance(port, int) or not isinstance(pid, int) or not relay_setting():
        return ""
    try:
        os.kill(pid, 0)
    except OSError:
        return ""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError:
        return ""
    return "http://127.0.0.1:%d" % port


def upstream_ok(url):
    p = urllib.parse.urlparse(url or "")
    host = p.hostname or ""
    return p.scheme == "https" and (host == "googlevideo.com" or host.endswith(".googlevideo.com"))


def parse_range(value):
    """'bytes=START-[END]' -> (start, end or None); anything else -> (0, None)."""
    m = re.match(r"^bytes=(\d{1,15})-(\d{0,15})$", (value or "").strip())
    if not m:
        return 0, None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else None
    if end is not None and end < start:
        return 0, None
    return start, end


def fetch_piece(entry, start, end):
    """One bounded request upstream. Returns (response, total size)."""
    if not upstream_ok(entry.get("url")):
        raise PermissionError("not a YouTube stream")
    from urllib import request as web

    class StayOnYouTube(web.HTTPRedirectHandler):
        # The address is checked before the request; a redirect would be a
        # second address nobody checked. The station probe already guards
        # its redirects -- this one now does too.
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not upstream_ok(newurl):
                raise PermissionError("redirect away from YouTube")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    headers = dict(entry.get("headers") or {})
    headers["Range"] = "bytes=%d-%d" % (start, end)
    r = web.build_opener(StayOnYouTube).open(
        web.Request(entry["url"], headers=headers), timeout=20)
    total = None
    m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
    if m:
        total = int(m.group(1))
    elif r.status == 200 and start == 0 and r.headers.get("Content-Length", "").isdigit():
        # A small file can come back whole; the read loop takes all of it.
        total = int(r.headers["Content-Length"])
    return r, total


def relay_serve(handler, vid, send_body):
    """Answer one request from MPD for video `vid`."""
    watch = "https://www.youtube.com/watch?v=" + vid
    start, end = parse_range(handler.headers.get("Range"))
    ranged = handler.headers.get("Range") is not None
    entry = yt_resolve(watch, hurry=True)
    refreshed = False

    def piece(at, until):
        nonlocal entry, refreshed
        from urllib.error import HTTPError
        try:
            return fetch_piece(entry, at, until)
        except HTTPError as e:
            # 403/410: the link expired or was refused; resolve once more.
            if e.code in (403, 410) and not refreshed:
                refreshed = True
                entry = yt_resolve(watch, fresh=True, hurry=True)
                return fetch_piece(entry, at, until)
            raise

    first_end = start + RELAY_CHUNK - 1 if end is None else min(end, start + RELAY_CHUNK - 1)
    resp, total = piece(start, first_end)
    if total is None:
        resp.close()
        handler.send_error(502)
        return
    last = total - 1 if end is None else min(end, total - 1)
    if start > last:
        resp.close()
        handler.send_response(416)
        handler.send_header("Content-Range", "bytes */%d" % total)
        handler.end_headers()
        return
    handler.send_response(206 if ranged else 200)
    handler.send_header("Content-Type", resp.headers.get("Content-Type") or "audio/webm")
    handler.send_header("Content-Length", str(last - start + 1))
    handler.send_header("Accept-Ranges", "bytes")
    if ranged:
        handler.send_header("Content-Range", "bytes %d-%d/%d" % (start, last, total))
    handler.end_headers()
    if not send_body:
        resp.close()
        return
    at = start
    while True:
        began = at
        while at <= last:
            block = resp.read(64 << 10)
            if not block:
                break
            block = block[:last - at + 1]    # never more than was promised
            handler.wfile.write(block)
            at += len(block)
        resp.close()
        if at > last or at == began:
            return             # done, or upstream sent nothing: never spin
        resp, _ = piece(at, min(at + RELAY_CHUNK - 1, last))


def start_relay():
    """Start the relay in a thread of the watcher. Returns the port, or 0."""
    import http.server

    slots = threading.BoundedSemaphore(RELAY_MAX_CLIENTS)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # stdout is the card's JSON channel; stay quiet

        def answer(self, send_body):
            m = re.match(r"^/yt/([A-Za-z0-9_-]{11})$", self.path)
            if not m:
                self.send_error(404)
                return
            if not slots.acquire(blocking=False):
                self.send_error(503)
                return
            try:
                relay_serve(self, m.group(1), send_body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # MPD skipped, seeked or stopped
            except Exception:
                try:
                    self.send_error(502)
                except Exception:
                    pass
            finally:
                slots.release()
                self.close_connection = True

        def do_GET(self):
            self.answer(True)

        def do_HEAD(self):
            self.answer(False)

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    # The same port as last time if it is free, so songs already queued
    # keep working after the shell restarts.
    server = None
    for port in (load("relay.json", {}).get("port"), 0):
        if not isinstance(port, int):
            continue
        try:
            server = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        return 0
    port = server.server_address[1]
    save("relay.json", {"port": port, "pid": os.getpid()})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


# ---------------------------------------------------------------- commands

# ---------------------------------------------------------------- album art
#
# The card only ever shows a picture from a local file. Loading an https
# image inside the shell is what crashed it before (Omarchy #8026), so any
# download happens here, in Python, and QML gets a path.

ART_DIR = os.path.expanduser("~/.cache/rushi.songbook/art")


def cmd_art():
    m = mpd()
    cur = m.dict("currentsong")
    path = cur.get("file", "")
    if not path:
        m.close()
        out({"art": ""})
        return
    # hash() changes every run; the cache key has to be stable.
    key = hashlib.sha1(path.encode()).hexdigest()[:16]
    source = ""
    if is_stream(path):
        m.close()
        source = load("titles.json", {}).get(path, {}).get("source", "")
        vid = urllib.parse.parse_qs(urllib.parse.urlparse(source).query).get("v", [""])[0]
        if not vid or not all(c.isalnum() or c in "-_" for c in vid):
            out({"art": ""})  # radio, or nothing to show
            return
        key = "yt-" + vid
    target = os.path.join(ART_DIR, key + ".img")
    if os.path.exists(target):
        if not source:
            m.close()
        out({"art": target})
        return
    os.makedirs(ART_DIR, exist_ok=True)
    if source:
        url = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid
        from urllib import request as web
        try:
            req = web.Request(url, headers={"User-Agent": USER_AGENT})
            with web.urlopen(req, timeout=6) as r:
                data = r.read(5 << 20)
        except Exception:
            data = b""
    else:
        data, kind = m.binary("readpicture", path)
        if not data:
            data, kind = m.binary("albumart", path)
        m.close()
    # Only keep real JPEG or PNG data.
    if not (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG")):
        out({"art": ""})
        return
    write_file(target, data)
    prune_art()
    out({"art": target})


def prune_art(keep=300):
    """Keep the newest few hundred covers; about 60 KB each."""
    try:
        files = sorted((os.path.join(ART_DIR, n) for n in os.listdir(ART_DIR)),
                       key=os.path.getmtime)
    except OSError:
        return
    for f in files[:-keep]:
        try:
            os.remove(f)
        except OSError:
            pass


def status_view(m):
    st = m.dict("status")
    cur = m.dict("currentsong")
    titles = load("titles.json", {})
    now = load("now.json", {})
    path = cur.get("file", "")
    kind = "music"
    if is_stream(path):
        kind = "youtube" if (is_youtube(path) or path in titles) else "radio"
    song = song_view(cur, titles) if cur else None
    station = ""
    book = None
    if kind == "music" and song:
        # Part of the audiobook being read (books.py): the book's icon, and
        # inside one .m4b the chapter as the title.
        try:
            import books
            elapsed = float(st.get("elapsed") or 0)
            book = books.now_playing(m, path, elapsed)
            # A long file, or an .m4b, plays as an audiobook on its own too.
            single = book is None
            if single:
                book = books.single_file(path, float(st.get("duration") or 0), elapsed)
        except Exception:
            book, single = None, False
        if book:
            kind = "book"
            if book["chapter"]:
                song["title"] = book["chapter"]
            if not single:
                song["artist"] = book["title"]
    if kind == "radio" and song:
        # With a list of stations in the queue, the one playing now is
        # whichever MPD moved to, so its name comes from the list.
        station = station_name(path) or now.get("station") or cur.get("Name") or ""
        # For radio, MPD's Title is the ICY "Artist - Song" the station sends.
        song["title"] = cur.get("Title") or ""
        song["artist"] = station
    return {
        "state": st.get("state", "stop"),
        "elapsed": float(st.get("elapsed") or 0),
        "duration": float(st.get("duration") or 0),
        "song": int(st.get("song", -1)),
        "length": int(st.get("playlistlength", 0)),
        "random": st.get("random") == "1",
        "repeat": st.get("repeat") == "1",
        "single": st.get("single") == "1",
        "kind": kind,
        "station": station,
        "book": book,
        "current": song,
        # Not "error": the card reads that key as "MPD is unreachable", and
        # this is only a song that failed to play (a 403, a dead stream).
        "playError": st.get("error", ""),
    }


def cmd_status():
    m = mpd()
    view = status_view(m)
    m.close()
    out(view)


# ---------------------------------------------------------------- recovery
#
# Two things used to just stop the music: a YouTube link that expired (they
# last a few hours, so anything queued or paused a while fails with HTTP
# 403) and a radio station dropping on a network blip. The watcher sees
# both and mends them: a fresh YouTube link replaces the dead one in place,
# a dropped station is tried again. Each song gets a couple of tries per ten
# minutes, never a loop.

RECOVER_TRIES = {}


def may_retry(key, limit):
    now = time.time()
    tries = [t for t in RECOVER_TRIES.get(key, []) if now - t < 600]
    if len(tries) >= limit:
        RECOVER_TRIES[key] = tries
        return False
    RECOVER_TRIES[key] = tries + [now]
    return True


def recover(m, failed):
    """failed: {"file", "pos", "play"}. Returns a message, or None."""
    path, pos = failed["file"], failed["pos"]
    if not is_stream(path) or pos < 0:
        return None
    titles = load("titles.json", {})
    meta = titles.get(path) or {}
    if meta.get("source"):
        source = meta["source"]
        if not may_retry(source, 2):
            return None
        titles.pop(path, None)               # the dead link leaves the cache
        save("titles.json", titles)
        stream, fresh = yt_stream(source, fresh=True)
        check_arg(stream)
        remember_title(stream, fresh)
        new_id = m.dict("addid", stream, str(pos)).get("Id")
        after = m.records("playlistinfo", str(pos + 1))
        if after and after[0].get("file") == path:
            m.raw("delete", str(pos + 1))
        m.raw("clearerror")
        if failed["play"] and new_id:
            m.raw("playid", new_id)
        return "The YouTube link had expired; fetched a fresh one"
    if not failed["play"]:
        # With a list of stations queued, MPD already moved on to the next
        # one; going back would fight it.
        m.raw("clearerror")
        return "That station did not answer; playing the next one"
    if not may_retry(path, 3):
        return None
    m.raw("clearerror")
    time.sleep(3)
    if failed["play"]:
        m.raw("play", str(pos))
    return "The station dropped; reconnected"


def failed_song(view, prev):
    """Which song needs mending, if any."""
    cur = view.get("current") or {}
    here = {"file": cur.get("file", ""), "pos": view.get("song", -1)}
    if view.get("playError"):
        if view.get("state") != "play":
            return dict(here, play=True)
        if prev.get("file") and prev["file"] != here["file"]:
            return {"file": prev["file"], "pos": prev.get("pos", -1), "play": False}
    # A station that stops by itself dropped: nothing in the card stops a
    # station (pause pauses), so play → stop on radio means the stream ended.
    if (prev.get("state") == "play" and view.get("state") == "stop"
            and view.get("kind") == "radio" and here["file"] == prev.get("file")):
        return dict(here, play=True)
    return None


OUT_LOCK = threading.Lock()


def emit(obj):
    with OUT_LOCK:
        out(obj)
        sys.stdout.flush()


def cmd_watch():
    """Print the status now and again every time MPD says something changed.

    MPD's `idle` blocks until an event (play, pause, seek, next song, a new
    title from a radio station, the queue, options, a saved playlist), so
    the card stays current without asking once a second. One long-lived
    process instead of a fresh one per poll; the headphone watch runs in a
    thread of it, and it mends expired YouTube links and dropped stations.
    """
    import extras
    sweep_temps()
    # Catch up on what changed while nothing was running: inotify only sees
    # what happens while it is watching, so a folder moved with the machine
    # off would stay wrong until something asked MPD to look (2026-09-30).
    try:
        m = Mpd()
        m.raw("update")
        m.close()
    except Exception:
        pass                             # no MPD yet; the watch asks again
    threading.Thread(target=extras.unplug_loop, args=(emit,), daemon=True).start()
    threading.Thread(target=extras.note_loop, daemon=True).start()
    # The music folder itself, for the changes MPD never reports.
    threading.Thread(target=extras.folder_loop, args=(emit,), daemon=True).start()
    try:
        start_relay()
    except Exception:
        pass  # YouTube falls back to direct links, as before the relay
    prev = {}
    paused_at = 0
    while True:
        try:
            m = Mpd()
            m.sock.settimeout(None)  # idle waits as long as it needs to
            changed = []
            while True:
                view = status_view(m)
                view["favoritesChanged"] = "stored_playlist" in changed
                # A rescan finished (a new music folder, a download landed).
                view["libraryChanged"] = "database" in changed
                emit(view)
                extras.note_view(view)      # so the position thread can skip
                if view.get("state") == "pause" and prev.get("state") == "play":
                    try:
                        extras.note_position(view)
                    except Exception:
                        pass
                if view.get("state") in ("play", "pause"):
                    try:
                        books.note(m, view)
                    except Exception:
                        pass
                # A book going on after a pause of 5 min or more steps back
                # a little, to pick up the thread (Settings → Audiobooks).
                if view.get("state") == "pause" and prev.get("state") == "play":
                    paused_at = time.time()
                elif view.get("state") != "pause":
                    if (view.get("state") == "play" and prev.get("state") == "pause"
                            and paused_at and time.time() - paused_at >= 300
                            and view.get("kind") == "book"):
                        back = extras.settings().get("bookRewind", 10)
                        if back and float(view.get("elapsed") or 0) > back:
                            m.raw("seekcur", "-%d" % back)
                            emit({"recovered": "Went back %d s after the pause" % back})
                    if view.get("state") != "play" or prev.get("state") == "pause":
                        paused_at = 0
                try:
                    note = extras.resume_position(m, view, prev)
                except Exception:
                    note = None
                if note:
                    emit({"recovered": note})
                failed = failed_song(view, prev)
                cur = view.get("current") or {}
                prev = {"file": cur.get("file", ""), "pos": view.get("song", -1),
                        "state": view.get("state")}
                if failed:
                    m.sock.settimeout(60)
                    try:
                        note = recover(m, failed)
                    except Exception:
                        note = None
                    m.sock.settimeout(None)
                    if note:
                        emit({"recovered": note})
                        continue       # report the mended state straight away
                changed = [v for k, v in m.raw("idle", "player", "playlist", "options",
                                               "mixer", "stored_playlist", "database")
                           if k == "changed"]
        except BrokenPipeError:
            return  # the card went away
        except Exception as e:
            try:
                emit({"error": "MPD unreachable: %s" % e})
            except BrokenPipeError:
                return
            time.sleep(3)


def cmd_queue():
    m = mpd()
    recs = m.records("playlistinfo")
    m.close()
    titles = load("titles.json", {})
    # Stations in Up next show their names, not their stream addresses.
    for url, st in load("radio-queue.json", {}).items():
        if isinstance(st, dict) and st.get("name"):
            titles.setdefault(url, {"title": st["name"], "artist": "Radio"})
    out({"queue": [song_view(r, titles) for r in recs if r["_type"] == "file"]})


MOST_FOLDERS = 400


def folder_counts(m):
    result = []
    for rec in m.records("lsinfo"):
        if rec["_type"] != "directory":
            continue
        name = rec["directory"]
        try:
            n = int(m.dict("count", "base", name).get("songs", 0))
        except RuntimeError:
            n = 0
        result.append(dict({"name": name, "count": n}, **book_info(m, name)))
    # And the folders MPD cannot see: it indexes sound, so a folder holding
    # only novels is not in its database and would appear nowhere -- not here,
    # and not in Settings -> Book folders, which is built from this same list
    # (his report, 2026-09-30: ~/Music/Book, one .epub, never showed up).
    import books
    known = {f["name"] for f in result}
    try:
        names = sorted(os.listdir(MUSIC_DIR))
    except OSError:
        names = []
    for name in names:
        if len(result) >= MOST_FOLDERS:
            break
        if name in known or name.startswith("."):
            continue
        if not os.path.isdir(os.path.join(MUSIC_DIR, name)):
            continue
        if books.holds_text(name):
            result.append(dict({"name": name, "count": 0}, **book_info(m, name)))
    result.sort(key=lambda f: f["name"].lower())
    return result


def read_part(rel):
    """How far through a novel he has read, as the reader wrote it down. It
    is in the library, so it is the same after a restart or a shutdown."""
    try:
        import library
        full = os.path.join(MUSIC_DIR, rel)
        if not os.path.isfile(full):
            return 0
        record = library.get(library.text_id(full))
        return round(min(1.0, float(record["read"].get("percent") or 0)), 3)
    except Exception:
        return 0


def book_info(m, folder):
    """{"book": True, "heard", "chapters"} for a folder that is an audiobook."""
    import books
    try:
        files = books.files_of(m, folder)
        text = books.has_text(folder)
        if not books.is_book(folder, files):
            if not files and books.in_book_folders(folder):
                # One novel and nothing else, in a Book folder: a book to read.
                texts = books.text_files(folder)
                if len(texts) == 1 and not books.has_subfolders(folder):
                    rel = folder + "/" + texts[0]
                    return {"book": True, "textOnly": True, "text": True, "chapters": 0,
                            "heard": 0, "part": read_part(rel), "file": rel}
                # Several books (novels, audiobook folders): a shelf, opened
                # like a folder rather than played.
                return {"shelf": True}
            return {}
        return dict({"book": True, "text": text},
                    **books.summary(folder, files, books.listen_of(files)))
    except Exception:
        return {}


# ---------------------------------------------------------------- favorites
#
# Starred songs live in an MPD stored playlist, so starring a song from a
# folder copies nothing. An online song cannot stay online (YouTube links
# expire), so starring one downloads it into ~/Music/Favorites first. That
# folder is hidden from the folder list: its songs show under Favorites.

FAV = "Favorites"


def fav_files(m):
    try:
        return [v for k, v in m.raw("listplaylist", FAV) if k == "file"]
    except RuntimeError:
        return []  # the playlist does not exist until the first star


def cmd_config():
    """Where the card found MPD, the music folder and the beat FIFO."""
    out({"host": MPD_HOST, "port": MPD_PORT, "musicDir": MUSIC_DIR,
         "fifo": FIFO, "fifoFound": bool(FIFO) and os.path.exists(FIFO)})


def cmd_home():
    m = mpd()
    folders = [f for f in folder_counts(m) if f["name"] != FAV]
    # Settings → Library: folders can be hidden from the home page (they
    # stay in the list, marked, so Settings can show them back) and sorted
    # by when they were last played.
    hidden = setting("hiddenFolders", []) or []
    played = load("played.json", {})
    for f in folders:
        f["hidden"] = f["name"] in hidden
        f["played"] = played.get(f["name"], 0)
    if setting("folderSort", "name") == "recent":
        folders.sort(key=lambda f: -f["played"])
    favs = fav_files(m)
    m.close()
    out({"folders": folders, "stars": load("stars.json", []),
         "favorites": favs, "last": load("last.json", {})})


def cmd_fav_songs():
    m = mpd()
    try:
        recs = m.records("listplaylistinfo", FAV)
    except RuntimeError:
        recs = []
    m.close()
    out({"songs": [song_view(r, {}) for r in recs if r["_type"] == "file"]})


def cmd_fav_toggle(path):
    check_arg(path)
    # MPD removes from a playlist by position. Two quick clicks (or the card
    # and rmpc) could each read the list, then one removes a position the
    # other has shifted: the wrong song goes. One toggle at a time.
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path("favorites.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        m = mpd()
        files = fav_files(m)
        if path in files:
            m.raw("playlistdelete", FAV, str(files.index(path)))
            starred = False
        else:
            m.raw("playlistadd", FAV, path)
            starred = True
        files = fav_files(m)
        m.close()
    out({"starred": starred, "favorites": files})


def cmd_play_favorites(start_file=""):
    m = mpd()
    files = fav_files(m)
    if not files:
        m.close()
        fail("No favorites yet: star a song with Alt+Space")
    new_fill_token()
    m.raw("clear")
    m.raw("load", FAV)
    pos = files.index(start_file) if start_file in files else 0
    m.raw("play", str(pos))
    m.close()
    save("last.json", {"type": "favorites", "title": FAV})
    set_now("music")
    out({"ok": True})


def cmd_songs(folder):
    """A folder's own songs and its subfolders (artist → album → songs).
    Playing a folder plays everything under it, subfolders included."""
    import books
    m = mpd()
    recs = m.records("lsinfo", folder)
    dirs = []
    for r in recs:
        if r["_type"] != "directory":
            continue
        try:
            n = int(m.dict("count", "base", r["directory"]).get("songs", 0))
        except RuntimeError:
            n = 0
        # n == 0 and text inside: a folder of novels, which MPD counts as
        # empty (the same blind spot as folder_counts, one level down).
        if n > 0 or books.holds_text(r["directory"]):
            dirs.append(dict({"name": r["directory"], "label": os.path.basename(r["directory"]),
                              "count": n}, **book_info(m, r["directory"])))
        if len(dirs) >= 200:
            break
    m.close()
    songs = [song_view(r, {}) for r in recs if r["_type"] == "file"]
    # Novels sitting in the folder (MPD does not list them): each one a book.
    texts = [{"name": folder + "/" + n, "label": os.path.splitext(n)[0], "book": True,
              "textOnly": True, "text": True, "file": folder + "/" + n,
              "part": read_part(folder + "/" + n)}
             for n in books.text_files(folder)]
    out({"folder": folder, "dirs": dirs, "songs": songs, "texts": texts})


def cmd_discover():
    # Radio Browser's random order gives a fresh handful each time. Ask for a
    # few extra and keep ones from different countries so it feels worldwide.
    pool = stations(order="random", limit=40, has_extended_info=None)
    # The directory caches its "random" order for a while; shuffle here too
    # so ↻ really brings new stations.
    random.shuffle(pool)
    # One per country, so it feels worldwide; then test those and keep only
    # stations that start well (good or ok). Slow and dead ones never show.
    candidates, countries = [], set()
    for s in pool:
        if s["country"] not in countries:
            countries.add(s["country"])
            candidates.append(s)
    candidates = candidates[:16]
    # Settings → Radio → Discover leans towards: stations from those
    # countries come first, and two of the three shown are theirs when they
    # start well; one stays from anywhere, so it is still a discovery.
    lean = setting("radioCountries", []) or []
    home = []
    for code in lean[:10]:
        try:
            home += stations(countrycode=code, order="random", limit=max(4, 12 // len(lean)))
        except RuntimeError:
            pass
    random.shuffle(home)
    home = home[:10]
    if home:
        urls_home = {s["url"] for s in home}
        candidates = home + [s for s in candidates if s["url"] not in urls_home][:6]
    urls = [s["url"] for s in candidates]
    # Stop as soon as three have started well; no need to wait for the rest.
    tiers = probe_many(urls, deadline=6.0, enough=lambda t: sum(
        1 for u in urls if t.get(u) in ("good", "ok")) >= (6 if home else 3))
    fine = [s for s in rank(candidates, tiers) if s["tier"] in ("good", "ok")]
    if home:
        ours = [s for s in fine if s["url"] in urls_home]
        rest = [s for s in fine if s["url"] not in urls_home]
        picked = ours[:2] + rest[:1]
        picked += [s for s in fine if s not in picked][:3 - len(picked)]
    else:
        picked = fine[:3]
    random.shuffle(picked)          # a fresh order, not always the fastest first
    out({"stations": picked})


# Radio Browser's most common tags are mostly noise ("radio", "fm", place
# names), so the genre row is a short list picked by hand.
GENRES = ["lofi", "chillout", "jazz", "pop", "rock", "classical", "electronic",
          "hip hop", "ambient", "bollywood", "jpop", "kpop", "anime", "lounge",
          "soundtrack", "news"]


# ---------------------------------------------------------------- kept lists
#
# The World page and every genre or country list are kept for as long as
# the speed results (Settings → Test radio speed: 5 or 10 days), so opening
# one is instant and already sorted. ↻ on a list fetches it fresh.

def kept_list(key, fetch, fresh=False):
    lists = load("lists.json", {})
    hit = lists.get(key)
    if (not fresh and isinstance(hit, dict)
            and time.time() - hit.get("at", 0) < retest_days() * DAY):
        return hit["data"], hit["at"]
    data = fetch()
    lists[key] = {"at": time.time(), "data": data}
    if len(lists) > 80:            # every genre and many countries, at most
        lists = dict(sorted(lists.items(), key=lambda kv: kv[1].get("at", 0))[-60:])
    save("lists.json", lists)
    return data, lists[key]["at"]


def cmd_world(fresh=""):
    def fetch():
        countries = radio("countries", order="stationcount", reverse="true",
                          hidebroken="true")
        return sorted(
            [{"code": c.get("iso_3166_1", ""), "name": c.get("name", ""),
              "count": c.get("stationcount", 0)}
             for c in countries if c.get("stationcount", 0) >= 20],
            key=lambda c: c["name"])
    countries, _ = kept_list("world", fetch, fresh == "fresh")
    hidden = setting("hiddenGenres", []) or []
    out({"tags": [g for g in GENRES if g not in hidden], "genres": GENRES,
         "countries": countries})


def cmd_stations(by, value, fresh=""):
    key = {"country": "countrycode", "tag": "tag", "name": "name"}[by]
    # Best reviewed first from the directory; the card then tests them all
    # (probe-stations) and they re-sort by speed.
    params = {key: value, "order": "votes", "reverse": "true"}
    if by == "tag":
        params["tagExact"] = "true"
    found, at = kept_list("%s:%s" % (by, value.lower()), lambda: stations(**params),
                          fresh == "fresh")
    out({"stations": rank([dict(s) for s in found], known_quality()), "at": at})


# ---------------------------------------------------------------- test again now

def radio_known_urls():
    """Every station the card has a list of, plus the starred ones."""
    urls = []
    for entry in load("lists.json", {}).values():
        data = entry.get("data") if isinstance(entry, dict) else None
        if isinstance(data, list):
            urls += [s.get("url") for s in data if isinstance(s, dict) and s.get("url")]
    urls += [s.get("url") for s in load("stars.json", []) if isinstance(s, dict)]
    return [u for u in dict.fromkeys(urls) if isinstance(u, str) and u]


def cmd_radio_retest():
    """Settings → Test radio speed now: test every known station again, in
    the background; progress in retest.json."""
    state = load("retest.json", {})
    if state.get("state") == "running" and time.time() - state.get("at", 0) < 900:
        out({"ok": True, "already": True})
        return
    total = len(radio_known_urls())
    save("retest.json", {"state": "running", "done": 0, "total": total, "at": time.time()})
    subprocess.Popen([sys.executable, __file__, "radio-retest-run"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    out({"ok": True, "total": total})


def cmd_radio_retest_run():
    urls = radio_known_urls()
    done = 0
    for i in range(0, len(urls), 40):
        batch = urls[i:i + 40]
        probe_many(batch, deadline=12.0, force=True)
        done += len(batch)
        save("retest.json", {"state": "running", "done": done, "total": len(urls),
                             "at": time.time()})
    save("retest.json", {"state": "done", "done": done, "total": len(urls), "at": time.time()})


def cmd_radio_info():
    """For the Settings row: how often, when last, and a running test."""
    tested = [q.get("at", 0) for q in load("quality.json", {}).values() if isinstance(q, dict)]
    out({"days": retest_days(), "stations": len(tested),
         "lastTested": max(tested) if tested else 0, "retest": load("retest.json", {})})


def cmd_search(query):
    """Local songs and radio stations. YouTube is separate because it is slow."""
    m = mpd()
    try:
        recs = m.raw("search", "(any contains %s)" % m.quote(query), "window", "0:8")
    except RuntimeError:
        recs = []
    m.close()
    songs, cur = [], None
    for k, v in recs:
        if k == "file":
            cur = {"file": v}
            songs.append(cur)
        elif cur is not None:
            cur[k] = v
    try:
        found = rank(stations(name=query, order="clickcount", reverse="true", limit=6),
                     known_quality())
    except RuntimeError:
        found = []
    out({"songs": [song_view(s, {}) for s in songs], "stations": found})


def cmd_ytsearch(query):
    n = setting("searchResults", 6)
    n = n if n in (6, 10, 15) else 6
    if setting("youtubeMusic", False):
        # YouTube Music's song search: songs only, no covers, talk or
        # reaction videos. Its entries carry no channel or length.
        target = "https://music.youtube.com/search?%s#songs" % urllib.parse.urlencode({"q": query})
        data = json.loads(ytdlp("-J", "--flat-playlist", "-I", "1:%d" % n, "--", target,
                                timeout=30))
        videos = []
        for e in data.get("entries") or []:
            if VIDEO_ID.match(e.get("id") or ""):
                v = yt_entry_view(e)
                v["url"] = "https://www.youtube.com/watch?v=" + e["id"]
                v["artist"] = v["artist"] or "YouTube Music"
                videos.append(v)
        out({"videos": videos})
        return
    data = json.loads(ytdlp("-J", "--flat-playlist", "--", "ytsearch%d:%s" % (n, query), timeout=30))
    out({"videos": [yt_entry_view(e) for e in data.get("entries") or []]})


# His own YouTube, once signed in (Settings → YouTube sign-in): the Liked
# list and his playlists. Both need the browser's sign-in; without it
# YouTube answers with a sign-in page, so the card says so instead.
MY_PLAYLISTS = "https://www.youtube.com/feed/playlists"
LIKED = "https://www.youtube.com/playlist?list=LL"


def cmd_yt_mine():
    if not auth_args():
        fail("Sign in first: Settings → YouTube → YouTube sign-in")
    lists = [{"title": "Liked videos", "url": LIKED, "count": 0}]
    try:
        data = json.loads(ytdlp("-J", "--flat-playlist", "--", MY_PLAYLISTS, timeout=40))
    except RuntimeError as e:
        text = str(e)
        if "401" in text or "does not exist" in text:
            # yt-dlp copies the browser's cookie file but not its journal
            # (cookies.sqlite-wal), where a sign-in made minutes ago still
            # sits; the browser folds it in when it closes.
            fail("YouTube did not accept the browser's sign-in. If you signed in or "
                 "switched account there recently, close that browser fully once and "
                 "try again; otherwise sign in to YouTube there again. (%s)" % text)
        fail("Could not read your playlists: %s" % text)
    for e in data.get("entries") or []:
        pid = e.get("id") or ""
        # Liked is already first. YouTube's own Mixes (RD…) never end and
        # change each time, so only his playlists (PL…) and Watch later stay.
        if pid != "WL" and not re.match(r"^PL[A-Za-z0-9_-]{8,62}$", pid):
            continue
        lists.append({"title": e.get("title") or "Playlist",
                      "url": "https://www.youtube.com/playlist?list=" + pid,
                      "count": int(e.get("playlist_count") or 0)})
    out({"lists": lists[:60]})


def cmd_link(url):
    data = json.loads(ytdlp("-J", "--flat-playlist", "--", url, timeout=30))
    entries = data.get("entries")
    if entries is not None:
        out({"type": "playlist", "title": data.get("title") or "YouTube playlist",
             "count": len(entries), "url": url})
    else:
        out({"type": "video", "url": url, **yt_entry_view(data)})


def add_at(m, uri, mode):
    """mode: replace (clear first), next (after current) or end."""
    if mode == "next":
        st = m.dict("status")
        if "song" in st:
            return m.dict("addid", uri, str(int(st["song"]) + 1)).get("Id")
    return m.dict("addid", uri).get("Id")


def cmd_play_folder(folder, start_file=""):
    check_arg(folder)
    check_arg(start_file)
    new_fill_token()
    m = mpd()
    m.raw("clear")
    m.raw("add", folder)
    shuffled = setting("shuffleFolders", False)
    if shuffled:
        m.raw("shuffle")
    pos = 0
    if start_file:
        for r in m.records("playlistinfo"):
            if r.get("file") == start_file:
                pos = int(r.get("Pos", 0))
                break
        if shuffled and pos:
            # The song clicked plays first; the rest follow in shuffled order.
            m.raw("move", str(pos), "0")
            pos = 0
    m.raw("play", str(pos))
    m.close()
    save("last.json", {"type": "folder", "folder": folder, "title": folder})
    played = load("played.json", {})
    played[folder.split("/")[0]] = time.time()
    save("played.json", dict(sorted(played.items(), key=lambda kv: kv[1])[-200:]))
    set_now("music")
    out({"ok": True})


def cmd_add_song(path, mode):
    check_arg(path)
    if mode == "replace" and not is_stream(path):
        # A song picked from a book's folder is a chapter of that book: the
        # book plays from there, in order, keeping its place (books.py).
        import books
        folder = os.path.dirname(path)
        m = mpd()
        try:
            files = books.files_of(m, folder) if folder else []
        finally:
            m.close()
        if folder and books.is_book(folder, files) and not path.lower().endswith(".m4b"):
            import library
            books.cmd_play_book(folder, library.chapter_key(path))
            return
        if path.lower().endswith(".m4b") and folder and books.is_book(folder, files):
            books.cmd_play_book(folder)
            return
    m = mpd()
    if mode == "replace":
        new_fill_token()
        m.raw("clear")
    song_id = add_at(m, path, mode)
    if mode == "replace" and song_id:
        m.raw("playid", song_id)
    m.close()
    if mode == "replace":
        set_now("music")
    out({"ok": True})


def playable_station(s):
    """Stations come from Radio Browser, which anyone can add to. Only
    network streams, never a local file or another scheme, and never an
    address inside this computer or the local network."""
    url = str(s.get("url", "")) if isinstance(s, dict) else ""
    p = urllib.parse.urlparse(url)
    return p.scheme in ("http", "https") and "\n" not in url and "\r" not in url \
        and public_host(p.hostname)


def station_group(chosen, given):
    """The stations to queue around `chosen`, in the order given.

    A station tested lately already passed the address check (a private
    address tests as "bad"), so only untested ones are looked up, all at
    once. Stations known not to work are left out, so next skips them. The
    tier comes from this machine's own test results, never from the caller.
    """
    import concurrent.futures as cf
    known = known_quality()
    keep = {}
    untested = []
    for x in given:
        url = x.get("url")
        if not isinstance(url, str) or "\n" in url or "\r" in url:
            continue
        if url == chosen["url"]:
            keep[url] = True
        elif known.get(url) in ("good", "ok", "slow"):
            keep[url] = urllib.parse.urlparse(url).scheme in ("http", "https")
        elif url not in known:
            untested.append(x)
    if untested:
        with cf.ThreadPoolExecutor(max_workers=16) as pool:
            for x, ok in zip(untested, pool.map(playable_station, untested)):
                keep[x["url"]] = ok
    group = [x for x in given if keep.get(x.get("url"))]
    return group if any(x["url"] == chosen["url"] for x in group) else [chosen]


def station_name(url):
    """The name of a station in the queue, from the list it was played from."""
    return (load("radio-queue.json", {}).get(url) or {}).get("name", "")


def cmd_play_station(station_json, mode="replace", list_json=""):
    """Play a station. With the list it was picked from (in the order the
    card shows), the whole list goes into the queue, so next and previous
    move between stations, from the card, the media keys or anywhere else.
    """
    s = json.loads(station_json)
    if not playable_station(s):
        fail("Not a radio stream: " + str(s.get("url", ""))[:80])
    check_arg(s["url"])
    group = [s]
    if mode == "replace" and list_json:
        try:
            given = [x for x in json.loads(list_json) if isinstance(x, dict)][:60]
        except ValueError:
            given = []
        group = station_group(s, given)
    if mode == "replace":
        new_fill_token()
    m = mpd()
    if mode == "replace":
        m.raw("clear")
        chosen = None
        seen = set()
        for x in group:
            if x["url"] in seen:
                continue
            seen.add(x["url"])
            song_id = m.dict("addid", check_arg(x["url"])).get("Id")
            if x["url"] == s["url"]:
                chosen = song_id
        if chosen:
            m.raw("playid", chosen)
    else:
        add_at(m, s["url"], mode)
    m.close()
    names = {} if mode == "replace" else load("radio-queue.json", {})
    for x in group:
        names[x["url"]] = {"name": str(x.get("name", ""))[:120], "uuid": x.get("uuid", "")}
    save("radio-queue.json", names)
    if mode == "replace":
        save("last.json", {"type": "station", "station": s, "title": s["name"]})
        set_now("radio", station=s["name"])
    count_click(s.get("uuid"))
    out({"ok": True, "stations": len(group)})


def cmd_play_youtube(url, mode="replace", fill=False):
    """Play a video or a whole playlist.

    A playlist starts on its first song straight away; the rest are resolved
    by a detached child (`yt-fill`) so the card never waits on all of them.
    """
    if fill:
        token, entries = mode, json.loads(url)
        m = None
        for n, e in enumerate(entries):
            # Pace the rest of a playlist so it does not trip the rate limit.
            if n:
                time.sleep(4)
            if not fill_still_wanted(token):
                break
            try:
                stream, meta = yt_stream(e)
            except Exception:
                continue
            if not fill_still_wanted(token):
                break
            remember_title(stream, meta)
            m = m or mpd()
            m.raw("add", stream)
        if m:
            m.close()
        return

    # A plain video needs one request, not two: only playlists get listed first.
    data, entries = {}, None
    if is_playlist_link(url):
        data = json.loads(ytdlp("-J", "--flat-playlist", "--", url, timeout=30))
        entries = data.get("entries")
    watch = [yt_entry_view(e)["url"] for e in entries] if entries is not None else [url]
    if not watch:
        fail("That playlist is empty")
    stream, meta = yt_stream(watch[0])
    check_arg(stream)
    remember_title(stream, meta)
    m = mpd()
    if mode == "replace":
        m.raw("clear")
    song_id = add_at(m, stream, mode)
    if mode == "replace" and song_id:
        m.raw("playid", song_id)
    m.close()
    if mode == "replace":
        title = data.get("title") or meta["title"]
        save("last.json", {"type": "youtube", "url": url, "title": title})
        set_now("youtube")
    token = new_fill_token() if mode == "replace" else load("fill.json", {}).get("token", "")
    if len(watch) > 1:
        subprocess.Popen([sys.executable, __file__, "yt-fill", token, json.dumps(watch[1:])],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    out({"ok": True, "title": meta["title"], "more": len(watch) - 1})


def cmd_resume():
    m = mpd()
    st = m.dict("status")
    if int(st.get("playlistlength", 0)) > 0:
        m.raw("play")
        m.close()
        out({"ok": True})
        return
    m.close()
    last = load("last.json", {})
    t = last.get("type")
    if t == "folder":
        cmd_play_folder(last["folder"])
    elif t == "station":
        cmd_play_station(json.dumps(last["station"]))
    elif t == "youtube":
        cmd_play_youtube(last["url"])
    elif t == "favorites":
        cmd_play_favorites()
    elif t == "book":
        import books
        books.cmd_play_book(last["folder"])
    else:
        fail("Nothing to resume yet")


def cmd_star(station_json):
    s = json.loads(station_json)
    stars = load("stars.json", [])
    if any(x.get("url") == s.get("url") for x in stars):
        stars = [x for x in stars if x.get("url") != s.get("url")]
        starred = False
    else:
        stars.insert(0, s)
        starred = True
    save("stars.json", stars)
    out({"starred": starred, "stars": stars})


# ---------------------------------------------------------------- beat feed
#
# The dancer hears the beat through an MPD "fifo" output, which copies all
# the audio into a pipe. Left on, it costs MPD about as much again as
# playing (measured 2026-09-22: 0.37% -> 0.83% of a core), even with no
# dancer to read it. So the card switches it on only while a dancer is on
# screen and off otherwise (MPD's enableoutput/disableoutput; MPD
# remembers). Nothing in mpd.conf changes and the music is not interrupted.

def cmd_beat_feed(state):
    if state not in ("on", "off"):
        fail("beat-feed takes on or off")
    m = mpd()
    changed = []
    for o in m.records("outputs", starts=("outputid",)):
        if o.get("plugin") != "fifo":
            continue
        want = "1" if state == "on" else "0"
        if o.get("outputenabled") != want:
            m.raw("enableoutput" if state == "on" else "disableoutput", check_arg(o["outputid"]))
            changed.append(o.get("outputname", ""))
    m.close()
    out({"ok": True, "state": state, "changed": changed})


def cmd_mpd(*args):
    m = mpd()
    try:
        m.raw(*args)
    except RuntimeError as e:
        m.close()
        fail(e)
    m.close()
    out({"ok": True})


# ---------------------------------------------------------------- soft pause
#
# Settings → Playback → Soft pause: the music fades out over half a second
# before it pauses and fades back in when it resumes, instead of cutting.
# It turns MPD's own volume (the PipeWire stream, not the system volume)
# down and back; the volume it had is written down first, so a fade cut
# short (the card closed, the computer slept) is put right on the next one.

FADE_OUT, FADE_IN, FADE_STEPS = 0.4, 0.6, 10


def ramp(m, start, end, seconds):
    for i in range(1, FADE_STEPS + 1):
        m.raw("setvol", str(int(round(start + (end - start) * i / FADE_STEPS))))
        time.sleep(seconds / FADE_STEPS)


def cmd_pause():
    m = mpd()
    st = m.dict("status")
    state = st.get("state")
    try:
        volume = int(st.get("volume", -1))
    except ValueError:
        volume = -1
    if not setting("softPause", False) or volume < 0 or state not in ("play", "pause"):
        m.raw("pause")
        m.close()
        out({"ok": True})
        return
    kept = load("fade.json", {})
    if isinstance(kept.get("volume"), int) and 0 <= kept["volume"] <= 100:
        volume = kept["volume"]          # an earlier fade was cut short
    save("fade.json", {"volume": volume})
    try:
        if state == "play":
            ramp(m, int(st.get("volume")), 0, FADE_OUT)
            m.raw("pause", "1")
            m.raw("setvol", str(volume))
        else:
            m.raw("setvol", "0")
            m.raw("pause", "0")
            ramp(m, 0, volume, FADE_IN)
    finally:
        try:
            m.raw("setvol", str(volume))
        except Exception:
            pass
        m.close()
        try:
            os.remove(state_path("fade.json"))
        except OSError:
            pass
    out({"ok": True})


SLEEP_FADE = 10.0


def cmd_sleep_fade():
    """The sleep timer's end: the sound fades out over 10 s, then pauses,
    and the volume goes back for next time (Settings → Playback)."""
    m = mpd()
    st = m.dict("status")
    try:
        volume = int(st.get("volume", -1))
    except ValueError:
        volume = -1
    if st.get("state") != "play":
        m.close()
        out({"ok": True})
        return
    if volume <= 0 or not setting("sleepFade", True):
        m.raw("pause", "1")
        m.close()
        out({"ok": True})
        return
    save("fade.json", {"volume": volume})
    try:
        # By the clock, not by steps: MPD's answers take time too, and the
        # chapter-end sleep counts on the fade ending when it says.
        began = time.monotonic()
        while True:
            done = min(1.0, (time.monotonic() - began) / SLEEP_FADE) if SLEEP_FADE else 1.0
            m.raw("setvol", str(int(round(volume * (1 - done)))))
            if done >= 1.0:
                break
            time.sleep(0.25)
            if m.dict("status").get("state") != "play":
                break                   # paused or changed by hand meanwhile
        m.raw("pause", "1")
    finally:
        try:
            m.raw("setvol", str(volume))
        except Exception:
            pass
        m.close()
        try:
            os.remove(state_path("fade.json"))
        except OSError:
            pass
    out({"ok": True})


def cmd_toggle(mode):
    m = mpd()
    st = m.dict("status")
    m.raw(mode, "0" if st.get(mode) == "1" else "1")
    m.close()
    out({"ok": True})


def safe_folder(name):
    name = name.strip().replace("/", "-")
    if not name or name in (".", ".."):
        fail("Folder name is empty")
    # A leading dot would make a hidden folder you never see in the card.
    if name.startswith("."):
        fail("Folder names cannot start with a dot")
    return name


def cmd_mkdir(name):
    name = safe_folder(name)
    os.makedirs(os.path.join(MUSIC_DIR, name), exist_ok=True)
    out({"ok": True, "folder": name})


def download_target(source):
    """Only a YouTube link is downloaded as a link. Anything else, including
    the song title a station sends (free text, even if it looks like a url),
    becomes a YouTube search, so an outsider cannot pick the download site."""
    if source.startswith(("https://", "http://")) and is_youtube(source):
        return source
    return "ytsearch1:" + source


def cmd_download(source, folder):
    """Start a download in the background. `source` is a url or a search.

    Downloading into Favorites also stars the song once it has landed.
    """
    folder = safe_folder(folder)
    # Pressing the same thing again while it is still going must not start a
    # second one beside the first: that only makes what he asked for slower
    # (his report, 2026-09-25). The same song is simply the one already
    # running; a different one waits for its turn to be asked for again.
    import jobs
    busy = jobs.downloading()
    if busy:
        same = str(busy.get("title", "")) == str(source)
        out({"ok": False, "busy": True, "title": busy.get("title", ""),
             "percent": busy.get("percent", 0),
             "error": ("That one is already downloading · %d%%" % busy.get("percent", 0))
                      if same else
                      ("Already downloading %s · %d%%. Let it finish first."
                       % (str(busy.get("title", ""))[:40], busy.get("percent", 0)))})
        return
    os.makedirs(os.path.join(MUSIC_DIR, folder), exist_ok=True)
    save("download.json", {"state": "starting", "percent": 0, "title": source,
                           "folder": folder, "at": time.time()})
    subprocess.Popen([sys.executable, __file__, "download-run", source, folder],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    out({"ok": True})


def cmd_download_run(source, folder):
    # Checked again here, not only in cmd_download. Every "-run" command is
    # in the same table as the rest and can be called on its own, so the half
    # that does the work cannot lean on the half that started it: this one
    # decides where a file lands (2026-09-24).
    folder = safe_folder(folder)
    check_arg(source)
    target = download_target(source)
    template = os.path.join(MUSIC_DIR, folder, "%(title)s.%(ext)s")
    import extras
    cmd = ["yt-dlp", "--newline", "--no-playlist"] + auth_args() + extras.download_args() + [
           "--progress-template", "download:%(progress._percent_str)s",
           "--print", "before_dl:TITLE %(title)s",
           "--print", "after_move:PATH %(filepath)s",
           "-o", template, "--", target]
    title, saved, problem = source, "", ""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    last_write = 0
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("ERROR:"):
            problem = line[:400]           # why it failed, for the card's Copy
        if line.startswith("TITLE "):
            title = line[6:]
        elif line.startswith("PATH "):
            saved = line[5:]
        elif line.endswith("%"):
            try:
                pct = float(line.rstrip("%"))
            except ValueError:
                continue
            if time.time() - last_write > 0.5:
                save("download.json", {"state": "downloading", "percent": pct,
                                       "title": title, "folder": folder,
                                       "at": time.time(), "pid": os.getpid()})
                last_write = time.time()
    ok = proc.wait() == 0
    if ok:
        try:
            m = Mpd()
            m.raw("update", folder)
            if folder == FAV and saved:
                # MPD only accepts songs it has indexed; wait for the scan.
                for _ in range(60):
                    if "updating_db" not in m.dict("status"):
                        break
                    time.sleep(0.5)
                rel = os.path.relpath(saved, MUSIC_DIR)
                if rel not in fav_files(m):
                    m.raw("playlistadd", FAV, rel)
            m.close()
        except Exception:
            pass
    save("download.json", {"state": "done" if ok else "failed", "percent": 100 if ok else 0,
                           "title": title, "folder": folder, "at": time.time(),
                           "error": "" if ok else problem})


def stop_with_children():
    """When the card stops a call (an outdated search), take its yt-dlp
    down too. Downloads and playlist fills run in their own sessions and
    are not affected."""
    import signal
    try:
        os.setpgid(0, 0)
    except OSError:
        return

    def on_term(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.killpg(0, signal.SIGTERM)
    signal.signal(signal.SIGTERM, on_term)


def main(argv):
    if not argv:
        fail("no command")
    stop_with_children()
    cmd, args = argv[0], argv[1:]
    table = {
        "status": cmd_status,
        "watch": cmd_watch,
        "config": cmd_config,
        "queue": cmd_queue,
        "home": cmd_home,
        "songs": cmd_songs,
        "discover": cmd_discover,
        "world": cmd_world,
        "stations": cmd_stations,
        "search": cmd_search,
        "ytsearch": cmd_ytsearch,
        "link": cmd_link,
        "play-folder": cmd_play_folder,
        "add-song": cmd_add_song,
        "play-station": cmd_play_station,
        "probe-stations": cmd_probe_stations,
        "radio-retest": cmd_radio_retest,
        "radio-retest-run": cmd_radio_retest_run,
        "radio-info": cmd_radio_info,
        "beat-feed": cmd_beat_feed,
        "play-youtube": cmd_play_youtube,
        "yt-fill": lambda token, entries: cmd_play_youtube(entries, token, fill=True),
        "resume": cmd_resume,
        "star": cmd_star,
        "yt-mine": cmd_yt_mine,
        "toggle": cmd_toggle,
        "mkdir": cmd_mkdir,
        "download": cmd_download,
        "download-run": cmd_download_run,
        "click-run": cmd_click_run,
        "fav-songs": cmd_fav_songs,
        "art": cmd_art,
        "fav-toggle": cmd_fav_toggle,
        "play-favorites": cmd_play_favorites,
        "pause": cmd_pause,
        "pause-on": lambda: cmd_mpd("pause", "1"),
        # The sleep timer's "after this song" is MPD's single oneshot.
        "single": lambda v: cmd_mpd("single", v) if v in ("0", "1", "oneshot")
        else fail("single takes 0, 1 or oneshot"),
        "next": lambda: cmd_mpd("next"),
        "previous": lambda: cmd_mpd("previous"),
        "playpos": lambda pos: cmd_mpd("play", pos),
        "seek": lambda secs: cmd_mpd("seekcur", secs),
        # Relative: "-10" back, "30" ahead (a book's ⟲ and ⟳).
        "seek-by": lambda secs: cmd_mpd("seekcur", ("%+d" % int(secs)))
        if re.match(r"^-?\d{1,4}$", secs) else fail("seek-by takes whole seconds"),
        "sleep-fade": cmd_sleep_fade,
    }
    fn = table.get(cmd)
    if fn is None:
        # Settings, lyrics, queue editing and the Inbox live in extras.py.
        import extras
        fn = extras.COMMANDS.get(cmd)
    if fn is None:
        fail("unknown command " + cmd)
    try:
        fn(*args)
    except TypeError as e:
        fail("bad arguments for %s: %s" % (cmd, e))
    except Exception as e:
        fail(e)


if __name__ == "__main__":
    main(sys.argv[1:])
