# For an AI changing Songbook

You are probably here because he asked you to change his music card. This file
is what you need to do that without breaking it or his music. Read it once;
`DESIGN.md` is the shape of the thing, `REVIEW.md` is what is known to be
wrong and what was decided, `README.md` is the user-facing side.

Two habits matter more than anything below:

1. **Verify by looking at the real card**, not by reasoning that the change
   must work. The loop is in "Checking your work", and it has caught every
   regression this plugin has had — including whole screens drawing nothing
   while an error log stayed empty.
2. **Measure before you claim.** Every number in these files was measured on
   the development machine and most of them surprised the person who guessed
   first.

## What it is

An Omarchy (Quickshell) bar widget: one icon, and a card that plays music
through MPD — local folders, radio from Radio Browser, YouTube through
yt-dlp, lyrics from LRCLIB — and that also keeps audiobooks, reads novels
aloud with an offline voice, and opens a book in a terminal reader of its own.

It is `rushi.songbook`. **`rmpc` in this code means a different program**: a
terminal MPD client someone else wrote, which the card can launch on a right
click. Never rename those references; they are in `BAR_ACTIONS`, the bar
action, and a few comments about what other clients can see.

## Where things are

| File | Lines | What it does |
|---|---|---|
| `manifest.json` | | The Omarchy manifest. `id` is permanent once published. |
| `BarWidget.qml` | 231 | The bar icon, its clicks, and the IPC commands (`omarchy-shell songbook …`). |
| `MusicPanel.qml` | 2463 | The card. One list of rows whose content depends on `view`. |
| `CardService.qml` | 900 | All state, and the only thing that speaks to the backend. Never touches the network. |
| `PanelRow.qml` | 201 | One row: its icons, its hover, and the progress fill. |
| `NowPlaying.qml` | 322 | The top of the card: art, title, controls. |
| `SettingsRows.qml` | 376 | The rows of every settings screen. |
| `scripts/card.py` | 2525 | Playback, folders, search, stations, YouTube, downloads, art, the watcher, the relay. |
| `scripts/extras.py` | 1499 | Settings, setup check, queue edits, lyrics, imports, music folder, headphones, the note tick. Owns the command table. |
| `scripts/library.py` | 375 | One record per book, keyed by content. Every writer goes through `library.edit()`. |
| `scripts/books.py` | 584 | Audiobooks: detection, chapters, how far through, the reader's launch. |
| `scripts/booktext.py` | 847 | Text: EPUB and TXT into chapters, paragraphs, sentences, words. The per-chapter cache. |
| `scripts/speak.py` | 1160 | Reading a novel aloud: the queue, the pieces, joining, levelling, the `.lrc`. |
| `scripts/voices.py` | 772 | Voice engines as a connector, and installing Kokoro. |
| `scripts/kokoro_say.py` | 99 | Kokoro itself, inside its own Python. |
| `scripts/reader.py` | 2522 | The terminal reader (curses, no dependencies). |
| `scripts/jobs.py` | 172 | Everything the card is busy with, in one shape. Reads only. |
| `scripts/meaning.py` | 130 | What a word means, offline (`sdcv`) or online. |
| `scripts/beat.py` | 74 | Reads MPD's FIFO and prints a line per beat, for the dancer. |
| `tests/security_test.py` | 5523 | 331 tests. Run them after every change. |

## How it fits together

- **QML draws, Python works.** `CardService.call([...])` runs
  `python3 scripts/card.py <command> <args>` and gets **one line of JSON**
  back. Add a feature as a helper command first, try it in a terminal, then
  call it from QML.
- **The command table**: 89 commands in all. `card.main()` holds 43;
  `extras.COMMANDS` holds the other 46 — 25 its own, and the rest added by
  `books` (5), `speak` (13), `jobs` (2) and `meaning` (1) through
  `COMMANDS.update`. A new command goes in the table of whichever file owns
  the subject, and nothing else has to change.
- **Status is pushed, never polled.** `card.py watch` blocks on MPD's `idle`
  and prints a line on every change. It has five threads: the `idle` loop,
  the YouTube relay, the headphone watch (blocking on `pactl subscribe`), the
  music-folder watch (blocking on an inotify `read`), and one 10 s tick that
  writes down where playback got to. Four of the five block on something and
  cost nothing; only the tick wakes on its own.
- **Idle costs nothing.** This is a rule, not an aspiration, and it is the
  thing most likely to be broken by an innocent-looking addition:

  | | quick, while something is happening | idle |
  |---|---|---|
  | the card asking what is running | 2 s | 10 s, plus file watches so work started anywhere shows at once |
  | the reader's window | 250 ms while this book plays | 1 s |
  | the note tick | 10 s, connecting only when something plays | no connection |
  | the voice | 2 cores (his setting) | nothing |

  Before adding a timer, ask what event could tell you instead. `FileView`
  with `watchChanges: true` is usually the answer; the three job files are
  watched that way.
