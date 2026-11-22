"""
Meeting Copilot — a live on-screen helper for Zoom / Meet / Teams calls (Windows).

How it works (all client-side, nothing joins the call):
  1. Captures what your PC is PLAYING (the other person's voice) via WASAPI loopback.
  2. Cuts it into utterances when they pause, transcribes each one (Groq Whisper or local).
     Audio lives only in RAM and is thrown away right after transcription. Nothing is saved.
  3. Every few seconds it glances at your screen; when the screen changes it gets a short
     text summary of what's visible (shared slides, chat, names) — cached so answers stay fast.
  4. When the other person asks you something, it pulls the best matching notes from your
     Obsidian vault and streams 2-4 talking points into a small always-on-top panel.

Hotkeys (default):  Ctrl+Alt+A answer now   Ctrl+Alt+P auto on/off   Ctrl+Alt+S re-read screen
                    Ctrl+Alt+H hide/show    Ctrl+Alt+Q quit
"""

import base64
import ctypes
import io
import json
import math
import os
import queue
import re
import sys
import threading
import time
import wave
from collections import Counter, deque
from pathlib import Path

import numpy as np
import requests

APP_DIR = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"
TARGET_RATE = 16000          # Whisper wants 16 kHz mono
FRAME_MS = 30                # VAD frame size

# ----------------------------------------------------------------------------- config

DEFAULT_CONFIG = {
    "your_name": "Me",                          # your first name, as the AI should write it
    "vault_path": "~/Documents/MyVault",        # path to your Obsidian vault
    # core facts sent with EVERY answer (paths inside the vault). Falls back to pinned_folders if empty.
    "pinned_notes": [],                         # e.g. ["00 Me/About Me.md", "00 Me/Goals.md"]
    "pinned_folders": ["00 Me"],
    "pinned_max_chars": 4800,
    # folders that matter more (>1) or less (<1) when searching; paths inside the vault
    "folder_boosts": {"Essays": 1.5, "People": 0.9, "Journal": 0.6},
    "exclude_folders": ["Templates", ".obsidian", ".trash", ".git", "Calls"],   # Calls = saved sessions, never used as facts
    # notes that are instructions for Claude, not facts about you
    "exclude_notes": ["CLAUDE.md", "Home.md", "Inbox.md"],

    "transcriber": "groq",                        # "groq" (fast, needs key) or "local" (offline, needs faster-whisper)
    "whisper_models": ["whisper-large-v3-turbo", "whisper-large-v3"],   # 2nd = backup when the 1st is rate-limited
    "local_whisper_model": "base.en",
    "listen_to_mic": False,                       # also transcribe YOU (use headphones if on)

    "auto_answer": True,
    "save_sessions": True,                        # save each call's transcript + answers as a note
    "ask_topic_on_start": True,                   # ask "what's this call about?" when the panel opens
    "session_folder": "Journal/Calls",            # folder inside your vault (keep the last part named "Calls")
    "answer_models": [
        ["groq", "openai/gpt-oss-120b"],
        ["groq", "openai/gpt-oss-20b"]
    ],
    "answer_reasoning": "low",                    # how hard gpt-oss thinks first: low (fastest) / medium / high
    "solve_reasoning": "high",                    # math / logic / knowledge questions: think hard, get it right
    "verify_models": [],

    "screen_watch": True,
    "screen_monitor": 1,                          # 1 = primary monitor
    "screen_check_seconds": 4,
    "screen_min_vision_interval": 30,             # seconds between screen reads (saves your Groq limits for answers)
    "vision_models": [
        ["groq", "meta-llama/llama-4-scout-17b-16e-instruct"]
    ],
    "hide_overlay_from_capture": True,            # keeps the panel out of its own screenshots (and out of screen share)

    "silence_ms": 450,                            # pause length that ends an utterance
    "max_segment_seconds": 8,
    "min_speech_rms": 120,

    "overlay": {"width": 430, "height": 560, "opacity": 0.93, "font_size": 12},
    "hotkeys": {
        "answer": "<ctrl>+<alt>+a",
        "more": "<ctrl>+<alt>+m",
        "bigger": "<ctrl>+<alt>+b",
        "toggle_auto": "<ctrl>+<alt>+p",
        "screen": "<ctrl>+<alt>+s",
        "hide": "<ctrl>+<alt>+h",
        "toggle_log": "<ctrl>+<alt>+l",
        "topic": "<ctrl>+<alt>+t",
        "quit": "<ctrl>+<alt>+q"
    }
}

API_BASES = {
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}
KEY_NAMES = {"groq": "GROQ_API_KEY", "nvidia": "NVIDIA_API_KEY", "mistral": "MISTRAL_API_KEY", "gemini": "GEMINI_API_KEY"}


def auth_headers(provider):
    return {"Authorization": f"Bearer {api_key(provider)}"}


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = APP_DIR / "config.json"
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def api_key(provider):
    return os.environ.get(KEY_NAMES.get(provider, ""), "").strip()


_tls = threading.local()

CALL = {"topic": "", "mode": "me"}     # what this call is about (typed in at startup)


def topic_mode(topic):
    """'me' = interviews etc. (answer about him); 'general' = debates/classes (answer the topic itself)."""
    t = topic.lower()
    if re.search(r"debat|argue|argument|mock trial|model un", t):
        return "general"
    if re.search(r"interview|admission|alumni|college|university|scholarship|job|recruit|coach|internship|"
                 r"application|about me|myself", t):
        return "me"
    if re.search(r"class|lecture|study|practice|discussion|seminar|presentation|meeting|homework|tutor|quiz|"
                 r"exam|project|group|lesson|review", t):
        return "general"
    return "me"


def topic_slug(topic, n=40):
    t = re.sub(r"[^\w\s-]", "", topic).strip()
    return re.sub(r"\s+", " ", t)[:n].strip()


def http():
    """One keep-alive session per thread (saves ~100-200 ms per call)."""
    s = getattr(_tls, "s", None)
    if s is None:
        s = _tls.s = requests.Session()
    return s


# ----------------------------------------------------------------------------- Obsidian vault

STOP = set("""a an the and or but if of to in on at for with by from as is are was were be been being it its
this that these those i me my you your we our they them he she his her do does did have has had not no so than
then there here whom can could would should will just about into over also
very really like get got one some any all more most much many okay ok um uh yeah tell know think""".split())
# (what/why/how/who/which/when/where are kept: they're what makes "Why this college?" match its prep heading)
TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text):
    out = []
    for t in TOKEN_RE.findall(text.lower()):
        if t in STOP or len(t) < 2:
            continue
        if len(t) > 4 and t.endswith("ing"):
            t = t[:-3]
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)
EMBED_RE = re.compile(r"!\[\[[^\]]*\]\]|!\[[^\]]*\]\([^)]*\)")
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
MDLINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
COMMENT_RE = re.compile(r"%%.*?%%|<!--.*?-->", re.S)


def clean_md(text):
    text = FRONTMATTER_RE.sub("", text)
    text = COMMENT_RE.sub("", text)
    text = EMBED_RE.sub("", text)
    text = WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), text)
    text = MDLINK_RE.sub(r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_md(text, max_chars=900):
    """Split by headings, then by paragraphs, keeping each chunk <= max_chars."""
    sections, heading, buf = [], "", []
    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            if "".join(buf).strip():
                sections.append((heading, "\n".join(buf).strip()))
            heading, buf = m.group(2).strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((heading, "\n".join(buf).strip()))

    chunks = []
    for heading, body in sections:
        cur = ""
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            if not para:
                continue
            while len(para) > max_chars:                     # very long paragraph
                chunks.append((heading, para[:max_chars]))
                para = para[max_chars:]
            if len(cur) + len(para) + 2 > max_chars and cur:
                chunks.append((heading, cur))
                cur = para
            else:
                cur = f"{cur}\n\n{para}" if cur else para
        if cur:
            chunks.append((heading, cur))
    return chunks


def note_facts(title, text, max_len=240):
    """Turn a note into short fact lines that keep their subject, e.g.
    'Me › People: Sam Rivera: piano teacher, writing my rec letter'.
    Keeping the subject on every line is what stops facts drifting onto the wrong person/thing."""
    out, heading = [], ""
    for line in text.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            heading = m.group(1).strip()
            continue
        l = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()
        l = re.sub(r"^\[[ xX]\]\s*", "", l)                      # task checkboxes
        l = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), l)   # bold
        if re.search(r"[\w.+-]+@[\w-]+\.\w+|https?://", l):            # emails / links: never useful to say
            continue
        l = re.sub(r"[✅⏫🔼🔽📅]\s*\d{4}-\d{2}-\d{2}", "", l).strip()
        if len(l) < 3 or l.startswith("|---"):
            continue
        ctx = title if not heading or heading.lower() == title.lower() else f"{title} › {heading}"
        if len(l) <= max_len:
            out.append(f"{ctx}: {l}")
            continue
        piece = ""
        for sent in re.split(r"(?<=[.!?])\s+", l):                  # long paragraphs -> sentence groups
            if piece and len(piece) + len(sent) > max_len:
                out.append(f"{ctx}: {piece}")
                piece = sent
            else:
                piece = f"{piece} {sent}".strip()
        if piece:
            out.append(f"{ctx}: {piece}")
    return out


class Vault:
    """Indexes the Obsidian vault on disk. 'Pinned' notes (your core facts) go into every answer whole;
    everything else is searched per question (BM25 + boosts for title/folder/conversation context)."""

    def __init__(self, cfg, log=print):
        self.root = Path(os.path.expanduser(cfg["vault_path"]))
        self.pinned_notes = [p.lower() for p in cfg.get("pinned_notes", [])]
        self.pinned_folders = [p.lower().rstrip("/") + "/" for p in cfg.get("pinned_folders", [])]
        self.boosts = sorted(((k.lower().rstrip("/"), v) for k, v in cfg.get("folder_boosts", {}).items()),
                             key=lambda kv: -len(kv[0]))
        self.exclude = {e.lower() for e in cfg.get("exclude_folders", [])}
        self.exclude_notes = {e.lower() for e in cfg.get("exclude_notes", [])}
        self.pinned_cap = cfg.get("pinned_max_chars", 4800)
        self.log = log
        self.lock = threading.Lock()
        self.chunks = []
        self.df = Counter()
        self.avgdl = 1.0
        self.pinned_text = ""
        self.pinned_items = []      # [(title, cleaned text)] in pinned order
        self.pinned_files = set()
        self.term_df = Counter()    # in how many fact lines each word appears (rare = specific, e.g. "piano")
        self.n_facts = 0
        self.sig = None

    def _files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in self.exclude]
            for f in filenames:
                if f.lower().endswith(".md"):
                    p = Path(dirpath) / f
                    if p.relative_to(self.root).as_posix().lower() not in self.exclude_notes:
                        yield p

    def _signature(self):
        n, latest = 0, 0.0
        for p in self._files():
            n += 1
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                pass
        return n, latest

    def _boost(self, rel):
        r = rel.lower()
        for prefix, v in self.boosts:
            if r.startswith(prefix):
                return v
        return 1.0

    def _is_pinned(self, rel):
        r = rel.lower()
        if self.pinned_notes:
            return r in self.pinned_notes
        return any(r.startswith(f) for f in self.pinned_folders)

    def build(self):
        if not self.root.exists():
            self.log(f"Vault not found at {self.root} — set vault_path in config.json")
            return
        t0 = time.time()
        chunks, df, pinned, term_df, n_facts = [], Counter(), {}, Counter(), 0
        for p in sorted(self._files()):
            try:
                raw = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = p.relative_to(self.root).as_posix()
            text = clean_md(raw)
            for f in note_facts(p.stem, text):
                term_df.update(set(tokenize(f.split(": ", 1)[-1])))
                n_facts += 1
            if self._is_pinned(rel):
                pinned[rel] = text
                continue                                  # always sent whole, no need to search it
            boost = self._boost(rel)
            title = p.stem
            for heading, body in chunk_md(text):
                toks = Counter(tokenize(f"{title} {heading} {body}"))
                if not toks:
                    continue
                chunks.append({
                    "file": rel, "title": title, "heading": heading, "text": body,
                    "toks": toks, "n": sum(toks.values()), "boost": boost,
                    "title_toks": set(tokenize(title)), "head_toks": set(tokenize(heading)),
                })
                df.update(toks.keys())

        # keep the configured order for pinned notes
        order = self.pinned_notes or sorted(k.lower() for k in pinned)
        parts, items, total = [], [], 0
        for want in order:
            for rel, text in pinned.items():
                if rel.lower() == want and total < self.pinned_cap:
                    piece = f"[{Path(rel).stem}]\n{text}"[: self.pinned_cap - total]
                    parts.append(piece)
                    items.append((Path(rel).stem, text))
                    total += len(piece)

        with self.lock:
            self.chunks, self.df = chunks, df
            self.avgdl = (sum(c["n"] for c in chunks) / len(chunks)) if chunks else 1.0
            self.pinned_text = "\n\n".join(parts)
            self.pinned_items = items
            self.pinned_files = set(pinned)
            self.term_df = term_df
            self.n_facts = n_facts
        self.sig = self._signature()
        self.log(f"Notes: {len(pinned)} core notes + {len(chunks)} searchable chunks ({time.time() - t0:.1f}s)")
        if not pinned:
            self.log("No core notes found — check pinned_notes in config.json")

    def _scores(self, toks):
        N = len(self.chunks) or 1
        qset = set(toks)
        out = {}
        for i, c in enumerate(self.chunks):
            s = 0.0
            for t in qset:
                f = c["toks"].get(t)
                if not f:
                    continue
                idf = math.log(1 + (N - self.df[t] + 0.5) / (self.df[t] + 0.5))
                s += idf * f * 2.5 / (f + 1.5 * (0.25 + 0.75 * c["n"] / self.avgdl))
            if s > 0:
                out[i] = s * (1 + 0.8 * len(qset & c["head_toks"]) + 0.2 * len(qset & c["title_toks"]))
        return out

    TOPIC_FILLER = set(tokenize("interview alumni call practice college university school meeting class debate "
                                "mock job chat talk session prep the with about for my and on of at"))

    def search(self, query, context="", k=6, max_chars=3600, topic=""):
        """query = what they just asked; context = the recent conversation (weaker signal);
        topic = what the call is about (strong signal: notes NAMED after it go to the top)."""
        q = tokenize(query)
        t = [w for w in tokenize(topic) if w not in self.TOPIC_FILLER]
        if not q and not t:
            return []
        with self.lock:
            main = self._scores(q + t)                   # the topic's key words count as part of the question
            ctx = self._scores(tokenize(context)) if context else {}
            top_ctx = max(ctx.values()) if ctx else 1.0
            tset = set(t)
            scored = []
            for i, s in main.items():
                c = self.chunks[i]
                s *= 1 + 0.4 * ctx.get(i, 0.0) / top_ctx
                if tset:                                 # note title / path names the topic -> strong boost
                    named = tset & (c["title_toks"] | set(tokenize(c["file"])))
                    s *= 1 + 2.0 * len(named) / len(tset)
                scored.append((s * c["boost"], i))
            scored.sort(reverse=True)
            out, used = [], 0
            for s, i in scored[: k * 2]:
                c = self.chunks[i]
                if used + len(c["text"]) > max_chars and out:
                    continue
                out.append(c)
                used += len(c["text"])
                if len(out) >= k:
                    break
            return out

    def watch(self, stop):
        while not stop.wait(45):
            try:
                if self._signature() != self.sig:
                    self.build()
            except Exception as e:  # never let the watcher die
                self.log(f"Vault refresh error: {e}")


