"""The words of what is playing, to read along: an audiobook's text, or a
song's lyrics kept beside the file.

Where the text can come from (looked up 2026-09-22; the Lyrics view shows
whichever is found first):
  - a file beside the audio with the same name: .lrc (also "enhanced" LRC
    with a time on every word), .srt, .vtt (also with word times), or .txt
    (plain text, nothing to follow). "chapter01.en.srt" counts too;
  - lyrics stored in the audio file's own tags;
  - an EPUB 3 with Media Overlays in the same folder: the read-along
    standard (Storyteller, syncabook), where each sentence of the book
    carries the time it is spoken in which audio file.

Whatever the source, it comes out the same way: paragraphs of sentences of
words, each word with the second it starts (-1 when nothing says). The card
highlights a sentence, a word or a paragraph from that (Settings → Lyrics).
A source without word times gets them spread over its line by length.

These files come from anywhere, so each is read with a size limit, an EPUB
is read inside the zip (nothing is unpacked to disk), and the text is only
ever shown as plain text.
"""

import hashlib
import html
import json
import os
import posixpath
import re
import subprocess
import zipfile
import xml.etree.ElementTree as ET

import card

MAX_FILE = 8 << 20          # a sidecar or one file inside an EPUB
# ... and a ceiling for the whole book, because a zip is compressed: a 0.28 MB
# EPUB of 200 repetitive documents took 50 s, 1.9 GB of memory and wrote a
# 172 MB cache file, all within the per-file limit (measured 2026-09-24). His
# real 535-chapter novel reads about 10 MB, so this leaves room to spare.
# Measured on his own books (2026-09-24): the largest, a 535-chapter novel,
# reads 6.0 MB across 548 files and comes to 813,000 words. These leave three
# to five times that.
MAX_TOTAL = 24 << 20        # everything read out of one EPUB
MAX_DOCS = 3000             # documents read out of one EPUB
MAX_BOOK_WORDS = 3000000    # words kept from one book -- what actually grows
MAX_WORDS = 60000           # about a long chapter; the rest is not sent
# What the index of one read-along document may MAKE, which is a different
# thing from what it reads. Every id whose text is kept holds a copy of all the
# text below it, so ids nested inside one another multiply: 400 nested ids
# around 200 KB held 82 MB, measured 2026-10-01, from a file of 0.2 MB. The
# byte budgets above count what comes in and never saw it (found by a
# marketplace reviewer, reading the source).
MAX_INDEX = 8 << 20         # characters made while indexing one document
SIDE = (".lrc", ".srt", ".vtt", ".txt")
CACHE_DIR = os.path.expanduser("~/.cache/rushi.songbook/text")
# Bumped whenever the parsing changes, so a saved answer from the older
# rules is not served instead (joining punctuation back on, 2026-09-23).
PARSE = 3

# ---------------------------------------------------------------- times

def clock(value):
    """'1:02:03.5', '02:03,5', '63.5', '63.5s', '1500ms', 'npt=63.5' -> seconds."""
    v = str(value or "").strip().replace(",", ".")
    if v.startswith("npt="):
        v = v[4:]
    try:
        if v.endswith("ms"):
            return float(v[:-2]) / 1000
        if v.endswith("s") and ":" not in v:
            return float(v[:-1])
        if v.endswith("min"):
            return float(v[:-3]) * 60
        if v.endswith("h") and ":" not in v:
            return float(v[:-1]) * 3600
        parts = [float(p) for p in v.split(":")]
    except ValueError:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def read_text(path):
    with open(path, "rb") as f:
        data = f.read(MAX_FILE)
    return clean(decode(data))


def decode(data):
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


TAG = re.compile(r"<[^>]*>")


CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def clean(text):
    """No control characters: a book is shown in a terminal, where an escape
    sequence hidden in its text could move the cursor or retitle the window.
    Bidirectional overrides go too (they can make text read other than it is)."""
    return CONTROL.sub("", text)


def plain(text):
    return " ".join(clean(html.unescape(TAG.sub(" ", text))).split())


# ---------------------------------------------------------------- cues
#
# A cue: {"t": start or None, "e": end or None, "text": str,
#         "words": [(word, t), ...] or None, "brk": a paragraph starts here}

LRC_TIME = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")
LRC_META = re.compile(r"^\[([a-z#]+):(.*)\]$", re.I)
LRC_WORD = re.compile(r"<(\d+):(\d+(?:[.:]\d+)?)>")


