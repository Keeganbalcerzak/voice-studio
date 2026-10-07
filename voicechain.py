"""Live studio voice chain — turns your mic into a polished, broadcast-grade
version of *your own* voice in real time, and sends it out a virtual cable so
apps (Discord/Zoom/OBS) pick it up as a microphone.

    python voicechain.py --list              # show audio devices, then quit
    python voicechain.py                      # run: mic -> chain -> virtual cable
    python voicechain.py --monitor            # also hear yourself in headphones
    python voicechain.py --pitch -2           # override pitch (semitones) for this run

All the tone lives in PARAMS below — edit and restart (startup is instant).
"""

import argparse
import queue
import sys

import numpy as np
import sounddevice as sd
from pedalboard import (
    Pedalboard, NoiseGate, Compressor, Gain, Limiter,
    HighpassFilter, LowShelfFilter, HighShelfFilter, PeakFilter,
    Distortion, PitchShift,
)

# --- audio engine ---------------------------------------------------------
SR = 48000          # sample rate; set both mic + cable to 48000 in Windows sound settings
BLOCK = 256         # frames per callback; 256 @ 48k ~= 5.3ms. Lower = less latency, more CPU.

# --- routing --------------------------------------------------------------
# Each can be: a substring of the device name (case-insensitive), an int index,
# or None (system default). Run with --list to see names/indices.
# WASAPI entries are preferred automatically when a name matches several.
INPUT_DEVICE  = None          # your real mic
OUTPUT_DEVICE = "CABLE Input" # the virtual cable's PLAYBACK side; apps select its mic side
MONITOR_DEVICE = None         # headphones, for --monitor (None = system default output)

# --- the "premium me" chain (every knob is here) --------------------------
PARAMS = {
    "hpf_hz":        85,     # high-pass: kill rumble / plosives / handling mud

    "gate_thresh_db": -45,   # noise gate: below this = room/breath, get attenuated
    "gate_ratio":     6.0,
    "gate_attack_ms": 1.0,
    "gate_release_ms": 120.0,

    "low_shelf_hz":   160,   # warmth / body
    "low_shelf_db":   3.0,

    "mud_hz":         260,   # tame boxy low-mids
    "mud_db":        -3.0,
    "mud_q":          1.0,

    "presence_hz":    4200,  # presence / intelligibility
    "presence_db":    3.5,
    "presence_q":     0.9,

    "air_hz":         11000, # "air" / expensive sheen
    "air_db":         3.0,

    "comp_thresh_db": -20.0, # compressor: even, always-close-to-the-mic feel
    "comp_ratio":     3.0,
    "comp_attack_ms": 8.0,
    "comp_release_ms": 140.0,

    "drive_db":       4.0,   # subtle harmonic saturation = richness/warmth

    "pitch_semitones": 0.0,  # 0 = off. -1..-3 = slightly deeper/fuller. (see note in build_board)

    "makeup_db":      4.0,   # output gain before the limiter
    "ceiling_db":     -1.0,  # brickwall limiter ceiling; never clips into the cable
    "limit_release_ms": 80.0,
}


def build_board(p, realtime=True):
    """Construct the effect chain from a PARAMS dict.

    realtime=True omits PitchShift unless it's actually engaged, because pitch
    shifting is the one block that can return a different number of samples than
    it was given — fine offline, but in a fixed-block audio callback a length
    change corrupts the stream. With pitch at 0 the whole chain is
    length-preserving, so the callback is safe.
    """
    chain = [
        HighpassFilter(cutoff_frequency_hz=p["hpf_hz"]),
        NoiseGate(threshold_db=p["gate_thresh_db"], ratio=p["gate_ratio"],
                  attack_ms=p["gate_attack_ms"], release_ms=p["gate_release_ms"]),
        LowShelfFilter(cutoff_frequency_hz=p["low_shelf_hz"], gain_db=p["low_shelf_db"]),
        PeakFilter(cutoff_frequency_hz=p["mud_hz"], gain_db=p["mud_db"], q=p["mud_q"]),
        PeakFilter(cutoff_frequency_hz=p["presence_hz"], gain_db=p["presence_db"], q=p["presence_q"]),
        HighShelfFilter(cutoff_frequency_hz=p["air_hz"], gain_db=p["air_db"]),
        Compressor(threshold_db=p["comp_thresh_db"], ratio=p["comp_ratio"],
                   attack_ms=p["comp_attack_ms"], release_ms=p["comp_release_ms"]),
        Distortion(drive_db=p["drive_db"]),
    ]
    if p["pitch_semitones"]:
        chain.append(PitchShift(semitones=p["pitch_semitones"]))
    chain += [
        Gain(gain_db=p["makeup_db"]),
        Limiter(threshold_db=p["ceiling_db"], release_ms=p["limit_release_ms"]),
    ]
    return Pedalboard(chain)


