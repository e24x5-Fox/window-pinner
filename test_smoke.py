"""Manual smoke test (not pytest): start the hook watcher, link two
controlled Tkinter test windows into a group, move one via SetWindowPos and
confirm the other follows by exactly the same delta."""

import subprocess
import sys
import time

from window_pinner import win_api
from window_pinner.groups import GroupManager

proc_a = subprocess.Popen([sys.executable, "test_helper_window.py", "PinTestA", "200", "200"])
proc_b = subprocess.Popen([sys.executable, "test_helper_window.py", "PinTestB", "700", "300"])
time.sleep(1.5)

try:
    all_wins = win_api.list_windows()
    win_a = next((w for w in all_wins if w[1] == "PinTestA"), None)
    win_b = next((w for w in all_wins if w[1] == "PinTestB"), None)
    print("A:", win_a, " B:", win_b)
    if not win_a or not win_b:
        print("FAIL: test windows not found")
        sys.exit(1)

    hwnd_a, hwnd_b = win_a[0], win_b[0]

    gm = GroupManager("test_config.json")
    watcher = win_api.WinEventWatcher(
        on_location_changed=gm.on_location_changed,
        on_destroyed=gm.on_window_destroyed,
    )
    watcher.start()

    group = gm.create_group([hwnd_a, hwnd_b])
    print("Group created:", group.id, group.hwnds())

    rect_a_before = win_api.get_window_rect(hwnd_a)
    rect_b_before = win_api.get_window_rect(hwnd_b)
    print("A before:", rect_a_before)
    print("B before:", rect_b_before)

    target_a = win_api.Rect(
        rect_a_before.left + 120,
        rect_a_before.top + 80,
        rect_a_before.right + 120,
        rect_a_before.bottom + 80,
    )
    win_api.move_window(hwnd_a, target_a)

    time.sleep(1.5)

    rect_a_after = win_api.get_window_rect(hwnd_a)
    rect_b_after = win_api.get_window_rect(hwnd_b)
    print("A after:", rect_a_after)
    print("B after:", rect_b_after)

    dx_a = rect_a_after.left - rect_a_before.left
    dy_a = rect_a_after.top - rect_a_before.top
    dx_b = rect_b_after.left - rect_b_before.left
    dy_b = rect_b_after.top - rect_b_before.top

    print(f"delta A = ({dx_a},{dy_a})  delta B = ({dx_b},{dy_b})")

    ok = (dx_a, dy_a) == (dx_b, dy_b) == (120, 80)
    print("RESULT:", "PASS" if ok else "FAIL")

    watcher.stop()
    sys.exit(0 if ok else 1)
finally:
    proc_a.terminate()
    proc_b.terminate()
