# How the card works

A map of the whole plugin at the top level: what runs, who owns what, where
things are kept, and the paths that cross the most parts. Written 2026-09-24
against version 3.4.2.

The shape in one sentence: **the QML side never thinks, and the Python side
never draws.** Everything the card can do is a command that takes strings and
prints one line of JSON, so the same thing can be done by the bar, by the
card, by the terminal reader, or by hand in a shell.

---

## 1. What runs

```
MPD  (outside the plugin: the player itself)
 │    socket ~/.config/mpd/socket or localhost:6600 · FIFO for the beat
 │
omarchy-shell  (Quickshell — the desktop shell loads the plugin)
└── BarWidget.qml ........ one icon in the bar, which kind is playing      198
    ├── MusicPanel.qml ... the card: the whole UI is one list of rows     3009
    └── CardService.qml .. the only thing that speaks to the backend       783
        │
        ├── card.py watch ......... ONE long-lived process
        │   │                       blocks on MPD's `idle`, prints on every change
        │   ├── thread: headphones unplugged → pause (blocks on pactl)
        │   ├── thread: where playback got to (one 10 s tick, books and long files)
        │   └── thread: the YouTube relay, asleep until MPD asks
        │
        ├── card.py <command> ..... one short process per action (~50 ms)
        │                           85 commands · JSON in, JSON out
        │
        ├── beat.py ............... only while the dancer is on
        │                           reads MPD's FIFO, prints a line per beat
        │
        ├── card.py speak-run ..... the read-aloud job, one at a time (flock)
        │   └── kokoro_say.py ..... inside its own Python 3.12 + venv
        │
        └── foot → reader.py ...... the terminal reader, its own window
```

Nothing above polls on a timer except where a timer is the only way to know,
and then only as often as the answer can change. **Idle costs nothing** is the
rule; this is what it means in each place (all figures measured 2026-09-27 on
a sixteen-core machine):

| what | when it is quick | when it is idle |
|---|---|---|
| the card asking what is running | 2 s while a job runs | 10 s, and the job files are watched, so work started anywhere shows at once |
| the card re-reading the download and import notes | 1 s while a job runs | not at all |
| the reader's window | 250 ms while this book plays | 1 s (a key still wakes it instantly) |
| the watcher's note of where playback got to | one 10 s tick, connecting only when something plays | no connection |
| headphones, MPD state, the relay | the moment the system says so | nothing |
| the voice | two cores by choice (Settings → Read aloud) | nothing |

What this changed, in the same scenarios measured before and after:

| scenario | before | after |
|---|---|---|
| idle, card shut | 0 starts/min, reader 1.1% of a core | 0 starts/min, reader **0.13%** |
| card open, nothing running | 24 starts/min | **5 starts/min** |
| making a chapter | **733%** of a core in all (the voice at 1229%) | **197%**, a quarter of the total work |
| the watcher | 5 threads, two tickers | 4 threads, one ticker (5 since 3.14.0: the music-folder watch, which blocks) |

More cores do not even make the voice faster: sixteen took 192 s of cpu to
make 48 s of speech at 3.1x real time, four took 47 s at 3.5x, and two took
46 s at 1.9x. Only staying ahead of the listening matters, because a chapter
starts playing while it is still being made.

---

## 2. Who owns what

```
scripts/
├── card.py ........ 2189   the protocol, and the music itself
│   ├── Mpd, status_view ..... talking to MPD; one shape for "what is playing"
│   ├── load / save / state .. every json file; each writer its own temp file
│   ├── check_arg, fail, out .. the command contract
│   └── 43 commands .......... play, queue, folders, search, radio, YouTube, art
│
├── extras.py ...... 1388   second tier, and the whole command table
│   ├── 25 commands .......... settings, lyrics, inbox, stars, setup check
│   └── merges in books.py's and speak.py's commands
│
├── books.py ........ 525   audiobooks (5 commands)
│   └── what counts as a book · chapters · playing · what was heard
│
├── booktext.py ..... 685   the words, for reading along
│   └── .lrc/.srt/.vtt/.txt/tags/EPUB → paragraphs → sentences → [word, time]
│
├── library.py ...... 357   one record per book
│   └── known by CONTENT, not by path: a-… for audio, t-… for text
│       so moving a book keeps the place
│
├── reader.py ...... 1869   the terminal reader (curses) — its own app
│   └── draws through one put(y, x, text, style), so it renders headless
│       for tests: reader.py --render 78x20 --keys "C 5 8" BOOK
│
├── speak.py ........ 794   reading a novel aloud (12 commands)
│   └── the queue · making one chapter · playing it as the novel's own audio
│
├── voices.py ....... 503   the connector to any speech engine
│   └── kokoro · piper · espeak · edge · his own command · a test tone
│       an engine is a description: ready, voices, speak, batch, ext, words
│
├── kokoro_say.py .... 83   runs inside the private venv, reports word times
└── beat.py .......... 74   beats from MPD's audio, no numpy, no cava

tests/security_test.py  3084   189 tests — real MPDs on a spare port,
                               throwaway HOMEs, headless reader renders
```

