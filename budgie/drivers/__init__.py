"""
drivers/__init__.py — Auto-scanning driver registry.

Drop a *_driver.py file in this directory and it is automatically discovered
at import time.  Remove the file and it is cleanly absent — no other file
changes needed anywhere in the codebase.

Each driver file must contain exactly one subclass of CameraDriver.
"""

import importlib
import inspect
import pathlib

from .base import CameraDriver

_here = pathlib.Path(__file__).parent

DRIVERS: list  = []
DRIVER_MAP: dict = {}

for _f in sorted(_here.glob("*_driver.py")):
    try:
        _mod = importlib.import_module(f".{_f.stem}", package=__package__)
        for _cls_name, _cls in inspect.getmembers(_mod, inspect.isclass):
            if (issubclass(_cls, CameraDriver)
                    and _cls is not CameraDriver
                    and _cls not in DRIVERS):
                DRIVERS.append(_cls)
                DRIVER_MAP[_cls.__name__] = _cls
    except Exception as _e:
        print(f"[Driver] Failed to load {_f.name}: {_e}", flush=True)

if DRIVERS:
    print(f"[Driver] Loaded: {', '.join(DRIVER_MAP)}", flush=True)
else:
    print("[Driver] Warning: no drivers found in drivers/", flush=True)


def scan_all_devices() -> list:
    """Scan all registered drivers and return a merged device list."""
    results = []
    for driver_cls in DRIVERS:
        try:
            devices = driver_cls.scan_devices()
            for d in devices:
                d.setdefault("driver", driver_cls.__name__)
            results.extend(devices)
        except Exception:
            pass
    return results


__all__ = ["CameraDriver", "DRIVERS", "DRIVER_MAP", "scan_all_devices"]
