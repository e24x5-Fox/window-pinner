"""Throwaway windows for trying out grouping/pinning without needing real
applications. Each one is a plain Tkinter top-level titled "ДЕМОокно N" (like
the test windows used while developing this project) so it behaves like any
ordinary application window — the group manager and overlay pick it up the
same way they would a real one.

Each demo window gets its own Tk root running its own mainloop in a
dedicated daemon thread (the same pattern overlay.py already uses for its
badge), so they're independent of each other and of the main app's UI
threads, and simply vanish along with the process when the app exits.
"""

import threading
import tkinter as tk

_active = set()  # numbers currently in use by an open demo window
_active_lock = threading.Lock()

_CASCADE_STEP = 36
_BASE_X = 160
_BASE_Y = 160


def _claim_number():
    with _active_lock:
        number = 1
        while number in _active:
            number += 1
        _active.add(number)
        return number


def _release_number(number):
    with _active_lock:
        _active.discard(number)


def _run(number):
    offset = (number - 1) * _CASCADE_STEP
    root = tk.Tk()
    root.title(f"ДЕМОокно {number}")
    root.geometry(f"360x220+{_BASE_X + offset}+{_BASE_Y + offset}")
    tk.Label(root, text=f"ДЕМОокно {number}", font=("Segoe UI", 22)).pack(expand=True)
    try:
        root.mainloop()
    finally:
        _release_number(number)


def spawn_demo_window():
    """Create one demo window in its own thread and return immediately. Its
    number is the lowest one not currently used by another open demo
    window, so closing all of them resets the next one back to 1."""
    number = _claim_number()
    threading.Thread(target=_run, args=(number,), daemon=True).start()
    return number
