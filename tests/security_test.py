#!/usr/bin/env python3
"""Security tests for scripts/card.py.

Run: python3 tests/security_test.py

Nothing here touches the real MPD, network or ~/Music: MPD is replaced by a
fake that records the exact bytes sent, and subprocess by a recorder. The
data under test is what an outsider controls: station names and urls from
Radio Browser, the song title a station sends, YouTube titles and links, and
whatever gets typed or pasted into the search box.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import card  # noqa: E402


class FakeMpd(card.Mpd):
    """Speaks just enough protocol: every command answers OK."""

    def __init__(self):
        self.sent = []
        self.f = self
        self.sock = self

    def write(self, data):
        self.sent.append(data.decode())

    def flush(self):
        pass

    def readline(self):
        return b"OK\n"

    def close(self):
        pass


class Silent:
    """Swallow stdout from out() so test output stays readable."""

    def __enter__(self):
        self.old = sys.stdout
        sys.stdout = io.StringIO()
        return sys.stdout

    def __exit__(self, *a):
        sys.stdout = self.old


class MpdQuoting(unittest.TestCase):
    def test_newline_cannot_start_a_second_command(self):
        # A station url is third-party data. With a raw newline in it, MPD
        # would read the rest as a new command, e.g. deleting a playlist.
        m = FakeMpd()
        evil = 'http://x/stream\nrm "Favorites"'
        with self.assertRaises(Exception):
            m.raw("addid", evil)
        self.assertEqual(m.sent, [], "nothing may reach MPD")

    def test_quotes_and_backslashes_stay_inside_the_argument(self):
        m = FakeMpd()
        m.raw("add", 'a"b\\c')
        self.assertEqual(m.sent, ['add "a\\"b\\\\c"\n'])

    def test_search_filter_cannot_be_broken_out_of(self):
        # The search text sits inside a filter expression inside a quoted
        # argument: two layers of escaping.
        m = FakeMpd()
        q = 'x") OR (file != "'
        m.raw("search", "(any contains %s)" % m.quote(q), "window", "0:8")
        line = m.sent[0]
        self.assertEqual(line.count("\n"), 1)
        arg = re.match(r'search "(.*)" "window"', line).group(1)
        inner = arg.replace('\\\\', '\\').replace('\\"', '"')
        self.assertEqual(inner, '(any contains "x\\") OR (file != \\"")')


class StationUrls(unittest.TestCase):
    def play(self, url):
        fake = FakeMpd()
        card.mpd = lambda: fake
        card.count_click = lambda uuid: None
        card.save = lambda *a, **k: None
        card.load = lambda name, default: default
        # No real DNS in tests: radio.example stands for the public internet.
        card.public_host = lambda host: host == "radio.example"
        with Silent() as buf:
            try:
                card.cmd_play_station(json.dumps({"name": "x", "url": url}))
            except SystemExit:
                pass
        return fake.sent, buf.getvalue()

    def test_only_http_streams_are_played(self):
        for url in ["file:///etc/passwd", "/home/someone/.ssh/id_ed25519",
                    "Hindi/some song.mp3", "smb://host/share"]:
            sent, answer = self.play(url)
            self.assertFalse(any("addid" in s for s in sent), url)
            self.assertIn("error", answer, url)

    def test_refused_station_leaves_the_queue_alone(self):
        # Regression, 2026-09-19: a station refused for a line break was
        # refused only after `clear`, which emptied the queue.
        for url in ["http://a/b\nclear", "file:///etc/passwd"]:
            sent, answer = self.play(url)
            self.assertEqual(sent, [], url)
            self.assertIn("error", answer, url)

    def test_normal_station_plays(self):
        sent, _ = self.play("https://radio.example/stream")
        self.assertTrue(any("addid" in s for s in sent))

    def test_station_inside_this_network_is_refused(self):
        # A listing pointing at the router, MPD itself or the YouTube relay.
        for url in ["http://192.168.1.1/admin", "http://127.0.0.1:6600/", "http://localhost/x"]:
            sent, answer = self.play(url)
            self.assertEqual(sent, [], url)
            self.assertIn("error", answer, url)


class YtDlpArguments(unittest.TestCase):
    """A value that starts with "-" must never be read as a yt-dlp option
    (yt-dlp has --exec, which runs a shell command)."""

    def setUp(self):
        self.calls = []

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='{"entries": []}', stderr="")
        self.real_run = card.subprocess.run
        card.subprocess.run = fake_run

    def tearDown(self):
        card.subprocess.run = self.real_run

    def assert_after_separator(self, value):
        cmd = self.calls[-1]
        self.assertIn("--", cmd, cmd)
        self.assertLess(cmd.index("--"), cmd.index(value), cmd)

    def test_link(self):
        evil = "--exec=touch /tmp/pwned"
        with Silent():
            card.cmd_link(evil)
        self.assert_after_separator(evil)

    def test_resolve(self):
        evil = "--exec=touch /tmp/pwned"
        card.load = lambda name, default: default
        try:
            card.yt_stream(evil)
        except Exception:
            pass
        self.assert_after_separator(evil)


class Downloads(unittest.TestCase):
    def test_folder_names_stay_inside_music(self):
        for name in ["/etc", "a/../../b"]:
            folder = card.safe_folder(name)
            path = os.path.realpath(os.path.join(card.MUSIC_DIR, folder))
            self.assertTrue(path.startswith(os.path.realpath(card.MUSIC_DIR) + os.sep), name)
        for name in ["", "  ", ".", "..", "../../.ssh", ".hidden"]:
            with Silent(), self.assertRaises(SystemExit):
                card.safe_folder(name)

    def test_station_title_cannot_pick_the_download_site(self):
        # The "song title" a station sends is free text. If it looks like a
        # url, it must still only become a YouTube search, not a download
        # from wherever it points.
        self.assertEqual(card.download_target("http://evil.example/payload"),
                         "ytsearch1:http://evil.example/payload")
        self.assertEqual(card.download_target("--exec=id"), "ytsearch1:--exec=id")
        self.assertEqual(card.download_target("https://www.youtube.com/watch?v=abc"),
                         "https://www.youtube.com/watch?v=abc")


class PlaylistFill(unittest.TestCase):
    def test_fill_stops_when_something_else_is_played(self):
        # Playing a YouTube playlist adds the rest in the background. If you
        # then start a folder, the old playlist must not keep adding itself.
        with tempfile.TemporaryDirectory() as d:
            card.STATE_DIR = d
            token = card.new_fill_token()
            card.new_fill_token()  # something else started
            self.assertFalse(card.fill_still_wanted(token))


class QmlShowsPlainText(unittest.TestCase):
    def test_every_text_is_plain(self):
        # Station names and video titles are outside data. A Text item in
        # the default AutoText mode renders "<img src=...>" as rich text and
        # would fetch that image.
        for name in ["MusicPanel.qml", "BarWidget.qml"]:
            src = open(os.path.join(HERE, "..", name)).read()
            texts = len(re.findall(r"^\s*Text \{", src, re.M))
            plain = src.count("textFormat: Text.PlainText")
            # The one exception: a book's paragraph, StyledText (no images,
            # no links) built by paraHtml, which escapes every word first.
            styled = src.count("textFormat: rowItem.isPara ? Text.StyledText : Text.PlainText")
            self.assertEqual(texts, plain + styled, name)
            self.assertLessEqual(styled, 1, name)
        src = open(os.path.join(HERE, "..", "MusicPanel.qml")).read()
        body = src[src.index("function paraHtml("):src.index("Timer {", src.index("function paraHtml("))]
        self.assertIn("esc(x[0])", body)
        self.assertNotIn("x[0] +", body)

    def test_book_words_cannot_become_markup(self):
        # paraHtml's esc(), run in Python the same way: nothing a subtitle
        # file says can open a tag.
        esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.assertNotIn("<", esc('<img src="http://x/y.png">'))


class QmlNames(unittest.TestCase):
    def test_no_property_shares_a_name_with_an_id(self):
        # Regression, 2026-09-20: a property called `keys` was shadowed by
        # the Item with id `keys`, so no shortcut matched and the Shortcuts
        # list showed nothing.
        for name in QML_FILES:
            src = open(os.path.join(HERE, "..", name)).read()
            ids = set(re.findall(r"^\s*id:\s*(\w+)", src, re.M))
            props = set(re.findall(r"property\s+\w+\s+(\w+)", src))
            self.assertEqual(ids & props, set(), name)

    def test_no_function_shares_a_name_with_a_property_or_id(self):
        # Regression, 2026-09-21: a new function step() was hidden by the
        # dancer's `property int step`, so radio next/previous did nothing
        # ("Property 'step' ... is not a function" in the shell log).
        for name in ["MusicPanel.qml", "CardService.qml", "BarWidget.qml"]:
            src = open(os.path.join(HERE, "..", name)).read()
            ids = set(re.findall(r"^\s*id:\s*(\w+)", src, re.M))
            props = set(re.findall(r"property\s+\w+\s+(\w+)", src))
            funcs = set(re.findall(r"^\s*function\s+(\w+)\s*\(", src, re.M))
            self.assertEqual(funcs & (props | ids), set(), name)


# Every QML file of the plugin. MusicPanel.qml was split on 2026-09-24, and
# the checks that read it must read all of them or they quietly stop looking.
QML_FILES = sorted(n for n in os.listdir(os.path.join(HERE, "..")) if n.endswith(".qml"))


class QmlNavigation(unittest.TestCase):
    def test_every_nav_target_is_a_real_view(self):
        # Regression, 2026-09-20: activate() knew three nav targets and sent
        # the rest to the station list, so "Import songs" showed "No
        # stations found" for every click.
        # Across every QML file: the screens moved out of MusicPanel.qml on
        # 2026-09-24, and this looked only there.
        whole = "\n".join(open(os.path.join(HERE, "..", n)).read() for n in QML_FILES)
        targets = set(re.findall(r"\{\s*view:\s*\"(\w+)\"", whole))
        views = set(re.findall(r"view === \"(\w+)\"", whole))
        self.assertTrue(targets)
        self.assertEqual(targets - views, set())
        panel = open(os.path.join(HERE, "..", "MusicPanel.qml")).read()
        body = panel[panel.index("row.type === \"nav\") {"):][:400]
        self.assertIn("go(d.view)", body)

    def test_every_screen_the_card_lists_can_really_be_built(self):
        # `omarchy-shell songbook view <name>` opens any screen by name, so the
        # list it validates against must match the screens that exist.
        panel = open(os.path.join(HERE, "..", "MusicPanel.qml")).read()
        named = set(re.findall(r'"(\w+)"', panel[panel.index("property var viewNames"):
                                                 panel.index("function allViews")]))
        named |= set(re.findall(r'"(\w+)"', panel[panel.index("property var settingsViews"):
                                                  panel.index("function goHome")]))
        whole = "\n".join(open(os.path.join(HERE, "..", n)).read() for n in QML_FILES)
        built = set(re.findall(r"view === \"(\w+)\"", whole))
        missing = sorted(built - named - {"pick"})
        self.assertEqual(missing, [], "screens that exist but cannot be opened by name")


class QmlLoadsOnlyLocalImages(unittest.TestCase):
    def test_images_come_from_files(self):
        # An https image inside the shell crashed it once (Omarchy #8026);
        # card.py downloads art and QML may only load a local file.
        found = 0
        for name in QML_FILES:
            src = open(os.path.join(HERE, "..", name)).read()
            for line in re.findall(r"^\s*source:\s*(.+)$", src, re.M):
                found += 1
                self.assertIn('"file://"', line, "%s: %s" % (name, line))
        self.assertTrue(found)

    def test_art_is_only_jpeg_or_png(self):
        with open(card.__file__) as f:
            self.assertIn('startswith(b"\\xff\\xd8")', f.read())


class AlbumArt(unittest.TestCase):
    """Art arrives from outside: YouTube thumbnails and pictures embedded in
    downloaded files. Only a well-formed video id may reach the network, only
    JPEG/PNG bytes are kept, and the cache file name is never taken from the
    outside."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.ART_DIR = os.path.join(self.tmp.name, "art")
        self.fetched = []
        import urllib.request as ur
        self.real_open = ur.urlopen

        def fake_open(req, timeout=0):
            self.fetched.append(req.full_url)
            raise OSError("no network in tests")
        ur.urlopen = fake_open

    def tearDown(self):
        import urllib.request as ur
        ur.urlopen = self.real_open
        self.tmp.cleanup()

    def art_for_stream(self, source):
        fake = FakeMpd()
        fake.dict = lambda cmd, *a: {"file": "https://rr1.googlevideo.com/x"} if cmd == "currentsong" else {}
        card.mpd = lambda: fake
        card.load = lambda name, default: {"https://rr1.googlevideo.com/x": {"source": source}} \
            if name == "titles.json" else default
        with Silent() as buf:
            card.cmd_art()
        return json.loads(buf.getvalue())

    def test_bad_video_ids_never_reach_the_network(self):
        for source in ["https://www.youtube.com/watch?v=../../etc/passwd",
                       "https://www.youtube.com/watch?v=a%2F..%2Fb",
                       "https://www.youtube.com/watch?v=x\nHost: evil",
                       "https://www.youtube.com/watch", ""]:
            self.assertEqual(self.art_for_stream(source), {"art": ""}, source)
        self.assertEqual(self.fetched, [])

    def test_good_id_asks_only_ytimg(self):
        self.art_for_stream("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(self.fetched, ["https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"])

    def test_only_images_are_saved_and_names_are_hashed(self):
        for payload, kept in [(b"<html>not an image", False), (b"\xff\xd8jpegdata", True),
                              (b"\x89PNGdata", True), (b"#!/bin/sh\nrm -rf ~", False)]:
            fake = FakeMpd()
            fake.dict = lambda cmd, *a: {"file": "../../../etc/x.mp3"} if cmd == "currentsong" else {}
            fake.binary = lambda cmd, uri, limit=0, p=payload: (p, "image/jpeg")
            card.mpd = lambda: fake
            with Silent() as buf:
                card.cmd_art()
            art = json.loads(buf.getvalue())["art"]
            self.assertEqual(bool(art), kept, payload)
            if art:
                self.assertEqual(os.path.dirname(art), card.ART_DIR)
                self.assertRegex(os.path.basename(art), r"^[0-9a-f]{16}\.img$")
                os.remove(art)

    def test_oversized_art_is_dropped(self):
        class Huge(FakeMpd):
            def __init__(self):
                super().__init__()
                self.lines = []

            def readline(self):
                if not self.lines:
                    self.lines = [b"size: 99999999\n", b"binary: 4\n", b"x", b"\n", b"OK\n"]
                return self.lines.pop(0)

            def read(self, n):
                return b"\xff\xd8" + b"x" * (n - 2)
        m = Huge()
        data, _ = m.binary("readpicture", "a.mp3", limit=64)
        self.assertEqual(data, b"")


class RadioRequests(unittest.TestCase):
    def test_radio_lookup_builds_a_request(self):
        # Regression, 2026-09-19: a lazy `import urllib.request` inside a
        # function shadowed urllib.parse and broke every station list.
        import urllib.request as ur
        seen = []
        real = ur.urlopen

        def fake_open(req, timeout=0):
            seen.append((req.full_url, req.get_header("User-agent")))
            raise OSError("no network in tests")
        ur.urlopen = fake_open
        try:
            with self.assertRaises(RuntimeError):
                card.radio("stations/search", name="lo fi", hidebroken="true")
        finally:
            ur.urlopen = real
        self.assertTrue(seen)
        self.assertIn("name=lo+fi", seen[0][0])
        self.assertTrue(seen[0][0].startswith("https://"))
        self.assertEqual(seen[0][1], card.USER_AGENT)


class Watcher(unittest.TestCase):
    def test_a_failed_song_is_not_reported_as_mpd_down(self):
        # Regression: MPD's own "error" (a 403, a dead stream) was passed on
        # as `error`, which the card reads as "MPD is not running".
        fake = FakeMpd()
        fake.dict = lambda cmd, *a: {"state": "play", "error": "got HTTP status 403"} \
            if cmd == "status" else {}
        card.load = lambda name, default: default
        view = card.status_view(fake)
        self.assertNotIn("error", view)
        self.assertEqual(view["playError"], "got HTTP status 403")


class BeatReader(unittest.TestCase):
    def test_garbage_audio_does_not_crash(self):
        import array
        import random as r
        sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
        import beat
        for n in [0, 1, 2, 7, 4096]:
            samples = array.array("h", bytes(r.getrandbits(8) for _ in range(n - n % 2)))
            self.assertIsInstance(beat.energy(samples), int)


class Fuzz(unittest.TestCase):
    """Every read-only command, fed hostile text, answers one line of JSON
    and changes nothing in MPD."""

    NASTY = ["", "-", "--exec=id", "\n", "a\nclear", "\x00", "\"", "\\",
             "../../../etc/passwd", "$(id)", "`id`", "<img src=http://x>",
             "%s%s%n", "ʕ･ᴥ･ʔ", "x" * 5000, "\u202e", "file:///etc/passwd"]

    def test_commands_answer_json(self):
        env = dict(os.environ, HOME=tempfile.mkdtemp())  # state goes nowhere real
        script = os.path.join(HERE, "..", "scripts", "card.py")
        for cmd in ["songs", "search", "fav-toggle-dry", "play-station-dry"]:
            for arg in self.NASTY:
                if cmd == "fav-toggle-dry":
                    # would write; check the refusal path only
                    if "\n" not in arg and "\x00" not in arg:
                        continue
                    argv = ["fav-toggle", arg]
                elif cmd == "play-station-dry":
                    argv = ["play-station", json.dumps({"name": "x", "url": arg})]
                    if arg.startswith(("http://", "https://")) and "\n" not in arg:
                        continue  # a valid stream would really play
                else:
                    argv = [cmd, arg]
                if "\x00" in arg:
                    continue  # the OS itself refuses NUL in arguments
                r = subprocess.run([sys.executable, script] + argv, capture_output=True,
                                   text=True, timeout=60, env=env)
                lines = r.stdout.strip().splitlines()
                self.assertEqual(len(lines), 1, (argv, r.stdout, r.stderr))
                json.loads(lines[0])
                self.assertEqual(r.stderr.strip(), "", (argv, r.stderr))


sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import extras  # noqa: E402


class SongLists(unittest.TestCase):
    """The Inbox reads files other services export. They are outside data:
    parse them, never run or open anything they point at."""

    def write(self, name, text):
        d = tempfile.mkdtemp()
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_exportify_csv(self):
        path = self.write("liked.csv", "\ufeff\"Track URI\",\"Track Name\",\"Artist URI(s)\",\"Artist Name(s)\"\n"
                          "\"spotify:track:1\",\"Bones\",\"x\",\"Low Roar,Someone\"\n")
        self.assertEqual(extras.read_song_list(path), [{"title": "Bones", "artist": "Low Roar"}])

    def test_tunemymusic_csv_with_artist_in_title(self):
        path = self.write("lib.csv", "\ufeffTrack name,Artist name,Album,Playlist name,Type,ISRC\n"
                          '"Alex Warren - Ordinary (Lyrics)","","","Mix","Playlist",""\n')
        self.assertEqual(extras.read_song_list(path),
                         [{"title": "Ordinary (Lyrics)", "artist": "Alex Warren"}])

    def test_takeout_csv_with_preamble_and_ids(self):
        path = self.write("playlist.csv", "Playlist Id,Channel Id\nPL1,UC1\n\n"
                          "Video Id,Time Added\ndQw4w9WgXcQ,2024\nnot-an-id!,2024\n")
        songs = extras.read_song_list(path)
        self.assertEqual(songs, [{"title": "", "artist": "",
                                  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}])

    def test_text_and_m3u(self):
        txt = self.write("list.txt", "Low Roar - Bones\n\nAnuv Jain – Husn\nJust A Title\n"
                         "Prateek Kuhad - cold/mess\n")
        self.assertEqual(extras.read_song_list(txt), [
            {"title": "Bones", "artist": "Low Roar"},
            {"title": "Husn", "artist": "Anuv Jain"},
            {"title": "Just A Title", "artist": ""},
            {"title": "cold/mess", "artist": "Prateek Kuhad"}])
        m3u = self.write("mix.m3u", "#EXTM3U\n#EXTINF:170,Low Roar - Bones\n/music/x.mp3\n"
                         "/music/Hindi/Arz Kiya Hai.mp3\n")
        self.assertEqual(extras.read_song_list(m3u), [
            {"title": "Bones", "artist": "Low Roar"},
            {"title": "Arz Kiya Hai", "artist": ""}])

    def test_links_only_to_youtube(self):
        csvp = self.write("x.csv", "Title,URL\nA,https://evil.example/a.sh\nB,file:///etc/passwd\n"
                          "C,https://www.youtube.com/watch?v=dQw4w9WgXcQ\n")
        urls = [s.get("url") for s in extras.read_song_list(csvp)]
        self.assertEqual(urls, [None, None, "https://www.youtube.com/watch?v=dQw4w9WgXcQ"])

    def test_size_and_count_caps(self):
        big = self.write("big.txt", "a - b\n" * 800000)
        with self.assertRaises(ValueError):
            extras.read_song_list(big)
        many = self.write("many.txt", "a - b\n" * 5000)
        self.assertEqual(len(extras.read_song_list(many)), extras.MAX_SONGS)

    def test_only_list_files_are_read(self):
        for name in ["id_ed25519", "notes.md", "script.sh"]:
            path = self.write(name, "Low Roar - Bones\n")
            with Silent() as buf, self.assertRaises(SystemExit):
                extras.cmd_import_read(path)
            self.assertIn("error", buf.getvalue())


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load = self.real_load
        card.save = self.real_save

    real_load = staticmethod(card.__dict__["load"])
    real_save = staticmethod(card.__dict__["save"])

    def tearDown(self):
        self.tmp.cleanup()

    def set(self, key, value):
        with Silent() as buf:
            try:
                extras.cmd_set_setting(key, value)
            except SystemExit:
                pass
        return json.loads(buf.getvalue())

    def test_only_known_keys_and_types(self):
        self.assertIn("error", self.set("__proto__", "true"))
        self.assertIn("error", self.set("notify", '"yes"'))
        self.assertIn("error", self.set("notify", "not json"))
        self.assertEqual(self.set("notify", "true")["settings"]["notify"], True)

    def test_crossfade_only_known_steps(self):
        self.assertIn("error", self.set("crossfade", "999"))

    def test_a_tampered_file_falls_back_to_defaults(self):
        card.save("settings.json", {"notify": "rm -rf", "crossfade": [1], "evil": 1})
        self.assertEqual(extras.settings(), extras.DEFAULTS)


class Lyrics(unittest.TestCase):
    def test_lrc_parsing(self):
        lines = extras.parse_lrc("[00:05.27] second\n[00:00.33] first\nno time\n[01:02] third")
        self.assertEqual([l["line"] for l in lines], ["first", "second", "third"])
        self.assertEqual(lines[2]["t"], 62.0)

    def test_titles_are_cleaned_for_matching(self):
        self.assertEqual(extras.clean_title('Bones (Official Music Video) | Label'), "Bones")
        self.assertEqual(extras.split_artist_title("Low Roar - Bones"), ("Low Roar", "Bones"))

    def test_lookups_only_go_to_lrclib_over_https(self):
        import urllib.request as ur
        seen = []
        real = ur.urlopen

        def fake_open(req, timeout=0):
            seen.append(req.full_url)
            raise OSError("no network in tests")
        ur.urlopen = fake_open
        try:
            with self.assertRaises(extras.Offline):
                extras.lyrics_for("A", "B", 100)
        finally:
            ur.urlopen = real
        self.assertTrue(all(u.startswith("https://lrclib.net/api/") for u in seen), seen)


class QueueEdits(unittest.TestCase):
    def test_positions_must_be_numbers(self):
        for args in [("1; clear",), ("-1",), ("x",)]:
            with Silent() as buf, self.assertRaises(SystemExit):
                extras.cmd_queue_delete(*args)
            self.assertIn("error", buf.getvalue())


class Shortcuts(unittest.TestCase):
    def test_combos(self):
        ok = ["Ctrl+Space", "Alt+Up", "Shift+Return", "?", "Delete", "Ctrl+Alt+K", "F5", "Space"]
        bad = ["d", "7", "Ctrl+rm -rf", "Hyper+X", "Ctrl+", "", "Ctrl+Space\nclear", 42]
        for c in ok:
            self.assertTrue(extras.valid_combo(c), c)
        for c in bad:
            self.assertFalse(extras.valid_combo(c), c)

    def test_saved_keys_are_cleaned(self):
        keys = extras.clean_keys({"download": "Alt+D", "star": "d", "evil": "Ctrl+X", "guide": 5})
        self.assertEqual(keys["download"], "Alt+D")
        self.assertEqual(keys["star"], extras.KEY_DEFAULTS["star"])
        self.assertNotIn("evil", keys)
        self.assertEqual(keys["guide"], "?")

    def test_duplicates_and_unknown_actions_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            card.STATE_DIR = d
            card.load, card.save = Settings.real_load, Settings.real_save
            for value in [{"download": "Alt+Space"},        # clashes with star
                          {"nope": "Ctrl+Q"}, {"download": "q"}]:
                with Silent() as buf:
                    try:
                        extras.cmd_set_setting("keys", json.dumps(value))
                    except SystemExit:
                        pass
                self.assertIn("error", json.loads(buf.getvalue()), value)
            self.assertFalse(os.path.exists(os.path.join(d, "settings.json")))


class MusicFolder(unittest.TestCase):
    def test_only_the_top_level_line_changes(self):
        text = ('# my mpd\nmusic_directory "~/Music"\nport "6600"\n'
                'audio_output {\n  type "fifo"\n  music_directory "inside"\n}\n')
        new = extras.rewrite_music_dir(text, "~/Songs")
        self.assertIn('music_directory     "~/Songs"', new)
        self.assertIn('music_directory "inside"', new)
        self.assertIn('# my mpd', new)
        self.assertEqual(new.count("music_directory"), 2)

    def test_missing_line_is_added(self):
        self.assertTrue(extras.rewrite_music_dir('port "6600"\n', "/srv/m").startswith(
            'music_directory     "/srv/m"'))

    def test_refusals_touch_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "mpd.conf")
            with open(conf, "w") as f:
                f.write('music_directory "~/Music"\n')
            os.environ["MPD_CONF"] = conf
            try:
                for path in ["/no/such/folder", d + '/a"b', "x\ny"]:
                    with Silent() as buf, self.assertRaises(SystemExit):
                        extras.cmd_set_music_dir(path)
                    self.assertIn("error", buf.getvalue(), path)
                with open(conf) as f:
                    self.assertEqual(f.read(), 'music_directory "~/Music"\n')
                self.assertEqual(os.listdir(d), ["mpd.conf"])  # no backup either
            finally:
                del os.environ["MPD_CONF"]


class Headphones(unittest.TestCase):
    def sink(self, name, port_type, availability="available", bus="usb", api="alsa"):
        return {"name": name, "active_port": "p",
                "properties": {"device.bus": bus, "device.api": api},
                "ports": [{"name": "p", "type": port_type, "availability": availability}]}

    def test_detection(self):
        hp = self.sink("hp", "Headphones")
        self.assertTrue(extras.is_headphones("hp", [hp]))
        self.assertFalse(extras.is_headphones("hp", [self.sink("hp", "Headphones", "not available")]))
        self.assertFalse(extras.is_headphones("tv", [self.sink("tv", "HDMI")]))
        self.assertTrue(extras.is_headphones("bt", [self.sink("bt", "Unknown", api="bluez5")]))
        self.assertFalse(extras.is_headphones("gone", [hp]))

    def test_pauses_only_when_headphones_go_away(self):
        self.assertTrue(extras.should_pause(True, False))
        for was, now in [(False, True), (False, False), (True, True), (None, False), (True, None)]:
            self.assertFalse(extras.should_pause(was, now), (was, now))


class MusicFolderGuard(unittest.TestCase):
    def test_a_link_to_the_whole_disk_is_caught(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "game", "dosdevices"))
            os.symlink("/", os.path.join(d, "game", "dosdevices", "z:"))
            self.assertTrue(extras.links_everywhere(d).endswith("z:"))
            with Silent() as buf, self.assertRaises(SystemExit):
                extras.cmd_set_music_dir(d)
            self.assertIn("whole disk", buf.getvalue())

    def test_plain_music_folder_passes(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "Artist", "Album"))
            open(os.path.join(d, "Artist", "Album", "a.mp3"), "w").close()
            self.assertEqual(extras.links_everywhere(d), "")


class Subfolders(unittest.TestCase):
    def test_folders_list_subfolders_with_songs(self):
        class Lib(FakeMpd):
            def raw(self, cmd, *args):
                if cmd == "lsinfo":
                    return [("directory", "Artist/Album"), ("directory", "Artist/Empty"),
                            ("file", "Artist/intro.mp3"), ("Title", "Intro")]
                if cmd == "count":
                    return [("songs", "12" if args[1] == "Artist/Album" else "0")]
                return []
        lib = Lib()
        card.mpd = lambda: lib
        with Silent() as buf:
            card.cmd_songs("Artist")
        d = json.loads(buf.getvalue())
        self.assertEqual(d["dirs"], [{"name": "Artist/Album", "label": "Album", "count": 12}])
        self.assertEqual([s["title"] for s in d["songs"]], ["Intro"])