- **The list is plain data.** `MusicPanel.buildRows()` returns plain objects
  (`{type, key, title, sub, glyph, action, extras, part, data}`) behind a
  75 ms debounce. A new screen is a new `view` value plus a branch in
  `buildRows()`, `enter()` and `activate()`, and its name in `allViews()`.
  `part` (0…1) draws the row filling from the left — the quiet way progress
  is shown everywhere, deliberately not a number.
- **One record per book** (`library.py`). A book's id comes from its content
  — an audiobook from its files' names and lengths, a novel from its bytes —
  so moving or renaming files keeps his place. Never key progress by path.
  `library.playing_id()` is how a spoken chapter records under the novel it
  came from rather than as a new audiobook.
- **Reading aloud** (`speak.py`): a chapter is made in pieces of ~75 s so
  listening can start in about half a minute instead of at the end. Pieces
  are built under another name and `os.replace`d into place, because ffmpeg
  writes progressively and MPD will happily play a half-written file. The
  spoken folder is then simply an audiobook, which is why nothing else had to
  learn about it.
- **Voices are a connector** (`voices.py`): an engine is a dict
  (ready/voices/speak, its file type, whether it reports word times), so
  adding one is a few lines. Never add an engine's dependency to the card:
  the card must work with none installed. Kokoro runs through ONNX Runtime in
  its own Python under `~/.local/share/rushi.songbook`, and is told how many
  cores it may use (`voiceCores`) — left alone it takes every core and is not
  even faster for it.
- **The reader** keeps drawing apart from curses (`put(y, x, text, style)`),
  so `reader.py --render 90x30 --keys "C" PATH` prints a screen as text and
  the tests use that. **Never simulate keystrokes.** Card commands called
  from it go through `quiet()` so their JSON never reaches the screen.
- **YouTube goes through a relay** (`start_relay`, a thread of the watcher on
  127.0.0.1). MPD is given `http://127.0.0.1:<port>/yt/<id>`, never a
  googlevideo link, because YouTube cuts un-ranged requests after about a
  minute. Resolve with `yt_resolve` (cached in `streams.json`).
- **One file writer.** `card.write_file` is the only way to write a file:
  per-writer temp name, keeps the mode, fsync and a `.bak` for the precious
  ones (`library.json`, `stars.json`, `settings.json`, `reader.json`). A test
  fails if anyone writes a file their own way.
- **Locks are flocks** on: library edits (reentrant, per thread), the speak
  queue, the speak job, and yt-dlp's access to browser cookies (which
  serialise anyway — four at once took 28 s each).

## Adding a setting

One place now, not three (3.13.0):

1. `DEFAULTS` in `extras.py` — the default, which also fixes the type.
2. `RULES` in `extras.py` — what else must be true, and the sentence to say
   when it is refused. Both directions ask `usable()`, so there is nothing to
   keep in step. A few fixed choices can go in `ENUMS` instead, which builds
   its rule automatically; a list setting goes in `LISTS` with a per-item
   check and a limit.
3. A row in its screen's branch of `SettingsRows.qml`, and its steps in
   `cycles` in `toggleSetting()` if it is not on/off.
4. The same key in the default `settings` object in `CardService.qml` — a
   test checks that every key is there.

From Python, read one with `card.setting(key, default)`.

## Rules that keep it safe (each one was learned the hard way)