# ----------------------------------------------------------------------------- speech segmenting

class Segmenter:
    """Voice activity detection. Feed 16 kHz int16 mono; emits finished utterances.

    The noise floor is the 10th percentile of the last ~6 s of loudness, so steady background noise
    (fans, music, Zoom hiss) never counts as speech. Pauses are also judged relative to how loud the
    speaker is, so an utterance ends quickly even when there's sound under it."""

    def __init__(self, emit, silence_ms=450, max_seconds=8, min_rms=120):
        self.emit = emit
        self.frame = TARGET_RATE * FRAME_MS // 1000
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.soft_frames = int(4500 // FRAME_MS)        # after 4.5 s, cut at any short breath
        self.max_frames = int(max_seconds * 1000 // FRAME_MS)
        self.min_rms = min_rms
        self.history = deque(maxlen=200)                 # ~6 s of frame loudness
        self.floor = 0.0
        self.carry = np.zeros(0, dtype=np.int16)
        self.preroll = deque(maxlen=10)
        self.in_speech = False
        self.voiced_run = 0
        self.silence_run = 0
        self.voiced_total = 0
        self.voiced_levels = []
        self.seg = []
        self.nframes = 0

    def feed(self, samples):
        data = np.concatenate([self.carry, samples]) if self.carry.size else samples
        n = len(data) // self.frame
        for i in range(n):
            self._frame(data[i * self.frame:(i + 1) * self.frame])
        self.carry = data[n * self.frame:]

    def idle(self, ms):
        """Called when no audio arrives (WASAPI loopback sends nothing during true silence)."""
        if self.in_speech:
            self.silence_run += max(1, ms // FRAME_MS)
            if self.silence_run >= self.silence_frames:
                self._finish()

    def _frame(self, f):
        rms = float(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
        self.history.append(rms)
        self.nframes += 1
        if self.nframes % 10 == 0 and len(self.history) >= 20:
            self.floor = float(np.percentile(self.history, 10))
        voiced = rms > max(self.min_rms, self.floor * 3.0)
        if voiced and self.in_speech and len(self.voiced_levels) >= 10:
            # a dip far below this speaker's normal level counts as a pause
            voiced = rms > 0.18 * float(np.median(self.voiced_levels[-60:]))
        if not self.in_speech:
            self.preroll.append(f)
            self.voiced_run = self.voiced_run + 1 if voiced else 0
            if self.voiced_run >= 3:
                self.in_speech = True
                self.seg = list(self.preroll)
                self.silence_run = 0
                self.voiced_total = self.voiced_run
                self.voiced_levels = [rms]
            return
        self.seg.append(f)
        if voiced:
            self.silence_run = 0
            self.voiced_total += 1
            self.voiced_levels.append(rms)
        else:
            self.silence_run += 1
        if (self.silence_run >= self.silence_frames
                or (len(self.seg) >= self.soft_frames and self.silence_run >= 6)
                or len(self.seg) >= self.max_frames):
            self._finish()

    def _finish(self):
        seg, voiced = self.seg, self.voiced_total
        self.seg, self.in_speech, self.voiced_run, self.silence_run, self.voiced_total = [], False, 0, 0, 0
        self.voiced_levels = []
        self.preroll.clear()
        if voiced >= 8 and seg:                       # >= ~240 ms of actual speech
            self.emit(np.concatenate(seg))


def to_mono_16k(raw, channels, rate):
    a = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        a = a.reshape(-1, channels).mean(axis=1)
    a = a.astype(np.float32)
    if rate != TARGET_RATE and len(a):
        n_out = int(len(a) * TARGET_RATE / rate)
        a = np.interp(np.linspace(0, len(a) - 1, n_out), np.arange(len(a)), a)
    return np.clip(a, -32768, 32767).astype(np.int16)


class AudioSource(threading.Thread):
    """kind = 'loopback' (what the PC plays = the other person) or 'mic' (you)."""

    def __init__(self, label, kind, seg_q, cfg, log):
        super().__init__(daemon=True)
        self.label, self.kind, self.seg_q, self.cfg, self.log = label, kind, seg_q, cfg, log
        self.raw_q = queue.Queue()
        self.stop_evt = threading.Event()

    def _open(self):
        import pyaudiowpatch as pyaudio
        p = pyaudio.PyAudio()
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        if self.kind == "loopback":
            dev = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not dev.get("isLoopbackDevice"):
                for lb in p.get_loopback_device_info_generator():
                    if dev["name"] in lb["name"]:
                        dev = lb
                        break
                else:
                    raise RuntimeError("No loopback device found for your default speakers/headphones")
        else:
            dev = p.get_device_info_by_index(wasapi["defaultInputDevice"])
        ch = max(1, int(dev["maxInputChannels"]))
        rate = int(dev["defaultSampleRate"])

        def cb(in_data, frame_count, time_info, status):
            self.raw_q.put(in_data)
            return (None, pyaudio.paContinue)

        stream = p.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                        input_device_index=dev["index"], frames_per_buffer=int(rate * 0.03),
                        stream_callback=cb)
        stream.start_stream()
        self.log(f"Listening ({self.label}): {dev['name']}")
        return p, stream, ch, rate

    def run(self):
        try:
            p, stream, ch, rate = self._open()
        except Exception as e:
            self.log(f"Audio error ({self.label}): {e}")
            return
        seg = Segmenter(lambda a: self.seg_q.put((self.label, a)),
                        self.cfg["silence_ms"], self.cfg["max_segment_seconds"], self.cfg["min_speech_rms"])
        while not self.stop_evt.is_set():
            try:
                raw = self.raw_q.get(timeout=0.1)
            except queue.Empty:
                seg.idle(100)
                continue
            seg.feed(to_mono_16k(raw, ch, rate))
        stream.stop_stream()
        stream.close()
        p.terminate()


# ----------------------------------------------------------------------------- transcription

HALLUCINATIONS = {"thank you.", "thank you", "thanks for watching!", "thanks for watching.", "you", "you.",
                  "bye.", "bye", ".", "thank you for watching.", "so", "um", "uh", "okay.", "hmm."}


def wav_bytes(samples):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_RATE)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


_cool = {}          # (provider, model) -> unix time it's usable again (after a 429 rate limit)


def cooling(provider, model):
    return _cool.get((provider, model), 0) > time.time()


_budget = {}        # (provider, model) -> (tokens left this minute, unix time the budget refills)


def _parse_reset(v):
    """Groq's reset format: '7.66s', '1m2.5s', '120ms'."""
    if not v:
        return 60.0
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", str(v)):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total or 1.0


def note_budget(provider, model, headers):
    """Groq tells us how many tokens we have left with every reply; remember it so we can switch models
    BEFORE getting rate-limited instead of after."""
    rem = headers.get("x-ratelimit-remaining-tokens")
    if rem is None:
        return
    try:
        _budget[(provider, model)] = (int(float(rem)), time.time() + _parse_reset(headers.get("x-ratelimit-reset-tokens")))
    except ValueError:
        pass


def budget_left(provider, model):
    rem, at = _budget.get((provider, model), (None, 0))
    return None if rem is None or time.time() >= at else rem


def cool(provider, model, seconds):
    _cool[(provider, model)] = time.time() + seconds


def retry_after(r, default=8):
    try:
        return min(60.0, max(1.0, float(r.headers.get("retry-after", default))))
    except (TypeError, ValueError):
        return default


class Transcriber(threading.Thread):
    def __init__(self, seg_q, on_text, cfg, log):
        super().__init__(daemon=True)
        self.seg_q, self.on_text, self.cfg, self.log = seg_q, on_text, cfg, log
        self.local = None
        self.pending = deque()

    def _groq(self, samples):
        models = self.cfg["whisper_models"]
        for model in models:
            if cooling("groq", model):
                continue
            r = http().post(
                f"{API_BASES['groq']}/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key('groq')}"},
                files={"file": ("speech.wav", wav_bytes(samples), "audio/wav")},
                data={"model": model, "language": "en", "temperature": "0", "response_format": "json",
                      **({"prompt": CALL["topic"][:200]} if CALL["topic"] else {})},
                timeout=12,
            )
            if r.status_code == 429:
                cool("groq", model, retry_after(r))
                self.log(f"STT rate-limited on {model}, switching")
                continue
            if r.status_code in (400, 404) and "model" in r.text.lower():
                cool("groq", model, 3600)
                continue
            r.raise_for_status()
            return r.json().get("text", ""), model
        raise ProviderError("all Whisper models rate-limited", 429)

    def _local(self, samples):
        if self.local is None:
            from faster_whisper import WhisperModel
            self.log("Loading local Whisper model…")
            self.local = WhisperModel(self.cfg["local_whisper_model"], device="cpu", compute_type="int8")
        segs, _ = self.local.transcribe(samples.astype(np.float32) / 32768.0, language="en",
                                        beam_size=1, vad_filter=False)
        return " ".join(s.text for s in segs), "local"

    def _next(self):
        """Next utterance; if more are already waiting (we fell behind), merge the same speaker's
        back-to-back utterances into ONE request — fewer calls, no growing delay."""
        if not self.pending:
            self.pending.append(self.seg_q.get())
        while True:
            try:
                self.pending.append(self.seg_q.get_nowait())
            except queue.Empty:
                break
        label, samples = self.pending.popleft()
        parts = [samples]
        gap = np.zeros(TARGET_RATE // 5, dtype=np.int16)
        while self.pending and self.pending[0][0] == label and sum(map(len, parts)) < TARGET_RATE * 20:
            parts += [gap, self.pending.popleft()[1]]
        if len(parts) > 1:
            self.log(f"STT catching up: merged {len(parts) // 2 + 1} clips")
        return label, (np.concatenate(parts) if len(parts) > 1 else samples)

    def run(self):
        while True:
            label, samples = self._next()
            t0 = time.time()
            for attempt in range(3):
                try:
                    if self.cfg["transcriber"] == "local":
                        text, model = self._local(samples)
                    else:
                        text, model = self._groq(samples)
                    break
                except ProviderError:
                    time.sleep(1.5)                     # everything rate-limited: wait a beat, retry
                except Exception as e:
                    self.log(f"Transcription error: {e}")
                    time.sleep(0.5)
            else:
                self.log("Dropped a clip (transcription kept failing)")
                continue
            secs = len(samples) / TARGET_RATE
            del samples                                # audio is never kept
            self.log(f"stt {secs:.1f}s clip → {int((time.time() - t0) * 1000)}ms ({model})", quiet=True)
            text = text.strip()
            if text and text.lower() not in HALLUCINATIONS:
                self.on_text(label, text, secs)


# ----------------------------------------------------------------------------- LLM calls

class ProviderError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


# ---- model discovery: ask each API which models THIS key can use, so retired names never break it
SKIP_WORDS = ("whisper", "guard", "tts", "playai", "orpheus", "embed", "image", "audio", "live", "veo",
              "imagen", "aqa", "learnlm", "gemma", "compound", "distil", "transcribe", "search", "robotics",
              "computer-use", "native", "moderation", "ocr", "voxtral", "codestral", "devstral",
              "rerank", "reward", "safety", "nemoguard", "retriever", "parse", "cosmos", "riva", "coder",
              "translate", "medical", "med-", "bio", "chem", "nv-embed", "math-", "-base")
ANSWER_PREFS = {
    "nvidia": ["gpt-oss-120b", "glm-5", "mistral-large-4", "kimi-k3", "lightning", "deepseek-v3", "kimi-k2",
               "qwen3", "llama-3.3-70b", "nemotron-super"],
    "mistral": ["mistral-large", "mistral-small"],
    "groq": ["gpt-oss-120b", "llama-3.3-70b", "kimi-k2", "llama-4-maverick", "llama-4-scout", "gpt-oss-20b",
             "qwen3", "llama-3.1-8b", "llama", "gpt-oss"],
    "gemini": ["flash-lite-latest", "flash-latest", "flash-lite", "flash"],
}
VERIFY_PREFS = {
    "groq": ["gpt-oss-120b", "llama-3.3-70b", "kimi-k2", "llama-4-maverick", "qwen3", "gpt-oss-20b", "llama-4-scout"],
    "gemini": ["flash-latest", "flash"],
}
VISION_PREFS = {
    "nvidia": ["llama-4-maverick", "llama-4-scout", "llama-3.2-90b-vision"],
    "groq": ["llama-4-maverick", "llama-4-scout", "vision"],
    "gemini": ["flash-lite-latest", "flash-latest", "flash-lite", "flash"],
}


LIST_STATUS = {}     # provider -> what happened when listing models (shown in the log at startup)


def list_models(provider):
    """Model ids this key can use, or None if the list couldn't be fetched."""
    key = api_key(provider)
    if not key:
        LIST_STATUS[provider] = "no key in .env"
        return []
    try:
        url = f"{API_BASES[provider]}/models" + ("?limit=100" if provider == "mistral" else "")
        r = http().get(url, headers=auth_headers(provider), timeout=15)
        if r.status_code != 200:
            LIST_STATUS[provider] = f"HTTP {r.status_code}: {r.text[:150]}"
            return None
        ids = [m.get("id", "") for m in r.json().get("data", [])]
        LIST_STATUS[provider] = f"key OK, {len(ids)} models"
        return [i.split("/", 1)[1] if i.startswith("models/") else i for i in ids if i]
    except Exception as e:
        LIST_STATUS[provider] = f"error: {e}"[:180]
        return None


def pick(avail, prefs, n=2, keep_order=False):
    out = []
    usable = [a for a in avail if not any(w in a.lower() for w in SKIP_WORDS)]
    for pref in prefs:
        cands = [a for a in usable if pref in a.lower() and a not in out]
        # stable names before preview/experimental ones, then shortest (base model, not a variant)
        if not keep_order:
            cands.sort(key=lambda a: (any(w in a for w in ("preview", "exp")), len(a)))
        if cands:
            out.append(cands[0])
        if len(out) >= n:
            break
    return out


FAMILY_RANK = ["gpt-oss", "deepseek", "kimi", "qwen", "glm", "llama", "nemotron", "mistral", "mixtral", "gemma", "phi"]


def fallback_pick(avail, n=2, vision=False):
    """None of the preferred names matched: take the best-looking general chat (or vision) models available,
    ranked by family, then size (e.g. 235b > 70b), skipping embeddings/guards/coders/etc."""
    usable = [a for a in avail if not any(w in a.lower() for w in SKIP_WORDS)]
    if vision:
        usable = [a for a in usable if re.search(r"vision|maverick|scout|-vl|vlm", a.lower())]
    else:
        usable = [a for a in usable if not re.search(r"vision|-vl\b|vlm|-r1\b|reason", a.lower())]

    def key(a):
        low = a.lower()
        fam = next((i for i, f in enumerate(FAMILY_RANK) if f in low), len(FAMILY_RANK))
        size = max([float(x) for x in re.findall(r"(\d+(?:\.\d+)?)b\b", low)] or [0])
        return (fam, -size, "instruct" not in low and "chat" not in low, len(a))

    return sorted(usable, key=key)[:n]


def resolve_models(cfg, log):
    avail = {p: list_models(p) for p in API_BASES}
    for slot, prefs in (("answer_models", ANSWER_PREFS), ("vision_models", VISION_PREFS)):
        chosen = []
        for p, m in cfg[slot]:                        # keep configured ones that really exist
            if avail.get(p) is None and api_key(p):   # couldn't check -> keep and hope
                chosen.append([p, m])
            elif avail.get(p) and m in avail[p]:
                chosen.append([p, m])
        for p in prefs:                               # then add the best ones the key actually has
            got = pick(avail.get(p) or [], prefs[p], n=3 if slot == "answer_models" else 2)
            if not got and avail.get(p):              # none of our usual names: take the best available
                got = fallback_pick(avail[p], n=2, vision=(slot == "vision_models"))
            for m in got:
                if [p, m] not in chosen:
                    chosen.append([p, m])
        order = [p for p in ("groq", "nvidia", "mistral", "gemini") if p in prefs]
        chosen.sort(key=lambda pm: order.index(pm[0]) if pm[0] in order else 99)
        if slot == "answer_models":                   # every Groq model first (fast, reliable); NVIDIA / Mistral
            fast = [pm for pm in chosen if pm[0] == "groq"]                    # free servers can be slow, so
            slow = [pm for pm in chosen if pm[0] != "groq"]                    # they're only the last resort
            chosen = fast + slow
        cfg[slot] = chosen
        log(f"{slot.split('_')[0]} models: " + (" → ".join(f"{p}/{m}" for p, m in chosen[:4]) or "NONE FOUND"))
    for p in avail:
        if api_key(p) or p in ("groq", "nvidia"):
            log(f"provider {p}: {LIST_STATUS.get(p, '?')}")
        if avail.get(p):
            msg = f"{p} models on your key: " + ", ".join(avail[p])
            try:
                log(msg, quiet=True)                  # file only, not the panel
            except TypeError:
                log(msg)


def model_extras(provider, model, reasoning=None):
    """Keep 'thinking' short on reasoning models (it adds delay); answers can ask for more."""
    m = model.lower()
    if "gpt-oss" in m:
        return {"reasoning_effort": reasoning or "low"}
    if "qwen3" in m and provider == "groq":
        return {"reasoning_effort": "none"}
    if provider == "gemini" and "flash" in m:
        return {"reasoning_effort": "none"}
    if provider == "nvidia" and reasoning in (None, "low", "none"):
        return {"chat_template_kwargs": {"thinking": False, "enable_thinking": False}}
    return {}


def _post_chat(provider, model, body, stream, timeout, reasoning=None):
    key = api_key(provider)
    if not key:
        raise ProviderError(f"no {KEY_NAMES[provider]}")
    extras = model_extras(provider, model, reasoning)
    for attempt in (extras, {}) if extras else ({},):
        r = http().post(f"{API_BASES[provider]}/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": model, **body, **attempt}, stream=stream, timeout=timeout)
        if r.status_code == 400 and attempt:          # model didn't like the extra setting — retry plain
            r.close()
            continue
        break
    if r.status_code == 429:
        wait = retry_after(r)
        cool(provider, model, wait)
        r.close()
        raise ProviderError(f"{model.split('/')[-1]} rate-limited ({int(wait)}s)", 429)
    if r.status_code != 200:
        raise ProviderError(f"{provider}/{model} → {r.status_code}: {r.text[:160]}", r.status_code)
    note_budget(provider, model, getattr(r, "headers", None) or {})
    return r


def stream_chat(provider, model, messages, max_tokens, cancel, reasoning=None, first_word_timeout=None):
    t0 = time.time()
    r = _post_chat(provider, model, {"messages": messages, "max_tokens": max_tokens,
                                     "temperature": 0.3, "stream": True}, True,
                   (5, (first_word_timeout + 2) if first_word_timeout else 15), reasoning)
    got_text = False
    try:
        # read raw bytes and decode as UTF-8 ourselves: Groq's stream has no charset, and requests would
        # otherwise guess Latin-1 and turn "•" / "—" / "’" into garbage like "â€¢"
        for raw in r.iter_lines():
            if cancel.is_set():
                return
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            delta = parse_sse_line(line)
            if delta is None:
                if not got_text and first_word_timeout and time.time() - t0 > first_word_timeout:
                    cool(provider, model, 300)          # stuck "thinking": skip it for 5 minutes
                    raise ProviderError(f"{model.split('/')[-1]} too slow to start, skipped", 408)
                continue
            if delta is StopIteration:
                return
            got_text = True
            yield delta
    finally:
        r.close()


def parse_sse_line(line):
    if not line or not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return StopIteration
    try:
        j = json.loads(data)
    except ValueError:
        return None
    ch = j.get("choices") or []
    if not ch:
        return None
    c = (ch[0].get("delta") or {}).get("content")
    if isinstance(c, list):                             # e.g. Mistral: [{"type": "thinking"...}, {"type": "text"...}]
        c = "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return c or None


def one_shot_chat(provider, model, messages, max_tokens, timeout=20):
    r = _post_chat(provider, model, {"messages": messages, "max_tokens": max_tokens, "temperature": 0},
                   False, timeout)
    return r.json()["choices"][0]["message"]["content"] or ""


# ----------------------------------------------------------------------------- screen watcher

class ScreenWatcher(threading.Thread):
    PROMPT = ("This is a screenshot of someone's computer during a video call. In under 110 words, extract what "
              "matters for answering questions: any shared slide/document text, chat messages, names/titles of "
              "people, the meeting or company/school name, and any question visible on screen. Plain text, no intro.")

    def __init__(self, cfg, ui_q, log):
        super().__init__(daemon=True)
        self.cfg, self.ui_q, self.log = cfg, ui_q, log
        self.summary = ""
        self.updated = 0.0
        self.force_evt = threading.Event()
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            age = time.time() - self.updated if self.updated else None
            return self.summary, age

    def force(self):
        self.force_evt.set()

    def run(self):
        try:
            import mss
            from PIL import Image
        except ImportError as e:
            self.log(f"Screen watch off: {e}")
            return
        last_thumb, last_call, dirty = None, 0.0, True
        with (getattr(mss, "MSS", None) or mss.mss)() as sct:
            while True:
                forced = self.force_evt.wait(self.cfg["screen_check_seconds"])
                self.force_evt.clear()
                try:
                    mons = sct.monitors
                    mon = mons[min(self.cfg["screen_monitor"], len(mons) - 1)]
                    shot = sct.grab(mon)
                    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                    thumb = np.asarray(img.convert("L").resize((96, 54)), dtype=np.int16)
                    if last_thumb is None or np.abs(thumb - last_thumb).mean() > 5:
                        dirty = True
                        last_thumb = thumb
                    if forced or (dirty and time.time() - last_call >= self.cfg["screen_min_vision_interval"]):
                        last_call = time.time()
                        dirty = False
                        self._describe(img)
                except Exception as e:
                    self.log(f"Screen error: {e}")

    def _describe(self, img):
        img = img.copy()
        img.thumbnail((960, 960))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=55)                # small enough for NVIDIA's inline-image limit
        if buf.tell() > 170_000:
            img.thumbnail((720, 720))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=50)
        b64 = base64.b64encode(buf.getvalue()).decode()
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": self.PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}]
        last = "no vision models available"
        for provider, model in list(self.cfg["vision_models"]):
            if cooling(provider, model):
                last = f"{model} cooling down"
                continue
            try:
                text = one_shot_chat(provider, model, msgs, 180).strip()[:700]
                with self.lock:
                    self.summary, self.updated = text, time.time()
                self.ui_q.put(("status", "screen", "screen read"))
                return
            except Exception as e:
                last = e
                if getattr(e, "status", None) == 404:
                    try:
                        self.cfg["vision_models"].remove([provider, model])
                    except ValueError:
                        pass
        self.log(f"Screen read failed: {last}")


