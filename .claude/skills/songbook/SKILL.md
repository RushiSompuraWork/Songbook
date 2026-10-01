---
name: songbook
description: >
  REQUIRED before changing, debugging or explaining Songbook, the Omarchy music
  and books card (~/.config/omarchy/plugins/rushi.songbook, formerly
  rushi.rmpc). Use when asked about: the music card, the bar widget, the card's
  rows or screens, MPD playback through it, radio stations, YouTube search or
  download in it, lyrics, audiobooks, novels, chapters, reading a chapter
  aloud, TTS or Kokoro voices, the terminal book reader, `omarchy-shell
  songbook …`, or anything under that plugin's scripts/ or its .qml files.
  Triggers: songbook, music card, rushi.songbook, rushi.rmpc, read aloud,
  spoken chapter, book reader, reader.py, card.py, MusicPanel, CardService,
  voiceCores, spokenAhead. Excludes `rmpc` the third-party terminal MPD
  client, which the card only launches.
---

# Songbook

His Omarchy (Quickshell) bar widget: music through MPD, world radio, YouTube,
lyrics, audiobooks, novels read aloud by an offline voice, and a terminal
reader. About 21,000 lines, 312 tests, version in `manifest.json`.

```
~/.config/omarchy/plugins/rushi.songbook
├── AGENTS.md    ← READ THIS FIRST. The working guide: the file map, how the
│                  parts fit, the safety rules, the traps, how to verify.
├── DESIGN.md      The shape of it as a tree, and what each timer costs.
├── REVIEW.md      Every known problem, what was fixed and what was decided
│                  against — check here before proposing a direction.
└── README.md      The user-facing side.
```

**Read `AGENTS.md` before editing anything in the plugin.** It is 250 lines
and it exists because each line of it cost something to learn.

## The three things to get right

1. **Verify against the real card, always.** After any QML change: restart the
   shell, open all 36 screens, and fail on an empty answer, `rows=0` or `w=0`
   — not just on something in the log. Whole screens have drawn nothing while
   the log stayed empty, and once the entire card became unclickable.

   ```sh
   omarchy restart shell
   for v in $(omarchy-shell songbook views); do
     omarchy-shell songbook view "$v"; omarchy-shell songbook probe
   done
   omarchy-shell songbook titles          # what the rows say, not just how many
   omarchy-shell songbook press <row key> # what a click really runs; `view` skips it
   python3 tests/security_test.py         # all 312, after every change
   journalctl --user --since "2 min ago" | grep -i "songbook\|QML"
   ```

2. **Measure; do not guess.** Every number in those files was measured on this
   machine, and most contradicted the first guess: sixteen cores made speech
   *slower* than four, a book's cache was 180 MB to show 224 words, four
   signed-in yt-dlp runs took 28 s each because they serialise on the browser's
   cookies. If you did not see it, do not claim it.

3. **Idle costs nothing.** Timers are paced by what can actually change, and a
   watched file beats a poll. Before adding any timer, find the event instead.

## Do not

- Rename the `rmpc` references that mean the **other** program (a terminal MPD
  client the card can launch): `BAR_ACTIONS`, the bar action, some comments.
- Add a voice engine's dependency to the card — it must work with none
  installed.
- Let QML fetch anything from the network, or render untrusted text as rich
  text. Both have crashed the whole shell before.
- Write his real settings, or leave his MPD queue or playback changed, in a
  test.
- Touch `id` in the manifest: it is permanent once published.

## His way of working

Phases: the questions only he can answer first, in plain words; then what you
decide — design it, look again from another angle, revise, then build; then
small fixes. Interface text is plain language, never jargon. Comments say
*why*, with the date and the measurement. Nothing gets added that he did not
ask for, and nothing he asked for is quietly dropped.
