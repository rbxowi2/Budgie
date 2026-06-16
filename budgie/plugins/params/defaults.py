"""
plugins/params/defaults.py — Adjustable defaults for BasicParamsPlugin.

Fallback parameter bounds used when the driver does not report hardware limits.
Edit these values to tune the UI slider ranges shown before a camera is opened.
"""

FALLBACK_EXP_MIN       = 100.0
FALLBACK_EXP_MAX       = 200_000.0
FALLBACK_GAIN_MIN      = 0.0
FALLBACK_GAIN_MAX      = 24.0
FALLBACK_FPS_MIN       = 1.0
FALLBACK_FPS_MAX       = 1000.0
FALLBACK_EXP_AUTO_UPPER = 100_000.0
