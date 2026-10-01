#!/usr/bin/env python3
"""Say text with Kokoro, inside the card's own small Python.

Kokoro needs a 310 MB model, which does not belong in the card's own Python
(and pip refuses to install into the system one). The card makes a private
environment for it (voices.install_kokoro) and runs this file with that
environment's Python:

    kokoro_say.py <text file> <output .wav> [voice] [speed]
    kokoro_say.py --job <job file>      several at once, one model load

It prints a list of word times per item, as JSON. Through ONNX the model
reports phonemes rather than words, and the model that reports anything at
all is nearly half the speed, so nothing is claimed: an empty list means the
reader spreads the words across the sentence by length. The time each
SENTENCE starts is exact whatever happens here -- speak.py makes them one at
a time and measures each one.
"""

import json
import os
import sys


def engine(voice, threads=None):
    """The voice, told how much of the machine it may use.

    Left alone, ONNX Runtime takes every core it can see. On this machine
    (sixteen cores) making 48 seconds of speech, measured 2026-09-27:

        cores   made in    speed      cpu used
           16     15.6s     3.1x     192s (1229% of a core)
            4     13.8s     3.5x      47s  (344%)
            2     24.8s     1.9x      46s  (188%)
            1     38.2s     1.2x      39s  (102%)

    Sixteen is slower than four and does four times the work to be so. And
    speed past about 1.5x buys nothing anyone can hear: a chapter starts
    playing while it is still being made, so it only has to stay ahead of
    the listening. Two cores do that at an eighth of the load.
    """
    import os
    import onnxruntime
    if threads is None:
        threads = os.environ.get("RMPC_VOICE_THREADS") or 2
    from kokoro_onnx import Kokoro
    models = os.environ.get("RMPC_VOICE_MODELS") or ""
    if not models:
        # ../models beside the environment this is running in
        models = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "models")
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = max(1, int(threads))
    options.inter_op_num_threads = 1
    session = onnxruntime.InferenceSession(os.path.join(models, "kokoro-v1.0.onnx"),
                                           options, providers=["CPUExecutionProvider"])
    return Kokoro.from_session(session, os.path.join(models, "voices-v1.0.bin"))


def language(voice):
    """Kokoro's voices are named by language: a for American, b for British,
    h for Hindi, and so on."""
    return {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi",
            "i": "it", "j": "ja", "p": "pt-br", "z": "cmn"}.get((voice or "a")[0], "en-us")


def say(k, text, out, voice, speed):
    import soundfile
    samples, rate = k.create(text, voice=voice, speed=float(speed or 1.0),
                             lang=language(voice))
    if samples is None or len(samples) == 0:
        raise SystemExit("Kokoro said nothing for: " + text[:60])
    soundfile.write(out, samples, rate)
    return []


def many(job_file):
    """A whole batch in one go: the model is loaded once."""
    with open(job_file) as f:
        job = json.load(f)
    voice = job.get("voice") or "af_heart"
    speed = float(job.get("speed") or 1.0)
    k = engine(voice, job.get("threads"))
    print(json.dumps([say(k, item["text"], item["out"], voice, speed)
                      for item in job["items"]]))


def main(argv):
    if argv and argv[0] == "--job":
        return many(argv[1])
    text_file, out = argv[0], argv[1]
    voice = argv[2] if len(argv) > 2 and argv[2] else "af_heart"
    speed = argv[3] if len(argv) > 3 and argv[3] else 1.0
    with open(text_file) as f:
        text = f.read()
    print(json.dumps(say(engine(voice), text, out, voice, speed)))


if __name__ == "__main__":
    main(sys.argv[1:])