def lrc_seconds(mm, ss):
    return int(mm) * 60 + float(ss.replace(":", "."))


def parse_lrc(text):
    cues, offset, brk = [], 0.0, False
    for raw in text.splitlines():
        line = raw.strip()
        meta = LRC_META.match(line)
        if meta and not LRC_TIME.match(line):
            if meta.group(1).lower() == "offset":
                try:
                    offset = float(meta.group(2)) / 1000   # ms; positive = earlier
                except ValueError:
                    pass
            continue
        times = []
        while True:
            m = LRC_TIME.match(line)
            if not m:
                break
            times.append(lrc_seconds(m.group(1), m.group(2)) - offset)
            line = line[m.end():]
        if not times:
            continue
        words = None
        if LRC_WORD.search(line):
            words = []
            parts = LRC_WORD.split(line)          # text, mm, ss, text, ...
            lead = parts[0].strip()
            for i in range(1, len(parts) - 1, 3):
                t = lrc_seconds(parts[i], parts[i + 1]) - offset
                for w in parts[i + 2].split():
                    words.append((w, t))
            if lead:
                words = [(w, times[0]) for w in lead.split()] + words
            line = LRC_WORD.sub("", line)
        text_ = plain(line)
        if not text_:
            brk = True                            # an empty timed line: a pause
            continue
        for t in times:
            cues.append({"t": t, "e": None, "text": text_, "words": words, "brk": brk})
            brk = False
    cues.sort(key=lambda c: c["t"])
    return cues


SUB_TIME = re.compile(r"((?:\d+:)?\d+:\d+(?:[.,]\d+)?)\s*-->\s*((?:\d+:)?\d+:\d+(?:[.,]\d+)?)")
VTT_WORD = re.compile(r"<((?:\d+:)?\d+:\d+\.\d+)>")


def parse_subs(text):
    """SRT and VTT: blocks with a 'start --> end' line, then the words."""
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            m = SUB_TIME.search(line)
            if not m:
                continue
            t, e = clock(m.group(1)), clock(m.group(2))
            body = "\n".join(lines[i + 1:])
            words = None
            if VTT_WORD.search(body):
                words = []
                parts = VTT_WORD.split(body)
                for w in plain(parts[0]).split():
                    words.append((w, t))
                for j in range(1, len(parts) - 1, 2):
                    wt = clock(parts[j])
                    for w in plain(parts[j + 1]).split():
                        words.append((w, wt))
            text_ = plain(body)
            if text_ and t is not None:
                cues.append({"t": t, "e": e, "text": text_, "words": words, "brk": False})
            break
    cues.sort(key=lambda c: c["t"])
    return cues


def parse_txt(text):
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    return [{"t": None, "e": None, "text": p, "words": None, "brk": True} for p in paras if p]


def parse_any(name, text):
    ext = os.path.splitext(name.lower())[1]
    if ext == ".lrc" or (ext not in (".srt", ".vtt", ".txt") and LRC_TIME.search(text)):
        return parse_lrc(text)
    if ext in (".srt", ".vtt") or SUB_TIME.search(text):
        return parse_subs(text)
    return parse_txt(text)


# ---------------------------------------------------------------- EPUB 3 Media Overlays

OPF = "{http://www.idpf.org/2007/opf}"
SMIL = "{http://www.w3.org/ns/SMIL}"
XHTML = "{http://www.w3.org/1999/xhtml}"
BLOCKS = {"p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "section",
          "td", "dd", "figcaption"}


class Budget:
    """What is left to read out of one EPUB. A book is many small files, so
    the limit that matters is the total, not any one of them."""

    def __init__(self, total=MAX_TOTAL, docs=MAX_DOCS):
        self.bytes_left, self.docs_left = total, docs
        self.spent = False

    def take(self, size):
        self.bytes_left -= size
        self.docs_left -= 1
        if self.bytes_left < 0 or self.docs_left < 0:
            self.spent = True
            raise Spent("this book is larger than the card will read at once")


class Spent(Exception):
    """The budget for one EPUB ran out: keep what was read, stop there."""