class Reset(unittest.TestCase):
    def test_reset_returns_defaults_and_touches_only_our_file(self):
        with tempfile.TemporaryDirectory() as d:
            card.STATE_DIR = d
            card.load, card.save = Settings.real_load, Settings.real_save
            card.save("settings.json", {"notify": True})
            card.save("stars.json", [{"url": "x"}])
            card.Mpd = FakeMpd
            with Silent() as buf:
                extras.cmd_reset_settings()
            self.assertEqual(json.loads(buf.getvalue())["settings"], extras.DEFAULTS)
            self.assertEqual(sorted(os.listdir(d)), ["stars.json"])


class InboxMoves(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dl = os.path.join(self.tmp.name, "Downloads")
        self.music = os.path.join(self.tmp.name, "Music")
        os.makedirs(self.dl)
        os.makedirs(self.music)
        self.old = (extras.INBOX_DIRS, card.MUSIC_DIR, card.Mpd)
        extras.INBOX_DIRS = (self.dl,)
        card.MUSIC_DIR = self.music
        card.Mpd = FakeMpd

    def tearDown(self):
        extras.INBOX_DIRS, card.MUSIC_DIR, card.Mpd = self.old
        self.tmp.cleanup()

    def move(self, path, folder="Hindi"):
        with Silent() as buf:
            try:
                extras.cmd_inbox_move(path, folder)
            except SystemExit:
                pass
        return json.loads(buf.getvalue())

    def test_moves_without_overwriting(self):
        for _ in range(2):
            with open(os.path.join(self.dl, "song.mp3"), "w") as f:
                f.write("x")
            self.assertTrue(self.move(os.path.join(self.dl, "song.mp3"))["ok"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.music, "Hindi"))),
                         ["song (2).mp3", "song.mp3"])

    def test_refuses_anything_else(self):
        secret = os.path.join(self.tmp.name, "id_ed25519.mp3")
        open(secret, "w").close()
        notes = os.path.join(self.dl, "notes.txt")
        open(notes, "w").close()
        link = os.path.join(self.dl, "link.mp3")
        os.symlink(secret, link)
        for path in [secret, notes, link, os.path.join(self.dl, "..", "id_ed25519.mp3")]:
            self.assertIn("error", self.move(path), path)
        self.assertTrue(os.path.exists(secret))
        self.assertIn("error", self.move(os.path.join(self.dl, "x.mp3"), "../.ssh"))


class Imports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        import subprocess as sp
        self.spawned = []
        self.real_popen = sp.Popen
        sp.Popen = lambda cmd, **kw: self.spawned.append(cmd)

    def tearDown(self):
        import subprocess as sp
        sp.Popen = self.real_popen
        self.tmp.cleanup()

    def start(self, mode, folder, source):
        with Silent() as buf:
            try:
                extras.cmd_import_start(mode, folder, source)
            except SystemExit:
                pass
        return json.loads(buf.getvalue())

    def test_only_youtube_links_survive_and_text_is_capped(self):
        songs = [{"title": "Bones", "artist": "Low Roar"},
                 {"title": "x", "url": "https://evil.example/a.sh"},
                 {"title": "y" * 999, "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
                 "not a dict", {"artist": "no title, no link"}]
        self.assertEqual(self.start("play", "", json.dumps(songs))["total"], 3)
        job = card.load("import-job.json", {})
        self.assertNotIn("url", job["songs"][1])
        self.assertEqual(len(job["songs"][2]["title"]), 200)
        self.assertEqual(len(self.spawned), 1)

    def test_refusals(self):
        self.assertIn("error", self.start("rm", "", "[]"))
        self.assertIn("error", self.start("play", "", "[]"))
        self.assertIn("error", self.start("play", "", "not json"))
        self.assertIn("error", self.start("download", "../..", json.dumps([{"title": "a"}])))
        self.assertIn("error", self.start("download", "Eng", "playlist:https://evil.example/x"))
        self.assertEqual(self.spawned, [])

    def test_targets_search_or_link(self):
        self.assertEqual(extras.song_target({"artist": "A", "title": "--exec=id"}),
                         "ytsearch1:A --exec=id")
        self.assertEqual(extras.song_target({"url": "https://youtu.be/x"}), "https://youtu.be/x")

    def test_stop_ends_the_worker(self):
        self.start("download", "Eng", json.dumps([{"title": "a"}, {"title": "b"}]))
        token = card.load("import.json", {})["token"]
        with Silent():
            extras.cmd_import_stop()
        calls = []
        extras.download_one = lambda target, folder: calls.append(target) or True
        extras.cmd_import_worker(token)
        self.assertEqual(calls, [])
        self.assertEqual(card.load("import.json", {})["state"], "stopped")


class Recovery(unittest.TestCase):
    """Expired YouTube links and dropped stations are mended, a couple of
    times at most, never in a loop."""

    def setUp(self):
        card.RECOVER_TRIES.clear()

    def view(self, state, file, pos=0, err="", kind="youtube"):
        return {"state": state, "song": pos, "playError": err, "kind": kind,
                "current": {"file": file}}

    def test_which_song_failed(self):
        yt = "https://rr1.googlevideo.com/a"
        # stopped on the failing song: mend it and play
        self.assertEqual(card.failed_song(self.view("stop", yt, 2, "got HTTP status 403"), {}),
                         {"file": yt, "pos": 2, "play": True})
        # MPD skipped ahead: mend the previous one in place, keep playing
        self.assertEqual(card.failed_song(self.view("play", "b.mp3", 3, "403"),
                                          {"file": yt, "pos": 2, "state": "play"}),
                         {"file": yt, "pos": 2, "play": False})
        # a station that stopped by itself dropped
        st = "http://radio.example/s"
        self.assertEqual(card.failed_song(self.view("stop", st, 0, "", "radio"),
                                          {"file": st, "state": "play"}),
                         {"file": st, "pos": 0, "play": True})
        # the person paused: nothing to mend
        self.assertIsNone(card.failed_song(self.view("pause", st, 0, "", "radio"),
                                           {"file": st, "state": "play"}))
        self.assertIsNone(card.failed_song(self.view("play", "a.mp3"), {}))

    def test_tries_are_limited(self):
        self.assertTrue(card.may_retry("k", 2))
        self.assertTrue(card.may_retry("k", 2))
        self.assertFalse(card.may_retry("k", 2))

    def test_youtube_link_replaced_in_place(self):
        old = "https://rr1.googlevideo.com/old"

        class Q(FakeMpd):
            def __init__(self):
                super().__init__()
                self.cmds = []

            def raw(self, cmd, *args):
                self.cmds.append((cmd,) + args)
                if cmd == "addid":
                    return [("Id", "77")]
                if cmd == "playlistinfo":
                    return [("file", old), ("Pos", args[0])]
                return []
        with tempfile.TemporaryDirectory() as d:
            card.STATE_DIR = d
            card.load, card.save = Settings.real_load, Settings.real_save
            card.save("titles.json", {old: {"title": "t", "source": "https://www.youtube.com/watch?v=x"}})
            real = card.yt_stream
            card.yt_stream = lambda url, fresh=False: ("https://rr1.googlevideo.com/new",
                                                       {"title": "t", "source": url})
            try:
                m = Q()
                note = card.recover(m, {"file": old, "pos": 4, "play": True})
            finally:
                card.yt_stream = real
            self.assertIn("fresh", note)
            self.assertIn(("addid", "https://rr1.googlevideo.com/new", "4"), m.cmds)
            self.assertIn(("delete", "5"), m.cmds)
            self.assertIn(("playid", "77"), m.cmds)
            self.assertNotIn(old, card.load("titles.json", {}))
            # a second failure of the same video: one more try, then no more
            card.yt_stream = lambda url, fresh=False: ("https://rr1.googlevideo.com/n2", {"source": url})
            try:
                card.save("titles.json", {old: {"source": "https://www.youtube.com/watch?v=x"}})
                self.assertIsNotNone(card.recover(Q(), {"file": old, "pos": 4, "play": True}))
                card.save("titles.json", {old: {"source": "https://www.youtube.com/watch?v=x"}})
                self.assertIsNone(card.recover(Q(), {"file": old, "pos": 4, "play": True}))
            finally:
                card.yt_stream = real

    def test_local_files_are_left_alone(self):
        self.assertIsNone(card.recover(FakeMpd(), {"file": "Hindi/a.mp3", "pos": 0, "play": True}))


class FakeYouTube:
    """A local stand-in for googlevideo: like the real one since mid-2026 it
    refuses any request without a bounded byte range, and it can answer 403
    once, as an expired link does."""

    def __init__(self, body, expire_once=False):
        import http.server
        import threading
        self.body, self.expire_once, self.requests = body, expire_once, []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range", "")
                outer.requests.append((self.path, rng, dict(self.headers)))
                m = re.match(r"^bytes=(\d+)-(\d+)$", rng)
                if not m:
                    self.send_error(403)
                    return
                if outer.expire_once and "/old" in self.path:
                    outer.expire_once = False
                    self.send_error(403)
                    return
                a, b = int(m.group(1)), min(int(m.group(2)), len(outer.body) - 1)
                self.send_response(206)
                self.send_header("Content-Type", "audio/webm")
                self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, len(outer.body)))
                self.send_header("Content-Length", str(b - a + 1))
                self.end_headers()
                self.wfile.write(outer.body[a:b + 1])

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class YouTubeRelay(unittest.TestCase):
    VID = "TO-_3tck2tg"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = (card.yt_resolve, card.upstream_ok, card.RELAY_CHUNK)
        card.RELAY_CHUNK = 1000                     # many pieces per song
        card.upstream_ok = lambda url: url.startswith("http://127.0.0.1:")
        self.body = bytes(range(256)) * 20          # 5120 bytes
        self.yt = FakeYouTube(self.body, expire_once=True)
        self.resolves = []

        def resolve(url, fresh=False, hurry=False):
            # `hurry` is how the relay asks: MPD is waiting at the other end,
            # so it must not sit in a queue for the browser's cookies.
            self.resolves.append((url, fresh))
            path = "/new" if fresh else "/old"
            return {"url": self.yt.url + path, "headers": {"User-Agent": "yt-dlp-ua"},
                    "meta": {"title": "Song", "artist": "", "source": url}}
        card.yt_resolve = resolve
        self.port = card.start_relay()

    def tearDown(self):
        card.yt_resolve, card.upstream_ok, card.RELAY_CHUNK = self.saved
        self.yt.close()
        self.tmp.cleanup()

    def get(self, path, headers=None, method="GET"):
        from urllib import request as web
        from urllib.error import HTTPError
        req = web.Request("http://127.0.0.1:%d%s" % (self.port, path),
                          headers=headers or {}, method=method)
        try:
            with web.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except HTTPError as e:
            return e.code, dict(e.headers), b""

    def test_listens_on_this_machine_only(self):
        self.assertGreater(self.port, 0)
        self.assertEqual(card.load("relay.json", {})["port"], self.port)
        import socket
        s = socket.socket()
        try:
            addr = [a for a in socket.gethostbyname_ex(socket.gethostname())[2]
                    if not a.startswith("127.")]
        except OSError:
            addr = []
        for a in addr:   # not reachable on the machine's network address
            self.assertNotEqual(s.connect_ex((a, self.port)), 0)
        s.close()

    def test_plain_request_gets_the_whole_song_in_bounded_pieces(self):
        status, headers, data = self.get("/yt/" + self.VID)
        self.assertEqual(status, 200)
        self.assertEqual(data, self.body)
        self.assertEqual(headers["Content-Length"], str(len(self.body)))
        ranges = [r for _, r, _ in self.yt.requests]
        self.assertTrue(all(re.match(r"^bytes=\d+-\d+$", r) for r in ranges), ranges)
        self.assertGreater(len(ranges), 4)          # really came in pieces

    def test_expired_link_is_resolved_again_mid_request(self):
        status, _, data = self.get("/yt/" + self.VID)
        self.assertEqual(data, self.body)
        self.assertIn(("https://www.youtube.com/watch?v=" + self.VID, True), self.resolves)

    def test_seek(self):
        status, headers, data = self.get("/yt/" + self.VID, {"Range": "bytes=3000-"})
        self.assertEqual(status, 206)
        self.assertEqual(data, self.body[3000:])
        self.assertEqual(headers["Content-Range"], "bytes 3000-5119/5120")
        status, _, data = self.get("/yt/" + self.VID, {"Range": "bytes=10-19"})
        self.assertEqual((status, data), (206, self.body[10:20]))
        status, headers, _ = self.get("/yt/" + self.VID, {"Range": "bytes=9999-"})
        self.assertEqual(status, 416)

    def test_head(self):
        status, headers, data = self.get("/yt/" + self.VID, method="HEAD")
        self.assertEqual((status, data), (200, b""))
        self.assertEqual(headers["Content-Length"], str(len(self.body)))

    def test_serves_nothing_but_a_video_id(self):
        before = len(self.yt.requests)
        for path in ("/", "/yt/", "/yt/short", "/yt/" + self.VID + "x", "/yt/../etc/passwd",
                     "/yt/" + self.VID + "?u=http://evil", "/http://evil.example/x",
                     "/yt/%2e%2e%2fetc"):
            self.assertEqual(self.get(path)[0], 404, path)
        self.assertEqual(len(self.yt.requests), before)   # nothing fetched

    def test_callers_headers_never_reach_youtube(self):
        self.get("/yt/" + self.VID, {"Cookie": "secret=1", "Authorization": "Bearer x",
                                     "X-Forwarded-For": "1.2.3.4"})
        for _, _, sent in self.yt.requests:
            self.assertNotIn("Cookie", sent)
            self.assertNotIn("Authorization", sent)
            self.assertNotIn("X-Forwarded-For", sent)
            self.assertEqual(sent.get("User-Agent"), "yt-dlp-ua")

    def test_only_youtube_stream_hosts_are_fetched(self):
        card.upstream_ok = self.saved[1]
        for url in ("http://127.0.0.1:1/x", "https://evil.example/x", "file:///etc/passwd",
                    "https://googlevideo.com.evil.net/x", "http://r1.googlevideo.com/x"):
            with self.assertRaises(PermissionError):
                card.fetch_piece({"url": url, "headers": {}}, 0, 10)
        self.assertTrue(card.upstream_ok("https://rr2---sn-x.googlevideo.com/videoplayback"))

    def test_queue_gets_the_relay_link_and_falls_back_without_it(self):
        url = "https://www.youtube.com/watch?v=" + self.VID
        stream, meta = card.yt_stream(url)
        self.assertEqual(stream, "http://127.0.0.1:%d/yt/%s" % (self.port, self.VID))
        self.assertEqual(meta["title"], "Song")
        card.save("settings.json", {"youtubeRelay": False})
        stream, _ = card.yt_stream(url)
        self.assertEqual(stream, self.yt.url + "/old")   # the direct link
        # a relay that is not running is never handed to MPD
        card.save("settings.json", {})
        card.save("relay.json", {"port": 1, "pid": 999999999})
        self.assertEqual(card.relay_address(), "")

    def test_video_ids(self):
        good = ["https://www.youtube.com/watch?v=jNQXAC9IVRw", "https://youtu.be/jNQXAC9IVRw?t=3",
                "https://music.youtube.com/watch?v=jNQXAC9IVRw&list=x",
                "https://www.youtube.com/shorts/jNQXAC9IVRw"]
        for u in good:
            self.assertEqual(card.video_id(u), "jNQXAC9IVRw", u)
        for u in ["https://evil.com/watch?v=jNQXAC9IVRw", "https://www.youtube.com/watch?v=short",
                  "https://www.youtube.com/watch?v=jNQXAC9IVR%0A", "ytsearch1:x", ""]:
            self.assertEqual(card.video_id(u), "", u)

    def test_ranges(self):
        self.assertEqual(card.parse_range("bytes=100-"), (100, None))
        self.assertEqual(card.parse_range("bytes=5-9"), (5, 9))
        for bad in ("bytes=9-5", "bytes=0-1,5-9", "items=0-5", "bytes=-500", None, ""):
            self.assertEqual(card.parse_range(bad), (0, None), bad)


class FakeStation:
    """Local radio stations for the speed test: fast, slow, a web page, an
    HLS playlist, and one that redirects into the local network."""

    def __init__(self):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/inside")
                    self.end_headers()
                    return
                kind = {"/fast": "audio/mpeg", "/slow": "audio/mpeg", "/page": "text/html",
                        "/hls": "application/vnd.apple.mpegurl"}.get(self.path, "audio/mpeg")
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.end_headers()
                try:
                    if self.path == "/slow":           # 32 KB in about 2 s: "ok"
                        for _ in range(8):
                            self.wfile.write(b"\0" * 4096)
                            self.wfile.flush()
                            time.sleep(0.25)
                    elif self.path == "/hls":
                        self.wfile.write(b"#EXTM3U\n#EXT-X-VERSION:3\n")
                    else:
                        self.wfile.write(b"\0" * (64 << 10))
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        # "localhost" plays the public internet; 127.0.0.1 stays private.
        self.url = "http://localhost:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RadioSpeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        # The gate moved: what is checked now is the address that will be
        # dialled, not the name (2026-09-24). The test's own server is on
        # this machine, so that one address is let through.
        self.saved = card.allow_address
        card.allow_address = lambda text: str(text) in ("127.0.0.1", "::1")
        self.st = FakeStation()

    def tearDown(self):
        card.allow_address = self.saved
        self.st.close()
        self.tmp.cleanup()

    def test_tiers(self):
        self.assertEqual(card.probe_station(self.st.url + "/fast")["tier"], "good")
        self.assertEqual(card.probe_station(self.st.url + "/slow")["tier"], "ok")
        self.assertEqual(card.probe_station(self.st.url + "/page")["tier"], "bad")
        self.assertEqual(card.probe_station(self.st.url + "/hls")["tier"], "bad")

    def test_never_touches_this_machine_or_the_local_network(self):
        card.allow_address = self.saved    # the real check
        for url in ("http://127.0.0.1:6600/", "http://192.168.0.1/", "http://10.0.0.8/",
                    "http://[::1]/", "http://169.254.169.254/latest", "file:///etc/passwd",
                    "ftp://example.com/x"):
            self.assertEqual(card.probe_station(url)["tier"], "bad", url)

    def test_a_redirect_into_the_network_is_refused(self):
        self.assertEqual(card.probe_station(self.st.url + "/redirect")["tier"], "bad")

    def test_results_are_kept_and_reused(self):
        urls = [self.st.url + "/fast", self.st.url + "/page"]
        self.assertEqual(card.probe_many(urls), {urls[0]: "good", urls[1]: "bad"})
        real = card.probe_station
        card.probe_station = lambda url: self.fail("tested again")
        try:
            self.assertEqual(card.probe_many(urls), {urls[0]: "good", urls[1]: "bad"})
        finally:
            card.probe_station = real

    def test_order_speed_first_then_reviews(self):
        found = [{"url": "a", "votes": 900, "clicks": 1}, {"url": "b", "votes": 5, "clicks": 1},
                 {"url": "c", "votes": 50, "clicks": 1}, {"url": "d", "votes": 999, "clicks": 1},
                 {"url": "e", "votes": 1, "clicks": 1}]
        ranked = card.rank(found, {"a": "slow", "b": "good", "c": "good", "d": "bad", "e": "ok"})
        self.assertEqual([s["url"] for s in ranked], ["c", "b", "e", "a", "d"])
        ranked = card.rank([{"url": "x", "votes": 1}, {"url": "y", "votes": 2}], {"x": "ok"})
        self.assertEqual([s["tier"] for s in ranked], ["ok", ""])   # untested after ok

    def test_discover_shows_only_good_and_ok(self):
        pool = [{"url": "u%d" % i, "country": "C%d" % i, "name": "s%d" % i, "votes": 0, "clicks": 0}
                for i in range(8)]
        tiers = {"u0": "slow", "u1": "bad", "u2": "good", "u3": "slow", "u4": "ok",
                 "u5": "bad", "u6": "good", "u7": "slow"}
        real_st, real_probe = card.stations, card.probe_station
        card.stations = lambda **kw: [dict(s) for s in pool]
        card.probe_station = lambda url: {"tier": tiers[url], "secs": 1}
        try:
            with Silent() as buf:
                card.cmd_discover()
            picked = json.loads(buf.getvalue())["stations"]
        finally:
            card.stations, card.probe_station = real_st, real_probe
        self.assertTrue(picked)
        self.assertTrue(all(s["tier"] in ("good", "ok") for s in picked), picked)
        self.assertLessEqual(len(picked), 3)


class KeptRadioLists(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = card.stations
        self.fetches = 0

        def fake(**kw):
            self.fetches += 1
            return [{"url": "https://a.example/s", "name": "A", "votes": 3, "clicks": 1,
                     "country": "X", "tags": ""}]
        card.stations = fake

    def tearDown(self):
        card.stations = self.saved
        self.tmp.cleanup()

    def open_list(self, fresh=""):
        with Silent() as buf:
            card.cmd_stations("tag", "Lofi", fresh)
        return json.loads(buf.getvalue())

    def test_second_open_uses_the_kept_list(self):
        first = self.open_list()
        second = self.open_list()
        self.assertEqual(self.fetches, 1)
        self.assertEqual(first["stations"], second["stations"])
        self.assertEqual(first["at"], second["at"])

    def test_refresh_fetches_again(self):
        self.open_list()
        self.open_list("fresh")
        self.assertEqual(self.fetches, 2)

    def test_kept_for_the_chosen_days_then_fetched_again(self):
        self.open_list()
        lists = card.load("lists.json", {})
        lists["tag:lofi"]["at"] -= 6 * card.DAY
        card.save("lists.json", lists)
        self.open_list()                                   # 5 days: stale
        self.assertEqual(self.fetches, 2)
        card.save("settings.json", {"radioRetestDays": 10})
        lists = card.load("lists.json", {})
        lists["tag:lofi"]["at"] -= 6 * card.DAY
        card.save("lists.json", lists)
        self.open_list()                                   # 10 days: still kept
        self.assertEqual(self.fetches, 2)

    def test_speed_results_follow_the_setting_and_dead_ones_retry_daily(self):
        self.assertEqual(card.quality_ttl("good"), 5 * card.DAY)
        self.assertEqual(card.quality_ttl("bad"), card.DAY)
        card.save("settings.json", {"radioRetestDays": 10})
        self.assertEqual(card.quality_ttl("slow"), 10 * card.DAY)

    def test_only_5_or_10_days(self):
        for bad in (0, 3, 365, "5"):
            with Silent() as buf:
                try:
                    extras.cmd_set_setting("radioRetestDays", json.dumps(bad))
                except SystemExit:
                    pass
            self.assertIn("error", buf.getvalue(), bad)
        card.save("settings.json", {"radioRetestDays": 99})
        self.assertEqual(extras.settings()["radioRetestDays"], 5)

    def test_test_now_covers_kept_lists_and_stars(self):
        self.open_list()
        card.save("stars.json", [{"url": "https://star.example/s", "name": "S"}])
        self.assertEqual(card.radio_known_urls(), ["https://a.example/s", "https://star.example/s"])


class PhaseOneSettings(unittest.TestCase):
    """Settings added 2026-09-22 (phase 1): each one reaches the right place
    and nothing else."""

    class Q(FakeMpd):
        def __init__(self, files=()):
            super().__init__()
            self.cmds, self.files = [], list(files)

        def raw(self, cmd, *args):
            self.cmds.append((cmd,) + args)
            if cmd == "playlistinfo":
                out = []
                for i, f in enumerate(self.files):
                    out += [("file", f), ("Pos", str(i))]
                return out
            return []

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = (card.mpd, card.ytdlp, subprocess.Popen, extras.has_mutagen)

    def tearDown(self):
        card.mpd, card.ytdlp, subprocess.Popen, extras.has_mutagen = self.saved
        self.tmp.cleanup()

    def use(self, **values):
        card.save("settings.json", values)

    def set_value(self, key, value):
        with Silent() as buf:
            try:
                extras.cmd_set_setting(key, json.dumps(value))
            except SystemExit:
                pass
        return buf.getvalue()

    def test_values_outside_the_choices_are_refused(self):
        for key, bad in (("downloadFormat", "exe"), ("downloadFormat", "m4a"),
                         ("searchResults", 500), ("danceStyle", 9), ("radioHide", "all"),
                         ("youtubeQuality", "4k"), ("searchResults", True)):
            self.assertIn("error", self.set_value(key, bad), (key, bad))
        self.use(downloadFormat="--exec", searchResults=999, danceStyle=-7)
        st = extras.settings()
        self.assertEqual((st["downloadFormat"], st["searchResults"], st["danceStyle"]), ("mp3", 6, -1))

    def test_download_options(self):
        self.assertIn("0", extras.download_args())                     # MP3, best
        self.use(downloadQuality="small")
        args = extras.download_args()
        self.assertEqual(args[args.index("--audio-quality") + 1], "5")
        self.use(downloadFormat="opus")
        extras.has_mutagen = lambda: False
        args = extras.download_args()
        self.assertIn("opus", args)
        self.assertNotIn("--embed-thumbnail", args)                    # would fail the download
        self.assertNotIn("--audio-quality", args)                      # kept as YouTube sends it
        extras.has_mutagen = lambda: True
        self.assertIn("--embed-thumbnail", extras.download_args())
        self.use(sponsorblock=True)
        args = extras.download_args()
        self.assertEqual(args[args.index("--sponsorblock-remove") + 1], "music_offtopic")

    def test_data_saver_streams_are_kept_apart(self):
        asked = []

        def fake(*args, timeout=60, hurry=False):
            asked.append(args)
            return json.dumps({"url": "https://r1.googlevideo.com/x?expire=%d" % (time.time() + 9999),
                               "title": "t"})
        card.ytdlp = fake
        url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        card.yt_resolve(url)
        self.use(youtubeQuality="saver")
        card.yt_resolve(url)
        self.assertEqual(len(asked), 2)                                # not the best stream reused
        self.assertEqual(asked[1][1], "bestaudio[abr<=70]/worstaudio")
        self.assertIn("jNQXAC9IVRw:saver", card.load("streams.json", {}))

    def test_results_per_search(self):
        asked = []
        card.ytdlp = lambda *a, timeout=60: (asked.append(a), '{"entries": []}')[1]
        self.use(searchResults=15)
        with Silent():
            card.cmd_ytsearch("hello")
        self.assertIn("ytsearch15:hello", asked[0])

    def test_plays_not_reported_when_turned_off(self):
        started = []
        subprocess.Popen = lambda *a, **k: started.append(a)
        card.count_click("abc")
        self.assertEqual(len(started), 1)
        self.use(radioReportPlays=False)
        card.count_click("abc")
        self.assertEqual(len(started), 1)

    def test_hiding_and_quality_order(self):
        found = [{"url": u, "votes": v, "bitrate": b, "clicks": 0}
                 for u, v, b in (("a", 9, 64), ("b", 1, 320), ("c", 5, 128), ("d", 9, 320))]
        tiers = {"a": "good", "b": "good", "c": "slow", "d": "bad"}
        self.assertEqual([s["url"] for s in card.rank([dict(s) for s in found], tiers)], ["a", "b", "c", "d"])
        self.use(radioQuality=True)
        self.assertEqual([s["url"] for s in card.rank([dict(s) for s in found], tiers)], ["b", "a", "c", "d"])
        self.use(radioHide="bad")
        self.assertEqual([s["url"] for s in card.rank([dict(s) for s in found], tiers)], ["a", "b", "c"])
        self.use(radioHide="slow")
        self.assertEqual([s["url"] for s in card.rank([dict(s) for s in found], tiers)], ["a", "b"])

    def test_shuffled_folder_starts_with_the_song_clicked(self):
        m = self.Q(["F/a.mp3", "F/b.mp3", "F/c.mp3"])
        card.mpd = lambda: m
        self.use(shuffleFolders=True)
        with Silent():
            card.cmd_play_folder("F", "F/c.mp3")
        names = [c[0] for c in m.cmds]
        self.assertLess(names.index("shuffle"), names.index("play"))
        self.assertIn(("move", "2", "0"), m.cmds)
        self.assertIn(("play", "0"), m.cmds)
        m2 = self.Q(["F/a.mp3"])
        card.mpd = lambda: m2
        self.use(shuffleFolders=False)
        with Silent():
            card.cmd_play_folder("F", "")
        self.assertNotIn("shuffle", [c[0] for c in m2.cmds])

    def test_remove_once_played_tells_mpd(self):
        m = self.Q()
        card.mpd = lambda: m
        self.set_value("consume", True)
        self.assertIn(("consume", "1"), m.cmds)

    def test_clear_saved_keeps_settings_stars_and_favorites(self):
        card.save("settings.json", {"notify": True})
        card.save("stars.json", [{"url": "https://x.example/s"}])
        card.save("lists.json", {"k": {}})
        card.save("quality.json", {"u": {}})
        art = os.path.join(self.tmp.name, "art")
        os.makedirs(art)
        open(os.path.join(art, "a.img"), "w").write("x")
        import booktext
        saved_art, saved_lyrics, saved_text = card.ART_DIR, extras.LYRICS_DIR, booktext.CACHE_DIR
        card.ART_DIR, extras.LYRICS_DIR = art, os.path.join(self.tmp.name, "nolyrics")
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "notext")
        # Every folder "Clear saved" removes must be in the test's own space:
        # an earlier version of this test emptied the real text cache.
        for path in extras.saved_paths():
            self.assertTrue(path.startswith(self.tmp.name), path)
        try:
            with Silent():
                extras.cmd_clear_saved()
        finally:
            card.ART_DIR, extras.LYRICS_DIR, booktext.CACHE_DIR = saved_art, saved_lyrics, saved_text
        self.assertFalse(os.path.exists(art))
        self.assertFalse(os.path.exists(card.state_path("lists.json")))
        self.assertFalse(os.path.exists(card.state_path("quality.json")))
        self.assertTrue(os.path.exists(card.state_path("settings.json")))
        self.assertTrue(os.path.exists(card.state_path("stars.json")))

    def test_dancer_style_choices_match_the_styles(self):
        src = open(os.path.join(HERE, "..", "CardService.qml")).read()
        block = src[src.index("danceStyles: ["):src.index("]\n\n", src.index("danceStyles: ["))]
        styles = len(re.findall(r"^\s*\[", block, re.M))
        self.assertEqual(extras.ENUMS["danceStyle"], tuple(range(-1, styles)))