---

## 3. Where things are kept

```
~/.config/omarchy/plugins/rushi.songbook/   the plugin (a git clone of its own)

~/.local/state/rushi.songbook/              what it remembers
├── settings.json .......... everything he chose
├── library.json + .lock ... every book: place, chapters heard, bookmarks
├── spoken.json ............ what is being read aloud right now
├── speak-queue.json + .lock  chapters ticked, in the order ticked
├── speak.lock ............. held by the job; the truth about "is it working"
├── speak-rate.json ........ how fast this machine really speaks
├── stars.json + favorites.lock   starred songs
├── last.json, now.json .... what played last, and what kind it is
└── positions.json, radio-queue.json, titles.json, download.json, …

~/.cache/rushi.songbook/
├── art/ ................... cover art
└── text/ .................. parsed books, keyed by file stamp AND parse version

~/.local/share/rushi.songbook/
├── python/ ................ a self-contained CPython 3.12 (Kokoro needs ≤3.12)
└── voices/ ................ the Kokoro venv, about 1.5 GB, deletable

~/Music/
├── <his music>
├── <book folders he chose in Settings>
└── Spoken/<book>/NNN title.mp3 + .lrc + .spoken.json   what the card read aloud
```

Three rules hold this together:

1. **A book is known by what it is, not where it is.** Progress survives moving
   or renaming the file.
2. **A file written by more than one process is written under a lock**, and
   every writer uses its own temporary file.
3. **The lock says what is happening, not the status file.** A job that was
   killed leaves its progress behind saying "making"; only the lock is true.

---

## 4. The paths that cross the most parts

**Pressing play in the card**

```
MusicPanel row click → CardService.act(["play-folder", name])
  → python3 card.py play-folder <name>      → MPD: clear, add…, play
  → card.py watch wakes on MPD's idle       → prints the new status
  → CardService.home/status change          → the card and the bar redraw
```

**Opening a book**

```
card row → books.cmd_book(folder)
  → library.audio_id(files)   (a hash of the file names and lengths)
  → library.get(id)           where he was, which chapters were heard
  → books.chapters(files)     one file per chapter, or inside one .m4b
  → the card lists them; the reader can open the same book full screen
```

**Ticking chapters, then reading aloud**

```
card: tick ☑ ☑          (nothing starts)
card: "Read aloud"      → speak.cmd_speak(book, "57,58")
                          → queue_add → start_job (only if none is running)
card.py speak-run        → takes one chapter at a time
  → booktext.open_book → sentences → voices.speak_many (batches of 20)
  → ffmpeg concat → NNN title.mp3  +  .lrc of word times
  → notify-send "Ready to read aloud"
Both the card and the reader read the SAME queue and the SAME lock,
so they never disagree about what is happening.
```

**Reading along while it plays**

```
MPD plays Spoken/<book>/058 ….mp3
reader.py poll() → card.status_view → which file, how far in
  → book.chapter_of_file(file)   → which chapter of the NOVEL
  → booktext.text_for(mp3)       → the .lrc beside it: [word, time]
  → bisect on the times          → the sentence being spoken lights up
```

---

## 5. What is deliberately not here

- **No database.** Small JSON files under locks; readable, repairable by hand.
- **No daemon of its own.** One watcher, started by the card, that is a plain
  Python process anybody can kill.
- **No polling loop for the UI.** MPD's `idle` is the clock.
- **No engine-specific code in the card.** A speech engine is a description in
  voices.py; adding one is a dict, not a branch in ten places.
- **The QML never parses music.** If a rule can be got wrong, it is in Python,
  where the tests are.