# ----------------------------------------------------------------------------- the brain

Q_START = re.compile(
    r"^(?:(?:so|and|but|okay|ok|well|now|alright|um|uh|great|cool|awesome|nice)[,.]?\s+)*"
    r"(what|what's|whats|why|how|how's|when|where|who|which|tell|walk|describe|talk|explain|share|give|"
    r"can you|could you|would you|will you|do you|did you|have you|are you|were you|is there|is it|any)\b", re.I)
Q_ANY = re.compile(
    r"\b(tell (me|us) (a bit |a little |more )?about|walk (me|us) through|talk (me |us )?(about|through)|"
    r"what about you|how about you|why do you|why did you|what made you|what do you|what are your|what's your|"
    r"describe (a|an|your|the)|can you (share|tell|explain|describe|talk)|i'?d love to hear|curious (about|what|why|how))\b",
    re.I)


def looks_like_question(text):
    """True if ANY sentence in the text is a question (Whisper doesn't always add '?')."""
    for s in re.split(r"(?<=[.?!])\s+", text.strip()):
        s = s.strip()
        if not s:
            continue
        if s.endswith("?") or Q_START.match(s) or Q_ANY.search(s):
            return True
    return False


_LATEX_WORDS = {"times": " x ", "cdot": " ", "approx": "~", "le": "<=", "ge": ">=", "leq": "<=", "geq": ">=",
                "pm": "+/-", "rightarrow": "->", "to": "->", "%": "%", "quad": " ", "qquad": " ", ",": " ",
                "ldots": "...", "dots": "...", "infty": "infinity", "deg": " degrees"}