class PhaseTwoSettings(unittest.TestCase):
    """Settings added 2026-09-22 (phase 2)."""

    class Q(FakeMpd):
        def __init__(self, status=None, songs=()):
            super().__init__()
            self.cmds, self.status, self.songs = [], dict(status or {}), list(songs)

        def raw(self, cmd, *args):
            self.cmds.append((cmd,) + args)
            if cmd == "setvol":
                self.status["volume"] = args[0]
            if cmd == "lsinfo":
                out = []
                for d in self.songs:
                    out += [("directory", d)]
                return out
            return []

        def dict(self, cmd, *args):
            self.cmds.append((cmd,) + args)
            if cmd == "status":
                return dict(self.status)
            if cmd == "count":
                return {"songs": "3"}
            return {}

        def records(self, cmd, *args):
            if cmd == "lsinfo":
                return [{"_type": "directory", "directory": d} for d in self.songs]
            return []

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = (card.mpd, card.ytdlp, card.stations, card.probe_many, card.auth_args,
                      extras.INBOX_DIRS, card.FADE_OUT, card.FADE_IN, card.MUSIC_DIR)
        card.FADE_OUT = card.FADE_IN = 0.0
        # An empty music folder of its own: cmd_home also looks on disk now,
        # for folders of novels that MPD cannot see, and this test was reading
        # his real ~/Music (found 2026-09-30).
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        os.makedirs(card.MUSIC_DIR, exist_ok=True)

    def tearDown(self):
        (card.mpd, card.ytdlp, card.stations, card.probe_many, card.auth_args,
         extras.INBOX_DIRS, card.FADE_OUT, card.FADE_IN, card.MUSIC_DIR) = self.saved
        self.tmp.cleanup()

    def use(self, **values):
        card.save("settings.json", values)

    def run_cmd(self, fn, *args):
        with Silent() as buf:
            try:
                fn(*args)
            except SystemExit:
                pass
        lines = buf.getvalue().strip().splitlines()
        return json.loads(lines[-1]) if lines else {}

    def set_value(self, key, value):
        return self.run_cmd(extras.cmd_set_setting, key, json.dumps(value))

    def test_lists_are_checked_item_by_item(self):
        for key, bad in (("hiddenFolders", ["../etc"]), ("hiddenFolders", [".hidden"]),
                         ("radioCountries", ["usa"]), ("radioCountries", ["us"]),
                         ("radioCountries", ["A%s" % chr(65 + i) for i in range(11)]),
                         ("hiddenGenres", ["not-a-genre"]), ("hiddenGenres", "jazz"),
                         ("downloadFolder", "a/b"), ("downloadFolder", ".x"),
                         ("notifyShows", "everything"), ("lyricsOffset", 250),
                         ("lyricsSize", "huge"), ("folderSort", "size")):
            self.assertIn("error", self.set_value(key, bad), (key, bad))
        self.assertIn("settings", self.set_value("radioCountries", ["IN", "JP", "IN"]))
        self.assertEqual(extras.settings()["radioCountries"], ["IN", "JP"])
        self.use(hiddenFolders=["ok", "../up", 5], hiddenGenres=["jazz", "x"], downloadFolder="../x")
        st = extras.settings()
        self.assertEqual((st["hiddenFolders"], st["hiddenGenres"], st["downloadFolder"]),
                         (["ok"], ["jazz"], ""))

    def test_soft_pause_fades_and_puts_the_volume_back(self):
        m = self.Q({"state": "play", "volume": "80"})
        card.mpd = lambda: m
        self.run_cmd(card.cmd_pause)
        self.assertEqual([c for c in m.cmds if c[0] == "pause"], [("pause",)])   # off: a plain toggle
        self.use(softPause=True)
        m = self.Q({"state": "play", "volume": "80"})
        card.mpd = lambda: m
        self.run_cmd(card.cmd_pause)
        vols = [int(c[1]) for c in m.cmds if c[0] == "setvol"]
        self.assertIn(0, vols)
        self.assertIn(("pause", "1"), m.cmds)
        self.assertEqual(vols[-1], 80)
        self.assertLess(m.cmds.index(("setvol", "0")), m.cmds.index(("pause", "1")))
        m = self.Q({"state": "pause", "volume": "80"})
        card.mpd = lambda: m
        self.run_cmd(card.cmd_pause)
        self.assertLess(m.cmds.index(("setvol", "0")), m.cmds.index(("pause", "0")))
        self.assertEqual(m.status["volume"], "80")
        self.assertFalse(os.path.exists(card.state_path("fade.json")))
        # A fade cut short left the volume low: the next one puts it back.
        card.save("fade.json", {"volume": 70})
        m = self.Q({"state": "pause", "volume": "5"})
        card.mpd = lambda: m
        self.run_cmd(card.cmd_pause)
        self.assertEqual(m.status["volume"], "70")
        # No volume control in MPD: just pause.
        m = self.Q({"state": "play", "volume": "-1"})
        card.mpd = lambda: m
        self.run_cmd(card.cmd_pause)
        self.assertNotIn("setvol", [c[0] for c in m.cmds])

    def test_long_files_carry_on(self):
        def view(file, elapsed, duration=3600, state="play"):
            return {"state": state, "elapsed": elapsed, "duration": duration,
                    "current": {"file": file}}
        extras.note_position(view("book.mp3", 1500))
        extras.note_position(view("song.mp3", 150, duration=200))          # not long
        extras.note_position(view("http://radio/x", 1500))                # a stream
        self.assertEqual(list(card.load("positions.json", {})), ["book.mp3"])
        m = self.Q()
        note = extras.resume_position(m, view("book.mp3", 0), {"file": "other"})  # always now
        self.assertIn(("seekcur", "1500"), m.cmds)
        self.assertIn("0:25:00", note)
        m = self.Q()
        self.assertIsNone(extras.resume_position(m, view("book.mp3", 0), {"file": "book.mp3"}))
        self.assertIsNone(extras.resume_position(m, view("book.mp3", 400), {"file": "x"}))
        extras.note_position(view("book.mp3", 3580))                      # finished
        self.assertEqual(card.load("positions.json", {}), {})

    def test_hidden_folders_and_recent_sort(self):
        m = self.Q(songs=["Alpha", "Beta", "Gamma"])
        card.mpd = lambda: m
        card.fav_files = lambda m: []
        card.save("played.json", {"Gamma": 200, "Beta": 100})
        self.use(hiddenFolders=["Beta"], folderSort="recent")
        home = self.run_cmd(card.cmd_home)
        self.assertEqual([f["name"] for f in home["folders"]], ["Gamma", "Beta", "Alpha"])
        self.assertEqual([f["hidden"] for f in home["folders"]], [False, True, False])

    def test_a_folder_of_novels_is_on_the_home_page(self):
        """His report, 2026-09-30: ~/Music/Books held one .epub and appeared
        nowhere -- not on the home page, and so not in Settings -> Book
        folders either, which is built from the same list. MPD indexes sound,
        so it never knew the folder existed."""
        m = self.Q(songs=["Alpha"])
        card.mpd = lambda: m
        card.fav_files = lambda m: []
        os.makedirs(os.path.join(card.MUSIC_DIR, "Books"))
        with open(os.path.join(card.MUSIC_DIR, "Books", "a novel.epub"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(card.MUSIC_DIR, "Empty"))          # nothing to read
        os.makedirs(os.path.join(card.MUSIC_DIR, ".hidden"))
        self.use(bookFolders=["Books"])
        home = self.run_cmd(card.cmd_home)
        names = [f["name"] for f in home["folders"]]
        self.assertEqual(names, ["Alpha", "Books"])
        books_row = home["folders"][1]
        self.assertEqual(books_row["count"], 0)
        self.assertTrue(books_row["book"] and books_row["textOnly"])
        self.assertEqual(books_row["file"], "Books/a novel.epub")

    def test_playing_a_folder_notes_when(self):
        m = self.Q()
        card.mpd = lambda: m
        self.run_cmd(card.cmd_play_folder, "Rock/Old", "")
        self.assertIn("Rock", card.load("played.json", {}))

    def test_discover_leans_to_chosen_countries(self):
        def fake(**params):
            code = params.get("countrycode", "")
            return [{"url": "https://%s%d.example/s" % (code or "w", i), "country": code or "W%d" % i,
                     "votes": 0, "clicks": 0, "bitrate": 0} for i in range(8)]
        card.stations = fake
        card.probe_many = lambda urls, **k: {u: "good" for u in urls}
        self.use(radioCountries=["IN"])
        got = self.run_cmd(card.cmd_discover)["stations"]
        self.assertEqual(sum(1 for s in got if s["url"].startswith("https://IN")), 2)
        self.assertEqual(len(got), 3)

    def test_music_search_keeps_only_video_ids(self):
        asked = []

        def fake(*a, timeout=60):
            asked.append(a)
            return json.dumps({"entries": [{"id": "fsiPzT50ZiM", "title": "Tum Hi Ho",
                                            "url": "https://music.youtube.com/watch?v=fsiPzT50ZiM"},
                                           {"id": "../../x", "title": "bad"}]})
        card.ytdlp = fake
        self.use(youtubeMusic=True)
        got = self.run_cmd(card.cmd_ytsearch, "tum hi ho & more")
        self.assertTrue(asked[0][-1].startswith("https://music.youtube.com/search?q=tum+hi+ho+%26+more"))
        self.assertEqual([v["url"] for v in got["videos"]],
                         ["https://www.youtube.com/watch?v=fsiPzT50ZiM"])

    def test_your_youtube_needs_sign_in_and_keeps_plain_ids(self):
        card.auth_args = lambda: []
        self.assertIn("error", self.run_cmd(card.cmd_yt_mine))
        card.auth_args = lambda: ["--cookies-from-browser", "firefox"]
        card.ytdlp = lambda *a, timeout=60: json.dumps({"entries": [
            {"id": "PLabcdefghij", "title": "Mine", "playlist_count": 4},
            {"id": "LL", "title": "Liked videos"}, {"id": "RDuwMhyoFWCrM", "title": "Mix"},
            {"id": "x&list=evil", "title": "bad"}]})
        got = self.run_cmd(card.cmd_yt_mine)["lists"]
        self.assertEqual([l["url"] for l in got],
                         [card.LIKED, "https://www.youtube.com/playlist?list=PLabcdefghij"])

    def test_stars_go_out_and_come_back(self):
        extras.INBOX_DIRS = (self.tmp.name,)
        card.save("stars.json", [{"name": "One\n#EXTINF:evil", "url": "https://one.example/s"},
                                 {"name": "Local", "url": "file:///etc/passwd"}])
        res = self.run_cmd(extras.cmd_stars_export)
        text = open(res["path"]).read()
        self.assertEqual(res["count"], 1)
        self.assertNotIn("file:", text)
        # The name stays on its own line: it cannot add an entry.
        self.assertEqual(sum(1 for l in text.splitlines() if l.startswith("#EXTINF")), 1)
        with open(os.path.join(self.tmp.name, "more.m3u"), "w") as f:
            f.write("#EXTM3U\n#EXTINF:-1,Two\nhttps://two.example/s\n"
                    "https://one.example/s\nfile:///etc/shadow\nsmb://nas/x\n")
        res = self.run_cmd(extras.cmd_stars_import, os.path.join(self.tmp.name, "more.m3u"))
        self.assertEqual(res["added"], 1)
        self.assertEqual([s["url"] for s in res["stars"]][-1], "https://two.example/s")
        outside = os.path.join(tempfile.gettempdir(), "elsewhere.m3u")
        self.assertIn("error", self.run_cmd(extras.cmd_stars_import, outside))
        self.assertIn("error", self.run_cmd(extras.cmd_stars_import, "/etc/passwd"))

    def test_qml_knows_every_new_choice(self):
        src = open(os.path.join(HERE, "..", "MusicPanel.qml")).read()
        for key in ("folderSort", "notifyShows", "lyricsOffset", "lyricsSize"):
            self.assertIn("cycles.%s = " % key, src)
        svc = open(os.path.join(HERE, "..", "CardService.qml")).read()
        for key in extras.DEFAULTS:
            if key != "keys":
                self.assertIn(key + ":", svc, key)


class Audiobooks(unittest.TestCase):
    """books.py: a folder opened as a book (2026-09-22)."""

    class Q(FakeMpd):
        def __init__(self, files=(), state="play", elapsed=0.0, current=""):
            super().__init__()
            self.cmds, self.files = [], list(files)

        def raw(self, cmd, *args):
            self.cmds.append((cmd,) + args)
            return []

        def dict(self, cmd, *args):
            return {}

        def records(self, cmd, *args):
            return [dict(f, _type="file") for f in self.files]

    def setUp(self):
        import books
        self.books = books
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = (card.mpd, books.m4b_chapters)

    def tearDown(self):
        card.mpd, self.books.m4b_chapters = self.saved
        self.tmp.cleanup()

    def use(self, **values):
        card.save("settings.json", values)

    @staticmethod
    def files(n=3, secs=1500, folder="Books/B", genre=""):
        return [{"file": "%s/%02d chapter.mp3" % (folder, i + 1), "duration": str(secs), "Genre": genre}
                for i in range(n)]

    def test_what_counts_as_a_book(self):
        b = self.books
        self.assertTrue(b.guess([{"file": "x/a.m4b"}]))
        self.assertTrue(b.guess([{"file": "x/a.mp3", "Genre": "Audiobook", "duration": "30"}]))
        self.assertTrue(b.guess(self.files(genre="Spoken")))
        self.assertFalse(b.guess(self.files()))                    # long, numbered: still songs
        self.assertFalse(b.guess(self.files(secs=240)))
        self.assertFalse(b.guess([]))
        # Book folders (Settings): the folder and everything inside it.
        self.assertFalse(b.is_book("Audio/Talks", self.files(secs=240)))
        self.use(bookFolders=["Audio"])
        self.assertTrue(b.is_book("Audio/Talks", self.files(secs=240)))
        self.assertTrue(b.is_book("Audio", self.files(secs=240)))
        self.assertFalse(b.is_book("Audiophile", self.files(secs=240)))
        self.assertFalse(b.is_book("Audio/Empty", []))
        # Length is not a reason. His 60-minute song was being counted as a
        # book (2026-09-23); only an .m4b, an audiobook genre, or a folder he
        # chose makes one.
        self.use(bookFolders=[])
        self.assertFalse(b.guess(self.files(secs=3600)))
        self.assertFalse(b.guess(self.files(secs=1500)))
        self.assertTrue(b.guess([{"file": "x/a.m4b", "Time": "60"}]))
        self.assertTrue(b.guess([{"file": "x/a.mp3", "Time": "60", "Genre": "Audiobook"}]))

    def test_a_song_from_a_book_folder_plays_the_book_from_there(self):
        import library
        files = self.files(secs=240, folder="Nightfall Suite")
        m = self.Q(files)
        card.mpd = lambda: m
        self.use(bookFolders=["Nightfall Suite"])
        with Silent():
            card.cmd_add_song(files[1]["file"], "replace")
        self.assertEqual([c[1] for c in m.cmds if c[0] == "add"], [f["file"] for f in files])
        self.assertIn(("play", "1"), m.cmds)
        self.assertEqual(library.playing_folder(), "Nightfall Suite")
        self.use(bookFolders=[])                                # music again: just the song
        m.cmds = []
        with Silent():
            card.cmd_add_song(files[1]["file"], "replace")
        self.assertNotIn("add", [c[0] for c in m.cmds])      # not the whole folder
        self.assertNotIn(("random", "0"), m.cmds)

    def test_only_an_m4b_on_its_own_plays_book_style(self):
        b = self.books
        self.assertIsNone(b.single_file("Mixes/set.mp3", 300, 0))
        self.assertIsNone(b.single_file("Mixes/set.mp3", 3600, 0))    # a long song is a song
        self.assertIsNone(b.single_file("http://radio/x", 99999, 0))
        b.m4b_chapters = lambda rel: [{"title": "One", "start": 0.0, "end": 60.0},
                                      {"title": "Two", "start": 60.0, "end": 120.0}]
        got = b.single_file("B/short.m4b", 120, 70)                        # any length
        self.assertEqual((got["chapter"], got["start"], got["length"]), ("Two", 60.0, 60.0))

    def test_settings_check_the_new_values(self):
        with Silent() as buf:
            for key, value in (("bookFolders", ["../etc"]), ("bookFolders", ["/abs"]),
                               ("bookFolders", ["a//b"]), ("bookHeardAfter", 9999),
                               ("voiceSpeed", 10), ("bookMinutes", 10)):   # gone: unknown now
                try:
                    extras.cmd_set_setting(key, json.dumps(value))
                except SystemExit:
                    pass
        self.assertEqual(buf.getvalue().count("error"), 6)

    def test_chapters_of_one_m4b_come_from_inside_it(self):
        self.books.m4b_chapters = lambda rel: [{"title": "One", "start": 0.0, "end": 100.0},
                                               {"title": "Two", "start": 100.0, "end": 250.0}]
        chs = self.books.chapters([{"file": "B/book.m4b", "duration": "250"}])
        self.assertEqual([(c["title"], c["start"], c["length"]) for c in chs],
                         [("One", 0.0, 100.0), ("Two", 100.0, 150.0)])
        self.assertEqual(chs[1]["id"], "book.m4b#100")     # the file's name: a move keeps it

    def test_finished_started_skipped_and_new_stay_apart(self):
        # It used to be "heard" after 30 or 60 seconds, and a tick said so --
        # a twelve-minute chapter sampled for a minute looked done, and once
        # the row filled as well the card contradicted itself (2026-09-25).
        b = self.books
        files = self.files()                       # three chapters of 1500 s
        import library
        keys = [library.chapter_key(f["file"]) for f in files]
        progress = {"ch": {keys[0]: {"at": 45, "far": 45},
                           keys[2]: {"at": 90, "far": 90}}, "last": keys[2]}
        view = lambda: b.book_view("Books/B", files, progress)
        states = lambda: [c["state"] for c in view()]
        # A minute into a 25-minute chapter is started, not finished; the one
        # between was passed over entirely.
        self.assertEqual(states(), ["started", "skipped", "started"])
        self.assertAlmostEqual(view()[0]["part"], 45 / 1500.0, places=3)
        progress["ch"][keys[0]]["far"] = 1490      # to the end of the first
        self.assertEqual(states(), ["finished", "skipped", "started"])
        self.assertEqual(view()[0]["part"], 1.0)
        progress["ch"][keys[2]]["far"] = 1500
        self.assertEqual(states(), ["finished", "skipped", "finished"])
        # Nothing touched at all: nothing is skipped either.
        self.assertEqual([c["state"] for c in b.book_view("Books/B", files, {"ch": {}})],
                         ["new", "new", "new"])

    def test_a_short_chapter_is_not_finished_a_few_seconds_in(self):
        b = self.books
        short = {"length": 120.0}
        self.assertFalse(b.is_finished(short, 6))     # a twentieth, not the end
        self.assertFalse(b.is_finished(short, 60))
        self.assertTrue(b.is_finished(short, 115))
        long_one = {"length": 720.0}
        self.assertFalse(b.is_finished(long_one, 690))
        self.assertTrue(b.is_finished(long_one, 701))   # within 20 s of the end
        self.assertFalse(b.is_finished({"length": 0}, 999))   # no length: never

    def test_carries_on_where_each_chapter_was_left(self):
        b = self.books
        chs = [{"id": "a", "length": 100}, {"id": "b", "length": 100}, {"id": "c", "length": 100}]
        progress = {"ch": {"a": {"at": 40}, "b": {"at": 95}}, "last": "b"}
        self.assertEqual(b.pick(chs, progress, ""), (2, 0))      # b was finished: on to c
        self.assertEqual(b.pick(chs, progress, "a"), (0, 40))    # back to a: where a stopped
        self.assertEqual(b.pick(chs, progress, "b"), (1, 0))     # a finished one starts over
        self.assertEqual(b.pick(chs, {}, ""), (0, 0))

    def test_playing_a_book_queues_it_in_order(self):
        files = self.files()
        m = self.Q(files)
        card.mpd = lambda: m
        import library
        key = library.chapter_key(files[1]["file"])
        with library.edit() as lib:
            rec = library.record(lib, library.audio_id(files))
            rec["listen"].update(ch={key: {"at": 300, "far": 300}}, last=key)
        with Silent():
            self.books.cmd_play_book("Books/B")
        self.assertIn(("random", "0"), m.cmds)
        self.assertEqual([c[1] for c in m.cmds if c[0] == "add"], [f["file"] for f in files])
        self.assertIn(("play", "1"), m.cmds)
        self.assertIn(("seekcur", "300.0"), m.cmds)
        import library
        self.assertEqual(library.playing_folder(), "Books/B")
        self.assertEqual(card.load("last.json", {})["type"], "book")

    def test_listening_is_noted_per_chapter(self):
        files = self.files()
        m = self.Q(files)
        import library
        library.set_playing("Books/B")
        view = lambda f, t, st="play": {"state": st, "elapsed": t, "current": {"file": f}}
        self.books.note(m, view(files[0]["file"], 200))
        self.books.note(m, view(files[0]["file"], 80))           # went back: at moves, far stays
        import library
        got = library.get(library.audio_id(files))["listen"]
        key = library.chapter_key(files[0]["file"])
        self.assertEqual(got["ch"][key], {"at": 80, "far": 200})
        self.assertEqual(got["last"], key)
        self.books.note(m, view("Rock/song.mp3", 10))            # music now: no longer the book
        self.assertEqual(library.playing_folder(), "")

    def test_next_and_previous_move_by_chapter_inside_one_m4b(self):
        f = [{"file": "Books/M/book.m4b", "duration": "300"}]
        self.books.m4b_chapters = lambda rel: [{"title": "One", "start": 0.0, "end": 100.0},
                                               {"title": "Two", "start": 100.0, "end": 200.0},
                                               {"title": "Three", "start": 200.0, "end": 300.0}]

        class P(self.Q):
            def dict(self, cmd, *args):
                return {"state": "play", "elapsed": "150", "song": "0"} if cmd == "status" else (
                    {"file": "Books/M/book.m4b"} if cmd == "currentsong" else {})
        m = P(f)
        card.mpd = lambda: m
        card.save("books.json", {"playing": "Books/M", "books": {"Books/M": {"ch": {
            "Books/M/book.m4b#200": {"at": 30, "far": 30}}}}})
        with Silent():
            self.books.cmd_book_step("next")
        self.assertIn(("seekcur", "230.0"), m.cmds)      # chapter three, where it was left
        self.assertNotIn("next", [c[0] for c in m.cmds])  # not MPD's next: that ends the book
        m.cmds = []
        with Silent():
            self.books.cmd_book_step("previous")
        self.assertIn(("seekcur", "0.0"), m.cmds)
        view = card.status_view(m)
        self.assertEqual((view["kind"], view["current"]["title"], view["current"]["artist"]),
                         ("book", "Two", "M"))

    def test_arguments_are_checked(self):
        for fn, args in ((self.books.cmd_play_book, ("Books/B\nclear",)),
                         (self.books.cmd_book_forget, ("Books/B", "x\ny"))):
            with Silent() as buf:
                try:
                    fn(*args)
                except SystemExit:
                    pass
            self.assertIn("error", buf.getvalue())


class BookText(unittest.TestCase):
    """booktext.py: every kind of text file, down to sentences and words."""

    def setUp(self):
        import booktext
        self.b = booktext

    def sentences(self, name, text):
        return [[" ".join(w for w, t in s) for s in p] for p in self.b.build(self.b.parse_any(name, text))]

    def test_lrc_lines_become_sentences_and_a_pause_a_paragraph(self):
        lrc = ("[ar:x]\n[00:01.00]High above the city. On a tall column,\n"
               "[00:05.00]stood the statue.\n[00:09.00]\n[00:11.00]Mr. Smith agreed!\n")
        self.assertEqual(self.sentences("a.lrc", lrc),
                         [["High above the city.", "On a tall column, stood the statue."],
                          ["Mr. Smith agreed!"]])

    def test_word_times_are_kept_when_the_file_has_them(self):
        paras = self.b.build(self.b.parse_any("a.lrc", "[00:01.00]<00:01.00>One <00:01.50>swallow <00:02.10>flew.\n"))
        self.assertEqual(paras[0][0], [["One", 1.0], ["swallow", 1.5], ["flew.", 2.1]])
        paras = self.b.build(self.b.parse_any("a.vtt", "WEBVTT\n\n00:01.000 --> 00:03.000\n"
                                                        "<00:01.000>Where <00:01.400>shall <00:02.500>I?\n"))
        self.assertEqual(paras[0][0], [["Where", 1.0], ["shall", 1.4], ["I?", 2.5]])

    def test_srt_tags_are_dropped_and_times_spread_over_words(self):
        paras = self.b.build(self.b.parse_any("a.srt", "1\n00:00:01,000 --> 00:00:03,000\n<i>His friends</i> left.\n"))
        words = paras[0][0]
        self.assertEqual([w for w, t in words], ["His", "friends", "left."])
        self.assertTrue(1.0 == words[0][1] < words[1][1] < words[2][1] < 3.0)

    def test_plain_text_has_no_times(self):
        paras = self.b.build(self.b.parse_any("a.txt", "One. Two.\n\nThree."))
        self.assertEqual(len(paras), 2)
        self.assertEqual(paras[0][0][0][1], -1)

    def test_epub_media_overlays_for_the_right_audio_file(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.epub")
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="O/b.opf"/></rootfiles></container>')
                z.writestr("O/b.opf", '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="c" href="c.xhtml" media-overlay="s"/><item id="s" href="c.smil"/></manifest><spine><itemref idref="c"/></spine></package>')
                z.writestr("O/c.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><p><span id="a">It was cold.</span> <span id="b">It rained.</span></p><p><span id="c">Then sun.</span></p></body></html>')
                z.writestr("O/c.smil", '<smil xmlns="http://www.w3.org/ns/SMIL"><body><par><text src="c.xhtml#a"/><audio src="a/ch01.mp3" clipBegin="2s" clipEnd="3.5s"/></par><par><text src="c.xhtml#b"/><audio src="a/ch01.mp3" clipBegin="0:00:03.500" clipEnd="5s"/></par><par><text src="c.xhtml#c"/><audio src="a/ch01.mp3" clipBegin="6s" clipEnd="7s"/></par><par><text src="c.xhtml#c"/><audio src="a/ch02.mp3" clipBegin="0s" clipEnd="1s"/></par></body></smil>')
            paras = self.b.build(self.b.parse_epub(path, "Book/ch01.mp3"))
        self.assertEqual([[" ".join(w for w, t in s) for s in p] for p in paras],
                         [["It was cold.", "It rained."], ["Then sun."]])
        self.assertEqual(paras[0][1][0][1], 3.5)

    def test_times_read_every_way_a_file_writes_them(self):
        c = self.b.clock
        self.assertEqual([c("1:02:03.5"), c("02:03,5"), c("63.5s"), c("1500ms"), c("npt=4")],
                         [3723.5, 123.5, 63.5, 1.5, 4.0])


class BookPlayerPhaseOne(unittest.TestCase):
    """Audiobook player, phase 1 (2026-09-22)."""

    class Q(PhaseTwoSettings.Q):
        pass

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = (card.mpd, card.SLEEP_FADE)
        card.SLEEP_FADE = 0.0

    def tearDown(self):
        card.mpd, card.SLEEP_FADE = self.saved
        self.tmp.cleanup()

    def run_main(self, *argv):
        with Silent() as buf:
            try:
                card.main(list(argv))
            except SystemExit:
                pass
        return buf.getvalue()

    def test_seek_by_takes_whole_seconds_only(self):
        m = self.Q()
        card.mpd = lambda: m
        self.run_main("seek-by", "-10")
        self.run_main("seek-by", "30")
        self.assertEqual([c for c in m.cmds if c[0] == "seekcur"], [("seekcur", "-10"), ("seekcur", "+30")])
        self.assertIn("error", self.run_main("seek-by", "10; clear"))
        self.assertIn("error", self.run_main("seek-by", "99999"))

    def test_sleep_fades_out_then_pauses_and_puts_the_volume_back(self):
        m = self.Q({"state": "play", "volume": "60"})
        card.mpd = lambda: m
        with Silent():
            card.cmd_sleep_fade()
        vols = [int(c[1]) for c in m.cmds if c[0] == "setvol"]
        self.assertEqual(min(vols), 0)
        self.assertLess(m.cmds.index(("setvol", "0")), m.cmds.index(("pause", "1")))
        self.assertEqual(vols[-1], 60)
        card.save("settings.json", {"sleepFade": False})
        m = self.Q({"state": "play", "volume": "60"})
        card.mpd = lambda: m
        with Silent():
            card.cmd_sleep_fade()
        self.assertNotIn("setvol", [c[0] for c in m.cmds])
        self.assertIn(("pause", "1"), m.cmds)


class TerminalReader(unittest.TestCase):
    """reader.py and the book loading behind it (2026-09-22)."""

    def setUp(self):
        import reader
        self.r = reader
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved_cache = booktext_cache = None
        import booktext
        self.bt = booktext
        self.saved = (booktext.CACHE_DIR, card.MUSIC_DIR)
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "cache")

    def tearDown(self):
        self.bt.CACHE_DIR, card.MUSIC_DIR = self.saved
        self.tmp.cleanup()

    def write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_lines_fit_the_width_and_justify_fills_it(self):
        para = [[["word%d" % i, -1] for i in range(40)]]
        for justify in (False, True):
            lines = self.r.layout([para], 30, 1, justify)
            for line in lines:
                if line["words"]:
                    x, text, _ = line["words"][-1]
                    self.assertLessEqual(x + len(text), 30)
        full = [l for l in self.r.layout([para], 30, 1, True) if l["words"]][0]
        x, text, _ = full["words"][-1]
        self.assertEqual(x + len(text), 30)                  # justified to the edge

    def test_a_txt_splits_at_chapter_lines(self):
        path = self.write("n.txt", "Chapter 1: Start\n\nOne. Two.\n\nThree.\n\nChapter 2 - Next\n\nFour.")
        book = self.r.TextBook(path)
        self.assertEqual([c["title"] for c in book.chapters], ["Chapter 1: Start", "Chapter 2 - Next"])
        self.assertEqual(len(book.paras(0)), 2)

    def test_an_epub_reads_by_its_spine_with_titles_from_its_contents(self):
        import zipfile
        path = os.path.join(self.tmp.name, "b.epub")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="O/b.opf"/></rootfiles></container>')
            z.writestr("O/b.opf", '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Tales</dc:title></metadata><manifest><item id="n" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/><item id="a" href="a.xhtml" media-type="application/xhtml+xml"/><item id="c" href="cover.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c"/><itemref idref="a"/></spine></package>')
            z.writestr("O/nav.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><ol><li><a href="a.xhtml">The Giant</a></li></ol></nav></body></html>')
            z.writestr("O/cover.xhtml", '<html><body><img src="c.jpg"/></body></html>')
            z.writestr("O/a.xhtml", '<html><body><h1>The Giant</h1><p>It was cold &amp; <em>grey</em>.&nbsp;He waited.</p><script>alert(1)</script><p>Spring came.</p></body></html>')
        book = self.r.TextBook(path)
        self.assertEqual(book.title, "Tales")
        self.assertEqual([c["title"] for c in book.chapters], ["The Giant"])   # the cover is skipped
        text = [" ".join(w for w, _ in s) for p in book.paras(0) for s in p]
        self.assertIn("It was cold & grey.", text)
        self.assertNotIn("alert(1)", " ".join(text))

    def test_the_screen_shows_the_book_and_hides_it_all_with_z(self):
        path = self.write("n.txt", "Chapter 1\n\nHigh above the city stood the Prince.\n\nChapter 2\n\nA swallow came.")
        _app, scr = self.r.render("70x12", "", path)
        self.assertIn("Chapter 1", scr.text().splitlines()[0])
        self.assertIn("? keys", scr.text().splitlines()[-1])
        _app, scr = self.r.render("70x12", "z", path)
        lines = [l.strip() for l in scr.text().splitlines() if l.strip()]
        self.assertEqual(lines, ["High above the city stood the Prince."])  # only the text
        _app, scr = self.r.render("70x12", "C", path)
        self.assertIn("❯ ●  1  Chapter 1", scr.text())          # numbered, so 600 are findable
        self.assertIn("○  2  Chapter 2", scr.text())
        _app, scr = self.r.render("70x12", "C 2", path)          # a number jumps to it
        self.assertIn("❯ ○  2  Chapter 2", scr.text())
        _app, scr = self.r.render("70x12", "C / z z <enter>", path)   # / searches the titles
        self.assertIn("nothing matching", scr.text())
        _app, scr = self.r.render("70x12", "C / 2 <enter>", path)
        self.assertIn("❯ ○  2  Chapter 2", scr.text())
        self.assertNotIn("Chapter 1", scr.text().split("Chapters")[1])
        _app, scr = self.r.render("70x12", "]", path, save=True)
        self.assertIn("A swallow came.", scr.text())
        _app, scr = self.r.render("70x12", "", path)             # opened again: chapter 2 kept
        self.assertIn("A swallow came.", scr.text())

    def test_the_sentence_being_spoken_is_lit(self):
        app = self.r.Reader(10, 60)
        para = [[["One", 1.0], ["two.", 1.5]], [["Three", 2.0], ["four.", 2.5]]]
        app.lines = self.r.layout([para], 50, 1, False)
        app.left = 5
        app.book = type("B", (), {"audio": True, "chapters": [{"title": "x"}], "title": "t"})()
        app.active = (0, 1, 0)
        scr = self.r.TextScreen(10, 60)
        app.draw_text(scr)
        self.assertEqual(scr.lit_text(), ["Three", "four."])
        card.save("settings.json", {"textHighlight": "word"})
        scr = self.r.TextScreen(10, 60)
        app.draw_text(scr)
        self.assertEqual(scr.lit_text(), ["Three"])

    def test_a_shelf_of_books_is_a_folder_and_each_novel_a_book(self):
        import books
        card.MUSIC_DIR = self.tmp.name
        shelf = os.path.join(self.tmp.name, "books")
        os.makedirs(os.path.join(shelf, "Tale"))
        for n in ("a.epub", "b.txt"):
            open(os.path.join(shelf, n), "w").close()
        os.makedirs(os.path.join(self.tmp.name, "one"))
        open(os.path.join(self.tmp.name, "one", "novel.epub"), "w").close()
        card.save("settings.json", {"bookFolders": ["books", "one"]})

        class M(FakeMpd):
            def records(self, cmd, *args):
                return []
        self.assertEqual(card.book_info(M(), "books"), {"shelf": True})
        self.assertEqual(card.book_info(M(), "one")["file"], "one/novel.epub")
        self.assertEqual(books.text_files("books"), ["a.epub", "b.txt"])

    def test_the_book_list_has_each_novel_and_nothing_gone(self):
        card.MUSIC_DIR = self.tmp.name
        shelf = os.path.join(self.tmp.name, "books")
        os.makedirs(os.path.join(shelf, "Audio"))
        open(os.path.join(shelf, "Audio", "01.mp3"), "w").close()
        for n in ("x.epub", "y.txt"):
            open(os.path.join(shelf, n), "w").close()
        card.save("settings.json", {"bookFolders": ["books"]})
        state = self.r.load_state()
        state["books"] = {"old": {"target": "Gone/away", "t": 9}, "shelf": {"target": "books", "t": 8}}
        got = [(b["kind"], b["target"]) for b in self.r.shelf(state)]
        self.assertEqual(got, [("read", "books/x.epub"), ("read", "books/y.txt"),
                               ("listen", "books/Audio")])

    def test_reader_open_takes_only_books(self):
        import books
        card.MUSIC_DIR = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, "Books", "Tale"))
        started = []
        saved_popen, saved_term = subprocess.Popen, books.default_terminal
        subprocess.Popen = lambda *a, **k: started.append(a[0])
        books.default_terminal = lambda: "foot.desktop"
        try:
            for bad in ("/etc/passwd", "../../etc", "Books/Tale\nx"):
                with Silent() as buf:
                    try:
                        books.cmd_reader_open(bad)
                    except SystemExit:
                        pass
                self.assertIn("error", buf.getvalue(), bad)
            with Silent():
                books.cmd_reader_open("Books/Tale")
        finally:
            subprocess.Popen, books.default_terminal = saved_popen, saved_term
        self.assertEqual(card.load("reader-open.json", {})["target"], "Books/Tale")
        self.assertEqual(len(started), 1)
        self.assertNotIn("Books/Tale", " ".join(started[0]))      # the path goes by file


class ReviewFixes(unittest.TestCase):
    """Found in the review of 2026-09-22."""

    def test_book_text_has_no_control_characters(self):
        import booktext
        text = booktext.plain("Hi\x1b]0;owned\x07 there\x1b[2J \u202eevil")
        self.assertEqual(text, "Hi]0;owned there[2J evil")
        paras, _ = booktext.html_paras(b"<p>A\x1b[31m red</p>")
        self.assertNotIn("\x1b", paras[0])

    def test_wide_characters_take_two_columns(self):
        import reader
        self.assertEqual(reader.cells("修仙"), 4)
        lines = reader.layout([[[["修仙修仙", -1], ["修仙修仙", -1], ["abc", -1]]]], 10, 0, False)
        self.assertEqual(len(lines), 3)                     # 8 + 1 + 8 does not fit in 10

    def test_saves_use_their_own_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            saved = card.STATE_DIR
            card.STATE_DIR = d
            try:
                Settings.real_save("x.json", {"a": 1})
                self.assertEqual(os.listdir(d), ["x.json"])
            finally:
                card.STATE_DIR = saved
        src = open(os.path.join(HERE, "..", "scripts", "card.py")).read()
        self.assertIn("os.getpid(), threading.get_ident()", src)

    def test_the_reader_never_crashes_without_a_book(self):
        import reader, booktext, zipfile
        with tempfile.TemporaryDirectory() as d:
            saved = (card.STATE_DIR, booktext.CACHE_DIR)
            card.STATE_DIR, booktext.CACHE_DIR = d, os.path.join(d, "c")
            try:
                broken = os.path.join(d, "broken.epub")
                zipfile.ZipFile(broken, "w").close()
                empty = os.path.join(d, "empty.txt")
                open(empty, "w").close()
                for target in (broken, empty, "/nowhere.epub"):
                    for keys in ("", "C", "A", "] [", "C <enter>", "L <enter>", "z"):
                        for size in ("1x1", "20x5", "80x24"):
                            reader.render(size, keys, target)
                _app, scr = reader.render("72x12", "", broken)
                self.assertIn("damaged, or not a real EPUB", scr.text())
            finally:
                card.STATE_DIR, booktext.CACHE_DIR = saved

    def test_line_height_reaches_foot_and_indent_moves_first_lines(self):
        import books, reader
        with tempfile.TemporaryDirectory() as d:
            saved = (card.STATE_DIR, books.default_terminal, books.foot_font)
            card.STATE_DIR = d
            books.default_terminal, books.foot_font = (lambda: "foot.desktop"), (lambda: ("Mono", 10.0))
            try:
                card.save("reader.json", {"prefs": {"lineHeight": 1.0}})
                self.assertNotIn("line-height", " ".join(books.launch_command()))
                card.save("reader.json", {"prefs": {"lineHeight": 1.5}})
                self.assertIn("-o main.line-height=19.8 ", books.launch_command()[2])
                card.save("reader.json", {"prefs": {"lineHeight": "1.5; rm -rf ~"}})
                self.assertNotIn("rm -rf", " ".join(books.launch_command()))   # only known values
                books.default_terminal = lambda: "kitty.desktop"
                self.assertEqual(books.launch_command()[0], "omarchy-launch-or-focus-tui")
            finally:
                card.STATE_DIR, books.default_terminal, books.foot_font = saved
        para = [[["one", -1], ["two", -1], ["three", -1], ["four", -1]]]
        lines = reader.layout([para], 12, 0, False, indent=4)
        self.assertEqual(lines[0]["words"][0][0], 4)
        self.assertEqual(lines[1]["words"][0][0], 0)

    def test_a_page_turn_slides_between_the_two_pages(self):
        import reader

        class Win:
            def refresh(self):
                pass
        app = reader.Reader(6, 10)
        before, after = reader.TextScreen(6, 10), reader.TextScreen(6, 10)
        for y in range(6):
            before.put(y, 0, "a" * 10, "")
            after.put(y, 0, "b" * 10, "")
        scr = reader.TextScreen(6, 10)
        saved = reader.TURN_DELAY
        reader.TURN_DELAY = 0
        try:
            reader.slide(Win(), scr, app, before, after, 1)
        finally:
            reader.TURN_DELAY = saved
        self.assertEqual(scr.text().splitlines()[2], "b" * 10)       # the new page, in full

    def test_the_last_page_is_as_short_as_what_is_left(self):
        """Reported 2026-09-30: the last page need not fill the window, and
        should not carry lines over from the page before. It was clamped to a
        screenful from the end, so the last page repeated the bottom of the
        page before it."""
        import reader
        app = reader.Reader(12, 60)
        app.prefs = dict(app.prefs, layout="pages")
        h = app.body_height()
        app.lines = [{"p": 0, "words": [(0, "w%d" % i, (0, i))]} for i in range(h * 3 + 1)]
        self.assertEqual(app.page_count(), 4)
        # The last page starts one line from the end, not a screenful from it.
        self.assertEqual(app.last_top(), h * 3)
        app.set_top(10 ** 6)
        self.assertEqual(app.top, h * 3)
        self.assertEqual(len(app.lines) - app.top, 1)          # one line, and only it
        # Scrolling never lands anywhere else either.
        app.top = 0
        for _ in range(10):
            app.scroll(h)
        self.assertEqual(app.top, h * 3)
        # A continuous scroll still fills the window: the change is paging only.
        app.prefs = dict(app.prefs, layout="scroll")
        self.assertEqual(app.last_top(), len(app.lines) - h)

    def test_the_mouse_chooses_a_word_a_sentence_or_a_line(self):
        """His ask, 2026-09-30. One click a word, two a sentence, three the
        line. The clicks are counted here, not by ncurses, because whether a
        terminal reports a double click at all varies."""
        import curses
        import reader
        with tempfile.TemporaryDirectory() as d:
            import booktext
            saved = (card.STATE_DIR, booktext.CACHE_DIR)
            card.STATE_DIR = d
            booktext.CACHE_DIR = os.path.join(d, "c")
            try:
                path = os.path.join(d, "n.txt")
                with open(path, "w") as f:
                    f.write("Chapter 1\n\nThe swallow flew on. He was very tired indeed.\n")
                app, scr = reader.render("80x14", "", path)
                app.draw(scr)
                x, _text, _idx = app.lines[app.top]["words"][2]
                y, col = app.body_top(), app.left + x
                press = curses.BUTTON1_PRESSED
                app.mouse(y, col, press)
                self.assertEqual((app.pick_kind, app.pick_text()), ("word", "flew"))
                app.mouse(y, col, press)
                self.assertEqual(app.pick_kind, "sentence")
                self.assertEqual(app.pick_text(), "The swallow flew on.")
                app.mouse(y, col, press)
                self.assertEqual(app.pick_kind, "line")
                self.assertIn("very tired", app.pick_text())
                # A click that is not part of a run starts again at one word.
                app.clicks = (0.0, -1, -1, 0)
                app.mouse(y, col, press)
                self.assertEqual(app.pick_kind, "word")
                # The other way round, and the one this terminal actually uses:
                # measured 2026-09-30, ncurses gives ONE event per run of
                # clicks, already counted. Counting them again here saw a
                # double click as a single one, so both paths are kept.
                app.clicks = (0.0, -1, -1, 0)
                app.mouse(y, col, curses.BUTTON1_DOUBLE_CLICKED)
                self.assertEqual(app.pick_kind, "sentence")
                app.clicks = (0.0, -1, -1, 0)
                app.mouse(y, col, curses.BUTTON1_TRIPLE_CLICKED)
                self.assertEqual(app.pick_kind, "line")
                # A counted click straight after a synthesised one is one word,
                # not the next step of a run that ncurses already finished.
                app.mouse(y, col, press)
                self.assertEqual(app.pick_kind, "word")
                # Clicking where there is no text chooses nothing new.
                app.pick = None
                self.assertFalse(app.pick_at(app.rows - 1, 0))
                self.assertIsNone(app.at_point(app.rows - 1, 0))
            finally:
                card.STATE_DIR, booktext.CACHE_DIR = saved

    def test_a_right_click_offers_what_can_be_done_and_a_click_does_it(self):
        import curses
        import reader
        with tempfile.TemporaryDirectory() as d:
            import booktext
            saved = (card.STATE_DIR, booktext.CACHE_DIR)
            card.STATE_DIR = d
            booktext.CACHE_DIR = os.path.join(d, "c")
            try:
                path = os.path.join(d, "n.txt")
                with open(path, "w") as f:
                    f.write("Chapter 1\n\nThe swallow flew on. He was very tired indeed.\n")
                app, scr = reader.render("80x14", "", path)
                app.draw(scr)
                x, _t, _i = app.lines[app.top]["words"][2]
                y, col = app.body_top(), app.left + x
                app.mouse(y, col, curses.BUTTON3_PRESSED)
                self.assertEqual(app.overlay, "menu")
                actions = [a for _label, a in app.menu]
                self.assertEqual(actions[:2], ["read", "meaning"])
                for wanted in ("copy", "find", "mark", "kind:sentence", "kind:line"):
                    self.assertIn(wanted, actions)
                # A dictionary takes one word, so the meaning is named for the
                # word under the pointer, never for a whole sentence.
                self.assertIn("flew", app.menu[1][0])
                app.draw(scr)
                y0, x0, width, height = app.panel_box
                self.assertEqual(app.menu_row(y0 + 1, x0 + 2), 0)
                self.assertIsNone(app.menu_row(y0 + height + 4, x0))   # outside it
                # Clicking a row does that row's action.
                app.mouse(y0 + 1 + actions.index("kind:line"), x0 + 2, curses.BUTTON1_PRESSED)
                self.assertIsNone(app.overlay)
                self.assertEqual(app.pick_kind, "line")
                # Clicking away closes it and leaves the choice alone.
                app.mouse(y, col, curses.BUTTON3_PRESSED)
                app.draw(scr)
                app.mouse(app.rows - 1, 0, curses.BUTTON1_PRESSED)
                self.assertIsNone(app.overlay)
                self.assertIsNotNone(app.pick)
            finally:
                card.STATE_DIR, booktext.CACHE_DIR = saved

    def test_a_page_turn_is_eased_and_keeps_to_a_clock(self):
        """His ask, 2026-09-30, that the turn feel smooth. Evenly spaced frames
        read as mechanical; ease-out cubic was worse, landing four frames on
        the same column at the end, which looks like a stall."""
        import reader
        self.assertEqual(reader.ease(0), 0.0)
        self.assertEqual(reader.ease(1), 1.0)
        self.assertEqual(reader.ease(0.5), 0.5)
        self.assertEqual(reader.ease(9.0), 1.0)              # nonsense is clamped
        steps = [reader.ease(i / reader.TURN_FRAMES) for i in range(1, reader.TURN_FRAMES + 1)]
        self.assertEqual(steps, sorted(steps))               # never goes backwards
        self.assertLess(steps[0], 0.1)                       # sets off gently
        # Symmetric: it eases in exactly as much as it eases out.
        for t in (0.1, 0.25, 0.4):
            self.assertAlmostEqual(reader.ease(t) + reader.ease(1 - t), 1.0, places=9)
        self.assertEqual(len(set(steps)), len(steps))        # no frame stands still

        class Win:
            def __init__(self): self.n = 0
            def refresh(self): self.n += 1
        saved = reader.TURN_DELAY
        reader.TURN_DELAY = 0
        try:
            for move in (reader.slide, reader.wipe):
                app = reader.Reader(16, 40)
                before, after = reader.TextScreen(16, 40), reader.TextScreen(16, 40)
                for yy in range(16):
                    before.put(yy, 0, "a" * 40, "")
                    after.put(yy, 0, "b" * 40, "")
                for direction in (1, -1):
                    scr, win = reader.TextScreen(16, 40), Win()
                    move(win, scr, app, before, after, direction)
                    self.assertEqual(scr.text().splitlines()[8], "b" * 40, move.__name__)
                    self.assertGreater(win.n, 0)
        finally:
            reader.TURN_DELAY = saved
        self.assertEqual(reader.ANIMATIONS, ("slide", "scroll", "none"))

    def test_the_zoom_is_kept_for_the_next_window(self):
        import books, reader
        with tempfile.TemporaryDirectory() as d:
            saved = (card.STATE_DIR, reader.cell_width, books.foot_font, books.default_terminal)
            card.STATE_DIR = d
            books.foot_font = lambda: ("JetBrainsMono Nerd Font", 9.0)
            books.default_terminal = lambda: "foot.desktop"
            try:
                app = reader.Reader(20, 80)
                self.assertEqual(app.prefs["width"], 140)            # his defaults
                self.assertEqual(app.prefs["lineHeight"], 1.2)
                app.base_size, app.base_cell = 9.0, 14.0
                reader.cell_width = lambda: 17.5                     # Ctrl + in foot
                app.note_zoom()
                self.assertEqual(app.prefs["fontSize"], 11.0)
                card.save("reader.json", {"prefs": app.prefs})
                command = books.launch_command()[2]
                self.assertIn("'main.font=JetBrainsMono Nerd Font:size=11'", command)
                self.assertIn("main.line-height=17.4", command)      # 11 × 1.32 × 1.2
                reader.cell_width = lambda: 14.0                     # back to normal
                app.note_zoom()
                self.assertIsNone(app.prefs["fontSize"])
                card.save("reader.json", {"prefs": {"fontSize": "11; rm -rf ~"}})
                self.assertNotIn("rm -rf", books.launch_command()[2])
            finally:
                card.STATE_DIR, reader.cell_width, books.foot_font, books.default_terminal = saved

    def test_a_word_longer_than_the_line_wraps(self):
        import reader
        lines = reader.layout([[[["x" * 25, -1]]]], 10, 0, False)
        self.assertEqual(["".join(t for _x, t, _i in l["words"]) for l in lines], ["x" * 10, "x" * 10, "x" * 5])

    def test_favorites_change_one_at_a_time(self):
        src = open(os.path.join(HERE, "..", "scripts", "card.py")).read()
        body = src[src.index("def cmd_fav_toggle"):src.index("def cmd_play_favorites")]
        self.assertIn("fcntl.flock", body)


class BookLibrary(unittest.TestCase):
    """library.py: one record per book, known by what the book is (2026-09-22)."""

    def setUp(self):
        import library
        self.lib = library
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = (card.MUSIC_DIR, card.mpd)
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        os.makedirs(card.MUSIC_DIR)

    def tearDown(self):
        card.MUSIC_DIR, card.mpd = self.saved
        self.tmp.cleanup()

    @staticmethod
    def files(folder, names=("01 one.mp3", "02 two.mp3"), secs=(1500, 1600)):
        return [{"file": "%s/%s" % (folder, n), "duration": str(secs[i])}
                for i, n in enumerate(names)]

    def test_a_book_keeps_its_name_when_the_folder_moves(self):
        here = self.files("Audiobooks/Tale")
        moved = self.files("books/Shelf/Tale")
        self.assertEqual(self.lib.audio_id(here), self.lib.audio_id(moved))
        # A different book: another file, or another length.
        self.assertNotEqual(self.lib.audio_id(here), self.lib.audio_id(self.files("x", secs=(1500, 9))))
        self.assertNotEqual(self.lib.audio_id(here),
                            self.lib.audio_id(self.files("x", names=("01 one.mp3", "03 three.mp3"))))

    def test_chapters_are_named_by_their_file_not_their_folder(self):
        self.assertEqual(self.lib.chapter_key("books/Tale/01 one.mp3"), "01 one.mp3")
        self.assertEqual(self.lib.chapter_key("books/Tale/b.m4b", 1475.4), "b.m4b#1475")

    def test_a_novel_is_known_by_its_bytes(self):
        one = os.path.join(self.tmp.name, "a.epub")
        two = os.path.join(self.tmp.name, "moved.epub")
        with open(one, "w") as f:
            f.write("a book")
        with open(two, "w") as f:
            f.write("a book")
        self.assertEqual(self.lib.text_id(one), self.lib.text_id(two))
        self.assertTrue(self.lib.text_id(one).startswith("t-"))
        self.assertEqual(self.lib.text_id("/nowhere.epub"), "")
        with open(two, "w") as f:
            f.write("another book")
        self.assertNotEqual(self.lib.text_id(one), self.lib.text_id(two))

    def test_two_writers_do_not_lose_each_others_changes(self):
        with self.lib.edit() as lib:
            self.lib.record(lib, "a-1")["listen"]["ch"]["one.mp3"] = {"at": 5, "far": 5}
        with self.lib.edit() as lib:
            self.lib.record(lib, "a-2")["read"]["chapter"] = 3
        data = self.lib.load()
        self.assertEqual(sorted(data["books"]), ["a-1", "a-2"])
        self.assertEqual(data["books"]["a-1"]["listen"]["ch"]["one.mp3"]["far"], 5)

    def test_the_old_records_move_over_even_when_the_book_moved(self):
        # The book was in Audiobooks/Tale and is now in books/Tale.
        os.makedirs(os.path.join(card.MUSIC_DIR, "books", "Tale"))
        card.save("settings.json", {"bookFolders": ["books"]})
        moved = self.files("books/Tale")

        class M(FakeMpd):
            def records(self, cmd, folder=""):
                return [dict(f, _type="file") for f in moved] if folder == "books/Tale" else []
        card.mpd = lambda: M()
        card.save("books.json", {"playing": None, "books": {"Audiobooks/Tale": {
            "ch": {"Audiobooks/Tale/01 one.mp3": {"at": 90, "far": 120}},
            "last": "Audiobooks/Tale/01 one.mp3", "t": 100}}})
        card.save("reader.json", {"prefs": {"width": 140}, "last": "book:Audiobooks/Tale",
                                  "books": {"book:Audiobooks/Tale": {"chapter": 1, "para": 7, "t": 100}}})
        data = self.lib.load()
        rec = data["books"][self.lib.audio_id(moved)]
        self.assertEqual(rec["listen"]["ch"], {"01 one.mp3": {"at": 90, "far": 120}})
        self.assertEqual(rec["listen"]["last"], "01 one.mp3")
        self.assertEqual(rec["read"]["chapter"], 1)
        self.assertEqual(rec["path"], "books/Tale")
        # The reader keeps its settings; its places are gone from that file.
        reader_state = card.load("reader.json", {})
        self.assertEqual(reader_state["prefs"]["width"], 140)
        self.assertNotIn("books", reader_state)


class ReaderPlaces(unittest.TestCase):
    """The reading place is a word, so no layout change can move it."""

    def setUp(self):
        import booktext
        import library
        import reader
        self.r, self.bt, self.lib = reader, booktext, library
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = booktext.CACHE_DIR
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "c")

    def tearDown(self):
        self.bt.CACHE_DIR = self.saved
        self.tmp.cleanup()

    def test_the_place_survives_a_different_width(self):
        path = os.path.join(self.tmp.name, "n.txt")
        with open(path, "w") as f:
            f.write("Chapter 1\n\n" + " ".join("word%d." % i for i in range(400)))
        app, _scr = self.r.render("80x12", "<pgdn> <pgdn>", path, save=True)
        word = app.word_of_line(app.top)
        self.assertGreater(word, 0)
        rec = self.lib.get(self.lib.text_id(path))["read"]
        self.assertEqual(rec["word"], word)
        # Opened again in a narrower window: the same word starts the screen.
        app2, _scr2 = self.r.render("50x12", "", path)
        self.assertEqual(app2.word_of_line(app2.top), word)