def zip_read(z, name, budget=None):
    info = z.getinfo(name)
    if info.file_size > MAX_FILE:
        raise ValueError("too large inside the EPUB: " + name)
    if budget is not None:
        budget.take(info.file_size)
    return z.read(name)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_epub(epub, audio_name):
    """Cues for one audio file (by its name) from an EPUB's Media Overlays."""
    budget = Budget()
    with zipfile.ZipFile(epub) as z:
        container = ET.fromstring(zip_read(z, "META-INF/container.xml", budget))
        root_file = next((el.get("full-path") for el in container.iter()
                          if local(el.tag) == "rootfile"), None)
        if not root_file:
            return []
        opf = ET.fromstring(zip_read(z, root_file, budget))
        base = posixpath.dirname(root_file)
        items = {}
        for el in opf.iter(OPF + "item"):
            items[el.get("id")] = el
        spine = [el.get("idref") for el in opf.iter(OPF + "itemref")]
        smils = []
        for idref in spine:
            item = items.get(idref)
            if item is not None and item.get("media-overlay") in items:
                smils.append(items[item.get("media-overlay")].get("href"))
        docs, cues = {}, []
        want = os.path.splitext(os.path.basename(audio_name))[0].lower()
        for href in smils:
            if budget.spent:
                break                     # keep what was read; do not eat the machine
            smil_path = posixpath.normpath(posixpath.join(base, href))
            try:
                smil = ET.fromstring(zip_read(z, smil_path, budget))
            except Spent:
                break
            smil_dir = posixpath.dirname(smil_path)
            # Which fragments this audio file actually asks for, before any
            # document is indexed: the index then makes only those.
            need = {}
            for par in smil.iter(SMIL + "par"):
                t_el, a_el = par.find(SMIL + "text"), par.find(SMIL + "audio")
                if t_el is None or a_el is None:
                    continue
                name = posixpath.basename(a_el.get("src", "").split("#")[0])
                if os.path.splitext(name)[0].lower() != want:
                    continue
                href, _, piece = t_el.get("src", "").partition("#")
                if piece:
                    need.setdefault(
                        posixpath.normpath(posixpath.join(smil_dir, href)), set()).add(piece)
            for par in smil.iter(SMIL + "par"):
                text_el = par.find(SMIL + "text")
                audio_el = par.find(SMIL + "audio")
                if text_el is None or audio_el is None:
                    continue
                src = posixpath.basename(audio_el.get("src", "").split("#")[0])
                if os.path.splitext(src)[0].lower() != want:
                    continue
                doc_href, _, frag = text_el.get("src", "").partition("#")
                doc_path = posixpath.normpath(posixpath.join(smil_dir, doc_href))
                if doc_path not in docs:
                    try:
                        docs[doc_path] = index_doc(zip_read(z, doc_path, budget),
                                                   need.get(doc_path))
                    except Spent:
                        break
                found = docs[doc_path].get(frag)
                if not found:
                    continue
                text_, block = found
                cues.append({"t": clock(audio_el.get("clipBegin", "0")),
                             "e": clock(audio_el.get("clipEnd", "")),
                             "text": text_, "words": None, "block": (doc_path, block)})
        cues.sort(key=lambda c: c["t"] or 0)
        # A new paragraph wherever the enclosing block (a <p>, a heading) changes.
        prev = None
        for c in cues:
            block = c.pop("block")
            c["brk"] = block != prev
            prev = block
        return cues


def index_doc(data, wanted=None):
    """id -> (its text, the id of the paragraph-like block it sits in).

    `wanted` is the set of ids the read-along actually asks for. Without it
    every id in the document was given a copy of all the text beneath it,
    whether anything ever looked at it or not -- which is most of the cost and
    all of the amplification. What is made is capped as well, because one
    wanted id can still sit inside another.
    """
    tree = ET.fromstring(data)
    parent = {child: el for el in tree.iter() for child in el}
    found = {}
    made = 0
    for el in tree.iter():
        eid = el.get("id")
        if not eid or (wanted is not None and eid not in wanted):
            continue
        text_ = " ".join("".join(el.itertext()).split())
        if not text_:
            continue
        made += len(text_)
        if made > MAX_INDEX:
            # Keep what is here and stop: a read-along that goes no further is
            # better than one that takes the machine with it.
            break
        up = el
        while up is not None and local(up.tag) not in BLOCKS:
            up = parent.get(up)
        block = up if up is not None else el
        found[eid] = (text_, id(block))
    return found


def epub_in(folder_abs):
    try:
        names = sorted(os.listdir(folder_abs))
    except OSError:
        return []
    return [os.path.join(folder_abs, n) for n in names if n.lower().endswith(".epub")]