1. **Never give a QML Repeater or ListView live objects** (MPRIS players,
   device lists). Rebuilding from a live list crashed the whole shell
   (Omarchy #11202, #11755). Copy into plain data first.
2. **Every `Text` is `textFormat: Text.PlainText`.** Station names and video
   titles come from strangers; rich text would render `<img src=…>` and fetch
   it. A test counts them.
3. **QML never loads anything from the internet.** Python fetches; QML gets a
   local file or JSON. An https image in the shell crashed it (Omarchy #8026).
4. **Check input before changing anything.** `card.check_arg()` refuses line
   breaks, which would inject MPD commands.
5. **User values reach yt-dlp after `--`**, never as options.
6. **No `sudo`, no system services, no downloading code and running it.** The
   only config file touched is `mpd.conf`'s `music_directory`, when he saves
   a new folder, with a dated backup.
7. **Do not give a property, a function and an element `id` the same name.**
   `keys` was a property and an id once and every shortcut stopped working; a
   function `step()` was hidden by the dancer's `property int step`, so radio
   next/previous did nothing. Two tests check.
8. **Child processes must die with their parent** (`die_with_parent`,
   `stop_with_children`).
9. **The relay stays narrow**: 127.0.0.1 only, `/yt/<11-char id>` only,
   upstream only `https://*.googlevideo.com`, no caller header forwarded but
   a parsed byte range. It must never become a proxy.
10. **The sign-in is never typed text**: `youtubeLogin` is a key of
    `BROWSERS`, the profile path comes from disk, and cookies are never
    copied anywhere — yt-dlp reads them from the browser each run.
11. **Never reach the local network from a station listing**: one DNS lookup,
    every address must be public, connect by number with the name given for
    TLS, and check redirects the same way. Listings are written by strangers.
12. **A job counts as running only if it said so recently.** A killed process
    leaves its note saying "working" for ever; that is how a stale "making
    38%" once hid a whole queue.

## Checking your work

Run all of this. It is four minutes, and it is the difference between "I
changed it" and "it works".

```sh
omarchy restart shell                     # hot-reload keeps the old component
omarchy-shell songbook views              # every screen's name
omarchy-shell songbook view <name>        # open one
omarchy-shell songbook probe              # "home rows=14 w=328 h=26"
omarchy-shell songbook titles             # what the rows actually say
omarchy-shell songbook press <row key>    # exactly what a click does
omarchy-shell songbook pressAction <key>  # the small button on the right
python3 tests/security_test.py            # 331 tests
journalctl --user --since "2 min ago" | grep -i "songbook\|QML"
```

**Sweep every screen after any QML change.** Open all 36, and fail on an
empty answer, `rows=0`, or `w=0` — not just on something in the log. All
three of these happened with an empty log:

- a lost `width: listWidth` in `PanelRow.qml` made every row zero-wide: the
  whole card became unclickable and drew no icons, and nothing was logged;
- a broken `SettingsRows.qml` stopped the widget loading entirely while a
  sweep that only looked for `w=0` still said "all screens fine";
- two `onPanelOpenChanged` handlers in one file is "Property value set
  multiple times" and the widget does not load.

**`view` skips the code a click runs.** "Import songs" once opened the
station list for every click while checks through `view` looked fine. Use
`press`.

**Never write his real settings in a test**: point `card.STATE_DIR` at a
temporary folder. **Before any playback test, save his queue** and restore it
— even a test you expect to be refused can run `clear` first. Tests may play,
change or stop his real MPD; restore what you touched.

## Traps that have actually cost hours

- **Blind regex over QML renames inside string literals.** A rename turned a
  sentence into "an panel.account" and made a screen draw zero rows. Read
  what you are about to replace.
- **`pkill -f <name>` matches your own shell**, because your command line
  contains the name. It has killed the session twice. Match in Python, or use
  `[n]ame`.
- **Test stand-ins go stale silently.** A fake that copies a signature keeps
  passing after the real function gains a parameter. When you add one, grep
  the tests for the name.
- **flock is per open file**, so a lock held by a function that calls itself
  deadlocks. `library.edit()` is reentrant through `threading.local`.
- **MPD's `update <dir>` refuses a directory it has never seen.** Update the
  parent that it knows.
- **ffmpeg's temp name must keep the real extension** — it infers the format
  from it, and `.making` is "Invalid argument".
- **Never byte-patch a `.pyc`.** Strings are length-prefixed, so a longer
  replacement corrupts the file. Delete `__pycache__` and let Python rebuild.
- **A venv keeps absolute paths** in `pyvenv.cfg` and every script in `bin/`;
  moving its folder needs those rewritten.
- **Measure before reporting.** "3.2 s to open a book warm" was a cold parse;
  warm was 0.44 s. A claimed checksum check had never run. A bomb that
  "parsed" was a bug in the bomb. If you did not see it, do not say it.

## How he works, and how to write for him

- **Phases.** Questions that need him first, in plain words; then the things
  you decide — design it, look at it again from another angle, revise, then
  build; then the small fixes.
- **Plain language in the interface.** "Nothing is running", not "idle".
  Sizes as "310 MB", times as "about 4 minutes left". No jargon, no
  percentages where a filling row will do — his words: *"this only replacing
  the 45% showing not to give dopamin"*.
- **Comments say why, with the date and the number.** The codebase reads as a
  record of decisions: what was tried, what it measured, what was rejected.
  Keep that. A comment that only restates the code is noise.
- **Nothing is added that he did not ask for**, and nothing he asked for is
  quietly dropped. If part of a request turns out to be a bad idea, say so in
  a sentence and build the rest.
