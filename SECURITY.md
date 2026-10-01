# Reporting a security problem

Songbook plays what other people publish — radio stations anyone can add to a
public directory, YouTube, lyrics servers, a dictionary, and books that arrive
from anywhere. Most of its risk is in reading things it did not write.

**Report anything you find by opening an issue**, or privately through GitHub's
[security advisories](https://github.com/RushiSompuraWork/Songbook/security/advisories/new)
if you would rather it were not public first. Either is welcome. This is one
person's project, so there is no guaranteed response time, but reports are
taken seriously and answered.

A report does not need to be a working exploit. The two problems found before
the first release were a source trace and a one-line observation, and both were
real.

## Credit

Anyone who reports a problem is credited by name in `CREDITS.md`, with their
permission and however they would like to be named. Say so in the report if you
would rather not be named.

## What it already does

Written down so a reporter can skip what is covered and look at what is not.

- **No third-party code.** Python standard library only. The one exception is
  `scripts/kokoro_say.py`, which runs inside an interpreter the optional voice
  installer places in its own folder; a normal install runs none of it.
- **No `sudo`, ever.** The `pacman` lines in the README are instructions for
  the reader, not commands the plugin runs.
- **Addresses are checked once and connected to by number.** A name is resolved
  once, every address it gives must be public, and the connection is made to an
  address that was checked, while the certificate is still checked against the
  name. Every redirect hop is checked the same way.
- **MPD is never handed an address from outside.** Stations and YouTube are
  fetched by a relay on 127.0.0.1, so nothing MPD fetches can be redirected
  somewhere private. Playlists are rewritten so their contents come back
  through the relay too.
- **Web answers are bounded** (8 MB) and every request has a timeout.
- **Books are read with limits** on each file, the whole book, the number of
  documents, and what indexing may generate.
- **Nothing drawn can drive the terminal.** Control characters and bidi
  overrides are stripped where the screen is written, so a book's text — or its
  file name — cannot retitle a window or move the cursor.
- **What you read is yours.** The state folder is `0700` and its files `0600`.
- **The voice installer verifies what it downloads** against the publisher's
  published `SHA256SUMS`, and refuses to install when there are none.

## What is not covered

- Approval on the Omarchy plugin marketplace is a **listing**, not a security
  review, and plugins run unsandboxed. Theirs is not an endorsement and neither
  is this file.
- MPD, yt-dlp, ffmpeg and the voice engines are other people's programs.
  Problems in them belong upstream.
- The plugin trusts the machine it runs on and the person running it.
