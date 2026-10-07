"""Headless self-check for the voice chain — no audio hardware needed.

Proves the DSP is wired correctly before any live test:
  - output stays finite and never clips past the limiter ceiling,
  - a loud voice-band signal passes through (we don't kill the signal),
  - quiet, below-the-gate noise gets attenuated (the gate engages).

Run:  python test_chain.py
"""

import numpy as np

from voicechain import build_board, PARAMS, SR


def rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


def main():
    board = build_board(PARAMS)
    dur = 1.0
    n = int(SR * dur)
    t = np.arange(n) / SR

    # loud, voice-band tone (~ -10 dBFS) that should sail through an open gate
    loud = (0.30 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    out_loud = board(loud, SR, reset=True)

    assert np.all(np.isfinite(out_loud)), "non-finite samples in output"
    peak = float(np.max(np.abs(out_loud)))
    assert peak <= 1.0 + 1e-3, f"output clipped past full scale: peak={peak:.3f}"
    assert rms(out_loud) > 0.05, f"loud signal got killed: rms={rms(out_loud):.4f}"

    # quiet broadband noise well below the gate threshold -> should be attenuated
    quiet = (0.001 * np.random.default_rng(0).standard_normal(n)).astype(np.float32)
    out_quiet = board(quiet, SR, reset=True)
    assert np.all(np.isfinite(out_quiet)), "non-finite samples on quiet input"
    assert rms(out_quiet) < rms(quiet), (
        f"gate did not attenuate sub-threshold noise: "
        f"in={rms(quiet):.5f} out={rms(out_quiet):.5f}")

    print("OK  peak=%.3f  loud_rms=%.3f  quiet_in=%.5f quiet_out=%.5f"
          % (peak, rms(out_loud), rms(quiet), rms(out_quiet)))


if __name__ == "__main__":
    main()
