"""
main.py — StreamBee orchestrator.

Reuses StreamTTS modules unchanged (copy them into src/):
  twitch.py, auth.py, eventsub.py, tts.py, voices.py, events.py, cooldown.py
New here: bee.py (BeeLLM driver), overlay.py (browser source server).
"""

import random
import sys
import threading
import time
import tomllib
from pathlib import Path

# Embeddable Python (._pth mode) doesn't put the script's folder on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bee import BeeBrain, apply_mood, interpret, match_action
from overlay import OverlayServer

from cooldown import Cooldown
from events import split_sfx, strip_sfx
from tts import PiperEngine, Speaker
from twitch import TwitchChat
from voices import VoicePool


def _fmt(template, **vals):
    # Never str.format() a template with {sfx "..."} tags — manual replace instead.
    for k, v in vals.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def main():
    if "--list-devices" in sys.argv:
        import sounddevice as sd
        print(sd.query_devices())
        return

    root = Path(__file__).resolve().parent.parent
    with open(root / "config.toml", "rb") as f:
        cfg = tomllib.load(f)

    # --- Voice: one fixed Piper voice for the bee ---
    vcfg = cfg.get("voice", {})
    pool = VoicePool(
        folder=str((root / vcfg.get("folder", "voices")).resolve()),
        default=vcfg.get("file", ""),
        mode="default",
    )
    voice = pool.default
    engine = PiperEngine(
        length_scale=float(vcfg.get("length_scale", 1.1)),
        pitch_semitones=float(vcfg.get("pitch_semitones", -3.0)),
    )
    speaker = Speaker(
        engine,
        output_device=int(cfg.get("audio", {}).get("output_device", -1)),
        sfx_dir=str((root / cfg.get("sfx", {}).get("folder", "sfx")).resolve()),
        max_queue=int(cfg.get("queue", {}).get("max", 5)),
    )

    # --- Overlay + brain ---
    ocfg = cfg.get("overlay", {})
    overlay = OverlayServer(
        root / "overlay",
        port=int(ocfg.get("port", 8765)),
        settings={
            "size": int(ocfg.get("bee_size", 64)),
            "speed": float(ocfg.get("speed", 1.0)),
            "image": ocfg.get("image", ""),
            "bubbles": bool(ocfg.get("bubbles", True)),
            "flowerSeconds": float(ocfg.get("flower_seconds", 30)),
            "pollinateSeconds": float(ocfg.get("pollinate_seconds", 5)),
            "maxFlowers": int(ocfg.get("max_flowers", 4)),
        },
    )
    overlay.start()

    moods = {k.lower(): v for k, v in cfg.get("moods", {}).items() if isinstance(v, dict)}
    rules = cfg.get("emotes", {}).get("rule", [])
    mcfg = dict(cfg.get("model", {}))
    scfg = cfg.get("speech", {})
    mcfg.setdefault("max_chars", int(scfg.get("max_chars", 90)))
    mcfg.setdefault("speak_actions", bool(scfg.get("speak_actions", True)))
    mcfg.setdefault("show_actions", bool(scfg.get("show_actions", True)))
    mcfg.setdefault("mood_seconds", float(cfg.get("speech", {}).get("mood_seconds", 90)))
    brain = BeeBrain(root, mcfg, mood_words=moods.keys())
    brain.start()
    mood_seconds = float(cfg.get("speech", {}).get("mood_seconds", 90))
    state = {"last_action": 0.0}

    def speak(line, bubble=None):
        seq = []
        for kind, val in split_sfx(line):
            if kind == "text" and val.strip():
                seq.append(("text", val.strip()))
            elif kind == "sfx":
                seq.append(("sfx", val))
        if seq:
            speaker.enqueue_sequence(seq, voice)
        if bubble:
            overlay.send({"type": "say", "text": bubble})

    def react(said, action="auto", spec=None, user="", remember=True):
        """Say something to the bee, move it, and return the parsed Thought."""
        spec = spec or {}
        th = brain.think(said, remember)
        mood, level = brain.current_mood()
        beh = apply_mood(interpret(th.raw), mood, level, moods)
        if th.mood:
            overlay.send({"type": "mood", "mood": th.mood, "level": th.level,
                          "seconds": mood_seconds})
        if action == "auto":                     # let the bee's own words decide
            rule = match_action(th.raw, rules)
            if rule:
                action, spec = rule.get("action", "fly"), {**rule, **spec}
            else:
                action = "land" if beh.land else spec.get("fallback", "buzz")
        ev = {"type": action, "energy": beh.energy, "wander": beh.wander,
              "duration": beh.duration, "user": user}
        if action in ("shoo", "swarm", "dance"):
            ev["seconds"] = float(spec.get("seconds", 15))
        if action == "swarm":
            ev["count"] = int(spec.get("count", 6))
        overlay.send(ev)
        print(f"[Bee] {action} ({user or 'idle'}) <- {said!r} :: {th.raw.strip()!r}"
              + (f"  [mood {th.mood}]" if th.mood else ""))
        return th

    def do_action(spec, user="", message=""):
        message = strip_sfx(message or "").strip()[:120]
        said = _fmt(spec.get("prompt", "{message}"), user=user, message=message).strip()
        th = react(said or spec.get("empty_prompt", "*waves*"),
                   spec.get("action", "auto"), spec, user)
        tpl = spec.get("template", "")
        if tpl:
            speak(_fmt(tpl, user=strip_sfx(user), buzz=th.speech))
        overlay.send({"type": "say", "text": th.bubble})   # always show how it reacted
        state["last_action"] = time.time()

    # --- Idle buzzing ---
    icfg = cfg.get("idle", {})
    lo = float(icfg.get("min_seconds", 8))
    hi = float(icfg.get("max_seconds", 25))
    speak_chance = float(icfg.get("speak_chance", 0.15))
    sound_chance = float(icfg.get("sound_chance", 0.5))
    idle_prompts = icfg.get("prompts", ["*buzzes around*"])
    stop = threading.Event()

    def idle_loop():
        while not stop.wait(random.uniform(lo, hi)):
            if time.time() - state["last_action"] < lo:
                continue  # let a chat-triggered action play out
            th = react(random.choice(idle_prompts), "auto", {"fallback": "fly"}, remember=False)
            r = random.random()
            if r < speak_chance:
                speak(_fmt(icfg.get("speak_template", "{buzz}"), buzz=th.speech),
                      bubble=th.bubble)
            elif r < speak_chance + sound_chance:
                speak(icfg.get("sfx_template", '{sfx "buzz_short.wav"}'))

    threading.Thread(target=idle_loop, daemon=True).start()

    # --- Chat commands, redeems, reactions ---
    ccfg = cfg.get("commands", {})
    prefix = ccfg.get("prefix", "!")
    default_cd = float(ccfg.get("cooldown", 10))
    commands = {a["name"].lower(): a for a in ccfg.get("action", [])}
    cooldowns = {n: Cooldown(float(a.get("cooldown", default_cd))) for n, a in commands.items()}
    rewards = {r["title"].lower(): r for r in cfg.get("redeems", {}).get("reward", [])}
    reactions = {k.lower(): v for k, v in cfg.get("reactions", {}).items()
                 if isinstance(v, dict) and v.get("enabled", True)}

    def on_event(ev):
        if ev.kind == "command":
            name = (ev.command or "").lower().lstrip(prefix)
            spec = commands.get(name)
            if not spec:
                return
            if not cooldowns[name].ready(name):
                print(f"[Command] !{name} on cooldown ({cooldowns[name].remaining(name):.0f}s)")
                return
        elif ev.kind == "redeem":
            spec = rewards.get((ev.reward_title or "").lower())
            if not spec:
                return
        else:
            spec = reactions.get(ev.kind)  # sub, resub, raid, cheer, ...
            if not spec:
                return
        do_action(spec, getattr(ev, "user", "") or "", getattr(ev, "message", "") or "")

    filters = cfg.get("filters", {})
    tw = TwitchChat(
        channel=cfg["twitch"]["channel"],
        on_event=on_event,
        ignore_users=filters.get("ignore_users", []),
        max_chars=int(filters.get("max_chars", 300)),
        command_prefix=prefix,
    )
    tw.start()

    es = None
    if cfg.get("redeems", {}).get("enabled") and rewards:
        from auth import TwitchAuth
        auth = TwitchAuth(str(root / "token.json"))
        if auth.ensure():
            from eventsub import EventSubClient
            es = EventSubClient(auth, on_event)
            es.start()
            print(f"[Redeems] listening for #{auth.login}")
        else:
            print("[Redeems] disabled — authorization not completed.")

    print("[Main] StreamBee is buzzing. Ctrl+C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Main] Shutting down.")
    finally:
        stop.set()
        tw.stop()
        speaker.stop()
        brain.stop()
        overlay.stop()


if __name__ == "__main__":
    main()