def resolve_device(spec, want_input):
    """Map a name-substring / int / None to a device index, preferring WASAPI."""
    if spec is None or isinstance(spec, int):
        return spec
    devs = sd.query_devices()
    apis = sd.query_hostapis()
    key = "max_input_channels" if want_input else "max_output_channels"
    matches = [i for i, d in enumerate(devs)
               if spec.lower() in d["name"].lower() and d[key] > 0]
    if not matches:
        raise SystemExit(f"No {'input' if want_input else 'output'} device matches "
                         f"{spec!r}. Run with --list to see names.")
    # prefer WASAPI when several entries share the name
    matches.sort(key=lambda i: 0 if apis[devs[i]["hostapi"]]["name"] == "Windows WASAPI" else 1)
    return matches[0]


def to_wasapi(idx, want_input):
    """Upgrade a device index to its WASAPI twin (same name) for low latency.

    System defaults on Windows are MME (~180ms); the WASAPI entry for the same
    physical device runs ~20ms. Returns idx unchanged if no WASAPI twin exists.
    """
    if idx is None:
        idx = sd.default.device[0 if want_input else 1]
    d = sd.query_devices(idx)
    if sd.query_hostapis(d["hostapi"])["name"] == "Windows WASAPI":
        return idx
    key = "max_input_channels" if want_input else "max_output_channels"
    for i, dd in enumerate(sd.query_devices()):
        if (dd[key] > 0 and dd["name"] == d["name"]
                and sd.query_hostapis(dd["hostapi"])["name"] == "Windows WASAPI"):
            return i
    return idx


def wasapi_settings(idx):
    """auto_convert lets a WASAPI device accept 48k even if its mix format differs
    (e.g. 44.1k headphones), resampling internally at low latency."""
    if sd.query_hostapis(sd.query_devices(idx)["hostapi"])["name"] == "Windows WASAPI":
        return sd.WasapiSettings(auto_convert=True)
    return None


def describe(idx):
    if idx is None:
        return "system default"
    d = sd.query_devices(idx)
    api = sd.query_hostapis(d["hostapi"])["name"]
    return f"[{idx}] {d['name']} ({api})"


def list_devices():
    print(sd.query_devices())
    print("\nHost APIs:", ", ".join(a["name"] for a in sd.query_hostapis()))


def _make_output_cb(q, xruns):
    """Output callback: pull a processed block from the queue, write it.
    Underflow (queue empty) -> silence rather than a crash."""
    def cb(outdata, frames, time, status):
        if status:
            xruns["n"] += 1
        try:
            data = q.get_nowait()
        except queue.Empty:
            outdata.fill(0); return
        if len(data) < frames:
            pad = np.zeros(frames, dtype=np.float32); pad[:len(data)] = data; data = pad
        outdata[:] = data[:frames, None]            # mono -> all output channels
    return cb