# ---------------------------------------------------------------- tags

def tag_lyrics(full):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags",
                            "-of", "json", "--", full], capture_output=True, text=True, timeout=20)
        tags = json.loads(r.stdout or "{}").get("format", {}).get("tags", {})
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""
    for key, value in tags.items():
        k = key.lower()
        if k.startswith("lyrics") or k in ("unsyncedlyrics", "uslt", "©lyr"):
            return str(value)[:MAX_FILE]
    return ""


# ---------------------------------------------------------------- words

ABBREV = {"mr.", "mrs.", "ms.", "dr.", "st.", "jr.", "sr.", "vs.", "etc.", "e.g.", "i.e.",
          "mt.", "no.", "prof.", "capt.", "col.", "gen.", "lt.", "sgt."}
END = re.compile(r"[.!?…。！？]+[\"'”’»)\]]*$")


def spread(text, t, e):
    """Word times across a line by length, when the source gives only the line's."""
    words = text.split()
    if t is None:
        return [(w, -1) for w in words]
    if e is None or e <= t:
        e = t + 0.35 * max(1, len(words))
    total = sum(len(w) + 1 for w in words) or 1
    out, done = [], 0
    for w in words:
        out.append((w, round(t + (e - t) * done / total, 2)))
        done += len(w) + 1
    return out


# Punctuation a voice engine reports as a word of its own. Kokoro times every
# token, so "He was tired ." came through with a space before the full stop
# (seen 2026-09-23). It is joined back onto the word it belongs to, keeping
# that word's time, so the text reads as it was written.
TAIL = set(".,;:!?)]}»”’…%") | {"''", "'s", "n't", "'re", "'ve", "'ll", "'d", "'m"}
HEAD = set("([{«“‘$#@")


def join_marks(words):
    """[(word, t)] with stray punctuation joined to its neighbour."""
    out = []
    glue_next = None
    for w, t in words:
        if glue_next is not None:
            out.append((glue_next + w, t))
            glue_next = None
            continue
        if w in HEAD:
            glue_next = w
            continue
        mark = w in TAIL or (len(w) > 1 and all(c in TAIL for c in w))
        if mark:
            if out:
                out[-1] = (out[-1][0] + w, out[-1][1])
            else:
                glue_next = w       # a quote opening a paragraph: onto the next word
            continue
        out.append((w, t))
    if glue_next is not None:
        out.append((glue_next, None))
    return out


def build(cues, start=None, end=None):
    """Cues -> paragraphs of sentences of [word, t]."""
    for i, c in enumerate(cues):
        if c["e"] is None and c["t"] is not None:
            nxt = next((d["t"] for d in cues[i + 1:] if d["t"] is not None and d["t"] > c["t"]), None)
            c["e"] = nxt
    stream = []                  # (word, t, paragraph starts here)
    last_end = None
    for c in cues:
        words = join_marks(c["words"]) if c["words"] \
            else spread(c["text"], c["t"], c["e"])
        # A pause of two seconds or more between lines reads as a new paragraph.
        gap = c["t"] is not None and last_end is not None and c["t"] - last_end >= 2.0
        for n, (w, t) in enumerate(words):
            stream.append((w, t, n == 0 and (c["brk"] or gap)))
        if c["e"] is not None:
            last_end = c["e"]
    if start is not None:
        stream = [s for s in stream if s[1] < 0 or (start - 0.5 <= s[1] < (end or 1e12))]
    stream = stream[:MAX_WORDS]
    paras, sentence, para = [], [], []
    for w, t, brk in stream:
        if brk and (sentence or para):
            if sentence:
                para.append(sentence)
            paras.append(para)
            sentence, para = [], []
        sentence.append([w, t])
        if END.search(w) and w.lower() not in ABBREV:
            para.append(sentence)
            sentence = []
    if sentence:
        para.append(sentence)
    if para:
        paras.append(para)
    # Sources without paragraphs (one long run of lines): about six sentences each.
    if len(paras) == 1 and len(paras[0]) > 8:
        run = paras[0]
        paras = [run[i:i + 6] for i in range(0, len(run), 6)]
    return paras


# ---------------------------------------------------------------- finding it

def sidecars(full):
    folder, name = os.path.split(full)
    stem = os.path.splitext(name)[0].lower()
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    found = []
    for n in names:
        low = n.lower()
        root, ext = os.path.splitext(low)
        if ext in SIDE and (root == stem or root.rsplit(".", 1)[0] == stem):
            found.append((SIDE.index(ext), os.path.join(folder, n)))
    return [p for _, p in sorted(found)]


