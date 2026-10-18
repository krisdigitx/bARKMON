"""Relative digital levels and estimated bark pulses; no SPL calibration."""

import json
import math

import numpy as np

from barkmon.detection import RATE

FRAME = 800  # 50 ms envelope; permits inexpensive cross-camera comparisons.


def measurements(audio, start, end):
    samples = audio[max(0, int(start * RATE)) : max(0, int(end * RATE))]
    if not len(samples):
        return {
            "rms_dbfs": None,
            "peak_dbfs": None,
            "bark_count": 0,
            "fingerprint": "[]",
        }
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    peak = float(np.max(np.abs(samples)))
    envelope = np.array(
        [
            np.sqrt(np.mean(samples[i : i + FRAME] ** 2))
            for i in range(0, len(samples), FRAME)
        ]
    )
    active = envelope >= max(0.002, float(envelope.max()) * 0.35)
    # Fill <=100 ms holes; count regions lasting >=50 ms as estimated pulses.
    for index in range(1, len(active) - 1):
        if active[index - 1] and active[index + 1]:
            active[index] = True
    count = sum(
        bool(value) and (i == 0 or not active[i - 1]) for i, value in enumerate(active)
    )
    normalized = envelope / max(float(envelope.max()), 1e-9)
    return {
        "rms_dbfs": round(20 * math.log10(max(rms, 1e-6)), 2),
        "peak_dbfs": round(20 * math.log10(max(peak, 1e-6)), 2),
        "bark_count": count,
        "fingerprint": json.dumps(
            np.round(normalized, 3).tolist(), separators=(",", ":")
        ),
    }