class ReaderPhaseTwo(unittest.TestCase):
    """The reader's library screen, search, bookmarks and reading time."""

    def setUp(self):
        import booktext
        import library
        import reader
        self.r, self.bt, self.lib = reader, booktext, library
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = (booktext.CACHE_DIR, card.MUSIC_DIR)
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "c")
        card.MUSIC_DIR = self.tmp.name

    def tearDown(self):
        self.bt.CACHE_DIR, card.MUSIC_DIR = self.saved
        self.tmp.cleanup()

    def book(self, name="n.txt", text=None):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            f.write(text or ("Chapter 1\n\nThe swallow flew over the city.\n\n"
                             "Chapter 2\n\nThe reed bowed low. The swallow left."))
        return path

    def app_for(self, path, keys=""):
        app, scr = self.r.render("80x16", keys, path)
        return app, scr

    def test_search_finds_every_place_in_the_book(self):
        app, _ = self.app_for(self.book())
        app.search_book("swallow")
        self.assertEqual([(h["chapter"], h["word"]) for h in app.hits], [(0, 1), (1, 5)])
        self.assertIn("swallow", app.hits[0]["snippet"])
        app.jump_to(1, 5)                       # a hit takes you there
        self.assertEqual(app.ci, 1)
        app.search_book("nothing here")
        self.assertEqual(app.hits, [])

    def test_a_bookmark_keeps_its_place_and_its_note(self):
        app, _ = self.app_for(self.book())
        app.add_mark()
        self.assertEqual(app.input["kind"], "note")           # ready for a note
        for ch in "later":
            app.key(ch)
        app.key(10)
        marks = self.lib.get(app.book.key)["bookmarks"]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["note"], "later")
        self.assertEqual(marks[0]["kind"], "read")
        app.marks = marks
        app.sel = 0
        self.lib.drop_mark(app.book.key, 0)
        self.assertEqual(self.lib.get(app.book.key)["bookmarks"], [])

    def test_the_book_list_sorts_filters_and_searches(self):
        app, _ = self.app_for(self.book())
        app.shelf = [
            {"title": "Anna", "percent": 0.5, "t": 10, "state": "reading", "kind": "read",
             "target": "a", "label": "Anna"},
            {"title": "Zoe", "percent": 1.0, "t": 30, "state": "finished", "kind": "read",
             "target": "z", "label": "Zoe"},
            {"title": "Mid", "percent": 0.0, "t": 20, "state": "new", "kind": "listen",
             "target": "m", "label": "Mid"}]
        self.assertEqual([b["title"] for b in app.visible_shelf()], ["Zoe", "Mid", "Anna"])
        app.sort = "title"
        self.assertEqual([b["title"] for b in app.visible_shelf()], ["Anna", "Mid", "Zoe"])
        app.sort = "progress"
        self.assertEqual([b["title"] for b in app.visible_shelf()], ["Zoe", "Anna", "Mid"])
        app.filter = "reading"
        self.assertEqual([b["title"] for b in app.visible_shelf()], ["Anna"])
        app.filter, app.query = "all", "zo"
        self.assertEqual([b["title"] for b in app.visible_shelf()], ["Zoe"])

    def test_the_whole_book_progress_counts_every_chapter(self):
        app, _ = self.app_for(self.book())
        app.ci = 0
        app.top = 0
        first = app.book_pct()
        app.ci = 1
        app.load_chapter()
        self.assertGreater(app.book_pct(), first)
        self.assertLessEqual(app.book_pct(), 1.0)

    def test_reading_time_counts_only_while_he_reads(self):
        app, _ = self.app_for(self.book())
        app.last_key = time.time()
        app.last_tick = time.time() - 4
        app.tick()
        self.assertGreater(app.read_seconds, 3)
        app.last_key = time.time() - 600                      # left it lying open
        app.last_tick = time.time() - 4
        before = app.read_seconds
        app.tick()
        self.assertEqual(app.read_seconds, before)
        app.read_seconds = 65
        app.tick()
        self.assertLess(app.read_seconds, 5)                  # written down
        stats = self.lib.reading_time()
        self.assertGreaterEqual(stats["today"], 65)
        self.assertEqual(stats["streak"], 1)


