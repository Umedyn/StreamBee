"""
bee.py — BeeLLM driver for StreamBee.

Runs llama-server with a BeeLLM .gguf as a hidden subprocess (or talks to a server
you already run, e.g. llama-server or KoboldCpp), says something to the bee, and
turns the reply into a Thought:
  - speech  : what Piper reads aloud (tags and *emotes* removed)
  - mood    : parsed from tags like [sad:high], tolerant of malformed ones
  - emotes  : *droops antennae* etc., used to pick an overlay action

Falls back to procedural buzzing if no model is available.
"""

import atexit
import json
import os
import random
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Behavior:
    energy: float    # 0..1  how fast / excited
    wander: float    # 0..1  how erratic the flight path is
    duration: float  # seconds this burst of flight lasts
    land: bool       # settle down and perch afterwards


@dataclass
class Thought:
    raw: str
    speech: str      # what Piper reads: buzzing plus narrated actions ("stings you.")
    bubble: str      # what the overlay shows: actions kept as *stings you*
    mood: str = ""
    level: float = 1.0
    emotes: list = field(default_factory=list)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# [sad:high], [happy:low] ... also catches malformed "!sad:low]" or "[mad:high"
_MOOD = re.compile(r"\[?\s*([A-Za-z]+)\s*:\s*(low|mid|med|medium|high)\s*\]?", re.I)
_EMOTE = re.compile(r"\*([^*\n]{1,60})\*")
_LEVELS = {"low": 0.6, "mid": 1.0, "med": 1.0, "medium": 1.0, "high": 1.4}
_KEEP = re.compile(r"[^A-Za-z .,!?'\-]+")


def _clean(seg: str) -> str:
    s = _KEEP.sub(" ", seg.replace("*", " "))           # stray ] [ * digits etc.
    s = re.sub(r"(?i)([a-z])\1{4,}", r"\1\1\1\1", s)   # BZZZZZZZZZ -> BZZZZ
    s = re.sub(r"[A-Za-z]{15,}", lambda m: m.group(0)[:10], s)  # zZzZzZzZzZzZzZz -> 10 chars
    return re.sub(r"\s+", " ", s).strip(" ,")


