#!/usr/bin/env python3
"""Print one line per beat heard in MPD's audio.

MPD already copies its output to a FIFO for visualizers (the "Visualizer
feed" in ~/.config/mpd/mpd.conf: 44100 Hz, 16-bit, stereo). This reads it,
measures the loudness of the low end in 23 ms blocks, and calls it a beat
when a block is clearly louder than the second before it. No numpy, no cava.

Only one program can read the FIFO usefully at a time: while rmpc's own
visualizer is open, the two share the audio and both see less of it.
"""

import array
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from card import FIFO as CONF_FIFO  # noqa: E402  the fifo output in mpd.conf

FIFO = sys.argv[1] if len(sys.argv) > 1 else (CONF_FIFO or "/tmp/mpd.fifo")
BLOCK = 1024                 # stereo frames per block, ~23 ms
HISTORY = 43                 # ~1 s of blocks to compare against
THRESHOLD = 1.35             # this much louder than the recent average
MIN_GAP = 0.22               # s; nothing faster than ~270 bpm


def energy(samples):
    # Mono mix of every 4th frame through a short moving average: a crude
    # low-pass, so kick drums count and hi-hats mostly do not.
    total, prev = 0, 0
    for i in range(0, len(samples) - 1, 8):
        mono = (samples[i] + samples[i + 1]) >> 1
        low = (mono + prev) >> 1
        prev = mono
        total += low * low
    return total


def main():
    # No visualizer FIFO in this MPD setup: wait quietly rather than exit,
    # so the card does not restart us every few seconds. The dancer still
    # sways on its own timer.
    while not os.path.exists(FIFO):
        time.sleep(10)
    history = []
    last_beat = 0.0
    with open(FIFO, "rb", buffering=0) as fifo:
        while True:
            data = fifo.read(BLOCK * 4)
            if not data:
                time.sleep(0.05)
                continue
            if len(data) % 2:
                data = data[:-1]
            samples = array.array("h", data)
            e = energy(samples)
            if len(history) >= HISTORY // 2:
                avg = sum(history) / len(history)
                now = time.monotonic()
                if e > avg * THRESHOLD and e > 2e6 and now - last_beat > MIN_GAP:
                    last_beat = now
                    sys.stdout.write("b\n")
                    sys.stdout.flush()
            history.append(e)
            if len(history) > HISTORY:
                history.pop(0)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