class VoiceConnector(unittest.TestCase):
    """voices.py and speak.py: any engine, and a chapter becoming audio."""

    def setUp(self):
        import speak
        import voices
        self.voices, self.speak = voices, speak
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = os.path.join(self.tmp.name, "state")
        card.load, card.save = Settings.real_load, Settings.real_save
        import booktext
        self.saved = (card.MUSIC_DIR, card.MPD_PORT, booktext.CACHE_DIR)
        self.bt = booktext
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        card.MPD_PORT = 6123                       # nothing there: no MPD is touched
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        os.makedirs(card.MUSIC_DIR)
        # The test hum, so these tests do not ask what is installed on the
        # machine running them: voices.chosen() never picks "tone" by itself,
        # so with no real engine here cmd_speak refused and two tests errored
        # (found 2026-09-30 on a machine with no engine at all).
        card.save("settings.json", {"voiceEngine": "tone"})

    def tearDown(self):
        card.MUSIC_DIR, card.MPD_PORT, self.bt.CACHE_DIR = self.saved
        self.tmp.cleanup()

    def novel(self, text=None):
        path = os.path.join(self.tmp.name, "novel.txt")
        with open(path, "w") as f:
            f.write(text or "Chapter 1\n\nThe swallow flew. He was tired.\n\nChapter 2\n\nDawn came.")
        return path

    def test_every_engine_is_listed_with_how_to_get_it(self):
        engines = self.voices.ready_engines()
        self.assertEqual([e["id"] for e in engines],
                         ["kokoro", "piper", "espeak", "edge", "custom", "tone"])
        for e in engines:
            self.assertTrue(e["install"] and e["about"])
        self.assertTrue(any(e["ready"] for e in engines))      # the test hum, at least

    def test_one_engine_says_one_piece_of_text(self):
        out = os.path.join(self.tmp.name, "a.wav")
        self.voices.speak("tone", "Two short words.", out)
        self.assertGreater(os.path.getsize(out), 1000)
        self.assertGreater(self.voices.audio_seconds(out), 0.3)
        with self.assertRaises(RuntimeError):
            self.voices.speak("no-such-engine", "hi", out)
        with self.assertRaises(RuntimeError):
            self.voices.speak("tone", "   ", out)

    def test_his_own_command_gets_the_text_in_a_file_and_no_shell(self):
        helper = os.path.join(self.tmp.name, "say.py")
        with open(helper, "w") as f:
            f.write("import sys, subprocess\n"
                    "text = open(sys.argv[1]).read()\n"
                    "subprocess.run(['ffmpeg','-y','-f','lavfi','-i','sine=d=%.2f' % (len(text)/20),"
                    "'-ac','1', sys.argv[2]], capture_output=True)\n")
        out = os.path.join(self.tmp.name, "b.wav")
        card.save("settings.json", {"voiceCommand": "python3 %s {text} {out}" % helper})
        self.voices.speak("custom", "A sentence for the command.", out)
        self.assertGreater(os.path.getsize(out), 1000)
        self.assertFalse(os.path.exists(out + ".txt"))          # the text file is cleaned up
        card.save("settings.json", {"voiceCommand": ""})
        with self.assertRaises(RuntimeError):
            self.voices.speak("custom", "hi", out)

    def test_a_chapter_becomes_audio_with_its_timings(self):
        card.save("settings.json", {"voiceEngine": "tone"})
        rel = self.speak.make_chapter(self.novel(), 0, token="")
        full = os.path.join(card.MUSIC_DIR, rel)
        self.assertTrue(rel.startswith("Spoken/"))
        self.assertGreater(os.path.getsize(full), 2000)
        lrc = os.path.splitext(full)[0] + ".lrc"
        text = self.bt.text_for(rel)
        said = [" ".join(w for w, _t in s) for p in text["paras"] for s in p]
        self.assertEqual(said, ["The swallow flew.", "He was tired."])
        times = [s[0][1] for p in text["paras"] for s in p]
        self.assertEqual(times, sorted(times))                  # each sentence after the last
        self.assertGreater(times[-1], 0)
        before = os.path.getmtime(full)
        self.assertEqual(self.speak.make_chapter(self.novel(), 0, token=""), rel)
        self.assertEqual(os.path.getmtime(full), before)        # made once, kept
        self.assertTrue(os.path.exists(lrc))

    def test_a_books_name_cannot_escape_the_spoken_folder(self):
        self.assertEqual(self.speak.safe_name("../../etc/passwd"), "etc passwd")
        self.assertEqual(self.speak.safe_name("a/b\nc"), "a b c")
        self.assertEqual(self.speak.safe_name(""), "Book")
        self.assertNotIn("..", self.speak.book_folder("../.."))

    def test_only_one_chapter_is_spoken_at_a_time(self):
        # Three jobs ran at once on 2026-09-23 (three presses of p), and the
        # fan with them. A job holds a lock; a second one is turned away.
        holder = self.speak.claim()
        self.assertIsNotNone(holder)
        try:
            self.assertIsNone(self.speak.claim())         # nobody else gets in
            card.save(self.speak.SPOKEN, {"state": "making", "book": "A book",
                                          "chapter": 2, "percent": 40})
            self.assertEqual(self.speak.busy().get("percent"), 40)
            started = []
            saved = subprocess.Popen
            subprocess.Popen = lambda *a, **k: started.append(a[0])
            try:
                with Silent() as buf:
                    self.speak.cmd_speak(self.novel(), "0")
                answer = json.loads(buf.getvalue().strip().splitlines()[-1])
            finally:
                subprocess.Popen = saved
            # A second ask does not race it and is not thrown away either:
            # the chapter joins the queue and this job takes it next.
            self.assertTrue(answer.get("ok"))
            self.assertEqual(answer["added"], [0])
            self.assertEqual(answer["token"], "")         # no second job
            self.assertEqual(started, [])                 # nothing else started
        finally:
            holder.close()
        self.assertEqual(self.speak.busy(), {})           # the lock is free again

    def test_stopping_is_not_wiped_by_the_job_writing_its_progress(self):
        token = "t-1"
        card.save(self.speak.SPOKEN, {"state": "making", "token": token})
        self.assertTrue(self.speak.wanted(token))
        with Silent():
            self.speak.cmd_speak_stop()
        self.assertFalse(self.speak.wanted(token))
        self.speak.progress(state="making", token=token, percent=50)   # the job carries on writing
        self.assertFalse(self.speak.wanted(token))        # and still must stop
        self.speak.clear_stop()
        self.assertTrue(self.speak.wanted(token))

    def test_cleaning_old_voices_judges_each_book_folder_on_its_own(self):
        # Two books can hold a chapter of the same name. Cleaning once kept
        # the names from every folder together, so a good chapter in one book
        # saved a hum chapter with the same name in another.
        card.save("settings.json", {"voiceEngine": "kokoro", "voiceName": "af_heart"})
        opts = self.speak.settings()
        stale = dict(opts, voice="af_bella")
        made = []
        for book, voice in (("One", opts), ("Two", stale)):
            rel = self.speak.book_folder(book)
            os.makedirs(os.path.join(card.MUSIC_DIR, rel))
            name = "001 Chapter One.mp3"
            for ext in (".mp3", ".lrc"):
                with open(os.path.join(card.MUSIC_DIR, rel, "001 Chapter One" + ext), "w") as f:
                    f.write("x" * 2000)
            self.speak.note_made(rel, name, voice)
            made.append(os.path.join(card.MUSIC_DIR, rel, name))
        with Silent() as buf:
            self.speak.cmd_spoken_clean()
        answer = json.loads(buf.getvalue().strip().splitlines()[-1])
        self.assertEqual((answer["removed"], answer["kept"]), (1, 1))
        self.assertTrue(os.path.exists(made[0]))               # the chosen voice stays
        self.assertFalse(os.path.exists(made[1]))              # the old voice goes
        self.assertFalse(os.path.exists(made[1][:-4] + ".lrc"))
        root = os.path.join(card.MUSIC_DIR, self.speak.settings()["folder"])
        self.assertFalse(os.path.exists(os.path.join(root, self.speak.MADE_BY)))
        self.assertEqual(list(self.speak.made_by(self.speak.book_folder("Two"))), [])

    def test_a_half_made_chapter_leaves_no_workroom_behind(self):
        card.save("settings.json", {"voiceEngine": "tone"})
        def half(engine, texts, outs, voice="", speed=1.0):
            with open(outs[0], "w") as f:       # one sentence written, then it fails
                f.write("x" * 100)
            raise RuntimeError("no")
        saved = self.voices.speak_many
        self.voices.speak_many = half
        try:
            with self.assertRaises(RuntimeError):
                self.speak.make_chapter(self.novel(), 0, token="")
        finally:
            self.voices.speak_many = saved
        left = [n for _d, _s, names in os.walk(card.MUSIC_DIR) for n in names]
        self.assertEqual([n for n in left if n.endswith(".wav")], [])
        self.assertEqual([d for _r, dirs, _n in os.walk(card.MUSIC_DIR)
                          for d in dirs if d.endswith(".parts")], [])

    def test_speaking_takes_only_a_book_and_can_be_stopped(self):
        started = []
        saved = subprocess.Popen
        subprocess.Popen = lambda *a, **k: started.append(a[0])
        try:
            for bad in ("/etc/passwd", "novel.txt\nclear", os.path.join(self.tmp.name, "nope.epub")):
                with Silent() as buf:
                    try:
                        self.speak.cmd_speak(bad)
                    except SystemExit:
                        pass
                self.assertIn("error", buf.getvalue(), bad)
            path = self.novel()
            with Silent() as buf:
                self.speak.cmd_speak(path, "0")
            token = json.loads(buf.getvalue().strip().splitlines()[-1])["token"]
            self.assertEqual(len(started), 1)
            self.assertTrue(self.speak.wanted(token))
            with Silent():
                self.speak.cmd_speak_stop()
            self.assertFalse(self.speak.wanted(token))
        finally:
            subprocess.Popen = saved


class BeatFeed(unittest.TestCase):
    """The dancer's feed (MPD's fifo output) is switched with the dancer;
    the speakers are never touched."""

    class Q(FakeMpd):
        def __init__(self):
            super().__init__()
            self.cmds = []

        def raw(self, cmd, *args):
            self.cmds.append((cmd,) + args)
            if cmd == "outputs":
                return [("outputid", "0"), ("outputname", "Speakers"), ("plugin", "pipewire"),
                        ("outputenabled", "1"),
                        ("outputid", "1"), ("outputname", "Visualizer feed"), ("plugin", "fifo"),
                        ("outputenabled", "1")]
            return []

    def run_feed(self, state):
        m = self.Q()
        saved = card.mpd
        card.mpd = lambda: m
        try:
            with Silent() as buf:
                try:
                    card.cmd_beat_feed(state)
                except SystemExit:
                    pass
        finally:
            card.mpd = saved
        return m.cmds, buf.getvalue()

    def test_off_switches_only_the_feed(self):
        cmds, _ = self.run_feed("off")
        self.assertIn(("disableoutput", "1"), cmds)
        self.assertNotIn(("disableoutput", "0"), cmds)

    def test_on_leaves_an_enabled_feed_alone(self):
        cmds, _ = self.run_feed("on")
        self.assertEqual([c for c in cmds if c[0] in ("enableoutput", "disableoutput")], [])

    def test_anything_else_is_refused(self):
        cmds, answer = self.run_feed("0\ndisableoutput 0")
        self.assertIn("error", answer)
        self.assertEqual(cmds, [])


class RadioNextPrevious(unittest.TestCase):
    """Playing a station from a list queues the list, so next/previous (the
    card, the media keys, rmpc) move between stations."""

    class Q(FakeMpd):
        def __init__(self):
            super().__init__()
            self.cmds, self.next_id = [], 0

        def raw(self, cmd, *args):
            self.cmds.append((cmd,) + args)
            if cmd == "addid":
                self.next_id += 1
                return [("Id", str(self.next_id))]
            return []

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = (card.mpd, card.count_click, card.public_host)
        self.m = self.Q()
        card.mpd = lambda: self.m
        card.count_click = lambda uuid: None
        card.public_host = lambda host: host.endswith(".example")

    def tearDown(self):
        card.mpd, card.count_click, card.public_host = self.saved
        self.tmp.cleanup()

    def play(self, chosen, listed):
        with Silent() as buf:
            try:
                card.cmd_play_station(json.dumps(chosen), "replace", json.dumps(listed))
            except SystemExit:
                pass
        return json.loads(buf.getvalue())

    def test_whole_list_queued_in_order_and_the_chosen_one_plays(self):
        listed = [{"url": "https://%s.example/s" % n, "name": n} for n in ("a", "b", "c")]
        self.play(listed[1], listed)
        added = [c[1] for c in self.m.cmds if c[0] == "addid"]
        self.assertEqual(added, [s["url"] for s in listed])
        self.assertIn(("playid", "2"), self.m.cmds)
        self.assertEqual(self.m.cmds[0], ("clear",))
        self.assertEqual(card.station_name("https://c.example/s"), "c")

    def test_dead_and_private_stations_are_left_out(self):
        card.save("quality.json", {"https://dead.example/s": {"tier": "bad", "at": time.time()}})
        listed = [{"url": "https://a.example/s", "name": "a"},
                  {"url": "https://dead.example/s", "name": "dead"},
                  {"url": "http://192.168.1.1/s", "name": "router"},
                  {"url": "http://b.example/x\nclear", "name": "inject"},
                  {"url": "https://b.example/s", "name": "b"}]
        self.play(listed[0], listed)
        added = [c[1] for c in self.m.cmds if c[0] == "addid"]
        self.assertEqual(added, ["https://a.example/s", "https://b.example/s"])

    def test_a_tier_from_the_caller_is_not_trusted(self):
        listed = [{"url": "https://a.example/s", "name": "a"},
                  {"url": "http://10.0.0.1/s", "name": "lan", "tier": "good"}]
        self.play(listed[0], listed)
        self.assertEqual([c[1] for c in self.m.cmds if c[0] == "addid"], ["https://a.example/s"])

    def test_without_a_list_one_station(self):
        with Silent():
            card.cmd_play_station(json.dumps({"url": "https://a.example/s", "name": "a"}))
        self.assertEqual(len([c for c in self.m.cmds if c[0] == "addid"]), 1)

    def test_a_station_that_fails_in_a_list_moves_on(self):
        m = self.Q()
        note = card.recover(m, {"file": "https://a.example/s", "pos": 0, "play": False})
        self.assertIn("next", note)
        self.assertNotIn("play", [c[0] for c in m.cmds])

    def test_names_follow_the_station_playing(self):
        listed = [{"url": "https://%s.example/s" % n, "name": n.upper()} for n in ("a", "b")]
        self.play(listed[0], listed)

        class S(FakeMpd):
            def dict(self, cmd, *args):
                if cmd == "status":
                    return {"state": "play", "song": "1", "playlistlength": "2"}
                return {"file": "https://b.example/s", "Name": "ICY name"}
        view = card.status_view(S())
        self.assertEqual((view["kind"], view["station"]), ("radio", "B"))


class YouTubeSignIn(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save
        self.home = tempfile.TemporaryDirectory()
        self.saved_browsers = dict(extras.BROWSERS)
        prof = os.path.join(self.home.name, "zen", "abc.Default (release)")
        os.makedirs(prof)
        open(os.path.join(prof, "cookies.sqlite"), "w").close()
        self.prof = prof
        for k in list(extras.BROWSERS):
            extras.BROWSERS[k] = ((os.path.join(self.home.name, "none-" + k),),) + extras.BROWSERS[k][1:]
        extras.BROWSERS["zen"] = ((os.path.join(self.home.name, "zen"),), "cookies.sqlite", "firefox")

    def tearDown(self):
        extras.BROWSERS.clear()
        extras.BROWSERS.update(self.saved_browsers)
        self.tmp.cleanup()
        self.home.cleanup()

    def set(self, value):
        with Silent() as buf:
            try:
                extras.cmd_set_setting("youtubeLogin", json.dumps(value))
            except SystemExit:
                pass
        return json.loads(buf.getvalue())

    def test_off_by_default(self):
        self.assertEqual(extras.settings()["youtubeLogin"], "")
        self.assertEqual(card.auth_args(), [])

    def test_only_a_browser_found_on_disk(self):
        self.assertIn("error", self.set("chrome"))          # known, but not installed
        self.assertIn("error", self.set("firefox:/etc"))    # typed text is never a spec
        self.assertIn("error", self.set("--exec rm -rf ~")) # nor an option
        self.assertEqual(extras.settings()["youtubeLogin"], "")
        self.assertEqual(self.set("zen")["settings"]["youtubeLogin"], "zen")

    def test_spec_is_built_from_the_profile_found(self):
        self.set("zen")
        self.assertEqual(card.auth_args(), ["--cookies-from-browser", "firefox:" + self.prof])

    def test_a_profile_yt_dlp_would_misread_is_refused(self):
        odd = os.path.join(self.home.name, "zen", "x::y")
        os.makedirs(odd)
        open(os.path.join(odd, "cookies.sqlite"), "w").close()
        os.utime(os.path.join(odd, "cookies.sqlite"), (4e9, 4e9))   # the newest
        self.set("zen")
        self.assertEqual(card.auth_args(), [])

    def test_a_hand_edited_settings_file_cannot_inject(self):
        card.save("settings.json", {"youtubeLogin": "firefox:/tmp --exec x"})
        self.assertEqual(extras.settings()["youtubeLogin"], "")
        self.assertEqual(card.auth_args(), [])

    def test_every_yt_dlp_call_carries_the_sign_in(self):
        self.set("zen")
        seen = []
        real = subprocess.run

        class R:
            returncode, stdout, stderr = 0, "{}", ""
        subprocess.run = lambda cmd, **kw: (seen.append(cmd), R())[1]
        try:
            card.ytdlp("-J", "--", "ytsearch1:x")
        finally:
            subprocess.run = real
        cmd = seen[0]
        i = cmd.index("--cookies-from-browser")
        self.assertLess(i, cmd.index("--"))                  # an option, before the user's text
        self.assertEqual(cmd[i + 1], "firefox:" + self.prof)
        # downloads build their own command line; they carry it too
        src = open(os.path.join(HERE, "..", "scripts", "card.py")).read()
        self.assertIn('"--no-playlist"] + auth_args()', src)
        src = open(os.path.join(HERE, "..", "scripts", "extras.py")).read()
        self.assertIn('"--no-playlist"] + card.auth_args()', src)


class SpokenBelongsToTheNovel(unittest.TestCase):
    """A chapter read aloud is the novel's chapter, not a book of its own.

    Reported 2026-09-23: chapters well into the book could not be reached.
    Speaking a chapter used to switch the reader to the
    Spoken folder, which holds only the chapters made so far, so a 535-chapter
    novel became a two-chapter book and his progress split in two.
    """

    def setUp(self):
        import booktext
        import library
        import reader
        import speak
        import voices
        self.r, self.bt, self.lib, self.speak, self.voices = reader, booktext, library, speak, voices
        self.tmp = tempfile.TemporaryDirectory()
        card.STATE_DIR = os.path.join(self.tmp.name, "state")
        card.load, card.save = Settings.real_load, Settings.real_save
        self.saved = (card.MUSIC_DIR, card.MPD_PORT, booktext.CACHE_DIR)
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        card.MPD_PORT = 6123                     # nothing there: no MPD is touched
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        os.makedirs(card.MUSIC_DIR)
        card.save("settings.json", {"voiceEngine": "tone", "bookFolders": ["books"]})
        # A library already here: nothing to move over, so no MPD is wanted.
        card.save(library.LIBRARY, {"version": library.VERSION, "books": {}})
        self.saved_mpd = card.mpd
        card.mpd = lambda: FakeMpd()
        # Nothing here may start a real background job: the child would run
        # against his own state folder, not the test's.
        self.started = []
        self.saved_popen = subprocess.Popen

        def no_background(*a, **k):
            cmd = list(a[0]) if a else []
            if any("speak-run" in str(c) for c in cmd):
                self.started.append(cmd)
                return None                # nobody waits on it
            return self.saved_popen(*a, **k)    # ffmpeg and the rest still run
        subprocess.Popen = no_background

    def tearDown(self):
        card.MUSIC_DIR, card.MPD_PORT, self.bt.CACHE_DIR = self.saved
        card.mpd = self.saved_mpd
        subprocess.Popen = self.saved_popen
        self.tmp.cleanup()

    def novel(self, name="n.txt", chapters=4):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            for i in range(chapters):
                f.write("Chapter %d\n\nThe swallow flew on. He was tired.\n\n" % (i + 1))
        return path

    def test_the_whole_novel_stays_open_when_one_chapter_is_spoken(self):
        path = self.novel()
        self.speak.make_chapter(path, 1, token="")
        book = self.r.TextBook(path)
        self.assertEqual(len(book.chapters), 4)          # not two, and not one
        self.assertEqual(list(book.spoken), [1])
        self.assertTrue(book.folder.startswith("Spoken/"))
        # The spoken chapter brings its timings; the others read as plain text.
        timed = book.paras(1)
        self.assertTrue(any(t is not None for p in timed for s in p for _w, t in s))
        plain = book.paras(2)
        self.assertTrue(all(t is None or t < 0 for p in plain for s in p for _w, t in s))
        self.assertEqual(book.key, self.lib.text_id(path))     # one book, one record

    def test_the_spoken_folder_is_not_a_book_of_its_own(self):
        path = os.path.join(card.MUSIC_DIR, "books", "n.txt")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("Chapter 1\n\nThe swallow flew.\n\nChapter 2\n\nDawn came.")
        self.speak.make_chapter(path, 0, token="")
        titles = [b["title"] for b in self.r.shelf({"prefs": {}})]
        self.assertIn("n", titles)
        self.assertEqual([t for t in titles if "Spoken" in t], [])
        for b in self.r.shelf({"prefs": {}}):
            self.assertNotIn("/Spoken/", os.path.realpath(self.r.resolve(b["target"])) + "/")

    def test_a_chapter_not_spoken_yet_is_ticked_not_played(self):
        path = self.novel()
        app = self.r.Reader(24, 80)
        self.assertTrue(app.open(path))
        app.ci = 2
        app.play_pause()                                  # space, on a chapter with no audio
        self.assertEqual([i["chapter"] for i in self.speak.queue_of()], [2])
        self.assertEqual(app.waiting_for, 2)
        self.assertIn("added", app.message)
        self.assertEqual(len(self.started), 1)               # one worker, not one per chapter
        self.assertIn("speak-run", self.started[0])
        app.play_pause()                                  # again: not queued twice
        self.assertEqual([i["chapter"] for i in self.speak.queue_of()], [2])

    def test_the_chapter_list_says_what_each_chapter_is(self):
        path = self.novel(chapters=3)
        self.speak.make_chapter(path, 0, token="")
        self.speak.queue_add(path, [2], "n")
        app = self.r.Reader(24, 80)
        app.open(path)
        app.book.refresh_spoken()
        kinds = [r["kind"] for r in app.chapter_rows()]
        self.assertEqual(kinds, ["ready", "new", "queued"])
        scr = self.r.TextScreen(24, 80)
        app.overlay, app.sel = "chapters", 0
        app.draw(scr)
        text = scr.text()
        self.assertIn("♪", text)                          # spoken, not a real audiobook
        self.assertIn("☑", text)                          # ticked and waiting
        self.assertIn("1 waiting", text)
        self.assertIn("1  Chapter 1", text)               # numbered, so 600 are findable

    def test_the_queue_is_made_in_order_and_one_bad_chapter_is_not_the_end(self):
        path = self.novel(chapters=3)
        self.speak.queue_add(path, [0, 1, 2], "n")
        self.speak.progress(state="starting", token="t")     # as start_job does
        made, real = [], self.speak.make_chapter

        def one(p, i, token, book=None, piece_ready=None):
            made.append(i)
            if i == 1:
                raise RuntimeError("no sound card")
            return real(p, i, token, book=book)
        self.speak.make_chapter = one
        try:
            self.speak.run_queue("t")
        finally:
            self.speak.make_chapter = real
        self.assertEqual(made, [0, 1, 2])                 # in order, and 2 still made
        self.assertEqual(self.speak.queue_of(), [])
        done = card.load(self.speak.SPOKEN, {})
        self.assertEqual((done["state"], done["made"], done["failed"]), ("done", 2, 1))

    def test_stopping_empties_the_queue_as_well(self):
        path = self.novel()
        self.speak.queue_add(path, [0, 1, 2], "n")
        with Silent() as buf:
            self.speak.cmd_speak_stop()
        self.assertEqual(json.loads(buf.getvalue().strip().splitlines()[-1])["dropped"], 3)
        self.assertEqual(self.speak.queue_of(), [])
        self.assertFalse(self.speak.wanted("t"))

    def test_a_run_of_chapters_is_ticked_at_once(self):
        # His ask, 2026-09-23: mark one end, move, and everything between is
        # ticked. A terminal cannot tell shift + space from space, so the run
        # is marked with v.
        path = self.novel(chapters=6)
        self.speak.make_chapter(path, 3, token="")          # already spoken: skipped
        self.speak.queue_add(path, [4], "n")                # already waiting: skipped
        app = self.r.Reader(24, 80)
        app.open(path)
        app.book.refresh_spoken()
        app.overlay, app.sel = "chapters", 1
        app.key("v")
        self.assertEqual(app.chapter_from, 1)
        self.assertIn("From chapter 2", app.message)
        app.sel = 5
        app.key(" ")
        self.assertEqual(sorted(int(i["chapter"]) for i in self.speak.queue_of()), [1, 2, 4, 5])
        self.assertIn("3 chapters added", app.message)
        self.assertEqual(app.chapter_from, -1)              # the run is used up

    def test_a_run_with_nothing_new_in_it_asks_for_nothing(self):
        path = self.novel(chapters=3)
        self.speak.make_chapter(path, 0, token="")
        self.speak.make_chapter(path, 1, token="")
        app = self.r.Reader(24, 80)
        app.open(path)
        app.book.refresh_spoken()
        app.overlay, app.sel = "chapters", 0
        app.key("v")
        app.sel = 1
        app.key(" ")
        self.assertEqual(self.speak.queue_of(), [])
        self.assertIn("Nothing to add", app.message)

    def test_a_write_that_does_not_finish_leaves_nothing_behind(self):
        # Six empty library.json.<pid>.<thread>.tmp files were found on
        # 2026-09-24, from processes killed while saving.
        class Unwritable:
            def __repr__(self):
                raise RuntimeError("boom")
        with self.assertRaises(Exception):
            card.save("x.json", {"a": Unwritable()})
        left = [n for n in os.listdir(card.STATE_DIR) if n.endswith(".tmp")]
        self.assertEqual(left, [])

    def test_temporary_files_from_a_killed_writer_are_swept(self):
        old = os.path.join(card.STATE_DIR, "library.json.999.888.tmp")
        os.makedirs(card.STATE_DIR, exist_ok=True)
        open(old, "w").close()
        os.utime(old, (time.time() - 7200, time.time() - 7200))
        fresh = os.path.join(card.STATE_DIR, "library.json.1.2.tmp")
        open(fresh, "w").close()
        card.sweep_temps()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))      # one being written right now

    def test_punctuation_the_voice_timed_on_its_own_is_joined_back(self):
        # Kokoro times every token, so a chapter read back as "He was tired ."
        # with a space before the stop. The mark belongs to the word.
        join = self.bt.join_marks
        self.assertEqual(join([("He", 1.0), ("was", 1.2), ("tired", 1.4), (".", 1.6)]),
                         [("He", 1.0), ("was", 1.2), ("tired.", 1.4)])
        self.assertEqual(join([("(", 2.0), ("really", 2.1), (")", 2.3), (",", 2.4)]),
                         [("(really),", 2.1)])
        self.assertEqual(join([("’", 0.1), ("But", 0.3)]), [("’But", 0.3)])
        self.assertEqual(join([]), [])
        self.assertEqual(join([("(", 1.0)]), [("(", None)])     # nothing to join to

    def test_a_saved_parse_is_not_served_after_the_rules_change(self):
        self.assertIsInstance(self.bt.PARSE, int)
        path = self.novel(chapters=2)
        first = self.bt.open_book(path)["chapters"][0]["title"]
        saved = self.bt.PARSE
        self.bt.PARSE = saved + 1
        try:
            names = os.listdir(self.bt.CACHE_DIR) if os.path.isdir(self.bt.CACHE_DIR) else []
            again = self.bt.open_book(path)["chapters"][0]["title"]
            self.assertEqual(again, first)
            # A new version asks a new question: it did not read the old answer.
            self.assertGreater(len(os.listdir(self.bt.CACHE_DIR)), len(names))
        finally:
            self.bt.PARSE = saved

    def test_listening_to_a_novel_is_recorded_as_the_novel(self):
        path = self.novel()
        self.speak.make_chapter(path, 0, token="")
        with self.lib.edit() as lib:
            lib["playing"] = "Spoken/n"
            lib["playing_id"] = self.lib.text_id(path)
        self.assertEqual(self.lib.playing_id(), self.lib.text_id(path))
        # An ordinary audiobook clears it again, so its own record is used.
        with self.lib.edit() as lib:
            lib["playing_id"] = None
        self.assertEqual(self.lib.playing_id(), "")