def run(monitor=False, seconds=None):
    # Two independent streams (mic in, cable out) linked by a queue — the standard
    # design when input and output are different devices. A single duplex stream
    # can't span two different WASAPI devices.
    in_idx = to_wasapi(resolve_device(INPUT_DEVICE, want_input=True), want_input=True)
    out_idx = to_wasapi(resolve_device(OUTPUT_DEVICE, want_input=False), want_input=False)
    out_ch = min(2, sd.query_devices(out_idx)["max_output_channels"]) or 1

    board = build_board(PARAMS)
    xruns = {"n": 0}
    out_q = queue.Queue(maxsize=8)
    mon_q = queue.Queue(maxsize=8) if monitor else None

    def push(q, block):
        try:
            q.put_nowait(block)
        except queue.Full:                          # stay current: drop oldest, then push
            try: q.get_nowait()
            except queue.Empty: pass
            try: q.put_nowait(block)
            except queue.Full: pass

    def in_cb(indata, frames, time, status):
        if status:
            xruns["n"] += 1
        mono = indata[:, 0].astype(np.float32)
        out = board(mono, SR, reset=False)
        if len(out) != frames:                      # defensive: keep blocks aligned
            fixed = np.zeros(frames, dtype=np.float32)
            n = min(frames, len(out)); fixed[:n] = out[:n]; out = fixed
        push(out_q, out)
        if mon_q is not None:
            push(mon_q, out.copy())

    mon_idx = to_wasapi(resolve_device(MONITOR_DEVICE, want_input=False), want_input=False) if monitor else None

    print("Studio voice chain")
    print("  in :", describe(in_idx))
    print("  out:", describe(out_idx), f"({out_ch}ch)")
    if monitor:
        print("  mon:", describe(mon_idx))
    if PARAMS["pitch_semitones"]:
        print(f"  pitch: {PARAMS['pitch_semitones']:+g} semitones")

    in_stream = sd.InputStream(device=in_idx, samplerate=SR, blocksize=BLOCK,
                               channels=1, dtype="float32", latency="low",
                               extra_settings=wasapi_settings(in_idx), callback=in_cb)
    out_stream = sd.OutputStream(device=out_idx, samplerate=SR, blocksize=BLOCK,
                                 channels=out_ch, dtype="float32", latency="low",
                                 extra_settings=wasapi_settings(out_idx),
                                 callback=_make_output_cb(out_q, xruns))
    streams = [out_stream, in_stream]               # start output first so it has data to pull
    if monitor:
        mon_ch = min(2, sd.query_devices(mon_idx)["max_output_channels"]) or 1
        streams.insert(0, sd.OutputStream(device=mon_idx, samplerate=SR, blocksize=BLOCK,
                                          channels=mon_ch, dtype="float32", latency="low",
                                          extra_settings=wasapi_settings(mon_idx),
                                          callback=_make_output_cb(mon_q, xruns)))

    # prime output queues with one silent block so consumers don't starve at start
    silent = np.zeros(BLOCK, dtype=np.float32)
    out_q.put(silent.copy())
    if mon_q is not None:
        mon_q.put(silent.copy())

    try:
        for s in streams:
            s.start()
        olat = out_stream.latency
        print(f"  latency: ~{(in_stream.latency + olat + BLOCK / SR) * 1000:.0f}ms end-to-end")
        if seconds:
            print(f"  running {seconds:g}s (smoke test)...")
            sd.sleep(int(seconds * 1000))
        else:
            print("  speaking... pick the cable's mic side in your app. Ctrl+C to stop.")
            try:
                while True:
                    sd.sleep(1000)
            except KeyboardInterrupt:
                pass
    finally:
        for s in streams:
            s.stop(); s.close()
    print(f"\nstopped. xrun/glitch callbacks: {xruns['n']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Live studio voice chain")
    ap.add_argument("--list", action="store_true", help="list audio devices and exit")
    ap.add_argument("--monitor", action="store_true", help="also play to headphones so you can hear yourself")
    ap.add_argument("--pitch", type=float, metavar="SEMITONES",
                    help="override pitch shift for this run (e.g. -2)")
    ap.add_argument("--in", dest="in_dev", metavar="NAME", help="input device name/substring (overrides INPUT_DEVICE)")
    ap.add_argument("--out", dest="out_dev", metavar="NAME", help="output device name/substring (overrides OUTPUT_DEVICE)")
    ap.add_argument("--seconds", type=float, metavar="N", help="run for N seconds then stop (smoke test)")
    args = ap.parse_args()
    if args.list:
        list_devices(); sys.exit(0)
    if args.pitch is not None:
        PARAMS["pitch_semitones"] = args.pitch
    if args.in_dev is not None:
        INPUT_DEVICE = args.in_dev
    if args.out_dev is not None:
        OUTPUT_DEVICE = args.out_dev
    run(monitor=args.monitor, seconds=args.seconds)
