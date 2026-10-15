"""
make_sfx.py — generates StreamBee's default sound effects into sfx/.
Pure numpy + stdlib wave, so the sounds are original and license-free.
Run:  python tools/make_sfx.py
"""

import wave
from pathlib import Path

import numpy as np

SR = 22050
OUT = Path(__file__).resolve().parent.parent / "sfx"
rng = np.random.default_rng(7)


def env(n, attack=0.05, release=0.15):
    e = np.ones(n)
    a, r = int(n * attack), int(n * release)
    if a:
        e[:a] = np.linspace(0, 1, a)
    if r:
        e[-r:] = np.linspace(1, 0, r)
    return e


def lowpass(x, alpha=0.25):
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc += alpha * (v - acc)
        y[i] = acc
    return y


def buzz(seconds, f0=220.0, glide=None, vib=6.0, wobble=7.0, seed_phase=0.0):
    n = int(SR * seconds)
    t = np.arange(n) / SR
    f = np.full(n, f0) if glide is None else np.interp(t, np.linspace(0, seconds, len(glide)), glide)
    f = f + vib * np.sin(2 * np.pi * wobble * t + seed_phase)
    phase = np.cumsum(f) / SR
    saw = 2 * (phase % 1.0) - 1                      # bright sawtooth = insect wing whine
    wing = 0.75 + 0.25 * np.sin(2 * np.pi * f * 0.5 * t)
    noise = rng.normal(0, 0.08, n)
    return lowpass(saw * wing + noise, 0.3)


def save(name, x, gain=0.5):
    x = x / (np.max(np.abs(x)) or 1) * gain
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    OUT.mkdir(exist_ok=True)
    with wave.open(str(OUT / name), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"wrote sfx/{name}")


def main():
    x = buzz(0.6, 225)
    save("buzz_short.wav", x * env(len(x), 0.15, 0.35), 0.45)

    # fly-by: pitch rises then falls, volume swells in the middle
    x = buzz(1.5, glide=[200, 250, 190])
    sw = np.sin(np.linspace(0, np.pi, len(x))) ** 1.5
    save("buzz_long.wav", x * sw, 0.5)

    # flying away: pitch drops and fades out
    x = buzz(1.2, glide=[260, 170])
    save("buzz_away.wav", x * np.linspace(1, 0, len(x)) ** 1.3, 0.5)

    # swarm: a handful of detuned buzzes layered
    n = int(SR * 2.5)
    mix = np.zeros(n)
    for _ in range(7):
        b = buzz(2.5, rng.uniform(190, 280), vib=rng.uniform(4, 12),
                 wobble=rng.uniform(4, 10), seed_phase=rng.uniform(0, 6))
        mix += b * rng.uniform(0.5, 1.0)
    save("swarm.wav", mix * env(n, 0.25, 0.3), 0.5)

    # pop: quick rising blip for flowers
    n = int(SR * 0.14)
    t = np.arange(n) / SR
    f = np.linspace(600, 1300, n)
    blip = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 28)
    save("pop.wav", blip, 0.6)


if __name__ == "__main__":
    main()
