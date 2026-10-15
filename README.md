# StreamBee

A little bee that lives on your stream. It buzzes around an OBS browser source, reacts
to chat commands, channel point redeems, subs and raids, and narrates what it's doing
in a low Piper voice, with speech bubbles on screen. Its brain is
[BeeLLM](https://huggingface.co/schizophyllume), a tiny model that only speaks bee and
has moods of its own. Everything runs locally.

---

## Quick start (Windows)

1. **Unzip** the whole folder anywhere.
2. **Add a voice.** Drop one Piper voice (`.onnx` **and** its `.onnx.json`) into `voices\`.
   A deeper voice suits the bee. Voices: <https://huggingface.co/rhasspy/piper-voices/tree/main>
3. **Add the brain** (optional, but it's what gives the bee moods and reactions):
   - Put `BeeLLM-2.0-12M-Mood.gguf` in `models\`.
   - From <https://github.com/ggml-org/llama.cpp/releases>, download the Windows **CPU** zip
     (`llama-bXXXX-bin-win-cpu-x64.zip`) and copy `llama-server.exe` **and every `.dll` next
     to it** into `bin\`.
   - Without these the bee still works, using simple procedural buzzing.
4. **Set your channel.** Open `config.toml` and change `channel` under `[twitch]`.
5. **Run** `run.bat`.
6. **Add it to OBS:** Sources → **+** → **Browser**
   - **URL:** `http://127.0.0.1:8765/` (leave **Local file** unticked)
   - **Width / Height:** your canvas size, e.g. 1920 × 1080

The console shows what the bee hears and how it reacts:

```
[Bee] BeeLLM ready at http://127.0.0.1:8081 (chat api)
[Bee] buzz (viewer) <- 'would you like a flower?' :: '[confused:low] buzz??'  [mood confused]
```

### Mac / Linux

Use `run.sh` with a system Python 3.11+ (the bundled `python\` folder is Windows-only).
For the brain, either put the macOS/Ubuntu `llama-server` build in `bin/` or install
llama.cpp with your package manager (`brew install llama.cpp`); StreamBee finds it on your
PATH. Linux also needs PortAudio (`libportaudio2` on Debian/Ubuntu). On macOS, if a
downloaded `llama-server` is blocked: `xattr -d com.apple.quarantine bin/llama-server`.

---

## What viewers can do

| Command                | What happens                                                    |
|------------------------|-----------------------------------------------------------------|
| `!bee <anything>`      | says it to the bee; it reacts however it feels like             |
| `!pat`                 | pats the bee's head                                             |
| `!flower`              | a flower grows and the bee goes to it (it may not appreciate it) |
| `!dance`               | plays music; figure-eight dance                                 |
| `!shoo`                | the bee leaves for a while, then comes back                     |

Raids bring a swarm, and subs make it dance. Redeems are off by default (see below).

---

## How the bee behaves

- **It reacts, not chats.** BeeLLM replies with actions and buzzing, like
  `*STINGS YOU* BZZZZ`. The bee **says** the actions out loud ("stings you.") and shows
  them in its bubble in *amber italics*.
- **Its actions move it.** With `action = "auto"`, words in its reply pick the move:
  stings/attacks/bites → dive, hides → flies off briefly, droops/rests → lands,
  dances/spins → dances. Edit `[[emotes.rule]]` to change or add these.
- **It has moods.** Replies are tagged like `[angry:high]`. A mood changes how it flies
  (`[moods.*]` in config) and how it looks: angry or mad flushes orange and scowls, sad or
  sleepy droops its antennae, happy floats hearts, scared trembles. Moods fade after
  `mood_seconds`.
- **It remembers a little.** It keeps the last `history_turns` viewer exchanges, so a
  grudge can last a few messages. Idle buzzing is never remembered.
- **It does its own thing** between commands: flying, landing, and every so often saying
  something (`[idle]`).

---

## Configuration (`config.toml`)

| Section                | What it controls                                                        |
|------------------------|-------------------------------------------------------------------------|
| `[voice]`              | Piper voice file, speed (`length_scale`), pitch (`pitch_semitones`)     |
| `[speech]`             | max reply length, whether actions are spoken/shown, mood duration       |
| `[model]`              | which `.gguf`, llama-server location, or `server_url` for your own server |
| `[overlay]`            | port, bee size, speed, bubbles on/off, custom sprite                    |
| `[idle]`               | how often it acts on its own and how often it talks                     |
| `[moods.*]`            | how each mood changes flight (`energy`, `wander`, `land_chance`)        |
| `[[emotes.rule]]`      | which words in its replies trigger which moves                          |
| `[[commands.action]]`  | chat commands: `prompt` (what the viewer does), `action`, `template`    |
| `[redeems]`            | channel point rewards (one-time Twitch login, same as StreamTTS)        |
| `[reactions.*]`        | raid / sub / resub reactions                                            |

Templates can use `{user}`, `{buzz}` (the bee's reply) and `{sfx "file.wav"}`. Use **single
quotes** in TOML so the inner double quotes survive: `template = '{sfx "pop.wav"} {buzz}'`.
Viewers can't trigger sounds; sfx tags are stripped from anything they type.

**Using a server you already run** (KoboldCpp, your own llama-server): set
`server_url = "http://127.0.0.1:5001"`. If replies look worse than in that server's own UI,
try `api = "completion"` and set `prompt_template` to the format it uses.

---

## Sounds and sprite

- Sounds live in `sfx\`. Replace any of them, or regenerate the defaults with
  `python tools\make_sfx.py` (they're generated, so there are no licensing worries).
- For a custom bee, put a PNG in `overlay\` and set `image = "yourbee.png"` under `[overlay]`.

---

## Troubleshooting

**The bee flies but never reacts, or there are no bubbles.** Add `?debug` to the browser
source URL (`http://127.0.0.1:8765/?debug`). The panel shows the page address, the event
stream, how many events have arrived, and any error.
- `received: 0` with "event stream lost": make sure StreamBee is running. Open
  `http://127.0.0.1:8765/events` in a browser; it should show `: hello` and keep loading.
  If it doesn't, a firewall or antivirus may be blocking it.
- After updating files, open the browser source properties in OBS and click
  **Refresh cache of current page**.

**`llama-server (bin/ or PATH) not found`.** The `.exe` or its DLLs aren't in `bin\`. The bee
falls back to procedural buzzing until they are.

**`ModuleNotFoundError`** with the bundled Python: make sure you're running `run.bat` from the
StreamBee folder, with the `python\` folder next to `src\`.

**No sound.** Run `run.bat --list-devices` and set `output_device` under `[audio]`.

**The bee is very angry.** That's BeeLLM. Lower `history_turns` so it forgives sooner, or set
it to `0` for a bee with no memory at all.