def clean_answer(text):
    """Strip [F#] source tags, markdown / LaTeX / emoji the model sneaks in, and the AI-ish em dash."""
    t = strip_cites(text, streaming=True)
    t = re.sub(r"\\(begin|end)\{[^}]*\}", "", t)                          # \begin{vmatrix} …
    t = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", t)            # \frac{a}{b} -> a/b
    t = re.sub(r"\\(?:text|textbf|textit|mathrm|mathbf|operatorname|boxed)\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\\\", "\n", t)                                          # LaTeX line break
    t = re.sub(r"\\[()\[\]]", "", t)                                      # \( \) \[ \]
    t = re.sub(r"\\([a-zA-Z]+|[%,])", lambda m: _LATEX_WORDS.get(m.group(1), m.group(1)), t)
    t = re.sub(r"\$+", "", t)
    if "\\" in text:                                                      # it was LaTeX: drop table/brace leftovers
        t = re.sub(r"\s*&\s*", " ", t)
        t = re.sub(r"[{}]", "", t)
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t)   # bold
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"(?m)^\s*#{1,6}\s*", "", t)                               # headings
    t = re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", "• ", t)                  # list markers -> bullet
    t = re.sub(r"(?m)^\s*•\s*•\s*", "• ", t)
    t = re.sub(r"\s*—\s*", ", ", t)                                        # em dash reads as AI: use a comma
    t = re.sub(r"\s+–\s+", ", ", t)
    t = t.replace("–", "-")                                               # ranges like 7th–11th -> 7th-11th
    t = re.sub(r",\s*,", ",", t)
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", t)      # emojis
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


BANNED = ("passionate, delve, leverage, journey, testament, showcase, foster, navigate, resonate, thrive, "
          "dynamic, holistic, multifaceted, invaluable, tapestry, embark, pivotal, robust, synergy, "
          "\"I'm excited to\", \"I'm eager to\", \"Furthermore\", \"Additionally\", \"Moreover\", \"In conclusion\", "
          "\"I believe that\", \"it's worth noting\", \"at the end of the day\"")

SYSTEM_PROMPT = """You help {name} live on a video call (usually an interview). Write what {name} says back, word for word.

FORMAT: {length}, in two short paragraphs: (1) the direct answer + the best concrete detail, (2) one more specific (what happened, a number, a name) + a short real reason it matters to him. Casual chat or not about him: 1-2 sentences. If there's nothing to respond to, write one short line starting "Tip:".

VOICE: a real student talking in an interview. Plain words, contractions, some short sentences, an occasional "honestly". Concrete, not poetic: no metaphors, slogans or life-lesson lines ("push my limits", "everything clicks", "taught me resilience", "that's what drives me"). No em dashes. Never use: {banned}, resilience, integrity, accountability, holistic.

TRUTH (most important):
- Only use details written in the numbered FACTS. Each fact belongs only to its subject (before the colon); never move a detail to another person or thing.
- Tag every sentence about his life with the facts it uses, right before the period: "I volunteer at the library [F3]."
- Questions about experiences (leadership, teamwork, a challenge, a time when...): describe ONLY what the facts say he actually did. Never invent duties, events or results. Pick the fact that fits best (e.g. a role he holds).
- No fitting fact: give an honest opinion or plan instead (no tag), never a made-up example.
- General-knowledge, math or logic questions (not about him): actually answer them correctly. Tags are only for claims about his own life.
- Only use facts that answer THIS question. Never mention notes or missing info.

MEMORY: EARLIER IN THIS CALL = what he already said: don't repeat it; refer back briefly ("like I said earlier") and add something new. EARLIER IN THIS SESSION = older parts of this conversation: use them if they ask about something said before ("what did I say about...", "going back to..."). PAST CALLS / FROM PAST CALLS = earlier calls: stay consistent, word it fresh, never "as I said last time" (unless they clearly were there).

Plain text only (the [F#] tags are the only extra)."""

REPAIR_PROMPT = """Each numbered sentence below, written for {name} to say on a call, has a detail that isn't supported by the FACTS (the problem is listed). Rewrite each one so every detail comes from the facts (each fact belongs only to its own subject), keeping the same point, length and casual spoken voice, with [F#] tags before the period. If no fact supports the point, rewrite it as an honest opinion or plan with no specific claims and no tag.
Output exactly one rewritten sentence per line, numbered the same way ("1. ..."), nothing else."""

TOPIC_NOTE = """

CALL CONTEXT (typed in by {name} before the call): "{topic}". Tailor every answer to this situation."""

GENERAL_NOTE = """
This call is NOT mainly about {name}'s background. Answer the actual topic: arguments, explanations, positions,
examples, the way {name} would say them out loud. In a debate: take a clear side (his side if obvious), give
2-3 strong points or directly rebut what they just said, and keep it punchy. Use his notes only when they
genuinely help (and tag those). General knowledge is fine here; never invent statistics or quotes."""

SOLVE_NOTE = """

THIS IS A MATH / LOGIC / KNOWLEDGE QUESTION: solve it for real. Think it through carefully and get it RIGHT
(check your work). Then write what {name} says: the actual answer in the first sentence, then a short, clear
explanation of how he got it (the key steps, spoken). Write math in plain text: 1/(x+2), x^3, sqrt(x), e^x, pi,
3x^2*y. No LaTeX. General knowledge is fine here (no tags needed); only tag claims about {name}'s own life.
If the problem is ambiguous, state the assumption in a few words and solve it."""

TECH_RE = re.compile(
    r"\b(derivative|integral|integrate|differentiate|gradient|limit of|slope|equation|solve|calculate|compute|"
    r"probability|probabilit|chance[s]? (that|of)|odds|expected value|optimal(ly)?|percent|percentage|average of|"
    r"factor|simplify|prove|proof|matrix|vector|log(arithm)?|ln|sin|cos|tan|sqrt|square root|squared|cubed|"
    r"how many|how much (is|would|does)|what is \d|what's \d|times|divided by|plus|minus|formula|"
    r"riddle|puzzle|logic|game theory|explain how|why does|what causes|define|definition of)\b", re.I)


def is_technical(text):
    """Math / logic / general-knowledge question (needs real thinking, not his notes)."""
    t = text.lower()
    if re.search(r"\d\s*[-+*/^=x]\s*\d|\bx\s*\^|\bf\s*\(\s*x", t):
        return True
    return len(TECH_RE.findall(t)) >= 1 and not re.search(r"\b(you|your|yourself)\b.*\b(feel|like|want|why)\b", t)


MODES = {
    "quick": {"length": "About 70-110 words (4-6 sentences)", "tokens": 1400},
    "solve": {"length": "As long as the explanation needs, usually 40-120 words", "tokens": 6000},
    "more": {"length": "About 160-230 words (9-12 sentences), with one fuller real story from the facts", "tokens": 2200},
}

TURN_GAP = 3.0       # seconds of quiet (between transcript lines) that starts a new "turn"
TURN_END = 1.2       # after they stop talking this long, answer the turn even if it didn't "look" like a question

CITE_RE = re.compile(r"\s*\[\s*F\d+(?:\s*[,;/]\s*F?\d+)*\s*\]")
PARTIAL_CITE_RE = re.compile(r"\s*\[(?:\s*F?\d*(?:\s*[,;/]\s*F?\d*)*)?$")
_SKIP_CAPS = {"i", "i'm", "i've", "i'd", "i'll", "ap", "honors", "monday", "tuesday", "wednesday", "thursday",
              "friday", "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july",
              "august", "september", "october", "november", "december", "english", "ok", "okay", "tip", "yeah",
              "honestly", "plus", "also", "and", "but", "so", "that", "that's", "it's", "my", "what", "when"}


GENERIC = set("""honestly really love like think thing thing people always help learn learned also actually myself around
matter keep idea side number money good great best better time year year make made want feel felt life work hard
part kind sure lot team ever every never still even much more most well back being doing going getting someone
something everything anything nothing reason because though since while where there here their them they these those
just only pretty super very definitely probably maybe whole stuff point show shown sort first last next""".split())


def strip_cites(text, streaming=False):
    t = CITE_RE.sub("", text)
    if streaming:
        t = PARTIAL_CITE_RE.sub("", t)                  # hide a tag that's still arriving
    return re.sub(r"\s+([.,!?;:])", r"\1", t)


def split_sentences(raw):
    """Split an answer into sentences, keeping each sentence's [F#] tags with it."""
    raw = re.sub(r"([.!?])\s*(\[\s*F[\d,\s;/F]+\](?:\s*\[\s*F[\d,\s;/F]+\])*)", r" \2\1", raw.strip())  # 'XC. [F3]' -> 'XC [F3].'
    return [s for s in re.split(r"(?<=[.!?])\s+", raw) if s.strip()]


CLAIM_WORDS = set("""won winner champion championship champ captain record records award awarded podium medal
medalist mvp all-state allstate state national nationals regional regionals county conference qualifier qualified
finalist semifinalist president founder founded cofounder manager internship intern interned published
scholarship scholarships valedictorian salutatorian ranked certified certification varsity elected""".split())
CLAIM_STEMS = {(tokenize(w) or [w])[0] for w in CLAIM_WORDS}


def sentence_map(raw):
    """[[sentences of paragraph 1], [paragraph 2], ...] so fixes can be spliced in without losing the layout."""
    return [split_sentences(p) for p in re.split(r"\n\s*\n", raw.strip()) if p.strip()]


def join_map(m):
    return "\n\n".join(" ".join(p) for p in m if p)


def check_answer(raw, facts, vault_df, allowed_text, self_name="me", only_cited=False):
    """Free, instant grounding check. Returns [(sentence, reason)] for sentences that look made up.
    Checks numbers, names and 'achievement' words (won, captain, state, internship...) against the facts each
    sentence cites; sentences about someone else (he/she/a name) also get a word-by-word check, because that's
    where facts get mixed up between people. Everyday words and opinions are left alone."""
    fact_text = {fid: _norm(t) for fid, t in facts.items()}
    all_facts = " ".join(fact_text.values())
    all_stems = set(tokenize(all_facts))
    proper = set()                                      # words your notes write Capitalized mid-sentence (names)
    for t in facts.values():
        body_ = t.split(": ", 1)[-1]
        for m in re.finditer(r"(?<=[a-z,;]\s)([A-Z][A-Za-z'’&.-]+)", body_):
            proper.add(_norm(m.group(1)).strip(".'-"))
        proper.update(_norm(w).strip(".'-") for w in re.findall(r"[A-Z][A-Za-z'’&.-]+", t.split(": ", 1)[0]))
    fact_stems = Counter()
    for t in fact_text.values():
        fact_stems.update(set(tokenize(t.split(": ", 1)[-1])))
    allowed = _norm(allowed_text)                       # things the other person said are always OK to repeat
    allowed_stems = set(tokenize(allowed))
    flags = []
    if raw.strip().lower().startswith("tip"):
        return flags

    def has(text_stems, text, term):
        if re.fullmatch(r"[\d:.,/]+", term):
            return term in text
        st = (tokenize(term) or [_norm(term)])[0]
        return st in text_stems or any(len(c) >= 4 and c[:3] == st[:3] and (c.startswith(st) or st.startswith(c))
                                       for c in text_stems)

    for sent in split_sentences(raw):
        ids = [int(n) for n in re.findall(r"F?(\d+)", " ".join(re.findall(r"\[[^\]]*\]", sent)))]
        body = strip_cites(sent)
        if only_cited and not ids:
            continue                                    # debate/class: arguments aren't claims about his life
        bad_ids = [i for i in ids if i not in fact_text]
        if bad_ids:
            flags.append((body, f"cites F{bad_ids[0]}, which doesn't exist"))
            continue
        cited = " ".join(fact_text[i] for i in ids)
        cited_stems = set(tokenize(cited))
        third_person = bool(re.search(r"\b(he|she|him|her|his|hers|they|them|their)\b", body, re.I))
        first_person = bool(re.search(r"\b(i|i'm|i’m|i've|i’ve|i'd|i’d|i'll|me|my|mine|we|our|us)\b", body, re.I))
        mentions_notes = any(_norm(w).strip(".'-").split("'")[0] in proper
                             for w in re.findall(r"[A-Z][A-Za-z'’&.-]*", body))
        if not ids and not first_person and not third_person and not mentions_notes:
            continue                                    # general knowledge / math: not a claim about his life

        terms = [n for n in re.findall(r"\d+(?:[:.,/]\d+)*", body) if n not in allowed]
        names = []
        words = re.findall(r"[A-Za-z][A-Za-z'’&.-]*", body)
        for k, w in enumerate(words):
            lw = _norm(w).strip(".'-")
            if not w[0].isupper() or lw in _SKIP_CAPS or len(lw) < 2 or lw in allowed or lw in allowed_stems:
                continue
            if k == 0 and lw.split("'")[0] not in proper:          # ordinary sentence-start word, not a name
                continue
            terms.append(w.strip(".'’"))
            if lw.split("'")[0] != self_name.lower():
                names.append(lw)
        # about someone/something else? (a pronoun, or a name like "Kevin") -> check every specific word
        third_person = third_person or bool(names)
        for st in set(tokenize(body)):
            if st in allowed_stems or len(st) < 3:
                continue
            if st in CLAIM_STEMS:
                terms.append(st)
            elif third_person and len(st) >= 4 and st not in GENERIC and st in fact_stems \
                    and 0 < vault_df.get(st, 0) <= 25:
                terms.append(st)
        terms = list(dict.fromkeys(terms))
        if not terms:
            continue
        if ids:
            problems = [t for t in terms if not has(cited_stems, cited, t)]
            # true detail with the wrong tag? fine, if ONE fact holds all of it and it's about him, not someone else
            if problems and not third_person and any(
                    all(has(set(tokenize(t)), t, x) for x in terms) for t in fact_text.values()):
                problems = []
        else:
            problems = [t for t in terms if re.fullmatch(r"[\d:.,/]+", t) or not has(all_stems, all_facts, t)]
        if problems:
            where = "isn't in the facts it cites" if ids else "has no source fact"
            flags.append((body, f"{', '.join(problems[:4])} {where}"))
    return flags