def _trim(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return cut.rstrip(" .,!?") + "..."


def parse(raw: str, max_chars: int = 90, mood_words=(),
          speak_actions: bool = True, show_actions: bool = True) -> Thought:
    text = raw or ""
    mood, level = "", 1.0
    tags = _MOOD.findall(text)
    if tags:
        mood, lv = tags[-1]                      # last tag wins
        mood, level = mood.lower(), _LEVELS.get(lv.lower(), 1.0)
    else:                                        # bare "sad..." with no tag = weak mood
        for w in reversed(re.findall(r"[a-z]+", text.lower())):
            if w in mood_words:
                mood, level = w, 0.6
                break
    emotes = [e.strip() for e in _EMOTE.findall(text)]

    # split into ordered ("buzz", text) / ("act", text) pieces, tags removed
    body = _MOOD.sub(" ", text)
    parts, last = [], 0
    for m in _EMOTE.finditer(body):
        pre, act = _clean(body[last:m.start()]), _clean(m.group(1))
        if pre:
            parts.append(("buzz", pre))
        if act:
            parts.append(("act", act))
        last = m.end()
    tail = _clean(body[last:])
    if tail:
        parts.append(("buzz", tail))

    # keep pieces in order until the length budget runs out
    kept, budget = [], max_chars
    for kind, t in parts:
        if budget <= 3:
            break
        t = _trim(t, budget)
        kept.append((kind, t))
        budget -= len(t) + 1

    speech, bubble = [], []
    for kind, t in kept:
        if kind == "act":
            if speak_actions:                    # lowercase so Piper doesn't spell "STINGS YOU"
                spoken = t.lower()
                speech.append(spoken if spoken[-1] in ".!?" else spoken + ".")
            if show_actions:
                bubble.append(f"*{t}*")
        else:
            speech.append(t)
            bubble.append(t)

    return Thought(raw=text, speech=" ".join(speech) or "bzz...",
                   bubble=" ".join(bubble) or "bzz...",
                   mood=mood, level=level, emotes=emotes)


def interpret(text: str) -> Behavior:
    """Read the bee's energy out of the shape of its output."""
    t = (text or "").strip()
    n = len(t)
    low = t.lower()
    z_runs = re.findall(r"z{2,}", low)
    longest_z = max((len(r) for r in z_runs), default=0)
    caps = sum(c.isupper() for c in t) / max(1, n)
    words = [len(w) for w in t.split()] or [0]
    mean = sum(words) / len(words)
    spread = (sum((w - mean) ** 2 for w in words) / len(words)) ** 0.5

    energy = _clamp(0.25 + longest_z / 16 + t.count("!") * 0.12 + caps * 0.4, 0.2, 1.0)
    wander = _clamp(0.15 + spread / 4 + len(words) * 0.06, 0.1, 1.0)
    duration = _clamp(1.5 + n / 10, 2.0, 10.0)
    land = n < 4 or t.endswith("...")
    return Behavior(energy, wander, duration, land)


def apply_mood(beh: Behavior, mood: str, level: float, table: dict) -> Behavior:
    """Pull the flight behavior toward the mood's settings in config [moods.*]."""
    m = table.get(mood) if mood else None
    if not m:
        return beh
    pull = _clamp(0.6 * level, 0.0, 1.0)
    if "energy" in m:
        beh.energy = _clamp(beh.energy + (float(m["energy"]) - beh.energy) * pull, 0.1, 1.0)
    if "wander" in m:
        beh.wander = _clamp(beh.wander + (float(m["wander"]) - beh.wander) * pull, 0.1, 1.0)
    if random.random() < float(m.get("land_chance", 0)) * level:
        beh.land = True
    return beh


def match_action(text: str, rules: list):
    """First [[emotes.rule]] whose words appear anywhere in the reply (emote or bare)."""
    low = (text or "").lower()
    for r in rules:
        for word in r.get("match", []):
            if word.lower() in low:
                return r
    return None


class BeeBrain:
    def __init__(self, root: Path, cfg: dict, mood_words=()):
        self.root = Path(root)
        self.mood_words = {w.lower() for w in mood_words}
        self.cfg = cfg
        self.mode = cfg.get("mode", "auto")           # auto | server | off
        self.api = cfg.get("api", "chat")             # chat | completion
        self.url = (cfg.get("server_url") or "").rstrip("/")
        self.template = cfg.get("prompt_template", "{input}")
        self.system = cfg.get("system", "")
        self.max_tokens = int(cfg.get("max_tokens", 40))
        self.temperature = float(cfg.get("temperature", 0.9))
        self.timeout = float(cfg.get("timeout", 4.0))
        self.history_turns = int(cfg.get("history_turns", 3))
        self.max_chars = int(cfg.get("max_chars", 90))
        self.speak_actions = bool(cfg.get("speak_actions", True))
        self.show_actions = bool(cfg.get("show_actions", True))
        self.history = []
        self.mood, self.level = "", 1.0
        self.mood_seconds = float(cfg.get("mood_seconds", 90))
        self._mood_until = 0.0
        self._proc = None
        self.online = False
        self._warned = False

    # ---------- lifecycle ----------
    def start(self):
        if self.mode == "off":
            print("[Bee] model off — using procedural buzzing")
            return

        if not self.url:
            if self.mode == "server":
                print("[Bee] mode = 'server' but no server_url set — using procedural buzzing")
                return
            binary = self._find_binary()
            model = self.root / self.cfg.get("model", "models/BeeLLM-2.0-12M-Mood.gguf")
            if binary is None or not model.is_file():
                missing = "llama-server (bin/ or PATH)" if binary is None else model
                print(f"[Bee] {missing} not found — using procedural buzzing")
                return
            port = int(self.cfg.get("port", 8081))
            self._launch(binary, model, port, int(self.cfg.get("ctx", 1024)))
            self.url = f"http://127.0.0.1:{port}"

        self.online = self._wait_ready(float(self.cfg.get("startup_timeout", 20)))
        if self.online:
            print(f"[Bee] BeeLLM ready at {self.url} ({self.api} api)")
        else:
            print("[Bee] BeeLLM didn't come up — using procedural buzzing")
            self.stop()

    def _find_binary(self):
        """Configured path if set, else bin/llama-server(.exe), else llama-server on PATH."""
        exe = "llama-server.exe" if os.name == "nt" else "llama-server"
        configured = self.cfg.get("binary", "")
        candidates = [self.root / configured] if configured else []
        candidates.append(self.root / "bin" / exe)
        for c in candidates:
            if c.is_file():
                if os.name != "nt" and not os.access(c, os.X_OK):
                    try:
                        os.chmod(c, c.stat().st_mode | 0o111)   # unzipped builds lose +x
                    except OSError:
                        continue
                return c
        found = shutil.which(exe)                               # e.g. brew install llama.cpp
        return Path(found) if found else None

    def _launch(self, binary, model, port, ctx):
        args = [str(binary), "-m", str(model), "-c", str(ctx),
                "--host", "127.0.0.1", "--port", str(port), "-ngl", "0"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, creationflags=flags)
        atexit.register(self.stop)
        print(f"[Bee] starting {binary.name} with {model.name} on port {port}")

    def _wait_ready(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if self._proc and self._proc.poll() is not None:
                return False
            for path in ("/health", "/v1/models"):   # llama-server / KoboldCpp
                try:
                    with urllib.request.urlopen(self.url + path, timeout=1) as r:
                        if r.status == 200:
                            return True
                except Exception:
                    pass
            time.sleep(0.5)
        return False

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
        self._proc = None

    # ---------- thinking ----------
    def current_mood(self):
        if self.mood and time.time() > self._mood_until:
            self.mood, self.level = "", 1.0
        return self.mood, self.level

    def think(self, said: str, remember: bool = True) -> Thought:
        """Say something to the bee ('*holds flower*', a viewer's message...) and parse the reply.
        remember=False (idle chatter) neither reads nor writes the conversation history,
        so background buzzing can't snowball a mood."""
        raw = ""
        if self.online:
            try:
                raw = (self._chat(said, remember) if self.api == "chat"
                       else self._complete(said))
            except Exception as e:
                if not self._warned:
                    print(f"[Bee] BeeLLM request failed ({e}) — falling back for now")
                    self._warned = True
        if not raw.strip():
            raw = self._fallback()
        elif remember:
            self.history += [{"role": "user", "content": said},
                             {"role": "assistant", "content": raw}]
            self.history = self.history[-2 * self.history_turns:] if self.history_turns else []

        th = parse(raw, self.max_chars, self.mood_words, self.speak_actions, self.show_actions)
        if th.mood:
            self.mood, self.level = th.mood, th.level
            self._mood_until = time.time() + self.mood_seconds
        return th

    def _post(self, path, payload):
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def _chat(self, said, remember=True):
        msgs = ([{"role": "system", "content": self.system}] if self.system else [])
        msgs += (self.history if remember else []) + [{"role": "user", "content": said}]
        out = self._post("/v1/chat/completions", {
            "messages": msgs, "max_tokens": self.max_tokens,
            "temperature": self.temperature})
        return out["choices"][0]["message"]["content"]

    def _complete(self, said):
        out = self._post("/completion", {
            "prompt": self.template.replace("{input}", said),
            "n_predict": self.max_tokens, "temperature": self.temperature,
            "cache_prompt": False})
        return out.get("content", "")

    @staticmethod
    def _fallback() -> str:
        words = []
        for _ in range(random.randint(1, 4)):
            w = "b" + "z" * random.randint(2, 7)
            if random.random() < 0.15:
                w = w.upper()
            words.append(w)
        s = " ".join(words) + random.choice(["", "!", "...", "."])
        if random.random() < 0.25:   # keeps the mood path exercised without a model
            s += f"[{random.choice(['happy', 'sad', 'mad', 'sleepy'])}:{random.choice(['low', 'high'])}]"
        return s
