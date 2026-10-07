# voice-studio

Live, local studio voice chain: your mic → broadcast-grade polish (still *you*) → a virtual mic apps can use. ~15-30ms latency, CPU-only.

## Setup (once)
```powershell
cd voice-studio
uv venv
uv pip install sounddevice pedalboard numpy
```
Install a virtual cable so apps can read the processed voice as a mic:
**VB-CABLE** (free) → https://vb-audio.com/Cable — installs "CABLE Input" (we play into it) and "CABLE Output" (apps select it as a mic). Reboot if Windows asks.

In Windows Sound settings, set your **mic** and **CABLE Input** to **48000 Hz** (Properties → Advanced) so nothing resamples.

## Run
```powershell
uv run python voicechain.py --list        # see device names; set them in voicechain.py if needed
uv run python voicechain.py --monitor      # run + hear yourself in headphones (for tuning)
uv run python voicechain.py                # run (output only to the cable)
```
Then in Discord/Zoom/OBS, pick **CABLE Output** as your microphone.

## Tune it
All tone is in the `PARAMS` dict at the top of `voicechain.py` — edit, save, restart (instant). Try `--pitch -2` for a deeper/fuller take without editing the file.

## Test (no hardware needed)
```powershell
uv run python test_chain.py
```