def _overlap(a, b):
    sa, sb = set(tokenize(a)), set(tokenize(b))
    return len(sa & sb) / (len(sa | sb) or 1)


def _short(text, n):
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def load_past_answers(folder, max_calls=10):
    """Read the copilot's answers out of saved call notes: [(date, question, answer)], newest call first."""
    out = []
    try:
        files = sorted(Path(folder).glob("*Call.md"), reverse=True)[:max_calls]
    except Exception:
        return out
    pat = re.compile(r"^> \[!note\]- Copilot answer \([^)]*\): (.*)\n((?:>.*\n?)*)", re.M)
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        date = f.name[:10]
        for m in pat.finditer(text):
            body = [l[1:].strip() for l in m.group(2).splitlines()]
            body = [l for l in body if l and not (l.startswith("*") and l.endswith("*"))]
            if body:
                out.append((date, m.group(1).strip(), " ".join(body)))
    return out


def load_past_lines(folder, max_calls=15):
    """What was SAID in earlier calls (from saved notes): [(date, speaker, text)], for 'remember when' lookups."""
    out = []
    try:
        files = sorted(Path(folder).glob("*Call.md"), reverse=True)[:max_calls]
    except Exception:
        return out
    pat = re.compile(r"^\*\*\d\d:\d\d:\d\d (Them|Me):\*\* (.+)$", re.M)
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in pat.finditer(text):
            if len(m.group(2).split()) >= 4:
                out.append((f.name[:10], m.group(1), m.group(2).strip()))
    return out


def related_lines(question, lines, k, min_overlap, get_text):
    scored = [(_overlap(question, get_text(x)), i) for i, x in enumerate(lines)]
    best = sorted([p for p in scored if p[0] >= min_overlap], reverse=True)[:k]
    return [lines[i] for _, i in sorted(best, key=lambda p: p[1])]       # keep original order


def _norm(t):
    t = t.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", t)


class Brain:
    def __init__(self, cfg, vault, screen, ui_q, log):
        self.cfg, self.vault, self.screen, self.ui_q, self.log = cfg, vault, screen, ui_q, log
        self.name = cfg["your_name"]
        self.transcript = deque(maxlen=4000)          # everything said since the app started
        self.auto = cfg["auto_answer"]
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.gen = 0
        self.turn = []           # the other person's lines since they started this turn
        self.turn_last = 0.0
        self.turn_fired = False  # already answered something in this turn
        self.fired_complete = True
        self.fired_len = 0       # how many of their lines the last answer covered
        self.turn_id = 0
        self.pauses = deque(maxlen=12)
        self.last_question = ""
        self.line_id = 0
        self.session = None      # SessionLog, set by main()
        self.history = []        # [(question, answer)] given in THIS call
        self.history_turns = []  # which turn each history entry answered
        self.last_answer_at = 0.0
        self.past = []           # [(date, question, answer)] from earlier calls (Calls/ notes)
        self.past_lines = []     # [(date, speaker, text)] what was said in earlier calls

    # ---- deciding when to answer
    def set_topic(self, topic):
        topic = re.sub(r"\s+", " ", topic or "").strip()[:200]
        CALL["topic"], CALL["mode"] = topic, topic_mode(topic)
        self.log(f"Call topic: {topic or '(none)'}" + (" (debate/class mode)" if CALL["mode"] == "general" else ""))
        if self.session:
            self.session.set_topic(topic)

    def _typical_pause(self):
        """How long THIS speaker usually pauses mid-thought (median of recent pauses between their lines)."""
        p = sorted(self.pauses)
        return p[len(p) // 2] if p else 0.0

    def turn_gap(self):
        """Quiet time that starts a NEW question. Slow talkers get more room (4-9 s)."""
        return max(TURN_GAP, min(9.0, 3.0 + 2.2 * self._typical_pause()))

    def turn_end_wait(self):
        """How long to wait after they stop before treating the question as finished (1.2-3.5 s)."""
        return max(TURN_END, min(3.5, TURN_END + 1.0 * self._typical_pause()))

    def on_transcript(self, speaker, text, dur=0.0):
        now = time.time()
        with self.lock:
            self.transcript.append((speaker, text))
            if speaker == "Them":
                if self.turn_last and dur is not None:
                    pause = (now - self.turn_last) - (dur or 0.0)
                    if 0 <= pause < 10:
                        self.pauses.append(pause)
                if now - self.turn_last > self.turn_gap():
                    self.turn, self.turn_fired, self.fired_len = [], False, 0
                    self.turn_id += 1
                self.turn.append(text)
                self.turn_last = now
                self.line_id += 1
                line_id = self.line_id
            else:
                self.turn, self.turn_fired, self.fired_len = [], False, 0   # you answered: next line = new turn
                self.turn_id += 1
            turn_text = " ".join(self.turn)[-700:]
        self.ui_q.put(("transcript", speaker, text))
        if self.session:
            self.session.transcript(speaker, text)
        if speaker != "Them" or not self.auto:
            return
        # when they stop talking: answer the turn, or re-answer it if they kept going after a quick answer
        threading.Timer(self.turn_end_wait(), self._turn_end, args=(line_id,)).start()
        if not looks_like_question(turn_text):
            self.log("line: not question-like (waiting for end of turn)", quiet=True)
            return
        # Quick answer on the first clear question. If they keep talking, DON'T restart on every line:
        # _turn_end re-answers once with the whole question when they finish (unless it's a brand-new question).
        if self.turn_fired and self.fired_complete:
            return                                      # already answered this turn: _turn_end redoes it once, with
                                                        # everything they said, when they stop (cut-off lines excepted)
        self.turn_fired = True
        self.fired_complete = text.strip().endswith((".", "?", "!"))
        self.fired_len = len(self.turn)
        self.log("auto: question detected", quiet=True)
        self.answer(turn_text)

    def _turn_end(self, line_id):
        with self.lock:
            if line_id != self.line_id or not self.auto:
                return                                  # they kept talking
            turn_text = " ".join(self.turn)[-700:]
            if self.turn_fired:
                # answered early on part of it, then they kept going: redo it ONCE with the full question
                extra = " ".join(self.turn[self.fired_len:])
                if len(extra.split()) < 5:
                    return
                self.fired_len = len(self.turn)
                why = "auto: they kept going, re-answering with the full question"
            else:
                if len(turn_text.split()) < 6:           # "okay", "great", "mm-hmm"
                    return
                if time.time() - self.last_answer_at < 8:     # just answered: don't burn Groq's limit on chatter
                    return
                self.turn_fired, self.fired_len = True, len(self.turn)
                why = "auto: end of their turn"
        self.log(why, quiet=True)
        self.answer(turn_text)

    # ---- building the prompt
    def build_facts(self, question, recent, max_facts=32, max_chars=4200):
        """Pick the ~30 fact lines that matter for THIS question (fewer tokens = faster, fewer rate limits,
        and the right fact stands out). Always keeps a few core 'who I am' lines."""
        convo_txt = " ".join(t for _, t in recent[-4:])
        notes = self.vault.search(question, context=convo_txt, k=8, max_chars=5000, topic=CALL["topic"])
        pinned_lines = []
        for title, text in self.vault.pinned_items:
            pinned_lines += note_facts(title, text)
        cand = list(pinned_lines)
        for rank, c in enumerate(notes):
            cand += note_facts(c["title"], f"# {c['heading']}\n{c['text']}" if c["heading"] else c["text"])
        seen, uniq = set(), []
        for l in cand:
            k = _norm(l)
            if k not in seen:
                seen.add(k)
                uniq.append(l)
        n = max(1, self.vault.n_facts)
        idf = lambda st: math.log(1 + n / (1 + self.vault.term_df.get(st, 0)))
        q = set(tokenize(question)) | (set(tokenize(CALL["topic"])) - self.vault.TOPIC_FILLER)
        ctx = set(tokenize(convo_txt)) - q

        def score(line):
            subj, _, body = line.partition(": ")
            ls = set(tokenize(line))
            sc = sum(idf(t) for t in q & ls) + 0.3 * sum(idf(t) for t in ctx & ls)
            sc += 0.8 * sum(idf(t) for t in q & set(tokenize(subj)))      # heading matches the question
            return sc

        core = [l for l in pinned_lines
                if (re.search(r"basics", l.split(": ")[0], re.I) or re.search(r"intended major", l, re.I))
                and not re.search(r"@|email|http", l)][:4]                  # a few "who I am" lines, always
        ranked = sorted(uniq, key=score, reverse=True)
        chosen, used = [], 0
        for l in core + [l for l in ranked if score(l) > 0] + pinned_lines:
            if l in chosen:
                continue
            if len(chosen) >= max_facts or used + len(l) > max_chars:
                break
            chosen.append(l)
            used += len(l)
        return {i + 1: l for i, l in enumerate(chosen)}, notes

    def memory_text(self, question, mode="quick"):
        """What was already said: this call (recent + related) and related answers from past calls."""
        hist = self.history[:-1] if mode == "more" and self.history else list(self.history)
        related = sorted(range(len(hist)), key=lambda i: -_overlap(question, hist[i][0] + " " + hist[i][1]))[:3]
        pick = sorted(set(related) | set(range(max(0, len(hist) - 3), len(hist))))
        this_call = "\n".join(f"- They asked: {_short(hist[i][0], 140)}\n  {self.name} said: {_short(hist[i][1], 420)}"
                              for i in pick)
        scored = sorted(self.past, key=lambda p: -_overlap(question, p[1]))
        past = [p for p in scored[:3] if _overlap(question, p[1]) >= 0.12]
        past_txt = "\n".join(f"- ({d}) They asked: {_short(q, 120)}\n  {self.name} said: {_short(a, 380)}"
                              for d, q, a in past)
        return this_call, past_txt

    def build_messages(self, question, recent, mode="quick"):
        facts, notes = self.build_facts(question, recent)
        this_call, past_txt = self.memory_text(question, mode)
        if mode == "solve":
            past_txt = ""          # math/logic: work it out fresh, don't stay "consistent" with an old wrong answer
        facts_txt = "\n".join(f"F{i} {t}" for i, t in facts.items()) or "(none)"
        scr, age = self.screen.get() if self.screen else ("", None)
        who = lambda sp: "Them" if sp == "Them" else self.name
        convo = "\n".join(f"{who(sp)}: {t}" for sp, t in recent[-12:]) or "(none yet)"
        older = related_lines(question, recent[:-12], 6, 0.15, lambda x: x[1])
        older_txt = "\n".join(f"{who(sp)}: {_short(t, 220)}" for sp, t in older)
        past_said = related_lines(question, self.past_lines, 4, 0.25, lambda x: x[2])
        past_said_txt = "\n".join(f"({d}) {who(sp)}: {_short(t, 200)}" for d, sp, t in past_said)
        sysmsg = SYSTEM_PROMPT.format(name=self.name, banned=BANNED, length=MODES[mode]["length"])
        if CALL["topic"]:
            sysmsg += TOPIC_NOTE.format(name=self.name, topic=CALL["topic"][:200])
            if CALL["mode"] == "general" and mode != "solve":
                sysmsg += GENERAL_NOTE.format(name=self.name)
        if mode == "solve":
            sysmsg += SOLVE_NOTE.format(name=self.name)
        more_note = ("\n(This is the LONGER version of the answer just given: expand it with more depth and a "
                     "fuller story. It's fine to reuse its points.)") if mode == "more" else ""
        user = (f"FACTS about {self.name} (from his Obsidian notes):\n{facts_txt}\n\n"
                + (f"SCREEN (what's visible):\n{scr}\n\n" if scr else "")
                + (f"EARLIER IN THIS CALL:\n{this_call}\n\n" if this_call else "")
                + (f"PAST CALLS (similar questions):\n{past_txt}\n\n" if past_txt else "")
                + (f"EARLIER IN THIS SESSION (related things said before):\n{older_txt}\n\n" if older_txt else "")
                + (f"FROM PAST CALLS (related things said):\n{past_said_txt}\n\n" if past_said_txt else "")
                + f"RECENT CONVERSATION:\n{convo}\n\nTHEY JUST SAID: \"{question}\"\n\n"
                f"Write {self.name}'s answer with [F#] tags.{more_note}")
        allowed = f"{convo}\n{older_txt}\n{question}\n{scr}\n{CALL['topic']}"
        return [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}], notes, facts, allowed

    # ---- answering
    def answer(self, question=None, mode="quick"):
        with self.lock:
            recent = list(self.transcript)
            if question is None:                       # manual: use their current/last turn
                question = " ".join(self.turn)[-600:] or next((t for s, t in reversed(recent) if s == "Them"), "")
        if mode == "more" and not question:
            question = self.last_question
        if not question:
            self.ui_q.put(("status", "info", "Nothing heard yet"))
            return
        self.last_question = question
        self.last_answer_at = time.time()
        self.cancel.set()                              # a newer question replaces an in-flight answer
        self.cancel = threading.Event()
        self.gen += 1
        threading.Thread(target=self._run, args=(self.gen, question, recent, self.cancel, mode, self.turn_id),
                         daemon=True).start()

    def more(self):
        """Longer, deeper version of the last answer."""
        self.answer(self.last_question or None, mode="more")

    def _stream(self, slot, messages, max_tokens, cancel, gen, kind, reasoning=None):
        """Stream from the first working model in cfg[slot]. Returns (text, model, ttft) or raises."""
        errors = []
        t0 = time.time()
        need = sum(len(m["content"]) for m in messages) / 3.5 + 500        # rough token estimate for this call
        order = [pm for pm in self.cfg[slot] if not cooling(*pm)]
        roomy = [pm for pm in order if (budget_left(*pm) is None or budget_left(*pm) >= need)]
        tight = sorted([pm for pm in order if pm not in roomy], key=lambda pm: -(budget_left(*pm) or 0))
        if not order:
            errors.append("all models cooling down")
        for provider, model in roomy + tight:                # models with room first, then the least-starved
            got, ttft = [], None
            try:
                fwt = 25 if reasoning == "high" else (7 if provider == "groq" else 5)
                for delta in stream_chat(provider, model, messages, max_tokens, cancel, reasoning=reasoning,
                                         first_word_timeout=fwt):
                    if ttft is None:
                        ttft = time.time() - t0
                    got.append(delta)
                    if kind:
                        self.ui_q.put((kind, gen, delta))
                if got or cancel.is_set():
                    return "".join(got), model, ttft
                errors.append(f"{model.split('/')[-1]} returned nothing")
            except Exception as e:
                if cancel.is_set() or got:             # replaced, or died mid-stream: keep what we have
                    return "".join(got), model, ttft
                if getattr(e, "status", None) == 404:
                    try:
                        self.cfg[slot].remove([provider, model])
                    except ValueError:
                        pass
                elif getattr(e, "status", None) in (401, 402, 403):   # not allowed on this plan (e.g. a free key
                    cool(provider, model, 3600)                      # can't use Mistral Large): skip it for an hour
                elif isinstance(e, requests.exceptions.RequestException) or getattr(e, "status", None) in (408, 500, 502, 503, 504):
                    cool(provider, model, 600)                       # slow / down server: skip it for 10 minutes
                errors.append(str(e))
                self.log(f"{slot} model failed: {e}")
        raise ProviderError(" | ".join(errors)[:240] or f"no {slot} available")

    def _run(self, gen, question, recent, cancel, mode, turn_id=None):
        t0 = time.time()
        reasoning = self.cfg.get("answer_reasoning", "low")
        if mode == "quick" and is_technical(question):
            mode, reasoning = "solve", self.cfg.get("solve_reasoning", "high")
            self.log("solve mode: math/logic/knowledge question, thinking harder", quiet=True)
            self.ui_q.put(("status", "ans", "working it out…"))
        messages, notes, facts, allowed = self.build_messages(question, recent, mode)
        src_titles = ", ".join(dict.fromkeys(c["title"] for c in notes[:3]))
        self.ui_q.put(("answer_start", gen, question))
        try:
            raw, model, ttft = self._stream("answer_models", messages, MODES[mode]["tokens"], cancel, gen,
                                            "answer_delta", reasoning=reasoning)
        except ProviderError as e:
            if not cancel.is_set():
                self.ui_q.put(("answer_done", gen, f"Error: {e}", []))
            return
        if cancel.is_set():
            self.log("answer replaced by newer one", quiet=True)
            return
        t_ans = time.time() - t0
        flags = check_answer(raw, facts, self.vault.term_df, allowed, self.name,
                             only_cited=(CALL["mode"] == "general"))
        fixed_note = "every detail matches your notes"
        if flags and not raw.strip().lower().startswith("tip"):
            self.log(f"check: {len(flags)} sentence(s) not backed by notes, repairing", quiet=True)
            self.ui_q.put(("status", "ans", "fixing a detail that isn't in your notes…"))
            raw2 = self._repair(raw, flags, facts, cancel)
            if cancel.is_set():
                return
            if raw2:
                flags2 = check_answer(raw2, facts, self.vault.term_df, allowed, self.name,
                                      only_cited=(CALL["mode"] == "general"))
                raw = raw2
            else:
                flags2 = flags
            bad = {s for s, _ in flags2}
            m = sentence_map(raw)
            m2 = [[x for x in p if strip_cites(x) not in bad] for p in m]
            kept = [x for p in m2 for x in p]
            if raw2 and bad and len(kept) >= 3:        # fix ran, still unsupported: drop just those sentences
                raw = join_map(m2)
                fixed_note = f"fixed {len(flags)} detail(s), removed {len(bad)} not in your notes"
                flags = []
            elif raw2:
                fixed_note = f"fixed {len(flags)} detail(s) not in your notes"
                flags = flags2
            else:                                      # couldn't fix (rate limit): keep it, highlight in orange
                fixed_note = "orange = not found in your notes, double-check"
                flags = flags2
            self.ui_q.put(("answer_replace", gen, raw))
        self.log(f"answer first word {ttft:.2f}s, done {t_ans:.2f}s, total {time.time() - t0:.2f}s ({model})",
                 quiet=True)
        meta = f"{ttft:.1f}s · {model.split('/')[-1]}" + (f" · from: {src_titles}" if src_titles else "")
        meta += f" · {fixed_note}"
        self.ui_q.put(("answer_done", gen, meta, [s for s, _ in flags]))
        final = clean_answer(raw)
        if final and not final.lower().startswith("tip"):
            if turn_id is not None and self.history_turns and self.history_turns[-1] == turn_id:
                self.history[-1] = (question, final)      # full-question answer replaces the partial one
            else:
                self.history.append((question, final))
                self.history_turns.append(turn_id)
        if self.session:
            self.session.answer(question, final, meta)

    def _repair(self, raw, flags, facts, cancel):
        """Rewrite ONLY the flagged sentences (small, fast call) and splice them back into the answer."""
        m = sentence_map(raw)
        where = {strip_cites(x): (pi, si) for pi, p in enumerate(m) for si, x in enumerate(p)}
        idx = [where.get(body) for body, _ in flags]
        facts_txt = "\n".join(f"F{i} {t}" for i, t in facts.items())
        listing = "\n".join(f"{k + 1}. {m[i[0]][i[1]] if i else body}  (problem: {why})"
                            for k, ((body, why), i) in enumerate(zip(flags, idx)))
        msgs = [{"role": "system", "content": REPAIR_PROMPT.format(name=self.name)},
                {"role": "user", "content": f"FACTS:\n{facts_txt}\n\nSENTENCES:\n{listing}"}]
        try:
            text, _, _ = self._stream("answer_models", msgs, 700, cancel, None, None, reasoning="low")
        except ProviderError as e:
            self.log(f"repair failed: {e}")
            return ""
        fixes = {}
        for line in text.splitlines():
            hit = re.match(r"\s*(\d+)[.)]\s*(.+)", line)
            if hit:
                fixes[int(hit.group(1)) - 1] = hit.group(2).strip()
        if not fixes:
            return ""
        for k, i in enumerate(idx):
            if i is not None and k in fixes:
                m[i[0]][i[1]] = fixes[k]
        return join_map(m)