class PhaseOneFoundation(unittest.TestCase):
    """card.py and library.py: what holds everything he cannot make again."""

    def setUp(self):
        import library
        self.lib = library
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (card.STATE_DIR, card.MUSIC_DIR, card.mpd, library.VERSION)
        card.STATE_DIR = os.path.join(self.tmp.name, "state")
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        os.makedirs(card.MUSIC_DIR)
        card.load, card.save = Settings.real_load, Settings.real_save
        card.mpd = lambda: FakeMpd()

    def tearDown(self):
        card.STATE_DIR, card.MUSIC_DIR, card.mpd, self.lib.VERSION = self.saved
        self.tmp.cleanup()

    def test_a_write_cut_short_does_not_lose_the_library(self):
        card.save("library.json", {"version": 2, "books": {"t-a": {"read": {"chapter": 57}}}})
        card.save("library.json", {"version": 2, "books": {"t-a": {"read": {"chapter": 58}}}})
        open(card.state_path("library.json"), "w").close()      # killed mid-write
        got = card.load("library.json", {"books": {}})
        self.assertEqual(got["books"]["t-a"]["read"]["chapter"], 57)   # one save back
        # A file that was never there still reads as "nothing yet".
        self.assertEqual(card.load("never.json", {"fresh": True}), {"fresh": True})
        # And only the precious ones carry a copy: art and caches do not.
        card.save("titles.json", {"a": 1})
        self.assertFalse(os.path.exists(card.state_path("titles.json") + ".bak"))

    def test_a_library_that_is_here_is_never_rebuilt(self):
        # Raising VERSION used to run the migration, which builds from
        # books.json and reader.json -- long since emptied -- and saved the
        # result. Every place, bookmark and chapter heard, gone (2026-09-24).
        card.save("library.json", {"version": 2, "ids": {}, "playing": None,
                                   "books": {"t-a": {"read": {"chapter": 57}, "bookmarks": [1]},
                                             "a-b": {"listen": {"ch": {"1.mp3": {"far": 900}}}}}})
        self.lib.VERSION = 3
        got = self.lib.load()
        self.assertEqual(sorted(got["books"]), ["a-b", "t-a"])
        self.assertEqual(got["books"]["t-a"]["read"]["chapter"], 57)
        self.assertEqual(got["version"], 3)
        # Unreadable, with no copy either: the file is left alone, not
        # written over with an emptier one.
        open(card.state_path("library.json"), "w").close()
        if os.path.exists(card.state_path("library.json") + ".bak"):
            os.remove(card.state_path("library.json") + ".bak")
        self.assertEqual(self.lib.load()["books"], {})
        # Left exactly as found -- not written over with an emptier library,
        # so the file can still be looked at or recovered by hand.
        self.assertEqual(os.path.getsize(card.state_path("library.json")), 0)

    def test_reaching_the_settings_does_not_grow_the_import_path(self):
        # Every function that reached extras.py added this folder to
        # sys.path again; the watcher runs for days (2026-09-24).
        before = len(sys.path)
        for _ in range(200):
            card.setting("voiceSpeed", 100)
        self.assertEqual(len(sys.path), before)

    def test_the_relay_will_not_follow_a_redirect_off_youtube(self):
        for bad in ("http://127.0.0.1:80/x", "https://evil.example/x",
                    "https://googlevideo.com.evil.example/x", ""):
            self.assertFalse(card.upstream_ok(bad), bad)
        self.assertTrue(card.upstream_ok("https://rr1---sn-x.googlevideo.com/videoplayback?x=1"))
        with self.assertRaises(PermissionError):
            card.fetch_piece({"url": "https://evil.example/a"}, 0, 10)

    def test_the_worker_half_of_a_command_checks_its_own_arguments(self):
        # card.py download-run x ../../../tmp/evil wrote outside the music
        # folder: the entry point checked, the worker did not (2026-09-24).
        with Silent() as buf:
            try:
                card.cmd_download_run("x", "../../../tmp/evil")
            except SystemExit:
                pass
        self.assertIn("error", buf.getvalue())
        started = []
        saved = subprocess.Popen
        subprocess.Popen = lambda *a, **k: started.append(a[0])
        try:
            with Silent():
                try:
                    card.cmd_download_run("x", "..")
                except SystemExit:
                    pass
        finally:
            subprocess.Popen = saved
        self.assertEqual(started, [])                  # no yt-dlp was started

    def test_a_station_click_is_only_ever_a_station_id(self):
        asked = []
        saved = card.radio
        card.radio = lambda path, **k: asked.append(path)
        try:
            for bad in ("../../json/stations/search?x", "a/b", "", "x" * 60, "a b"):
                card.cmd_click_run(bad)
            self.assertEqual(asked, [])
            card.cmd_click_run("9617a958-0601-11e8-ae97-52543be04c81")
            self.assertEqual(asked, ["url/9617a958-0601-11e8-ae97-52543be04c81"])
        finally:
            card.radio = saved

    def test_the_relay_answers_only_a_video_id(self):
        import re as _re
        ok = _re.compile(r"^/yt/([A-Za-z0-9_-]{11})$")
        for bad in ("/", "/yt/", "/yt/../../etc/passwd", "/yt/short", "/yt/%2e%2e",
                    "/yt/aaaaaaaaaaa/b", "/other"):
            self.assertIsNone(ok.match(bad), bad)
        self.assertTrue(ok.match("/yt/dQw4w9WgXcQ"))


class PhaseThreeBooks(unittest.TestCase):
    """booktext.py: files that came from anywhere, read without being trusted."""

    def setUp(self):
        import booktext
        self.bt = booktext
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = booktext.CACHE_DIR
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "cache")

    def tearDown(self):
        self.bt.CACHE_DIR = self.saved
        self.tmp.cleanup()

    def nasty_epub(self, docs=60, paras=4000):
        """Small on disk, very large when read: a zip is compressed."""
        import zipfile
        path = os.path.join(self.tmp.name, "nasty.epub")
        doc = b"<html><body>" + b"<p>The swallow flew on and on. </p>" * paras + b"</body></html>"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("META-INF/container.xml",
                       '<container><rootfile full-path="c.opf"/></container>')
            items = "".join('<item id="i%d" href="d%d.xhtml" media-type="application/xhtml+xml"/>'
                            % (i, i) for i in range(docs))
            spine = "".join('<itemref idref="i%d"/>' % i for i in range(docs))
            z.writestr("c.opf", '<package xmlns="http://www.idpf.org/2007/opf"><metadata>'
                                '<title>x</title></metadata><manifest>%s</manifest>'
                                '<spine>%s</spine></package>' % (items, spine))
            for i in range(docs):
                z.writestr("d%d.xhtml" % i, doc)
        return path

    def test_a_small_epub_cannot_ask_for_an_enormous_amount_of_work(self):
        # 0.28 MB of EPUB took 50 s, 1.9 GB of memory and wrote a 172 MB cache
        # file, every single document inside the per-file limit (2026-09-24).
        path = self.nasty_epub()
        self.assertLess(os.path.getsize(path), 200 << 10)     # tiny on disk
        saved = self.bt.MAX_BOOK_WORDS
        self.bt.MAX_BOOK_WORDS = 20000                        # a small budget, quickly
        try:
            book = self.bt.epub_book(path)
        finally:
            self.bt.MAX_BOOK_WORDS = saved
        self.assertTrue(book["partial"])                      # it says so
        self.assertGreater(len(book["chapters"]), 0)          # and keeps what it read
        words = sum(len(s) for ch in book["chapters"] for p in ch["paras"] for s in p)
        self.assertLess(words, 20000 + 8000)                  # one chapter's overshoot

    def test_a_real_book_is_read_whole(self):
        import zipfile
        path = os.path.join(self.tmp.name, "real.epub")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("META-INF/container.xml",
                       '<container><rootfile full-path="c.opf"/></container>')
            items = "".join('<item id="i%d" href="d%d.xhtml" media-type="application/xhtml+xml"/>'
                            % (i, i) for i in range(20))
            spine = "".join('<itemref idref="i%d"/>' % i for i in range(20))
            z.writestr("c.opf", '<package xmlns="http://www.idpf.org/2007/opf"><metadata>'
                                '<title>A Book</title></metadata><manifest>%s</manifest>'
                                '<spine>%s</spine></package>' % (items, spine))
            for i in range(20):
                z.writestr("d%d.xhtml" % i,
                           b"<html><body><h1>Chapter</h1><p>The swallow flew. He was tired.</p>"
                           b"</body></html>")
        book = self.bt.epub_book(path)
        self.assertFalse(book["partial"])
        self.assertEqual(len(book["chapters"]), 20)
        self.assertEqual(book["title"], "A Book")

    def test_one_file_inside_a_book_still_has_its_own_limit(self):
        import zipfile
        path = os.path.join(self.tmp.name, "one.epub")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("big.xhtml", b"<html><body>" + b"<p>x</p>" * 2 + b"</body></html>")
        with zipfile.ZipFile(path) as z:
            budget = self.bt.Budget()
            self.bt.zip_read(z, "big.xhtml", budget)          # fine
            budget.bytes_left = 0
            with self.assertRaises(self.bt.Spent):
                self.bt.zip_read(z, "big.xhtml", budget)

    def test_an_entity_bomb_is_refused_by_the_parser(self):
        # Checked rather than assumed: Python expands small internal entities
        # and refuses large ones, so nothing of ours is needed (2026-09-24).
        def bomb(depth):
            ents = ['<!ENTITY lol "lol">']
            for i in range(1, depth + 1):
                prev = "&lol;" if i == 1 else "&lol%d;" % (i - 1)
                ents.append('<!ENTITY lol%d "%s">' % (i, prev * 10))
            return ('<?xml version="1.0"?><!DOCTYPE d [%s]><html><body><p id="a">&lol%d;</p>'
                    '</body></html>' % ("".join(ents), depth)).encode()
        self.assertEqual(len(self.bt.index_doc(bomb(4))["a"][0]), 30000)   # small: harmless
        for deep in (7, 9):
            with self.assertRaises(Exception):
                self.bt.index_doc(bomb(deep))

    def test_an_external_entity_cannot_read_a_local_file(self):
        xxe = (b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
               b'<r id="a">&x;</r>')
        with self.assertRaises(Exception):
            self.bt.index_doc(xxe)

    def test_the_parsed_cache_is_held_down_by_size_not_only_by_count(self):
        # One book's parsed text can be tens of megabytes, so keeping 400 of
        # them is measured in gigabytes.
        os.makedirs(self.bt.CACHE_DIR)
        for i in range(12):
            path = os.path.join(self.bt.CACHE_DIR, "f%02d.json" % i)
            with open(path, "wb") as f:
                f.write(b"x" * 1000000)
            os.utime(path, (i, i))
        self.bt.prune_cache(keep=400, most=5 << 20)
        left = sorted(os.listdir(self.bt.CACHE_DIR))
        self.assertEqual(left, ["f%02d.json" % i for i in range(7, 12)])   # newest kept


class PhaseFourVoices(unittest.TestCase):
    """speak.py and voices.py: what runs engines and writes files."""

    def setUp(self):
        import speak
        import voices
        self.speak, self.voices = speak, voices

    def test_a_books_title_cannot_name_a_file_that_lies(self):
        # A title carrying a right-to-left override names a file that reads
        # on screen as something other than it is (2026-09-24).
        self.assertEqual(self.speak.safe_name("\u202eexe.txt"), "exe.txt")
        self.assertEqual(self.speak.safe_name("a\u200fb"), "a b")
        self.assertEqual(self.speak.safe_name("a\x01b"), "a b")
        self.assertEqual(self.speak.safe_name("A Real Title"), "A Real Title")

    def test_a_books_title_cannot_name_a_file_outside_the_music_folder(self):
        for title in ("../../etc", "..", "...", ".", "~/.ssh", "/etc/passwd",
                      "a\0b", "a\nb", "", "   ", "a/../../b"):
            folder = self.speak.book_folder(title)
            full = os.path.realpath(os.path.join(card.MUSIC_DIR, folder))
            self.assertTrue(full.startswith(os.path.realpath(card.MUSIC_DIR) + os.sep), title)

    def test_the_downloaded_python_must_match_a_published_checksum(self):
        # It looked for "<name>.sha256", which that project has never
        # published: `want` stayed empty and `if want and ...` never ran, so
        # every download was taken on trust (2026-09-24).
        src = open(os.path.join(HERE, "..", "scripts", "voices.py")).read()
        body = src[src.index("def fetch_python"):src.index("def install_kokoro")]
        self.assertIn("SHA256SUMS", body)
        self.assertNotIn('if want and got != want', body)     # the check that never ran
        self.assertIn("refusing to install it", body)          # no checksums: stop
        self.assertIn("not in the published checksums", body)  # not listed: stop
        self.assertIn('filter="data"', body)                   # nothing outside the folder


class PhaseFiveReader(unittest.TestCase):
    """reader.py: it must not fall over, whatever is pressed or removed."""

    def setUp(self):
        import booktext
        import library
        import reader
        self.r, self.bt = reader, booktext
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (card.STATE_DIR, card.MUSIC_DIR, card.MPD_PORT, booktext.CACHE_DIR)
        card.STATE_DIR = os.path.join(self.tmp.name, "state")
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        card.MPD_PORT = 6123                      # nothing there
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        os.makedirs(card.MUSIC_DIR)
        card.load, card.save = Settings.real_load, Settings.real_save
        card.save(library.LIBRARY, {"version": library.VERSION, "books": {}, "ids": {},
                                    "playing": None})
        self.book = os.path.join(self.tmp.name, "n.txt")
        with open(self.book, "w") as f:
            f.write("Chapter 1\n\n" + "The swallow flew. He was tired. " * 40 +
                    "\n\nChapter 2\n\nDawn came at last.")

    def tearDown(self):
        card.STATE_DIR, card.MUSIC_DIR, card.MPD_PORT, self.bt.CACHE_DIR = self.saved
        self.tmp.cleanup()

    def test_no_key_at_any_size_brings_it_down(self):
        import curses
        import random
        import speak
        keys = ([chr(c) for c in range(32, 127)] +
                [curses.KEY_UP, curses.KEY_DOWN, curses.KEY_LEFT, curses.KEY_RIGHT,
                 curses.KEY_NPAGE, curses.KEY_PPAGE, curses.KEY_HOME, curses.KEY_END,
                 curses.KEY_BACKSPACE, 10, 13, 27, 127, 8, 0, 255, 1000])
        random.seed(11)
        # Put back afterwards: a module patched for good would follow the
        # other tests around (it did, the first time I wrote this).
        saved, saved_job = subprocess.Popen, speak.start_job
        subprocess.Popen = lambda *a, **k: None
        speak.start_job = lambda: ""              # never start real speaking here
        try:
            for rows, cols in ((24, 80), (3, 12), (60, 200), (6, 20)):
                app = self.r.Reader(rows, cols)
                app.open(self.book)
                for _ in range(300):
                    try:
                        app.key(random.choice(keys))
                        app.draw(self.r.TextScreen(rows, cols))
                    except SystemExit:
                        pass
        finally:
            subprocess.Popen, speak.start_job = saved, saved_job

    def test_the_book_can_vanish_while_it_is_open(self):
        app = self.r.Reader(24, 80)
        self.assertTrue(app.open(self.book))
        os.remove(self.book)
        for k in ("j", "]", "C", "\n", "L", "m", "/", "\x1b", "G", "A", "z", "."):
            app.key(k)
            app.draw(self.r.TextScreen(24, 80))
        app.save(force=True)              # and his place is still written down

    def test_his_text_settings_are_kept_as_carefully_as_his_places(self):
        # reader.json holds the width, line height, indent and zoom he chose.
        self.assertIn("reader.json", card.PRECIOUS)
        card.save("reader.json", {"prefs": {"width": 140, "fontSize": 13}})
        card.save("reader.json", {"prefs": {"width": 120, "fontSize": 13}})
        open(card.state_path("reader.json"), "w").close()        # a write cut short
        self.assertEqual(card.load("reader.json", {})["prefs"]["width"], 140)


class PhaseSevenRelease(unittest.TestCase):
    """What a stranger's machine would hit."""

    def test_the_python_for_kokoro_matches_the_machine(self):
        import unittest.mock as mock
        import voices
        for arch, want in (("x86_64", "x86_64"), ("amd64", "x86_64"),
                           ("aarch64", "aarch64"), ("arm64", "aarch64")):
            with mock.patch("platform.machine", return_value=arch):
                self.assertTrue(voices.py_kind().startswith(want), arch)
        with mock.patch("platform.machine", return_value="riscv64"):
            with self.assertRaises(RuntimeError):
                voices.py_kind()          # said plainly, not a wrong download

    def test_a_missing_program_is_said_in_words(self):
        # It used to show Python's own: "[Errno 2] No such file or directory:
        # 'yt-dlp'", which says nothing about what to do (2026-09-24).
        saved = subprocess.run

        def gone(*a, **k):
            raise FileNotFoundError(2, "No such file or directory", "yt-dlp")
        subprocess.run = gone
        try:
            with self.assertRaises(RuntimeError) as caught:
                card.ytdlp("--version")
        finally:
            subprocess.run = saved
        self.assertIn("yt-dlp is not installed", str(caught.exception))
        self.assertIn("pacman", str(caught.exception))

    def test_the_setup_check_names_what_is_missing(self):
        import shutil
        saved = shutil.which
        shutil.which = lambda name: None          # nothing installed
        try:
            with Silent() as buf:
                extras.cmd_setup_check()
        finally:
            shutil.which = saved
        checks = json.loads(buf.getvalue().strip().splitlines()[-1])["checks"]
        names = {c["name"]: c for c in checks}
        self.assertFalse(names["yt-dlp"]["ok"])
        self.assertIn("yt-dlp", names["yt-dlp"]["fix"])
        self.assertFalse(names["ffmpeg"]["ok"])
        self.assertIn("ffmpeg", names["ffmpeg"]["fix"])


class OneWayToWriteAFile(unittest.TestCase):
    """card.write_file: every file the card writes goes through it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_reader_never_sees_half_a_file(self):
        path = os.path.join(self.d, "a.json")
        card.write_file(path, '{"a": 1}')
        self.assertEqual(open(path).read(), '{"a": 1}')
        self.assertEqual([n for n in os.listdir(self.d) if n.endswith(".tmp")], [])

    def test_it_keeps_the_mode_of_the_file_it_replaces(self):
        path = os.path.join(self.d, "a.txt")
        open(path, "w").close()
        os.chmod(path, 0o600)
        card.write_file(path, "new")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_a_link_is_replaced_unless_it_is_meant_to_be_followed(self):
        # os.replace swaps out the link itself, so someone whose mpd.conf is
        # a link into their dotfiles would quietly stop updating the real
        # file (2026-09-24).
        real, link = os.path.join(self.d, "real"), os.path.join(self.d, "link")
        with open(real, "w") as f:
            f.write("original")
        os.symlink(real, link)
        card.write_file(link, "not followed")
        self.assertFalse(os.path.islink(link))
        self.assertEqual(open(real).read(), "original")
        os.remove(link)
        os.symlink(real, link)
        card.write_file(link, "followed", follow=True)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(open(real).read(), "followed")

    def test_a_write_that_fails_takes_its_temporary_file_with_it(self):
        path = os.path.join(self.d, "a.txt")
        with self.assertRaises(TypeError):
            card.write_file(path, object())
        self.assertEqual(os.listdir(self.d), [])

    def test_bytes_and_text_both_work(self):
        card.write_file(os.path.join(self.d, "b.img"), b"\xff\xd8jpeg")
        self.assertEqual(open(os.path.join(self.d, "b.img"), "rb").read(), b"\xff\xd8jpeg")
        card.write_file(os.path.join(self.d, "t.txt"), "sur la mer \u2014 caf\u00e9")
        self.assertEqual(open(os.path.join(self.d, "t.txt"), encoding="utf-8").read(),
                         "sur la mer \u2014 caf\u00e9")

    def test_a_precious_file_keeps_the_one_before_it(self):
        path = os.path.join(self.d, "p.json")
        card.write_file(path, "first", precious=True)
        card.write_file(path, "second", precious=True)
        self.assertEqual(open(path).read(), "second")
        self.assertEqual(open(path + ".bak").read(), "first")
        card.write_file(os.path.join(self.d, "q.json"), "only")
        self.assertFalse(os.path.exists(os.path.join(self.d, "q.json.bak")))

    def test_nothing_writes_a_file_its_own_way_any_more(self):
        import glob
        here = os.path.join(HERE, "..", "scripts")
        offenders = []
        for path in sorted(glob.glob(os.path.join(here, "*.py"))):
            src = open(path).read()
            for n, line in enumerate(src.splitlines(), 1):
                if ".tmp" in line and "write_file" not in src[:src.index(line)][-200:]:
                    if os.path.basename(path) == "card.py":
                        continue                    # the helper itself
                    offenders.append("%s:%d" % (os.path.basename(path), n))
        self.assertEqual(offenders, [])


class ABookIsReadAChapterAtATime(unittest.TestCase):
    """The cache holds a book as an index plus one line per chapter, so
    showing one chapter does not carry the whole book about (2026-09-24)."""

    def setUp(self):
        import booktext
        self.bt = booktext
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = booktext.CACHE_DIR
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        booktext._open_book.clear()
        self.path = os.path.join(self.tmp.name, "n.txt")
        with open(self.path, "w") as f:
            for i in range(6):
                f.write("Chapter %d\n\nWords of chapter %d here. And more of them.\n\n"
                        % (i + 1, i + 1))

    def tearDown(self):
        self.bt.CACHE_DIR = self.saved
        self.bt._open_book.clear()
        self.tmp.cleanup()

    def test_opening_a_book_brings_its_names_and_lengths_only(self):
        book = self.bt.open_book(self.path)
        self.assertEqual(len(book["chapters"]), 6)
        for c in book["chapters"]:
            self.assertIn("title", c)
            self.assertGreater(c["words"], 0)
            self.assertNotIn("paras", c)          # no text came with it

    def test_a_chapter_comes_back_whole_and_in_the_right_order(self):
        self.bt.open_book(self.path)
        for i in range(6):
            paras = self.bt.chapter_paras(self.path, i)
            said = " ".join(w for p in paras for sent in p for w, _t in sent)
            self.assertIn("chapter %d" % (i + 1), said)
        self.assertEqual(self.bt.chapter_paras(self.path, 99), [])
        self.assertEqual(self.bt.chapter_paras(self.path, -1), [])

    def test_the_word_count_in_the_index_matches_the_text(self):
        book = self.bt.open_book(self.path)
        for i, c in enumerate(book["chapters"]):
            real = sum(len(s) for p in self.bt.chapter_paras(self.path, i) for s in p)
            self.assertEqual(c["words"], real, "chapter %d" % i)

    def test_a_chapter_can_be_read_without_the_book_being_opened_first(self):
        paras = self.bt.chapter_paras(self.path, 2)      # nothing opened it
        self.assertTrue(paras)

    def test_a_thrown_away_cache_is_simply_made_again(self):
        self.bt.open_book(self.path)
        self.bt._open_book.clear()
        for n in os.listdir(self.bt.CACHE_DIR):
            os.remove(os.path.join(self.bt.CACHE_DIR, n))
        self.assertTrue(self.bt.chapter_paras(self.path, 1))

    def test_the_book_going_missing_is_not_a_crash(self):
        self.bt.open_book(self.path)
        self.bt._open_book.clear()
        os.remove(self.path)
        self.assertEqual(self.bt.chapter_paras(self.path, 1), [])   # nothing to read

    def test_the_reader_keeps_only_a_few_chapters_in_hand(self):
        import reader
        book = reader.TextBook(self.path)
        for i in range(6):
            book.paras(i)
        self.assertLessEqual(len(book._plain), book.KEEP_CHAPTERS)
        self.assertIn(5, book._plain)                    # the last one asked for
        self.assertEqual(book.weights,
                         [c["words"] for c in self.bt.open_book(self.path)["chapters"]])


class TheSmallFixes(unittest.TestCase):
    """The last of the review's list (2026-09-24)."""

    def test_a_voice_command_may_live_in_a_folder_with_a_space(self):
        import shlex
        src = open(os.path.join(HERE, "..", "scripts", "voices.py")).read()
        body = src[src.index("def custom_speak"):src.index("def tone_speak")]
        self.assertIn("shlex.split", body)
        self.assertNotIn("template.split()", body)
        self.assertEqual(shlex.split('/home/me/my tts/say.sh --to {out}'.replace(
            "/home/me/my tts/say.sh", '"/home/me/my tts/say.sh"')),
            ["/home/me/my tts/say.sh", "--to", "{out}"])

    def test_a_voice_name_may_not_carry_control_characters(self):
        self.assertTrue(extras.valid_voice_name("af_heart"))
        self.assertTrue(extras.valid_voice_name("en-us+f3"))
        for bad in ("a\x01b", "a\nb", "a\rb", "a\x7fb", "x" * 400, 5, None):
            self.assertFalse(extras.valid_voice_name(bad), repr(bad))

    def test_the_position_thread_does_not_connect_for_nothing(self):
        # It opened its own MPD connection every 30 s for ever, whatever was
        # or was not playing: 2,880 a day.
        saved = dict(extras._seen)
        try:
            extras._seen.update(view=None, at=0)
            self.assertTrue(extras.worth_a_look())          # nothing known: look
            extras.note_view({"state": "stop"})
            self.assertFalse(extras.worth_a_look())
            extras.note_view({"state": "play", "duration": 200,
                              "current": {"file": "a.mp3"}})
            self.assertFalse(extras.worth_a_look())         # a short song
            extras.note_view({"state": "play", "duration": 4000,
                              "current": {"file": "a.mp3"}})
            self.assertTrue(extras.worth_a_look())          # a long one
            extras._seen["at"] = time.time() - 300
            self.assertTrue(extras.worth_a_look())          # stale: the watcher may be gone
        finally:
            extras._seen.update(saved)

    def test_the_card_notices_its_watcher_going_rather_than_polling(self):
        src = open(os.path.join(HERE, "..", "CardService.qml")).read()
        self.assertIn("onRunningChanged: if (!running) watchAgain.restart()", src)
        # and what is left of the two always-on timers is a slow net
        self.assertNotIn("interval: 5000\n    repeat: true\n    running: true", src)
        self.assertNotIn("interval: 2000\n    repeat: true\n    running: true\n"
                         "    triggeredOnStart: true", src)

    def test_only_the_last_few_mpd_conf_backups_are_kept(self):
        src = open(os.path.join(HERE, "..", "scripts", "extras.py")).read()
        body = src[src.index("def cmd_set_music_dir"):src.index("def cmd_reset_settings")]
        self.assertIn('glob.glob(conf + ".bak.*")', body)
        self.assertIn("[:-5]", body)

    def test_the_import_worker_checks_the_folder_it_was_handed(self):
        src = open(os.path.join(HERE, "..", "scripts", "extras.py")).read()
        body = src[src.index("def cmd_import_worker"):]
        self.assertIn("card.safe_folder(folder)", body[:600])