def stamp(path):
    try:
        st = os.stat(path)
        return "%d:%d" % (st.st_mtime, st.st_size)
    except OSError:
        return ""


def text_for(rel, start=None, end=None):
    """{"source", "synced", "paras"} for a file in the music folder, or None."""
    full = os.path.join(card.MUSIC_DIR, rel)
    if not os.path.isfile(full):
        return None
    candidates = [("file", p) for p in sidecars(full)]
    candidates += [("epub", p) for p in epub_in(os.path.dirname(full))]
    candidates.append(("tag", full))
    for kind, path in candidates:
        key = hashlib.sha1(("%s|%s|%s|%s|%s|%d" % (kind, path, stamp(path), rel, start, PARSE))
                           .encode()).hexdigest()[:20]
        cached = os.path.join(CACHE_DIR, key + ".json")
        try:
            with open(cached) as f:
                hit = json.load(f)
            if hit:
                return hit
            continue                              # known: nothing in this one
        except (OSError, ValueError):
            pass
        try:
            if kind == "file":
                cues = parse_any(path, read_text(path))
            elif kind == "epub":
                cues = parse_epub(path, rel)
            else:
                raw = tag_lyrics(full)
                cues = parse_any("tag", raw) if raw.strip() else []
        except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile):
            cues = []
        answer = None
        if cues:
            paras = build(cues, start, end)
            if paras:
                answer = {"source": os.path.splitext(path)[1].lstrip(".").lower() if kind != "tag"
                          else "tag", "synced": any(c["t"] is not None for c in cues),
                          "paras": paras}
        card.write_file(cached, json.dumps(answer, ensure_ascii=False))
        prune_cache()
        if answer:
            return answer
    return None


# ---------------------------------------------------------------- a whole book, to read
#
# The reader (reader.py) opens a book as chapters of paragraphs of
# sentences of words, the same shape as above, only without times: an
# EPUB by its spine (one file, one chapter; titles from its contents), a
# .txt by its "Chapter …" lines. Everything is read inside the zip, with
# the same size limits, and only ever shown as plain text.

from html.parser import HTMLParser

HEADING = re.compile(r"^\s*(chapter|ch\.|part|book|prologue|epilogue|interlude|volume|"
                     r"side story|afterword|foreword)\b.{0,80}$", re.I)
BLOCK_TAGS = BLOCKS | {"br", "tr", "hr", "article", "aside", "header", "footer", "pre"}
SKIP_TAGS = {"script", "style", "head", "title", "svg", "math", "nav"}


class Blocks(HTMLParser):
    """HTML to paragraphs: text inside block tags, headings kept apart."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras, self.buf, self.skip, self.heads = [], [], 0, []
        self.in_head = False

    def flush(self):
        text = " ".join(clean("".join(self.buf)).split())
        if text:
            self.paras.append(text)
            if self.in_head:
                self.heads.append(text)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in BLOCK_TAGS:
            self.flush()
            self.in_head = tag in ("h1", "h2", "h3")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK_TAGS:
            self.flush()
            self.in_head = False

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


def html_paras(data):
    parser = Blocks()
    parser.feed(decode(data))
    parser.flush()
    return parser.paras, parser.heads


def to_sentences(paras):
    """Plain paragraphs -> [[sentence of [word, -1]]] per paragraph."""
    cues = [{"t": None, "e": None, "text": p, "words": None, "brk": True} for p in paras if p]
    out = []
    for para in build(cues) if cues else []:
        out.append(para)
    return out


def toc_titles(z, opf_dir, opf, budget=None):
    """href (without #) -> title, from the EPUB 3 nav or the EPUB 2 toc.ncx."""
    titles = {}
    for item in opf.iter(OPF + "item"):
        href = posixpath.normpath(posixpath.join(opf_dir, item.get("href", "")))
        props = item.get("properties") or ""
        try:
            if "nav" in props.split():
                root = ET.fromstring(zip_read(z, href, budget))
                for a in root.iter():
                    if local(a.tag) == "a" and a.get("href"):
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(href),
                                                                   a.get("href").split("#")[0]))
                        titles.setdefault(target, " ".join("".join(a.itertext()).split()))
            elif item.get("media-type") == "application/x-dtbncx+xml":
                root = ET.fromstring(zip_read(z, href, budget))
                for point in root.iter():
                    if local(point.tag) != "navPoint":
                        continue
                    label = next((el for el in point.iter() if local(el.tag) == "text"), None)
                    content = next((el for el in point.iter() if local(el.tag) == "content"), None)
                    if label is not None and content is not None:
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(href),
                                                                   content.get("src", "").split("#")[0]))
                        titles.setdefault(target, " ".join((label.text or "").split()))
        except Spent:
            break                  # the chapters matter more than their names
        except (KeyError, ValueError, ET.ParseError):
            continue
    return titles