# ----------------------------------------------------------------------------- session log

class SessionLog:
    """Saves each call as a Markdown note (transcript + the copilot's answers), written line by line as it
    happens, so nothing is lost if the app closes. Goes to <vault>/Calls/ (or ./sessions/ without a vault)."""

    def __init__(self, cfg, log):
        self.enabled = bool(cfg.get("save_sessions", True))
        vault = Path(os.path.expanduser(cfg["vault_path"]))
        folder = cfg.get("session_folder", "Journal/Calls")
        self.dir = (vault / folder) if vault.exists() else (APP_DIR / "sessions")
        self.vault = vault if vault.exists() else None
        self.log = log
        self.lock = threading.Lock()
        self.path = None
        self.started = None
        self.questions = []
        self.topic = ""

    def _write(self, text):
        with self.lock:
            if not self.enabled:
                return
            try:
                if self.path is None:                        # create the note on the first thing said
                    self.started = time.localtime()
                    self.dir.mkdir(parents=True, exist_ok=True)
                    stamp = time.strftime("%Y-%m-%d %H%M", self.started)
                    slug = topic_slug(self.topic)
                    self.path = self.dir / (f"{stamp} {slug} Call.md" if slug else f"{stamp} Call.md")
                    topic_line = f"topic: \"{self.topic.replace(chr(34), '')}\"\n" if self.topic else ""
                    title = self.topic or "Call"
                    head = (f"---\ntype: call\ndate: {time.strftime('%Y-%m-%d', self.started)}\n"
                            f"start: \"{time.strftime('%H:%M', self.started)}\"\n{topic_line}"
                            f"kind: {'interview' if 'interview' in self.topic.lower() else ('call' if self.topic else 'practice')}\n"
                            f"tags: [call]\n---\n"
                            f"# {title} ({time.strftime('%Y-%m-%d %H:%M', self.started)})\n\n## Transcript\n")
                    self.path.write_text(head, encoding="utf-8")
                    self.log(f"Saving this call to {self.dir.name}/{self.path.name}")
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:
                self.log(f"Session log error: {e}")

    def set_topic(self, topic):
        with self.lock:
            self.topic = topic
            if self.path is None or not self.path.exists():
                return                                   # the note gets the topic when it's created
            try:                                         # already started: note the change in place
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(f"> *Topic changed to: {topic}*\n\n")
            except Exception:
                pass

    def transcript(self, speaker, text):
        who = "Them" if speaker == "Them" else "Me"
        self._write(f"**{time.strftime('%H:%M:%S')} {who}:** {text}\n\n")

    def answer(self, question, text, meta=""):
        if not text.strip():
            return
        if text.strip().lower().startswith("tip"):
            self._write(f"> [!tip] Copilot ({time.strftime('%H:%M:%S')})\n> {text.strip()}\n\n")
            return
        self.questions.append(question.strip())
        body = "\n".join(f"> {l}" if l.strip() else ">" for l in text.strip().splitlines())
        q = question.strip().replace("\n", " ")[-150:]
        self._write(f"> [!note]- Copilot answer ({time.strftime('%H:%M:%S')}): {q}\n{body}\n"
                    + (f"> *{meta}*\n" if meta else "") + "\n")

    def toggle(self):
        with self.lock:
            self.enabled = not self.enabled
        return self.enabled

    LINK_FILLER = {"interview", "call", "calls", "meeting", "practice", "prep", "chat", "with", "the", "and",
                   "for", "about", "of", "a", "an", "my", "test", "session", "class", "debate"}

    def _related(self):
        """Footer that hooks the note into the vault graph: the Calls hub, the project note, that day's daily
        note, and any notes whose titles match the call topic (e.g. 'State U Interview' links
        [[State University]] and [[Interview - State U]])."""
        links = ["[[Calls]]", "[[Meeting Copilot]]"]
        try:
            vault = self.vault
            if vault is None:
                return "\n---\nRelated: " + " · ".join(links) + "\n"
            day = time.strftime("%Y-%m-%d", self.started)
            if (vault / "Journal" / "Daily" / f"{day}.md").exists():
                links.append(f"[[{day}]]")
            words = [w for w in re.findall(r"[a-z0-9]+", self.topic.lower()) if w not in self.LINK_FILLER and len(w) > 1]
            if words:
                hits = []
                for f in vault.rglob("*.md"):
                    rel = f.relative_to(vault).parts
                    if rel[0].startswith(".") or "calls" in (x.lower() for x in rel[:-1]) or rel[0] in ("Templates",):
                        continue
                    stem = f.stem.lower()
                    if all(re.search(rf"\b{re.escape(w)}\b", stem) for w in words):
                        hits.append(f.stem)
                for h in sorted(set(hits), key=len)[:3]:
                    links.append(f"[[{h}]]")
        except Exception as e:
            self.log(f"Session log links: {e}")
        return "\n---\nRelated: " + " · ".join(dict.fromkeys(links)) + "\n"

    def close(self):
        with self.lock:
            if self.path is None or not self.path.exists():
                return
            try:
                end = time.strftime("%H:%M")
                text = self.path.read_text(encoding="utf-8")
                text = text.replace(f"start: \"{time.strftime('%H:%M', self.started)}\"\n",
                                    f"start: \"{time.strftime('%H:%M', self.started)}\"\nend: \"{end}\"\n", 1)
                if self.questions:
                    text += "\n## Questions they asked\n" + "".join(
                        f"- {q}\n" for q in dict.fromkeys(self.questions))
                text += self._related()
                self.path.write_text(text, encoding="utf-8")
            except Exception as e:
                self.log(f"Session log error: {e}")