class AddressIsCheckedAndThenKept(unittest.TestCase):
    """A station list is written by strangers. The card refuses addresses
    inside this machine or the home network -- and now connects to the
    address it checked, so the name cannot answer differently the second
    time (2026-09-24)."""

    def two_faced(self, first, second):
        """A name that answers `first` when asked, `second` after that."""
        import socket as s
        real = s.getaddrinfo
        self.asked = 0

        def fake(host, port, *a, **kw):
            if host == "sneaky.example":
                self.asked += 1
                ip = first if self.asked == 1 else second
                return [(s.AF_INET, s.SOCK_STREAM, 6, "", (ip, port or 80))]
            return real(host, port, *a, **kw)
        return real, fake

    def test_the_name_is_looked_up_once(self):
        import socket as s
        real, fake = self.two_faced("93.184.216.34", "192.168.1.1")
        s.getaddrinfo = fake
        try:
            used = []
            saved = card.socket.create_connection

            def note(address, *a, **kw):
                used.append(address[0])
                raise OSError("not really connecting")
            card.socket.create_connection = note
            try:
                with self.assertRaises(OSError):
                    card.pinned_get("http://sneaky.example/stream", 1.0)
            finally:
                card.socket.create_connection = saved
        finally:
            s.getaddrinfo = real
        self.assertEqual(self.asked, 1, "the name was asked more than once")
        self.assertEqual(used, ["93.184.216.34"])      # the address that was checked

    def test_a_private_answer_is_refused_before_anything_is_dialled(self):
        import socket as s
        real, fake = self.two_faced("192.168.1.1", "93.184.216.34")
        s.getaddrinfo = fake
        tried = []
        saved = card.socket.create_connection
        card.socket.create_connection = lambda *a, **k: tried.append(a) or (_ for _ in ()).throw(OSError())
        try:
            with self.assertRaises(PermissionError):
                card.pinned_get("http://sneaky.example/stream", 1.0)
        finally:
            s.getaddrinfo = real
            card.socket.create_connection = saved
        self.assertEqual(tried, [])                    # nothing was contacted at all

    def test_every_address_of_a_name_must_be_public(self):
        import socket as s
        real = s.getaddrinfo

        def both(host, port, *a, **kw):
            if host == "mixed.example":
                return [(s.AF_INET, s.SOCK_STREAM, 6, "", ("93.184.216.34", port or 80)),
                        (s.AF_INET, s.SOCK_STREAM, 6, "", ("10.0.0.5", port or 80))]
            return real(host, port, *a, **kw)
        s.getaddrinfo = both
        try:
            with self.assertRaises(PermissionError):
                card.checked_addresses("mixed.example", 80)
        finally:
            s.getaddrinfo = real

    def test_a_name_with_two_addresses_still_works(self):
        # Pinning to the first address alone called every station on a
        # machine without a route to it dead. Both are checked; both may be
        # tried.
        import socket as s
        real = s.getaddrinfo

        def pair(host, port, *a, **kw):
            if host == "dual.example":
                return [(s.AF_INET6, s.SOCK_STREAM, 6, "", ("2606:2800:220:1::1", port or 80)),
                        (s.AF_INET, s.SOCK_STREAM, 6, "", ("93.184.216.34", port or 80))]
            return real(host, port, *a, **kw)
        s.getaddrinfo = pair
        try:
            got = card.checked_addresses("dual.example", 80)
        finally:
            s.getaddrinfo = real
        self.assertEqual(got, ["2606:2800:220:1::1", "93.184.216.34"])


class RowsHaveASize(unittest.TestCase):
    """A row of the card's list must be given a width and a height.

    Moving the delegate into PanelRow.qml on 2026-09-24 dropped its width
    binding: every row was zero wide, so each row drew its icon, its title
    and its note on top of one another at the left edge, and nothing could be
    clicked. The card looked broken and no error was logged -- the rows were
    all there, and all of them were invisible. Counting rows is not looking
    at them.
    """

    def test_the_row_component_sizes_itself(self):
        src = open(os.path.join(HERE, "..", "PanelRow.qml")).read()
        self.assertRegex(src, r"(?m)^\s*width:\s*\S+", "PanelRow.qml sets no width")
        self.assertRegex(src, r"(?m)^\s*height:\s*\S+", "PanelRow.qml sets no height")
        self.assertIn("required property real listWidth", src)

    def test_the_list_hands_the_row_its_width(self):
        src = open(os.path.join(HERE, "..", "MusicPanel.qml")).read()
        made = src[src.index("delegate: PanelRow"):][:200]
        self.assertIn("listWidth:", made)
        self.assertIn("panel:", made)

    def test_the_card_can_say_how_big_its_rows_are(self):
        # `omarchy-shell songbook probe` reports the size, so a row that is there
        # but invisible can be seen from the command line.
        panel = open(os.path.join(HERE, "..", "MusicPanel.qml")).read()
        self.assertIn("function rowSize()", panel)
        bar = open(os.path.join(HERE, "..", "BarWidget.qml")).read()
        self.assertIn("panel.rowSize()", bar)


class YoutubeDoesNotFightItself(unittest.TestCase):
    """Signed in, every yt-dlp run opens the browser's cookie store, and two
    of them get in each other's way: one run took 9 s, four at once took 28 s
    each, which is how a song being resolved timed out at 60 (2026-09-24)."""

    def test_only_one_run_reads_the_cookies_at_a_time(self):
        import threading
        card.STATE_DIR = tempfile.mkdtemp()
        inside, most = [0], [0]
        lock = threading.Lock()

        def worker():
            with card.cookie_turn(wait=10):
                with lock:
                    inside[0] += 1
                    most[0] = max(most[0], inside[0])
                time.sleep(0.15)
                with lock:
                    inside[0] -= 1
        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(most[0], 1, "two runs read the cookies at once")

    def test_waiting_too_long_goes_ahead_rather_than_failing(self):
        import fcntl
        card.STATE_DIR = tempfile.mkdtemp()
        os.makedirs(card.STATE_DIR, exist_ok=True)
        holder = open(card.state_path("ytdlp.lock"), "w")
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            t0 = time.time()
            with card.cookie_turn(wait=1):
                pass                      # it waited its second, then carried on
            self.assertLess(time.time() - t0, 5)
        finally:
            holder.close()

    def test_a_signed_in_run_is_given_longer(self):
        seen = {}
        saved_run, saved_auth = card.run_ytdlp, card.auth_args
        card.run_ytdlp = lambda cmd, timeout: seen.update(cmd=cmd, timeout=timeout) or "{}"
        try:
            card.auth_args = lambda: []
            card.ytdlp("-J", "--", "x", timeout=60)
            self.assertEqual(seen["timeout"], 60)
            card.auth_args = lambda: ["--cookies-from-browser", "firefox:/p"]
            card.ytdlp("-J", "--", "x", timeout=60)
            self.assertEqual(seen["timeout"], card.COOKIE_TIMEOUT)
        finally:
            card.run_ytdlp, card.auth_args = saved_run, saved_auth

    def test_running_out_of_time_says_what_to_do(self):
        saved = subprocess.run

        def slow(*a, **k):
            raise subprocess.TimeoutExpired(a[0] if a else "yt-dlp", k.get("timeout", 60))
        subprocess.run = slow
        try:
            with self.assertRaises(RuntimeError) as caught:
                card.run_ytdlp(["yt-dlp", "-J"], 60)
        finally:
            subprocess.run = saved
        said = str(caught.exception)
        self.assertIn("took longer", said)
        self.assertIn("download", said)          # the usual reason
        self.assertNotIn("Traceback", said)

    def test_a_playlist_row_says_it_is_working_and_cannot_be_started_twice(self):
        src = open(os.path.join(HERE, "..", "MusicPanel.qml")).read()
        body = src[src.index('row.type === "ytlist"'):][:700]
        self.assertIn("busyKey = row.key", body)          # it spins while it works
        self.assertIn("busyKey = \"\"", body)              # and stops when it is done
        guard = src[src.index("function activate"):][:400]
        self.assertIn("row.key === busyKey", guard)       # a second click does nothing


class WhatTheCardIsDoing(unittest.TestCase):
    """jobs.py: one shape for everything that takes a while (2026-09-25)."""

    def setUp(self):
        import jobs
        self.jobs = jobs
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = card.STATE_DIR
        card.STATE_DIR = self.tmp.name
        card.load, card.save = Settings.real_load, Settings.real_save

    def tearDown(self):
        card.STATE_DIR = self.saved
        self.tmp.cleanup()

    def test_nothing_running_says_so(self):
        got = self.jobs.summary()
        self.assertFalse(got["busy"])
        self.assertEqual(got["line"], "")

    def test_a_download_is_reported_with_how_far_it_has_got(self):
        card.save("download.json", {"state": "downloading", "percent": 42,
                                    "title": "A Song", "folder": "Eng", "at": time.time()})
        got = self.jobs.summary()
        self.assertTrue(got["busy"])
        self.assertIn("Downloading", got["line"])
        self.assertIn("42%", got["line"])
        self.assertEqual(got["running"][0]["stop"], "download-stop")

    def test_work_nobody_is_doing_any_more_is_not_reported(self):
        # A killed process leaves its note saying "working" for ever. The
        # stale "making 38%" that hid a whole queue came from believing one.
        card.save("download.json", {"state": "downloading", "percent": 42,
                                    "title": "A Song", "at": time.time() - 3600})
        self.assertFalse(self.jobs.summary()["busy"])
        card.save("import.json", {"state": "running", "total": 10, "done": 3,
                                  "at": time.time() - 3600})
        self.assertFalse(self.jobs.summary()["busy"])

    def test_several_things_at_once_are_counted(self):
        now = time.time()
        card.save("download.json", {"state": "downloading", "percent": 10,
                                    "title": "A Song", "at": now})
        card.save("import.json", {"state": "running", "total": 8, "done": 2,
                                  "current": "Another", "at": now})
        got = self.jobs.summary()
        self.assertEqual(got["count"], 2)
        self.assertIn("1 more", got["line"])

    def test_the_same_download_pressed_twice_does_not_start_twice(self):
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        card.save("download.json", {"state": "downloading", "percent": 42,
                                    "title": "A Song", "folder": "Eng", "at": time.time()})
        started = []
        saved = subprocess.Popen
        subprocess.Popen = lambda *a, **k: started.append(a[0])
        try:
            with Silent() as buf:
                card.cmd_download("A Song", "Eng")
            answer = json.loads(buf.getvalue().strip().splitlines()[-1])
        finally:
            subprocess.Popen = saved
        self.assertTrue(answer["busy"])
        self.assertIn("already downloading", answer["error"])
        self.assertEqual(started, [])          # nothing was started beside it

    def test_stopping_a_download_asks_its_own_process_to_go(self):
        killed = []
        saved = os.kill
        os.kill = lambda pid, sig: killed.append((pid, sig))
        card.save("download.json", {"state": "downloading", "percent": 5,
                                    "pid": 999999, "at": time.time()})
        try:
            with Silent():
                self.jobs.cmd_download_stop()
        finally:
            os.kill = saved
        self.assertEqual(killed, [(999999, 15)])
        self.assertEqual(card.load("download.json", {})["state"], "stopped")
        self.assertFalse(self.jobs.summary()["busy"])


class ChaptersAheadAndTidyingUp(unittest.TestCase):
    """Phase B (2026-09-25): chapters kept ready ahead of the one playing,
    and spoken chapters removed once they have really been heard."""

    def setUp(self):
        import booktext
        import books
        import library
        import speak
        self.speak, self.books, self.bt, self.lib = speak, books, booktext, library
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (card.STATE_DIR, card.MUSIC_DIR, booktext.CACHE_DIR, card.MPD_PORT)
        card.STATE_DIR = os.path.join(self.tmp.name, "state")
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        card.MPD_PORT = 6123
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        os.makedirs(card.MUSIC_DIR)
        card.load, card.save = Settings.real_load, Settings.real_save
        card.save(library.LIBRARY, {"version": library.VERSION, "books": {}, "ids": {},
                                    "playing": None})
        self.path = os.path.join(self.tmp.name, "n.txt")
        with open(self.path, "w") as f:
            for i in range(8):
                f.write("Chapter %d\n\nWords of chapter %d. And some more of them.\n\n"
                        % (i + 1, i + 1))

    def tearDown(self):
        card.STATE_DIR, card.MUSIC_DIR, self.bt.CACHE_DIR, card.MPD_PORT = self.saved
        self.tmp.cleanup()

    def test_nothing_is_made_ahead_unless_he_asks(self):
        card.save("settings.json", {"voiceEngine": "tone"})
        self.assertEqual(self.speak.settings()["ahead"], 0)
        self.assertEqual(self.speak.keep_ahead(self.path, 0), [])
        self.assertEqual(self.speak.queue_of(), [])

    def test_with_it_on_the_next_few_are_ticked(self):
        card.save("settings.json", {"voiceEngine": "tone", "spokenAheadOn": True,
                                    "spokenAhead": 3})
        started = []
        saved = subprocess.Popen
        subprocess.Popen = lambda *a, **k: started.append(a[0])
        try:
            added = self.speak.keep_ahead(self.path, 1)
        finally:
            subprocess.Popen = saved
        self.assertEqual(added, [2, 3, 4])            # the next three, not the one playing
        self.assertEqual(len(started), 1)             # one worker, not one per chapter

    def test_chapters_already_made_or_waiting_are_not_asked_for_twice(self):
        card.save("settings.json", {"voiceEngine": "tone", "spokenAheadOn": True,
                                    "spokenAhead": 3})
        self.speak.make_chapter(self.path, 2, token="")     # already spoken
        self.speak.queue_add(self.path, [3], "n")           # already waiting
        saved = subprocess.Popen
        subprocess.Popen = lambda *a, **k: None
        try:
            added = self.speak.keep_ahead(self.path, 1)
        finally:
            subprocess.Popen = saved
        self.assertEqual(added, [4])

    def test_it_stops_at_the_end_of_the_book(self):
        card.save("settings.json", {"voiceEngine": "tone", "spokenAheadOn": True,
                                    "spokenAhead": 5})
        saved = subprocess.Popen
        subprocess.Popen = lambda *a, **k: None
        try:
            added = self.speak.keep_ahead(self.path, 6)
        finally:
            subprocess.Popen = saved
        self.assertTrue(all(c < 8 for c in added), added)

    def test_a_heard_chapter_is_removed_only_when_he_asked_for_that(self):
        card.save("settings.json", {"voiceEngine": "tone"})
        rel = self.speak.make_chapter(self.path, 0, token="")
        full = os.path.join(card.MUSIC_DIR, rel)
        self.assertTrue(os.path.exists(full))
        folder = os.path.dirname(rel)
        chs = [{"id": self.lib.chapter_key(rel), "file": rel, "start": 0.0,
                "length": 100.0, "title": "One"},
               {"id": "later.mp3", "file": folder + "/later.mp3", "start": 0.0,
                "length": 100.0, "title": "Two"}]
        book_id = self.lib.text_id(self.path)
        with self.lib.edit() as lib:
            lib["playing_id"] = book_id
            self.lib.record(lib, book_id)["listen"]["ch"][chs[0]["id"]] = {"at": 99, "far": 99}
        self.books.tidy_spoken(folder, chs, 1)          # playing the second one
        self.assertTrue(os.path.exists(full), "removed without being asked")
        card.save("settings.json", {"voiceEngine": "tone", "spokenTidy": True})
        self.books.tidy_spoken(folder, chs, 1)
        self.assertFalse(os.path.exists(full))          # heard, and behind him
        self.assertFalse(os.path.exists(os.path.splitext(full)[0] + ".lrc"))

    def test_the_one_playing_and_the_ones_to_come_are_left_alone(self):
        card.save("settings.json", {"voiceEngine": "tone", "spokenTidy": True})
        rel = self.speak.make_chapter(self.path, 0, token="")
        full = os.path.join(card.MUSIC_DIR, rel)
        folder = os.path.dirname(rel)
        chs = [{"id": self.lib.chapter_key(rel), "file": rel, "start": 0.0,
                "length": 100.0, "title": "One"}]
        book_id = self.lib.text_id(self.path)
        with self.lib.edit() as lib:
            lib["playing_id"] = book_id
            self.lib.record(lib, book_id)["listen"]["ch"][chs[0]["id"]] = {"at": 99, "far": 99}
        self.books.tidy_spoken(folder, chs, 0)          # it is the one playing
        self.assertTrue(os.path.exists(full))

    def test_a_real_audiobook_is_never_touched(self):
        card.save("settings.json", {"voiceEngine": "tone", "spokenTidy": True})
        folder = "books/A Real Book"
        os.makedirs(os.path.join(card.MUSIC_DIR, folder))
        rel = folder + "/01 one.mp3"
        with open(os.path.join(card.MUSIC_DIR, rel), "w") as f:
            f.write("x" * 2000)
        chs = [{"id": "01 one.mp3", "file": rel, "start": 0.0, "length": 100.0, "title": "One"}]
        self.books.tidy_spoken(folder, chs, 1)
        self.assertTrue(os.path.exists(os.path.join(card.MUSIC_DIR, rel)))


class HowFarThroughIsRemembered(unittest.TestCase):
    """The row fills as far as he has got, in every list that shows a book,
    and it comes from the library so a restart does not lose it (2026-09-25)."""

    def setUp(self):
        import books
        import library
        self.books, self.lib = books, library
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (card.STATE_DIR, card.MUSIC_DIR)
        card.STATE_DIR = os.path.join(self.tmp.name, "state")
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        os.makedirs(card.MUSIC_DIR)
        card.load, card.save = Settings.real_load, Settings.real_save
        card.save(library.LIBRARY, {"version": library.VERSION, "books": {}, "ids": {},
                                    "playing": None})

    def tearDown(self):
        card.STATE_DIR, card.MUSIC_DIR = self.saved
        self.tmp.cleanup()

    def files(self, n=4, secs=600):
        return [{"file": "B/%02d ch.mp3" % (i + 1), "duration": str(secs), "Genre": "Audiobook"}
                for i in range(n)]

    def test_an_audiobook_counts_seconds_not_chapters(self):
        files = self.files()
        # Half of the first chapter heard: a twelfth of a four-chapter book,
        # not nothing, and not a whole chapter.
        progress = {"ch": {"01 ch.mp3": {"at": 300, "far": 300}}}
        got = self.books.summary("B", files, progress)
        # Half of the first chapter is started, not finished, and an eighth
        # of the book has gone by. The two no longer disagree.
        self.assertEqual(got["finished"], 0)
        self.assertAlmostEqual(got["part"], 300 / 2400.0, places=2)
        progress["ch"]["02 ch.mp3"] = {"at": 600, "far": 600}
        got = self.books.summary("B", files, progress)
        self.assertAlmostEqual(got["part"], 900 / 2400.0, places=2)
        self.assertEqual(got["finished"], 1)          # the second one ran to its end

    def test_it_never_goes_over_the_whole(self):
        files = self.files(n=2, secs=100)
        progress = {"ch": {"01 ch.mp3": {"far": 9999}, "02 ch.mp3": {"far": 9999}}}
        self.assertLessEqual(self.books.summary("B", files, progress)["part"], 1.0)

    def test_nothing_heard_is_nothing_filled(self):
        self.assertEqual(self.books.summary("B", self.files(), {"ch": {}})["part"], 0)

    def test_a_novel_uses_what_the_reader_wrote_down(self):
        rel = "books/n.txt"
        os.makedirs(os.path.join(card.MUSIC_DIR, "books"))
        path = os.path.join(card.MUSIC_DIR, rel)
        with open(path, "w") as f:
            f.write("Chapter 1\n\nWords.\n")
        self.assertEqual(card.read_part(rel), 0)
        with self.lib.edit() as lib:
            self.lib.record(lib, self.lib.text_id(path))["read"]["percent"] = 0.37
        self.assertAlmostEqual(card.read_part(rel), 0.37, places=3)
        self.assertEqual(card.read_part("books/gone.txt"), 0)   # no such book

    def test_the_fill_is_quiet_and_costs_nothing_when_there_is_none(self):
        src = open(os.path.join(HERE, "..", "PanelRow.qml")).read()
        fill = src[src.index("Loader {"):][:600]
        self.assertIn("active: Number(rowItem.row.part) > 0", fill)   # nothing made for most rows
        self.assertRegex(fill, r"opacity: rowItem\.hot \? 0\.1[0-9]? : 0\.0[0-9]")

    def test_every_list_that_shows_a_book_fills(self):
        src = open(os.path.join(HERE, "..", "MusicPanel.qml")).read()
        book = src[src.index("function bookRow"):][:900]
        self.assertEqual(book.count("part:"), 2)        # a novel and an audiobook
        chapter = src[src.index('type: "chapter"'):][:400]
        self.assertIn("part:", chapter)                 # an audiobook's chapters
        novel = src[src.index('type: "novelchapter"'):][:700]
        self.assertIn("part:", novel)                   # a novel's chapters


class SpokenChaptersAreAsLoudAsHisMusic(unittest.TestCase):
    """Measured 2026-09-25: his songs average -15 dB, a chapter as the engine
    made it -26 dB. A book sounded quiet after a song."""

    def setUp(self):
        import speak
        self.speak = speak

    def test_the_gain_is_worked_out_for_each_chapter(self):
        # Engines differ -- the PyTorch Kokoro ran 3 dB under the ONNX one --
        # so a fixed gain would be wrong for one of them.
        target = self.speak.LOUDNESS
        for mean, want in ((-26.0, 10.0), (-22.2, 6.2), (-16.0, 0.0), (-10.0, 0.0)):
            gain = max(0.0, min(24.0, target - mean))
            self.assertAlmostEqual(gain, want, places=1)

    def test_it_never_turns_a_chapter_down_or_up_absurdly(self):
        target = self.speak.LOUDNESS
        self.assertEqual(max(0.0, min(24.0, target - (-5.0))), 0.0)     # already loud: leave it
        self.assertEqual(max(0.0, min(24.0, target - (-90.0))), 24.0)   # near silence: capped

    def test_the_limiter_does_not_put_the_gain_straight_back(self):
        # ffmpeg's alimiter levels its output back up unless told not to, so
        # the peak sat at full scale whatever the limit said.
        src = open(os.path.join(HERE, "..", "scripts", "speak.py")).read()
        body = src[src.index("def join("):src.index("def rescan(")]
        self.assertIn("level=false", body)
        self.assertIn("volumedetect", src)        # it measures before it decides

    def test_chapters_made_before_this_can_be_brought_up(self):
        src = open(os.path.join(HERE, "..", "scripts", "speak.py")).read()
        self.assertIn("def cmd_spoken_relevel", src)
        body = src[src.index("def cmd_spoken_relevel"):src.index("def mean_level")]
        self.assertIn("LOUDNESS - 1.0", body)     # one already right is left alone
        self.assertIn("level=false", body)


class KokoroRunsThroughOnnx(unittest.TestCase):
    """The same model and voices without PyTorch: 1.9 GB down to 0.7 GB, and
    the model loads in 0.5 s instead of 4.8 s (2026-09-25)."""

    def setUp(self):
        import voices
        self.voices = voices

    def test_it_needs_the_model_beside_the_environment(self):
        model, styles = self.voices.kokoro_files()
        self.assertTrue(model.endswith(".onnx"))
        self.assertTrue(styles.endswith(".bin"))
        self.assertIn("models", model)

    def test_a_missing_model_means_not_ready(self):
        saved = self.voices.VENV
        self.voices.VENV = tempfile.mkdtemp()
        try:
            self.assertFalse(self.voices.kokoro_ready())
        finally:
            self.voices.VENV = saved

    def test_nothing_asks_for_pytorch_any_more(self):
        src = open(os.path.join(HERE, "..", "scripts", "voices.py")).read()
        install = src[src.index("def install_kokoro"):src.index("def fetch_model")]
        self.assertNotIn("torch", install)
        self.assertIn("kokoro-onnx", install)

    def test_a_download_cut_off_is_not_taken_for_a_voice(self):
        src = open(os.path.join(HERE, "..", "scripts", "voices.py")).read()
        body = src[src.index("def fetch_one("):src.index("def plainly(")]
        self.assertIn('".part"', body)            # named only once it is whole
        self.assertIn("os.replace(part, target)", body)
        # And it refuses a short file rather than naming it.
        self.assertIn("the download stopped part way", body)

    def test_a_dropped_download_carries_on_where_it_stopped(self):
        """A 310 MB model over a poor connection must not start again."""
        import voices
        body = open(os.path.join(HERE, "..", "scripts", "voices.py")).read()
        one = body[body.index("def fetch_one("):body.index("def plainly(")]
        self.assertIn('add_header("Range", "bytes=%d-" % have)', one)
        self.assertIn("r.status == 206", one)      # only append if it really resumed
        self.assertIn('mode = "ab" if resumed else "wb"', one)
        self.assertIn("416", one)                  # a server that will not resume
        # The room is asked about before anything is downloaded.
        install = body[body.index("def install_kokoro"):body.index("def fetch_model")]
        self.assertIn("room_for(ROOM_NEEDED)", install)
        enough, why = voices.room_for(1 << 62)
        self.assertFalse(enough)
        self.assertIn("Not enough room", why)

    def test_the_install_says_how_far_along_it_is(self):
        import voices
        self.assertEqual(sorted(voices.STAGES), sorted(
            ["python", "engine", voices.MODEL, voices.VOICES_BIN]))
        low = 0
        for stage in ("python", "engine", voices.MODEL, voices.VOICES_BIN):
            a, b = voices.STAGES[stage]
            self.assertEqual(a, low, "stages must not leave a gap: " + stage)
            self.assertLess(a, b)
            low = b
        self.assertLessEqual(low, 100)
        # and the card is told a number it can draw
        import jobs
        note = {"state": "working", "at": __import__("time").time(), "percent": 42,
                "engine": "kokoro", "note": "Getting the voice", "doing": "114 MB of 310 MB"}
        saved = card.load
        try:
            card.load = lambda name, default=None: (note if name == voices.INSTALL
                                                    else (default if default is not None else {}))
            one = jobs.installing()
            self.assertEqual(one["percent"], 42)
            self.assertIn("114 MB", one["detail"])
        finally:
            card.load = saved

    def test_the_install_speaks_plainly_when_it_fails(self):
        import voices
        for machine, said in (
                ("urlopen error [Errno -2] Name or service not known", "No internet"),
                ("The read operation timed out", "carries on from where it stopped"),
                ("[Errno 28] No space left on device", "disk is full"),
                ("IncompleteRead(3 bytes read)", "connection dropped")):
            self.assertIn(said, voices.plainly(machine), machine)

    def test_pressing_install_twice_does_not_start_two(self):
        src = open(os.path.join(HERE, "..", "scripts", "speak.py")).read()
        body = src[src.index("def cmd_voice_install("):src.index("def cmd_voice_install_run(")]
        self.assertIn("It is already installing", body)
        self.assertIn("room_for", body)

    def test_it_no_longer_claims_word_times_it_does_not_have(self):
        # ONNX reports phonemes, and the model that reports anything runs at
        # 2.7x instead of 4.7x. Sentence times do not come from the engine, so
        # what he uses is unaffected; word-by-word falls back to spreading.
        kokoro = next(e for e in self.voices.ENGINES if e["id"] == "kokoro")
        self.assertFalse(kokoro["words"])
        say = open(os.path.join(HERE, "..", "scripts", "kokoro_say.py")).read()
        self.assertIn("create(", say)
        self.assertNotIn("start_ts", say)