def epub_book(path):
    """{"title", "chapters": [{"title", "paras"}]} from an EPUB."""
    budget = Budget()
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(zip_read(z, "META-INF/container.xml", budget))
        root_file = next((el.get("full-path") for el in container.iter()
                          if local(el.tag) == "rootfile"), None)
        opf = ET.fromstring(zip_read(z, root_file, budget))
        opf_dir = posixpath.dirname(root_file)
        title = next((" ".join("".join(el.itertext()).split()) for el in opf.iter()
                      if local(el.tag) == "title"), "") or os.path.splitext(os.path.basename(path))[0]
        items = {el.get("id"): el for el in opf.iter(OPF + "item")}
        titles = toc_titles(z, opf_dir, opf, budget)
        chapters, kept = [], 0
        for ref in opf.iter(OPF + "itemref"):
            item = items.get(ref.get("idref"))
            if item is None or "html" not in (item.get("media-type") or "html"):
                continue
            if budget.spent:
                break             # a book too big to read whole: keep what there is
            href = posixpath.normpath(posixpath.join(opf_dir, item.get("href", "")))
            try:
                paras, heads = html_paras(zip_read(z, href, budget))
            except Spent:
                break
            except (KeyError, ValueError):
                continue
            words = sum(len(p.split()) for p in paras)
            if words < 3:
                continue                      # a cover, a blank page
            kept += words
            if kept > MAX_BOOK_WORDS and chapters:
                # The words are what grows: read bytes become sentences, and
                # sentences become a cache file many times their size. One
                # chapter is always kept, however long it is -- an empty book
                # would be worse than a shortened one.
                budget.spent = True
                break
            name = titles.get(href) or (heads[0] if heads else "") or "Section %d" % (len(chapters) + 1)
            chapters.append({"title": name[:120], "paras": to_sentences(paras)})
            if kept > MAX_BOOK_WORDS:
                budget.spent = True
                break
    return {"title": title[:160], "chapters": chapters, "partial": budget.spent}


def txt_book(path):
    """A .txt: chapters at "Chapter …" lines, or parts of about 3000 words."""
    text = read_text(path)
    blocks = [" ".join(b.split()) for b in re.split(r"\n\s*\n", text)]
    blocks = [b for b in blocks if b]
    chapters, cur = [], {"title": "", "paras": []}
    for b in blocks:
        if HEADING.match(b) and len(b) < 90:
            if cur["paras"]:
                chapters.append(cur)
            cur = {"title": b, "paras": []}
        else:
            cur["paras"].append(b)
    if cur["paras"]:
        chapters.append(cur)
    if len(chapters) <= 1:                   # no headings: parts by length
        paras = chapters[0]["paras"] if chapters else []
        chapters, part, count = [], [], 0
        for p in paras:
            part.append(p)
            count += len(p.split())
            if count >= 3000:
                chapters.append({"title": "Part %d" % (len(chapters) + 1), "paras": part})
                part, count = [], 0
        if part:
            chapters.append({"title": "Part %d" % (len(chapters) + 1), "paras": part})
    for i, ch in enumerate(chapters):
        ch["title"] = ch["title"] or "Part %d" % (i + 1)
        ch["paras"] = to_sentences(ch["paras"])
    return {"title": os.path.splitext(os.path.basename(path))[0], "chapters": chapters}


CACHE_BYTES = 400 << 20     # everything parsed, together