# ----------------------------------------------------------------------------- overlay UI

class Overlay:
    # palette: soft dark, one accent
    BG, CARD, LINE = "#0f1115", "#171a20", "#232731"
    FG, DIM, FAINT = "#e8eaf0", "#9097a6", "#5d6372"
    ACCENT, THEM, YOU, WARN = "#7aa8ff", "#e7c46b", "#7fd3ad", "#ffb86b"
    DRAFT = "#b4b9c4"
    STATE_FILE = APP_DIR / "window.json"      # panel size/position only

    # Segoe Fluent Icons (Win 11) / Segoe MDL2 Assets (Win 10) glyphs, with text fallbacks
    ICONS = {"answer": ("", "Answer"), "more": ("", "More"), "auto": ("", "Auto"),
             "screen": ("", "Screen"), "log": ("", "Log"), "big": ("", "Bigger"),
             "small": ("", "Smaller"), "hide": ("", "–"), "close": ("", "×"),
             "down": ("", "▾"), "up": ("", "▴")}

    def __init__(self, cfg, ui_q, actions, has_screen=True):
        import tkinter as tk
        import tkinter.font as tkfont
        self.tk = tk
        self.cfg, self.ui_q, self.actions = cfg, ui_q, actions
        o = cfg["overlay"]
        fs = o["font_size"]
        self.gen = 0
        self.pending_q = None
        self.raw = ""
        self.final_raw = None
        self.hold = None        # first few characters of a new answer, to spot "Tip:" before showing
        self.tip_gen = None
        self.verifying = bool(cfg.get("verify_answers", True) and cfg.get("verify_models"))
        self.auto_on = cfg.get("auto_answer", True)
        self.log_on = cfg.get("save_sessions", True)
        self.root = root = tk.Tk()
        root.title("Meeting Copilot")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", max(0.9, o.get("opacity", 0.96)))
        root.configure(bg=self.BG)

        fams = set(tkfont.families())
        self.ui_font = next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in fams), "TkDefaultFont")
        self.ui_bold = next((f for f in ("Segoe UI Variable Display Semib", "Segoe UI Semibold") if f in fams), self.ui_font)
        self.icon_font = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.small_geo = f"{o['width']}x{o['height']}+{sw - o['width'] - 24}+80"
        try:
            self.small_geo = json.loads(self.STATE_FILE.read_text())["geometry"]
        except Exception:
            pass
        self.big = False
        self.big_size = (max(640, o["width"]), max(420, sh - 160))
        root.geometry(self.small_geo)

        # ---- header: status dot, title, icon buttons
        head = tk.Frame(root, bg=self.BG)
        head.pack(fill="x", padx=12, pady=(10, 6))
        self.dot = tk.Label(head, text="●", fg=self.YOU, bg=self.BG, font=(self.ui_font, fs - 2))
        self.dot.pack(side="left")
        title = tk.Label(head, text="Copilot", fg=self.FG, bg=self.BG, font=(self.ui_bold, fs), cursor="hand2")
        title.pack(side="left", padx=(6, 0))
        self.title_lbl = title
        self._tooltip(title, "Click to set what this call is about")
        title.bind("<Button-1>", lambda e: self.show_setup(), add="+")
        for w in (head, self.dot):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", lambda e: self.save_geometry())

        self.icon_btn(head, "close", actions["quit"], "Quit (Ctrl+Alt+Q)", hover_fg="#ff6b6b")
        self.icon_btn(head, "hide", actions["hide"], "Hide (Ctrl+Alt+H)")
        self.size_btn = self.icon_btn(head, "big", self.toggle_big, "Bigger / smaller (Ctrl+Alt+B)")
        tk.Frame(head, bg=self.LINE, width=1, height=16).pack(side="right", padx=6)
        self.log_btn = self.icon_btn(head, "log", actions["toggle_log"], "Save this call (Ctrl+Alt+L)")
        if has_screen:
            self.icon_btn(head, "screen", actions["screen"], "Re-read screen (Ctrl+Alt+S)")
        self.auto_btn = self.icon_btn(head, "auto", actions["toggle_auto"], "Auto-answer on/off (Ctrl+Alt+P)")
        self.icon_btn(head, "more", actions["more"], "Longer answer (Ctrl+Alt+M)")
        # primary action as an accent pill
        ans = tk.Label(head, text="Answer", fg="#0f1115", bg=self.ACCENT, padx=10, pady=2,
                       font=(self.ui_bold, fs - 2), cursor="hand2")
        ans.bind("<Button-1>", lambda e: actions["answer"]())
        ans.bind("<Enter>", lambda e: ans.configure(bg="#9cc0ff"))
        ans.bind("<Leave>", lambda e: ans.configure(bg=self.ACCENT))
        ans.pack(side="right", padx=(4, 6))
        self._tooltip(ans, "Answer what they just said (Ctrl+Alt+A)")
        self.set_auto(self.auto_on)
        self.set_log(self.log_on, announce=False)

        body = tk.Frame(root, bg=self.BG)
        body.pack(fill="both", expand=True, padx=10)

        # ---- answer card
        card = tk.Frame(body, bg=self.CARD, highlightthickness=1, highlightbackground=self.LINE)
        card.pack(fill="both", expand=True, pady=(0, 6))
        self.answer_card = card
        self._build_setup(body, fs)
        self.answer = tk.Text(card, bg=self.CARD, fg=self.FG, wrap="word", relief="flat", bd=0,
                              highlightthickness=0, padx=14, pady=12, font=(self.ui_font, fs + 1),
                              insertbackground=self.CARD, cursor="arrow", spacing1=2, spacing2=3, spacing3=6,
                              selectbackground="#2b3446", height=12)
        self.answer.pack(fill="both", expand=True)
        self.answer.tag_configure("q", foreground=self.DIM, font=(self.ui_font, fs - 2), spacing3=8)
        self.answer.tag_configure("meta", foreground=self.FAINT, font=(self.ui_font, fs - 3), spacing1=10)
        self.answer.tag_configure("draft", foreground=self.DRAFT)
        self.answer.tag_configure("final", foreground=self.FG)
        self.answer.tag_configure("unverified", foreground=self.WARN, underline=True)
        self.answer.insert("end", "Listening. Answers show up here when they ask you something.\n", "q")
        self.answer.mark_set("body", "end-1c")
        self.answer.mark_gravity("body", "left")
        self.tip = tk.Label(card, text="", fg=self.ACCENT, bg=self.CARD, font=(self.ui_font, fs - 1),
                            anchor="w", justify="left", wraplength=o["width"] - 60, padx=14)
        self.tip.pack(fill="x", pady=(0, 8))
        card.bind("<Configure>", lambda e: self.tip.configure(wraplength=max(200, e.width - 40)))

        # ---- transcript card (collapsible)
        tcard = tk.Frame(body, bg=self.CARD, highlightthickness=1, highlightbackground=self.LINE)
        tcard.pack(fill="x", pady=(0, 6))
        thead = tk.Frame(tcard, bg=self.CARD, cursor="hand2")
        thead.pack(fill="x", padx=12, pady=(6, 2))
        tl = tk.Label(thead, text="TRANSCRIPT", fg=self.FAINT, bg=self.CARD, font=(self.ui_bold, fs - 4))
        tl.pack(side="left")
        self.trans_chev = self._glyph_label(thead, "up", self.FAINT, self.CARD, fs - 3)
        self.trans_chev.pack(side="right")
        for w in (thead, tl, self.trans_chev):
            w.bind("<Button-1>", lambda e: self.toggle_transcript())
        self.trans = tk.Text(tcard, bg=self.CARD, fg=self.DIM, wrap="word", relief="flat", bd=0,
                             highlightthickness=0, padx=12, pady=4, font=(self.ui_font, fs - 2), height=5,
                             cursor="arrow", spacing3=3, insertbackground=self.CARD)
        self.trans.pack(fill="x", pady=(0, 6))
        self.trans.tag_configure("Them", foreground=self.THEM, font=(self.ui_bold, fs - 2))
        self.trans.tag_configure("You", foreground=self.YOU, font=(self.ui_bold, fs - 2))
        self.trans_visible = True
        self.trans_label = tl

        # ---- footer: status + resize grip
        foot = tk.Frame(root, bg=self.BG)
        foot.pack(fill="x", padx=12, pady=(0, 6))
        self.status = tk.Label(foot, text="starting…", fg=self.FAINT, bg=self.BG, font=(self.ui_font, fs - 4),
                               anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.status_parts = {}
        grip = tk.Canvas(foot, width=12, height=12, bg=self.BG, highlightthickness=0,
                         cursor="size_nw_se" if IS_WINDOWS else "bottom_right_corner")
        for d in (4, 8):
            grip.create_line(12, d, d, 12, fill=self.FAINT)
        grip.pack(side="right")
        grip.bind("<ButtonPress-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize)
        grip.bind("<ButtonRelease-1>", lambda e: self.save_geometry())

        root.update_idletasks()
        if IS_WINDOWS:
            try:
                hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
                pref = ctypes.c_int(2)                                       # DWMWCP_ROUND: Win 11 rounded corners
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
                if cfg.get("hide_overlay_from_capture"):
                    ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE
            except Exception:
                pass
        root.after(30, self._poll)
        root.after(2000, self._keep_on_top)
        if cfg.get("ask_topic_on_start", True):
            root.after(250, self.show_setup)

    # ---- "what's this call about?"
    DEFAULT_TOPICS = ["College interview", "Alumni interview", "Debate practice", "Job interview",
                      "Class discussion"]

    def _state(self):
        try:
            return json.loads(self.STATE_FILE.read_text())
        except Exception:
            return {}

    def _save_state(self, **kv):
        st = self._state()
        st.update(kv)
        try:
            self.STATE_FILE.write_text(json.dumps(st))
        except Exception:
            pass

    def _build_setup(self, parent, fs):
        tk = self.tk
        f = tk.Frame(parent, bg=self.CARD, highlightthickness=1, highlightbackground=self.ACCENT)
        tk.Label(f, text="What's this call about?", fg=self.FG, bg=self.CARD, font=(self.ui_bold, fs),
                 anchor="w").pack(fill="x", padx=14, pady=(12, 2))
        tk.Label(f, text="e.g. State U interview, debate on social media",
                 fg=self.FAINT, bg=self.CARD, font=(self.ui_font, fs - 3), anchor="w").pack(fill="x", padx=14)
        box = tk.Frame(f, bg=self.BG, highlightthickness=1, highlightbackground=self.LINE,
                       highlightcolor=self.ACCENT)
        box.pack(fill="x", padx=14, pady=(8, 6))
        self.topic_entry = tk.Entry(box, bg=self.BG, fg=self.FG, insertbackground=self.FG, relief="flat", bd=0,
                                    font=(self.ui_font, fs), highlightthickness=0)
        self.topic_entry.pack(fill="x", padx=8, pady=6)
        self.topic_entry.bind("<Return>", lambda e: self._submit_topic(self.topic_entry.get()))
        self.topic_entry.bind("<Escape>", lambda e: self.hide_setup())
        self.chips = tk.Frame(f, bg=self.CARD)
        self.chips.pack(fill="x", padx=12, pady=(0, 4))
        foot = tk.Frame(f, bg=self.CARD)
        foot.pack(fill="x", padx=14, pady=(2, 10))
        tk.Label(foot, text="Enter to start  ·  Esc to skip", fg=self.FAINT, bg=self.CARD,
                 font=(self.ui_font, fs - 4)).pack(side="left")
        go = tk.Label(foot, text="Start", fg="#0f1115", bg=self.ACCENT, padx=12, pady=2,
                      font=(self.ui_bold, fs - 2), cursor="hand2")
        go.pack(side="right")
        go.bind("<Button-1>", lambda e: self._submit_topic(self.topic_entry.get()))
        self.setup = f

    def _fill_chips(self):
        tk = self.tk
        for w in self.chips.winfo_children():
            w.destroy()
        recent = self._state().get("topics") or self.DEFAULT_TOPICS
        fs = self.cfg["overlay"]["font_size"]
        row, used = tk.Frame(self.chips, bg=self.CARD), 0
        row.pack(fill="x")
        for t in recent[:6]:
            label = t if len(t) <= 30 else t[:28] + "…"
            if used + len(label) > 46:                      # wrap to a new row
                row, used = tk.Frame(self.chips, bg=self.CARD), 0
                row.pack(fill="x")
            c = tk.Label(row, text=label, fg=self.DIM, bg=self.LINE, padx=8, pady=2,
                         font=(self.ui_font, fs - 3), cursor="hand2")
            c.pack(side="left", padx=2, pady=2)
            c.bind("<Button-1>", lambda e, t=t: self._submit_topic(t))
            c.bind("<Enter>", lambda e, c=c: c.configure(fg=self.FG))
            c.bind("<Leave>", lambda e, c=c: c.configure(fg=self.DIM))
            used += len(label) + 3

    def show_setup(self):
        self._fill_chips()
        self.topic_entry.delete(0, "end")
        self.topic_entry.insert(0, CALL.get("topic", ""))
        if not self.setup.winfo_ismapped():
            self.setup.pack(fill="x", pady=(0, 6), before=self.answer_card)
        self.root.after(50, lambda: (self.root.focus_force(), self.topic_entry.focus_set(),
                                     self.topic_entry.select_range(0, "end")))

    def hide_setup(self):
        self.setup.pack_forget()

    def _submit_topic(self, text):
        text = re.sub(r"\s+", " ", text or "").strip()
        self.actions["set_topic"](text)
        if text:
            recent = [text] + [t for t in (self._state().get("topics") or []) if t.lower() != text.lower()]
            self._save_state(topics=recent[:8])
        short = text if len(text) <= 32 else text[:30] + "…"
        self.title_lbl.configure(text=short or "Copilot")
        self.hide_setup()

    # ---- small widgets
    def _glyph_label(self, parent, name, fg, bg, size):
        glyph, text = self.ICONS[name]
        font = (self.icon_font, size) if self.icon_font else (self.ui_font, size)
        return self.tk.Label(parent, text=glyph if self.icon_font else text, fg=fg, bg=bg, font=font)

    def icon_btn(self, parent, name, cmd, tip, hover_fg=None):
        fs = self.cfg["overlay"]["font_size"]
        b = self._glyph_label(parent, name, self.DIM, self.BG, fs - 2)
        b.configure(padx=7, pady=3, cursor="hand2")
        b.icon_name = name
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=self.LINE, fg=hover_fg or self.FG))
        b.bind("<Leave>", lambda e: b.configure(bg=self.BG, fg=getattr(b, "rest_fg", self.DIM)))
        b.pack(side="right", padx=1)
        self._tooltip(b, tip)
        return b

    def _set_glyph(self, btn, name):
        glyph, text = self.ICONS[name]
        btn.configure(text=glyph if self.icon_font else text)

    def _tooltip(self, widget, text):
        tk = self.tk
        state = {"win": None, "job": None}

        def show():
            if state["win"]:
                return
            w = tk.Toplevel(self.root)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            tk.Label(w, text=text, bg="#262b36", fg=self.FG, padx=8, pady=3,
                     font=(self.ui_font, self.cfg["overlay"]["font_size"] - 3)).pack()
            w.update_idletasks()
            self._hide_from_capture(w)                    # tooltips stay off screen shares too
            x = widget.winfo_rootx() + widget.winfo_width() // 2 - w.winfo_width() // 2
            w.geometry(f"+{max(0, x)}+{widget.winfo_rooty() + widget.winfo_height() + 6}")
            state["win"] = w

        def enter(_):
            state["job"] = self.root.after(450, show)

        def leave(_):
            if state["job"]:
                self.root.after_cancel(state["job"])
                state["job"] = None
            if state["win"]:
                state["win"].destroy()
                state["win"] = None

        widget.bind("<Enter>", enter, add="+")
        widget.bind("<Leave>", leave, add="+")
        widget.bind("<Button-1>", leave, add="+")

    def _hide_from_capture(self, win):
        if IS_WINDOWS and self.cfg.get("hide_overlay_from_capture"):
            try:
                hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
                ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)   # WDA_EXCLUDEFROMCAPTURE
            except Exception:
                pass

    def _set_dot(self, state):
        if not hasattr(self, "dot"):
            return
        color = {"listening": self.YOU, "thinking": self.WARN, "off": self.FAINT}[state]
        if state == "listening" and not getattr(self, "auto_on", True):
            color = self.FAINT
        self.dot.configure(fg=color)

    # ---- moving / resizing
    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _resize_start(self, e):
        self._rs = (e.x_root, e.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _resize(self, e):
        x0, y0, w0, h0 = self._rs
        w, h = max(320, w0 + e.x_root - x0), max(260, h0 + e.y_root - y0)
        self.root.geometry(f"{w}x{h}")

    def toggle_big(self):
        r = self.root
        if not self.big:
            self.small_geo = r.geometry()
            w, h = self.big_size
            x = min(r.winfo_x(), r.winfo_screenwidth() - w - 24)
            r.geometry(f"{w}x{h}+{max(0, x)}+60")
            self._set_glyph(self.size_btn, "small")
        else:
            r.geometry(self.small_geo)
            self._set_glyph(self.size_btn, "big")
        self.big = not self.big

    def toggle_transcript(self):
        if self.trans_visible:
            self.trans.pack_forget()
            self._set_glyph(self.trans_chev, "down")
        else:
            self.trans.pack(fill="x", pady=(0, 6))
            self._set_glyph(self.trans_chev, "up")
        self.trans_visible = not self.trans_visible

    def save_geometry(self):
        if self.big:
            return
        self._save_state(geometry=self.root.geometry())

    def _keep_on_top(self):
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        finally:
            self.root.after(2000, self._keep_on_top)

    def set_log(self, on, announce=True):
        self.log_on = on
        self.log_btn.rest_fg = "#ff7a7a" if on else self.FAINT      # red dot = saving this call
        self.log_btn.configure(fg=self.log_btn.rest_fg)
        if announce:
            self._set_status("saving", "saving this call" if on else "not saving this call")

    def set_auto(self, on):
        self.auto_on = on
        self.auto_btn.rest_fg = self.ACCENT if on else self.FAINT
        self.auto_btn.configure(fg=self.auto_btn.rest_fg)
        self._set_dot("listening")

    def toggle_visible(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
        else:
            self.root.withdraw()

    def _set_status(self, key, text):
        self.status_parts[key] = text
        self.status.configure(text="  ·  ".join(v for v in self.status_parts.values() if v)[-140:])

    # ---- answer rendering
    def _begin_answer(self):
        self.answer.delete("1.0", "end")
        self.answer.insert("end", f"{self.pending_q[-160:]}\n", "q")
        self.answer.mark_set("body", "end-1c")
        self.pending_q, self.hold = None, None
        self.raw, self.final_raw = "", None
        self.tip.configure(text="")

    def _render(self, text, tag):
        self.answer.delete("body", "end-1c")          # (not "end": Tk would eat the line break before it)
        self.answer.insert("end", clean_answer(text), tag)
        self.answer.see("end")

    def _poll(self):
        try:
            for _ in range(200):
                ev = self.ui_q.get_nowait()
                kind = ev[0]
                if kind == "transcript":
                    _, who, text = ev
                    tag = "Them" if who == "Them" else "You"
                    self.trans.insert("end", f"{tag}: ", tag)
                    self.trans.insert("end", text + "\n")
                    lines = int(self.trans.index("end-1c").split(".")[0])
                    if lines > 60:
                        self.trans.delete("1.0", f"{lines - 60}.0")
                    self.trans.see("end")
                elif kind == "answer_start":
                    # keep the previous answer on screen until the new one's first words arrive
                    _, self.gen, self.pending_q = ev
                    self.hold, self.tip_gen = None, None
                    self._set_status("ans", "thinking…")
                    self._set_dot("thinking")
                elif kind == "answer_delta":
                    if ev[1] == self.gen and self.tip_gen == self.gen:
                        self.tip_text += ev[2]
                        self.tip.configure(text=clean_answer(self.tip_text))
                    elif ev[1] == self.gen:
                        if self.pending_q is not None:
                            # hold the first characters: a "Tip:" goes to the tip line, not over your answer
                            self.hold = (self.hold or "") + ev[2]
                            if len(self.hold.strip()) < 4:
                                continue
                            if self.hold.strip().lower().startswith("tip"):
                                self.tip_gen, self.tip_text = self.gen, self.hold.strip()
                                self.tip.configure(text=clean_answer(self.tip_text))
                                self.pending_q, self.hold = None, None
                                continue
                            ev = (kind, ev[1], self.hold)
                            self._begin_answer()
                        self.raw += ev[2]
                        self._render(self.raw, "draft" if self.verifying else "final")
                elif kind == "answer_checking":
                    if ev[1] == self.gen:
                        self._set_status("ans", "double-checking with your notes…")
                elif kind == "answer_replace":                # corrected version after the fact check
                    if ev[1] == self.gen and self.tip_gen != self.gen:
                        self.raw = ev[2]
                        self._render(self.raw, "final")
                elif kind == "final_delta":
                    if ev[1] == self.gen:
                        self.final_raw = (self.final_raw or "") + ev[2]
                        self._render(self.final_raw, "final")
                elif kind == "answer_done":
                    if ev[1] == self.gen:
                        self._set_dot("listening")
                    if ev[1] == self.gen and self.tip_gen == self.gen:
                        self._set_status("ans", "")
                    elif ev[1] == self.gen:
                        if self.pending_q is not None and self.hold:   # tiny answer still held back
                            held = self.hold
                            self._begin_answer()
                            self.raw = held
                            self._render(self.raw, "final")
                        self._set_status("ans", "")
                        self.pending_q = None              # (if it failed before any words, old answer stays)
                        if self.final_raw is None and self.raw:
                            self._render(self.raw, "final")
                        flags = ev[3] if len(ev) > 3 else []
                        for f in flags:                     # highlight details not found in your notes
                            start = "body"
                            while True:
                                pos = self.answer.search(f, start, stopindex="end")
                                if not pos:
                                    break
                                end = f"{pos}+{len(f)}c"
                                self.answer.tag_add("unverified", pos, end)
                                start = end
                        self.answer.insert("end", f"\n{ev[2]}", "meta")
                        self.answer.see("end")
                elif kind == "status":
                    self._set_status(ev[1], ev[2])
                elif kind == "log":
                    self._set_status("log", ev[1])
                elif kind == "action":
                    self.actions[ev[1]]()
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    def run(self):
        self.root.mainloop()


# ----------------------------------------------------------------------------- main

def main():
    load_env(APP_DIR / ".env")
    cfg = load_config()
    ui_q = queue.Queue()

    logf = open(APP_DIR / "copilot.log", "w", encoding="utf-8")   # timings + errors only, never what was said

    def log(msg, quiet=False):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        try:
            logf.write(line + "\n")
            logf.flush()
        except Exception:
            pass
        if not quiet:
            ui_q.put(("log", str(msg)[:120]))

    if IS_WINDOWS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    if cfg["transcriber"] == "groq" and not api_key("groq"):
        raise RuntimeError("Missing GROQ_API_KEY: put it in the .env file next to copilot.py "
                           "(or set \"transcriber\": \"local\" in config.json).")

    resolve_models(cfg, log)

    stop = threading.Event()
    vault = Vault(cfg, log)
    threading.Thread(target=lambda: (vault.build(), vault.watch(stop)), daemon=True).start()

    screen = None
    if cfg["screen_watch"] and cfg["vision_models"]:
        screen = ScreenWatcher(cfg, ui_q, log)
        screen.start()
    elif cfg["screen_watch"]:
        log("Screen watch off (no vision model available on your key)")

    brain = Brain(cfg, vault, screen, ui_q, log)
    session = SessionLog(cfg, log)
    brain.session = session
    brain.past = load_past_answers(session.dir)
    brain.past_lines = load_past_lines(session.dir)
    if brain.past:
        log(f"Memory: {len(brain.past)} answers from past calls")
    seg_q = queue.Queue()
    Transcriber(seg_q, brain.on_transcript, cfg, log).start()
    AudioSource("Them", "loopback", seg_q, cfg, log).start()
    if cfg["listen_to_mic"]:
        AudioSource("You", "mic", seg_q, cfg, log).start()

    overlay = None

    def toggle_auto():
        brain.auto = not brain.auto
        overlay.set_auto(brain.auto)

    def quit_app():
        stop.set()
        overlay.save_geometry()
        session.close()
        overlay.root.destroy()
        os._exit(0)

    actions = {
        "answer": lambda: brain.answer(),
        "more": brain.more,
        "set_topic": brain.set_topic,
        "topic": lambda: overlay.show_setup(),
        "toggle_log": lambda: overlay.set_log(session.toggle()),
        "bigger": lambda: overlay.toggle_big(),
        "toggle_auto": toggle_auto,
        "screen": lambda: screen.force() if screen else log("Screen watch is off"),
        "hide": lambda: overlay.toggle_visible(),
        "quit": quit_app,
    }
    overlay = Overlay(cfg, ui_q, actions, has_screen=screen is not None)

    try:
        from pynput import keyboard
        hk = {combo: (lambda n=name: ui_q.put(("action", n))) for name, combo in cfg["hotkeys"].items()}
        keyboard.GlobalHotKeys(hk).start()
    except Exception as e:
        log(f"Hotkeys off: {e}")

    overlay.run()


def _fatal(err):
    """Opened without a console (taskbar shortcut / pythonw): don't fail silently."""
    import traceback
    text = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    try:
        with open(APP_DIR / "copilot.log", "a", encoding="utf-8") as f:
            f.write("\nFATAL: " + text)
    except Exception:
        pass
    if sys.stdout is not None and sys.stdout.isatty():
        print(text)
        input("Press Enter to exit…")
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("Meeting Copilot", f"{err}\n\nDetails are in copilot.log.")
        r.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        _fatal(e)