class ListeningWhileItIsMade(unittest.TestCase):
    """A chapter is made in pieces and played from the first one, so he waits
    about half a minute instead of two and a half (his choice, 2026-09-25)."""

    def setUp(self):
        import booktext
        import library
        import speak
        self.speak, self.bt, self.lib = speak, booktext, library
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (card.STATE_DIR, card.MUSIC_DIR, booktext.CACHE_DIR, card.MPD_PORT)
        card.STATE_DIR = os.path.join(self.tmp.name, "state")
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        card.MPD_PORT = 6123                      # nothing there: MPD is not used
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        os.makedirs(card.MUSIC_DIR)
        card.load, card.save = Settings.real_load, Settings.real_save
        card.save(library.LIBRARY, {"version": library.VERSION, "books": {}, "ids": {},
                                    "playing": None})
        card.save("settings.json", {"voiceEngine": "tone"})
        self.path = os.path.join(self.tmp.name, "n.txt")
        with open(self.path, "w") as f:
            f.write("Chapter 1\n\n" + " ".join("Sentence number %d here." % i
                                                for i in range(40)) + "\n")

    def tearDown(self):
        card.STATE_DIR, card.MUSIC_DIR, self.bt.CACHE_DIR, card.MPD_PORT = self.saved
        self.tmp.cleanup()

    def test_pieces_are_handed_over_as_they_are_made(self):
        got = []
        saved = self.speak.PIECE_SECONDS
        self.speak.PIECE_SECONDS = 0.5            # the test hum is short
        try:
            rel = self.speak.make_chapter(self.path, 0, token="",
                                          piece_ready=lambda f, n, s, i: got.append((i, n, s)))
        finally:
            self.speak.PIECE_SECONDS = saved
        self.assertGreater(len(got), 1, "the chapter came in one lump")
        self.assertEqual([i for i, _n, _s in got], list(range(1, len(got) + 1)))
        self.assertEqual([n for _i, n, _s in got],
                         ["%03d.mp3" % i for i in range(1, len(got) + 1)])
        # and the chapter itself is still one file at the end
        self.assertTrue(os.path.exists(os.path.join(card.MUSIC_DIR, rel)))

    def test_a_piece_is_named_only_once_it_is_whole(self):
        # ffmpeg fills a file as it goes; MPD told about a half-written piece
        # gives up on it and moves to the next, which is a piece of the book
        # skipped (heard on 2026-09-27).
        seen = []
        saved = self.speak.PIECE_SECONDS
        self.speak.PIECE_SECONDS = 0.5
        try:
            def look(folder, name, seconds, number):
                full = os.path.join(card.MUSIC_DIR, folder, name)
                seen.append((os.path.exists(full), os.path.getsize(full)))
            self.speak.make_chapter(self.path, 0, token="", piece_ready=look)
        finally:
            self.speak.PIECE_SECONDS = saved
        for there, size in seen:
            self.assertTrue(there)               # it exists when handed over
            self.assertGreater(size, 500)        # and it is not a stub

    def test_the_pieces_all_share_one_loudness(self):
        # Measuring each separately would step the volume between them.
        src = open(os.path.join(HERE, "..", "scripts", "speak.py")).read()
        body = src[src.index("def hand_over"):src.index("    try:", src.index("def hand_over"))]
        self.assertIn("gain = join(", body)      # the gain comes back out
        self.assertIn("nonlocal", body)          # and is kept for the next one

    def test_where_he_got_to_moves_onto_the_finished_chapter(self):
        rel = "Spoken/B/001 One.mp3"
        book_id = self.lib.text_id(self.path)
        with self.lib.edit() as lib:
            seen = self.lib.record(lib, book_id)["listen"]["ch"]
            seen["001.mp3"] = {"at": 40, "far": 40}
            seen["002.mp3"] = {"at": 10, "far": 30}

        class Pieces:
            seconds = [100.0, 90.0, 80.0]
        self.speak.carry_over(self.path, rel, Pieces())
        ch = self.lib.get(book_id)["listen"]["ch"]
        self.assertNotIn("001.mp3", ch)          # the pieces' own records go
        self.assertNotIn("002.mp3", ch)
        # furthest reached: 100 s of the first piece, then 30 s into the second
        self.assertEqual(ch["001 One.mp3"]["far"], 130)

    def test_nothing_to_carry_is_not_a_crash(self):
        class Nothing:
            seconds = []
        self.speak.carry_over(self.path, "Spoken/B/001 One.mp3", Nothing())

    def test_only_the_first_of_a_batch_is_listened_to(self):
        self.speak.queue_add(self.path, [3, 4, 5], "n", listen=True)
        want = [(i["chapter"], i["listen"]) for i in self.speak.queue_of()]
        self.assertEqual(want, [(3, True), (4, False), (5, False)])

    def test_asking_for_more_cancels_an_earlier_stop(self):
        # A request made just after a Stop was killed by that stop: it joined
        # the queue, the job picked it up, saw the flag and gave up, and the
        # card said "done" having made nothing (2026-09-27).
        with Silent():
            self.speak.cmd_speak_stop()
        self.assertTrue(self.speak.stop_asked())
        saved = self.speak.busy
        self.speak.busy = lambda: {"state": "making"}     # one still winding down
        try:
            self.speak.start_job()
        finally:
            self.speak.busy = saved
        self.assertFalse(self.speak.stop_asked())

    def test_a_job_says_why_it_ended(self):
        src = open(os.path.join(HERE, "..", "scripts", "speak.py")).read()
        body = src[src.index("def run_queue"):]
        self.assertIn('why=why', body)
        self.assertIn('"nothing left to make"', body)
        self.assertIn('"stopped part way through"', body)


class ChoosingAWordInABook(unittest.TestCase):
    """Choosing a word or a sentence, to look up, mark, or read from
    (his ask, 2026-09-25)."""

    def setUp(self):
        import booktext
        import library
        import meaning
        import reader
        self.r, self.bt, self.lib, self.meaning = reader, booktext, library, meaning
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (card.STATE_DIR, card.MUSIC_DIR, booktext.CACHE_DIR, card.MPD_PORT)
        card.STATE_DIR = os.path.join(self.tmp.name, "state")
        card.MUSIC_DIR = os.path.join(self.tmp.name, "music")
        card.MPD_PORT = 6123
        booktext.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        os.makedirs(card.MUSIC_DIR)
        card.load, card.save = Settings.real_load, Settings.real_save
        card.save(library.LIBRARY, {"version": library.VERSION, "books": {}, "ids": {},
                                    "playing": None})
        self.path = os.path.join(self.tmp.name, "n.txt")
        with open(self.path, "w") as f:
            f.write("Chapter 1\n\nThe swallow flew on. He was very tired indeed.\n")

    def tearDown(self):
        card.STATE_DIR, card.MUSIC_DIR, self.bt.CACHE_DIR, card.MPD_PORT = self.saved
        self.tmp.cleanup()

    def open_at(self):
        app = self.r.Reader(24, 80)
        app.open(self.path)
        return app

    def test_choosing_starts_on_the_line_he_is_reading(self):
        app = self.open_at()
        self.assertIsNone(app.pick)
        app.key("s")
        self.assertIsNotNone(app.pick)
        self.assertTrue(app.pick_text())

    def test_moving_along_the_words_and_between_the_lines(self):
        app = self.open_at()
        app.key("s")
        first = app.pick_text()
        app.key("l")
        self.assertNotEqual(app.pick_text(), first)
        app.key("h")
        self.assertEqual(app.pick_text(), first)
        app.key("h")                       # before the first word: it stays put
        self.assertTrue(app.pick_text())

    def test_a_word_or_the_whole_sentence(self):
        app = self.open_at()
        app.key("s")
        word = app.pick_text()
        app.key("w")
        self.assertEqual(app.pick_kind, "sentence")
        sentence = app.pick_text()
        self.assertIn(word, sentence)
        self.assertGreater(len(sentence.split()), len(word.split()))
        app.key("w")
        self.assertEqual(app.pick_text(), word)

    def test_escape_puts_it_away(self):
        app = self.open_at()
        app.key("s")
        app.key("\x1b")
        self.assertIsNone(app.pick)
        app.key("j")                       # and reading works again
        self.assertIsNone(app.pick)

    def test_a_bookmark_keeps_the_words_he_chose(self):
        app = self.open_at()
        app.key("s")
        app.key("w")                       # the whole sentence
        sentence = app.pick_text()
        app.key("m")
        marks = self.lib.get(app.book.key)["bookmarks"]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["note"], sentence)
        self.assertEqual(marks[0]["chapter"], app.ci)

    def meaning_calls(self, offline_answers, web_answers, reachable=True, **settings):
        """Which source gets asked, with both stubbed. Nothing leaves here."""
        called = []
        saved = (self.meaning.from_sdcv, self.meaning.from_web, self.meaning.reachable)
        self.meaning.from_sdcv = lambda w, l: called.append("offline") or (
            [{"part": "noun", "definition": "a small bird", "example": ""}]
            if offline_answers else [])
        self.meaning.from_web = lambda w, l: called.append("web") or (
            [{"part": "noun", "definition": "to gulp", "example": ""}] if web_answers else [])
        self.meaning.reachable = lambda host=None: reachable
        self.meaning._no_way_out_until = 0.0
        card.save("settings.json", dict(settings))
        card.save(self.meaning.CACHE, {})
        try:
            got = self.meaning.look("swallow")
        finally:
            (self.meaning.from_sdcv, self.meaning.from_web,
             self.meaning.reachable) = saved
        return called, got

    def test_online_leads_when_connected_and_offline_takes_over_when_not(self):
        """Chosen 2026-10-01: online when connected, the offline dictionaries
        when not. It was the other way round (offline first, 2026-09-25); that
        order was reversed, and the reason it existed -- a word looked up is a
        word sent away -- is kept as the "offline" setting, which never asks the
        web at all."""
        called, got = self.meaning_calls(True, True, reachable=True)
        self.assertEqual(called, ["web"])            # connected: online answers
        self.assertEqual(got["where"], "Wiktionary")
        self.assertEqual(got["entries"][0]["definition"], "to gulp")

        called, got = self.meaning_calls(True, True, reachable=False)
        self.assertEqual(called, ["offline"])        # no way out: his dictionaries
        self.assertEqual(got["where"], "your dictionaries")

        # Connected but the web has no entry: the dictionaries still get a turn.
        called, _got = self.meaning_calls(True, False, reachable=True)
        self.assertEqual(called, ["web", "offline"])

    def test_either_source_can_be_pinned_and_offline_never_sends_the_word(self):
        called, _ = self.meaning_calls(True, True, reachable=False, meaningSource="online")
        self.assertEqual(called, ["web"])            # online only, connected or not
        called, got = self.meaning_calls(False, True, reachable=True,
                                         meaningSource="offline")
        self.assertEqual(called, ["offline"])        # never asked the web
        self.assertIn("offline", got["why"].lower())
        self.assertEqual(self.meaning.SOURCES, ("auto", "online", "offline"))
        # Nonsense falls back to asking both in the usual order.
        card.save("settings.json", {"meaningSource": "whatever"})
        self.assertEqual(self.meaning.source(), "auto")

    def test_a_word_already_looked_up_is_not_sent_again(self):
        called, _ = self.meaning_calls(False, True, reachable=True)
        self.assertEqual(called, ["web"])
        saved = (self.meaning.from_web, self.meaning.reachable)
        self.meaning.from_web = lambda w, l: called.append("web") or []
        self.meaning.reachable = lambda host=None: True
        try:
            got = self.meaning.look("swallow")       # the kept answer
        finally:
            self.meaning.from_web, self.meaning.reachable = saved
        self.assertEqual(called, ["web"])            # asked once, not twice
        self.assertEqual(got["entries"][0]["definition"], "to gulp")

    def test_no_way_out_is_believed_for_a_while_not_retried_per_word(self):
        """A name that resolves is not a promise -- a captive portal resolves
        everything -- so a request that fails writes the same verdict down."""
        saved = self.meaning._no_way_out_until
        try:
            self.meaning._no_way_out_until = 0.0
            self.assertTrue(self.meaning.reachable("localhost"))
            self.assertFalse(self.meaning.reachable("nothing-here.invalid"))
            # Now remembered: even a name that would resolve answers False.
            self.assertFalse(self.meaning.reachable("localhost"))
            self.meaning._no_way_out_until = 0.0
            self.assertTrue(self.meaning.reachable("localhost"))
        finally:
            self.meaning._no_way_out_until = saved

    def test_being_offline_with_no_dictionaries_says_so_not_no_entry(self):
        """Measured 2026-10-01: sdcv is not installed on this machine and there
        are no dictionaries anywhere, so the offline half answers nothing. "No
        entry for that word" would read as the word not existing."""
        saved = (self.meaning.offline_ready, self.meaning.from_sdcv,
                 self.meaning.reachable)
        self.meaning.offline_ready = lambda: False
        self.meaning.from_sdcv = lambda w, l: []
        self.meaning.reachable = lambda host=None: False
        card.save("settings.json", {})
        card.save(self.meaning.CACHE, {})
        try:
            got = self.meaning.look("swallow")
        finally:
            (self.meaning.offline_ready, self.meaning.from_sdcv,
             self.meaning.reachable) = saved
        self.assertIn("sdcv", got["why"])
        self.assertNotIn("No entry", got["why"])

    def test_only_a_word_is_ever_sent(self):
        for bad in ("", "   ", "a" * 80, "two words\nand a break", "\x01"):
            self.assertEqual(self.meaning.tidy(bad), "")
        self.assertEqual(self.meaning.tidy(" “plaque,” "), "plaque")
        self.assertEqual(self.meaning.tidy("Hall."), "Hall")

    def test_the_language_is_his_to_set_and_is_checked(self):
        self.assertEqual(self.meaning.language(), "en")
        card.save("settings.json", {"meaningLanguage": "gu"})
        self.assertEqual(self.meaning.language(), "gu")
        card.save("settings.json", {"meaningLanguage": "not a code"})
        self.assertEqual(self.meaning.language(), "en")     # nonsense falls back

    def test_reading_from_here_on_a_book_with_no_speech_says_so(self):
        app = self.open_at()
        app.key("s")
        started = []
        saved = subprocess.Popen
        subprocess.Popen = lambda *a, **k: started.append(a[0])
        try:
            app.key(" ")
        finally:
            subprocess.Popen = saved
        self.assertIn("aloud", app.message)
        self.assertIsNone(app.pick)          # it puts the choosing away


class TheCardShouldNotGetInItsOwnWay(unittest.TestCase):
    """What he reported on 2026-09-27: a YouTube song that would not start, a
    radio that seemed not to stop, and the whole thing feeling slower."""

    def test_the_relay_never_waits_long_for_the_cookies(self):
        # MPD reads from the relay. If the relay queues for the browser's
        # cookie store, the music queues with it and nothing he presses seems
        # to take.
        self.assertLessEqual(card.HURRIED_WAIT, 10)
        self.assertLessEqual(card.HURRIED_TIMEOUT, 60)
        src = open(os.path.join(HERE, "..", "scripts", "card.py")).read()
        serve = src[src.index("def relay_serve"):src.index("def start_relay")]
        self.assertEqual(serve.count("hurry=True"), 2)   # first ask, and the refresh

    def test_a_hurried_caller_waits_far_less(self):
        import fcntl
        card.STATE_DIR = tempfile.mkdtemp()
        held = open(card.state_path("ytdlp.lock"), "w")
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            t0 = time.time()
            with card.cookie_turn(wait=card.HURRIED_WAIT):
                pass
            hurried = time.time() - t0
        finally:
            held.close()
        self.assertLess(hurried, card.HURRIED_WAIT + 3)
        self.assertGreater(hurried, card.HURRIED_WAIT - 1)   # it did wait its turn

    def test_the_relay_uses_the_link_it_has_rather_than_failing(self):
        # A resolve it cannot make right now used to become an error, which
        # MPD reports as "Failed to decode" and the song does not start.
        card.STATE_DIR = tempfile.mkdtemp()
        card.load, card.save = Settings.real_load, Settings.real_save
        good = {"url": "https://r1.googlevideo.com/x?expire=%d" % (time.time() + 60),
                "headers": {}, "meta": {"title": "t", "artist": "", "source": "u"}}
        card.save("streams.json", {"dQw4w9WgXcQ": good})
        saved = card.ytdlp
        card.ytdlp = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("busy"))
        try:
            url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            got = card.yt_resolve(url, fresh=True, hurry=True)
            self.assertEqual(got["url"], good["url"])     # the one it had
            with self.assertRaises(RuntimeError):
                card.yt_resolve(url, fresh=True)          # not for the card itself
        finally:
            card.ytdlp = saved

    def test_the_card_asks_for_work_once_and_only_while_it_is_open(self):
        src = open(os.path.join(HERE, "..", "CardService.qml")).read()
        self.assertNotIn("speak-status", src)             # it was asked twice a second
        timer = src[src.index("id: workTimer"):][:220]
        self.assertIn("running: root.panelOpen", timer)   # nothing shows it when shut
        # Quick while something is running, slow when nothing is.
        self.assertIn("root.work.busy ? 2000 : 10000", timer)
        self.assertIn("function readAloudFrom", src)      # read out of the one answer

    def test_a_new_job_does_not_wait_for_the_slow_tick(self):
        """Starting work looks at once, so the idle wait cannot hide it."""
        src = open(os.path.join(HERE, "..", "CardService.qml")).read()
        panel = open(os.path.join(HERE, "..", "MusicPanel.qml")).read()
        started = src[src.index("function workStarted"):][:160]
        self.assertIn("workTimer.restart()", started)
        self.assertIn("loadWork()", started)
        for begins in ("import-start", "download"):
            at = src.index('"%s"' % begins)
            self.assertIn("workStarted", src[at:at + 400],
                          "%s starts work without looking" % begins)
        self.assertIn("workStarted", panel)               # retest and voices too


class OneRuleForASetting(unittest.TestCase):
    """A setting's rule is written once (extras.RULES), so the way in and the
    way out cannot drift apart. Before 3.13.0 they were two chains of
    `if key == ...` that had to agree and nothing made them."""

    def test_every_setting_that_needs_a_rule_has_one(self):
        # Anything that is not a plain on/off, a free string or a number with
        # no range should be named in the table; this is the list of the ones
        # deliberately left to their type alone.
        loose = {"musicDir", "notify", "consume", "single", "repeat", "random",
                 "dance", "sleepFade", "spokenAheadOn", "spokenTidy", "lyricsShow",
                 "radioReportPlays", "bookFolders", "hiddenFolders", "countries",
                 "genres", "keys", "starsFolder", "bookmarks"}
        for key in extras.ENUMS:
            self.assertIn(key, extras.RULES, key)
        for key in ("voiceSpeed", "voiceCores", "spokenAhead", "spokenFolder",
                    "voiceEngine", "voiceName", "downloadFolder", "meaningLanguage",
                    "crossfade", "barMiddle", "barRight", "youtubeLogin"):
            self.assertIn(key, extras.RULES, key)
        for key in extras.RULES:
            self.assertIn(key, extras.DEFAULTS, key)
            self.assertNotIn(key, loose, key)

    def test_what_is_refused_going_in_is_refused_coming_out(self):
        """The drift that used to be possible, checked for every rule at once.

        A value the card refuses must not be honoured if it appears in the
        file some other way, and a value it accepts must survive being read
        back. Both directions ask `usable`, so this holds by construction --
        and this test is what would notice if anyone wrote a third chain.
        """
        tries = {
            "voiceCores": ([1, 2, 4, 16], [0, -1, 17, 100]),
            "voiceSpeed": ([50, 100, 300], [49, 301, 0]),
            "spokenAhead": ([1, 5, 10], [0, 11, -3]),
            "crossfade": ([0, 3, 6, 10], [1, 7, 99]),
            "voiceEngine": (["", "kokoro", "espeak"], ["nonesuch", "Kokoro"]),
            "barMiddle": (list(extras.BAR_ACTIONS), ["something"]),
            "meaningLanguage": (["en", "hi", "gu"], ["english", "e", "!!"]),
            "downloadFolder": (["", "Songs"], ["../escape", "/etc"]),
            "spokenFolder": (["Spoken", "Books/Spoken"], ["../out", "/etc"]),
            "searchResults": ([6, 10, 15], [7, 0, 999]),
            "bookRewind": ([0, 5, 10, 30], [7, -1]),
        }
        for key, (good, bad) in tries.items():
            saved = card.load
            try:
                for value in good:
                    self.assertTrue(extras.usable(key, value),
                                    "%s should take %r" % (key, value))
                    # and it survives being read back out of the file
                    card.load = lambda name, default=None, k=key, v=value: (
                        {k: v} if name == "settings.json"
                        else (default if default is not None else {}))
                    self.assertEqual(extras.settings()[key], value,
                                     "%s lost %r on the way out" % (key, value))
            finally:
                card.load = saved
            for value in bad:
                self.assertFalse(extras.usable(key, value), "%s should refuse %r" % (key, value))

    def test_a_refused_value_in_the_file_is_ignored_not_trusted(self):
        """The file is ours, but a half-written or hand-edited one is not."""
        saved = card.load
        try:
            card.load = lambda name, default=None: (
                {"voiceCores": 64, "voiceSpeed": 9000, "voiceEngine": "nonesuch",
                 "spokenFolder": "../somewhere", "searchResults": 7, "folderSort": "name"}
                if name == "settings.json" else (default if default is not None else {}))
            got = extras.settings()
            self.assertEqual(got["voiceCores"], extras.DEFAULTS["voiceCores"])
            self.assertEqual(got["voiceSpeed"], extras.DEFAULTS["voiceSpeed"])
            self.assertEqual(got["voiceEngine"], extras.DEFAULTS["voiceEngine"])
            self.assertEqual(got["spokenFolder"], extras.DEFAULTS["spokenFolder"])
            self.assertEqual(got["searchResults"], extras.DEFAULTS["searchResults"])
            self.assertEqual(got["folderSort"], "name")        # this one was fine
        finally:
            card.load = saved

    def test_the_two_chains_are_gone(self):
        src = open(os.path.join(HERE, "..", "scripts", "extras.py")).read()
        merge = src[src.index("def settings():"):src.index("def cmd_settings():")]
        self.assertNotIn("voiceSpeed", merge)        # no rule repeated in the merge
        self.assertNotIn("1 <= value <= 10", merge)
        self.assertIn("usable(key, value)", merge)
        putting = src[src.index("def cmd_set_setting("):src.index('if key == "consume"')]
        self.assertNotIn("50 <= value <= 300", putting)
        self.assertIn("usable(key, value)", putting)


class LessWorkInTheBackground(unittest.TestCase):
    """What the card does when nobody is asking it for anything.

    Measured 2026-09-27: an idle open card started 24 processes a minute, a
    reader nobody was touching woke four times a second, the watcher ran two
    threads to write down the same thing, and making a chapter took fourteen
    of sixteen cores.
    """

    def test_one_tick_writes_down_where_playback_got_to(self):
        import extras
        import books
        self.assertTrue(hasattr(extras, "note_loop"))
        self.assertFalse(hasattr(extras, "position_loop"))   # folded in
        self.assertFalse(hasattr(books, "loop"))             # folded in
        card_src = open(os.path.join(HERE, "..", "scripts", "card.py")).read()
        watch = card_src[card_src.index("def cmd_watch"):]
        watch = watch[:watch.index("while True")]
        # headphones, note, and the music folder. The third was added on
        # 2026-09-30 with its reason: MPD never reports a folder with no sound
        # in it, so nothing could tell the card about a folder of novels. It
        # blocks on a read and wakes only when the folder changes, so it costs
        # nothing while nothing happens -- which is the rule this test guards,
        # not the number itself. Raise it only for another watch, never for a
        # ticker.
        self.assertEqual(watch.count("threading.Thread"), 3)
        self.assertIn("folder_loop", watch)

    def test_the_refresh_button_looks_at_folders_and_not_only_at_mpd(self):
        """2026-09-30. "Look for new songs now" only told MPD to look. MPD
        reports no change at all for a folder of novels, so pressing it after
        adding one did nothing whatever -- the one button a person would reach
        for could not fix the one thing they had noticed."""
        panel = open(os.path.join(HERE, "..", "MusicPanel.qml")).read()
        branch = panel[panel.index('row.type === "rescan"'):]
        branch = branch[:branch.index('row.type === "clearsaved"')]
        self.assertIn("svc.loadHome()", branch)      # the list, not just MPD
        rows = open(os.path.join(HERE, "..", "SettingsRows.qml")).read()
        self.assertIn("new songs and books", rows)   # and it says so

    def test_the_music_folder_is_watched_not_asked(self):
        """2026-09-30. MPD indexes sound, so a folder of novels is never in its
        database and no rescan puts it there; and a move adds the destination
        while leaving the source behind for ever (measured: create seen in
        5.6 s, delete in 6.4 s, a move never). So the card watches the folder.
        A watch, not a tick: it blocks until something happens."""
        import struct
        import extras
        head = lambda mask, name: (struct.pack("iIII", 1, mask, 0, len(name) + 1)
                                   + name.encode() + b"\0")
        names, overflow = extras._events(head(extras.IN_CREATE, "Books")
                                         + head(extras.IN_MOVED_FROM, "Eng"))
        self.assertEqual(names, ["Books", "Eng"])
        self.assertFalse(overflow)
        # The kernel dropping events is the one case that must not pass
        # quietly: what changed is then unknown, so everything counts as
        # changed. Silently missing it is the bug this whole watch fixes.
        _names, overflow = extras._events(struct.pack("iIII", -1, extras.IN_Q_OVERFLOW, 0, 0))
        self.assertTrue(overflow)
        # A name is never put into an MPD command: it may contain a newline,
        # and MPD's protocol is line-based, so that would be two commands.
        src = open(os.path.join(HERE, "..", "scripts", "extras.py")).read()
        watch = src[src.index("def watch_music_folder"):src.index("def _events")]
        self.assertIn('raw("update")', watch)
        self.assertNotIn('"update",', watch)

    def test_the_watch_reports_a_folder_of_novels_and_bundles_a_burst(self):
        import threading
        import extras
        tmp = tempfile.TemporaryDirectory()
        saved = (card.MUSIC_DIR, card.MPD_PORT, extras.SETTLE)
        card.MUSIC_DIR = tmp.name
        card.MPD_PORT = 6123               # nothing there: no MPD is touched
        extras.SETTLE = 0.2
        seen = []
        try:
            t = threading.Thread(target=extras.watch_music_folder,
                                 args=(seen.append,), kwargs={"once": True}, daemon=True)
            t.start()
            time.sleep(0.4)
            os.makedirs(os.path.join(tmp.name, "Books"))
            with open(os.path.join(tmp.name, "Books", "a novel.epub"), "w") as f:
                f.write("x")
            t.join(4)
            self.assertEqual(seen, [{"foldersChanged": True}])   # one report
            # A burst is one change to him, not two hundred.
            seen.clear()
            t = threading.Thread(target=extras.watch_music_folder,
                                 args=(seen.append,), kwargs={"once": True}, daemon=True)
            t.start()
            time.sleep(0.4)
            for i in range(40):
                with open(os.path.join(tmp.name, "f%02d.txt" % i), "w") as f:
                    f.write("x")
            t.join(4)
            self.assertEqual(len(seen), 1)
        finally:
            card.MUSIC_DIR, card.MPD_PORT, extras.SETTLE = saved
            tmp.cleanup()

    def test_the_note_tick_only_connects_when_there_is_something_to_write(self):
        import extras
        import books
        opened = []
        saved = (card.Mpd, books.playing_folder, extras.worth_a_look, time.sleep)

        class Fake:
            def __init__(self):
                opened.append(1)

            def close(self):
                pass
        rounds = [0]

        def sleep(_n):
            rounds[0] += 1
            if rounds[0] > 2:
                raise KeyboardInterrupt
        try:
            card.Mpd = Fake
            books.playing_folder = lambda: ""
            extras.worth_a_look = lambda: False
            time.sleep = sleep
            with self.assertRaises(KeyboardInterrupt):
                extras.note_loop()
            self.assertEqual(opened, [])          # nothing playing, nothing asked
        finally:
            card.Mpd, books.playing_folder, extras.worth_a_look, time.sleep = saved

    def test_the_reader_waits_longer_when_nothing_is_playing(self):
        import reader
        app = reader.Reader.__new__(reader.Reader)
        app.playing_here = False
        app.follow = False
        app.status = {}
        app.waiting_for = -1
        app.last_key = 0.0
        app._spoken, app._spoken_at = ({}, False), time.time()
        self.assertEqual(app.pace(), reader.Reader.RESTFUL)
        app.playing_here = True
        app.status = {"state": "play"}
        self.assertEqual(app.pace(), reader.Reader.QUICK)
        app.status = {"state": "pause"}
        self.assertEqual(app.pace(), reader.Reader.CALM)   # paused: nothing moves
        app.playing_here = False
        app.status = {"state": "play"}                     # something else is playing
        self.assertEqual(app.pace(), reader.Reader.RESTFUL)
        app.waiting_for = 3
        self.assertEqual(app.pace(), reader.Reader.CALM)
        src = open(os.path.join(HERE, "..", "scripts", "reader.py")).read()
        self.assertIn("win.timeout(app.pace())", src)

    def test_mpd_is_not_asked_once_per_keypress(self):
        import reader
        app = reader.Reader.__new__(reader.Reader)
        app.book = None
        app.playing_here = True
        app.polled_at = 0.0
        app.poll()                       # no book: nothing to ask about
        self.assertFalse(app.playing_here)
        src = open(os.path.join(HERE, "..", "scripts", "reader.py")).read()
        body = src[src.index("def poll(self"):][:900]
        self.assertIn("self.polled_at", body)

    def test_work_begun_elsewhere_is_noticed_at_once(self):
        """The slow tick is safe because the files say when work starts."""
        src = open(os.path.join(HERE, "..", "CardService.qml")).read()
        for name in ("spoken.json", "download.json", "import.json"):
            at = src.index(name)
            watch = src[at:at + 260]
            self.assertIn("watchChanges: true", watch, name)
            self.assertIn("root.loadWork()", watch, name)
        # And the second-by-second re-reading only runs while work does.
        timer = src[src.index("downloadFile.reload(); importFile.reload()") - 300:]
        self.assertIn("running: root.panelOpen && root.work && root.work.busy",
                      timer[:400])

    def test_the_voice_is_told_how_many_cores_it_may_use(self):
        import voices
        saved = card.setting
        try:
            card.setting = lambda key, default=None: 3 if key == "voiceCores" else default
            self.assertEqual(voices.voice_threads(), 3)
            self.assertEqual(voices.voice_env()["RMPC_VOICE_THREADS"], "3")
            card.setting = lambda key, default=None: -5 if key == "voiceCores" else default
            self.assertEqual(voices.voice_threads(), 1)      # never none
            card.setting = lambda key, default=None: 99 if key == "voiceCores" else default
            self.assertEqual(voices.voice_threads(), 16)     # never the whole machine twice
            card.setting = lambda key, default=None: "" if key == "voiceCores" else default
            self.assertEqual(voices.voice_threads(), 2)      # unset means the default
        finally:
            card.setting = saved
        src = open(os.path.join(HERE, "..", "scripts", "voices.py")).read()
        self.assertEqual(src.count("env=voice_env()"), 2)    # one voice and a batch
        say = open(os.path.join(HERE, "..", "scripts", "kokoro_say.py")).read()
        self.assertIn("intra_op_num_threads", say)
        self.assertIn("RMPC_VOICE_THREADS", say)


if __name__ == "__main__":
    unittest.main(verbosity=1)