def prune_cache(keep=400, most=CACHE_BYTES):
    """The newest parsed books and texts stay, by number and by size.

    Counting files alone was not enough: one book's parsed text can be tens
    of megabytes, so 400 of them is measured in gigabytes (2026-09-24). His
    535-chapter novel comes to 12 MB, so this holds about thirty like it."""
    try:
        names = [os.path.join(CACHE_DIR, n) for n in os.listdir(CACHE_DIR) if n.endswith(".json")]
        names.sort(key=os.path.getmtime)
    except OSError:
        return
    sizes, total = {}, 0
    for path in names:
        try:
            sizes[path] = os.path.getsize(path)
        except OSError:
            sizes[path] = 0
        total += sizes[path]
    drop = set(names[:-keep])
    for path in names:                         # oldest first, until it fits
        if total <= most:
            break
        if path not in drop:
            drop.add(path)
        total -= sizes[path]
    for path in drop:
        try:
            os.remove(path)
        except OSError:
            pass


# How a book is kept once it has been read.
#
# It used to be one file holding every word, loaded whole to show one chapter:
# his 535-chapter novel left the reader holding 180 MB to display 224 words,
# and 0.40 s of every open went on decoding 12.4 MB of it (2026-09-24).
#
# Now the same file starts with an index -- the title, and for each chapter
# its name, how many words it has, and where its text sits in the rest of the
# file -- and each chapter is one line after that. Opening a book reads the
# index. A chapter is one seek and one line. Everything that only wants to
# count words or list names (the card's chapter list, what a chapter would
# cost to speak) never touches the text at all.
#
# One file per book, not one per chapter: a 535-chapter novel would otherwise
# put 535 files in a cache that keeps the newest 400, and pruning would start
# deleting chapters of the book being read.
BOOK_V = 1


def book_cache(path):
    key = hashlib.sha1(("book|%s|%s|%d" % (path, stamp(path), PARSE)).encode()).hexdigest()[:20]
    return os.path.join(CACHE_DIR, key + ".book")


def pack_book(book):
    """The index line, then one line per chapter."""
    head = {"v": BOOK_V, "title": book["title"], "partial": bool(book.get("partial")),
            "chapters": []}
    body, at = [], 0
    for ch in book["chapters"]:
        raw = (json.dumps({"paras": ch["paras"]}, ensure_ascii=False) + "\n").encode("utf-8")
        head["chapters"].append({"title": ch["title"],
                                 "words": sum(len(s) for p in ch["paras"] for s in p),
                                 "at": at, "len": len(raw)})
        body.append(raw)
        at += len(raw)
    return (json.dumps(head, ensure_ascii=False) + "\n").encode("utf-8") + b"".join(body)


_open_book = {}          # the index of the book in hand: {cache path: (head, where)}


def read_head(cached):
    """The index, and where the chapters start."""
    hit = _open_book.get(cached)
    if hit:
        return hit
    with open(cached, "rb") as f:
        head = json.loads(f.readline())
        where = f.tell()
    if head.get("v") != BOOK_V:
        raise ValueError("a book cache from another version")
    _open_book.clear()                    # one book at a time is the whole need
    _open_book[cached] = (head, where)
    return head, where


def open_book(path):
    """A book to read: its title and its chapters' names and lengths.

    The words of a chapter come from chapter_paras(), so a long book is not
    carried around whole to show one page of it."""
    cached = book_cache(path)
    try:
        head, _where = read_head(cached)
        return {"title": head["title"], "partial": head["partial"],
                "chapters": [{"title": c["title"], "words": c["words"]}
                             for c in head["chapters"]]}
    except (OSError, ValueError):
        pass
    low = path.lower()
    book = epub_book(path) if low.endswith(".epub") else txt_book(path)
    _open_book.pop(cached, None)
    card.write_file(cached, pack_book(book))
    prune_cache()
    return {"title": book["title"], "partial": bool(book.get("partial")),
            "chapters": [{"title": c["title"],
                          "words": sum(len(s) for p in c["paras"] for s in p)}
                         for c in book["chapters"]]}


def chapter_paras(path, index):
    """One chapter's paragraphs of sentences of [word, time]."""
    cached = book_cache(path)
    try:
        head, where = read_head(cached)
    except (OSError, ValueError):
        # Not read yet, or the cache was cleared. It can also be that the book
        # itself has gone since it was opened, in which case there is nothing
        # to read and nothing to say: the reader keeps the chapter it has.
        try:
            open_book(path)
            head, where = read_head(cached)
        except Exception:
            return []
    if not 0 <= index < len(head["chapters"]):
        return []
    at = head["chapters"][index]
    try:
        with open(cached, "rb") as f:
            f.seek(where + at["at"])
            return json.loads(f.read(at["len"]))["paras"]
    except (OSError, ValueError, KeyError):
        return []
