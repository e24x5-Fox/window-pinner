"""Floating on-screen badge attached to whichever window currently has
focus, anywhere in the system:

- If that window is already in a (locked or unlocked) group, the badge lets
  you lock/unlock that group, delete the whole group, or — if any other
  windows are currently marked pending (see below) — pull them into this
  group.
- If it isn't in any group yet, the badge offers "add to group": click it on
  a few windows in turn (switching focus between them) to mark them, then
  either click "Link" on the badge of any one of those marked windows to
  create a new group from the whole marked set, or focus an existing
  group's window and click its "Add here" button to extend that group
  instead.

The badge itself is a real HTML/CSS/JS page (window_pinner/static/badge.html)
shown in a small frameless, always-on-top WebView2 window — the same
rendering engine as the main UI — rather than being drawn with Tk/GDI
primitives. That gets us real CSS layout, typography and hover/press
transitions for free instead of approximating them with hand-rolled canvas
drawing. WebView2's own "transparent window" flag only fakes transparency
within its control hierarchy, though — it does not do real per-pixel alpha
against arbitrary content behind the window on Windows, so instead the
window is clipped to the badge's exact rounded-pill shape with
``SetWindowRgn`` (the same proven, DWM-composited approach the old Tk-based
badge used) rather than relying on any soft/blurred edges.

Only one window can be focused system-wide at a time, so a single webview
window is reused and repositioned/reconfigured every tick instead of keeping
one per tracked window. Positioning (``window.move``) is a raw win32
SetWindowPos call under the hood and safe to call every tick from a
background thread; pushing new content (``evaluate_js``) only happens when
the badge's state actually changes, and resizing the window to hug the
content is driven by a ResizeObserver in the page itself calling back into
Python — so hover/press feedback lives entirely in CSS with zero round
trips. A state change also proactively grows the window using a rough text
width estimate right before the new content is pushed, so a button growing
wider (e.g. "Связать (2)") doesn't get clipped for the one frame it takes
the real ResizeObserver measurement to arrive; the observer then corrects
the estimate down to the exact size, shrinking is left to it alone.

NOTE: do not set new extended window styles (``GWL_EXSTYLE`` via
``SetWindowLong``) on the badge's hwnd from this background thread — that
was tried (to add WS_EX_TOOLWINDOW) and deadlocked, because changing a
window's style synchronously requires cooperation from the thread that owns
the window, which can itself be busy at that moment. ``SetWindowRgn`` and
``SetWindowPos`` (used for move/resize) do not have this problem and are
safe to call cross-thread — that's proven by this exact codebase's prior
Tk-based badge, which did so every tick without issue.
"""

import json
import threading
import time
import winreg

import win32gui

from . import win_api
from .groups import SCHEDULER_TICK

# Match the group manager's own scheduler tick (~66 Hz) so the badge tracks
# its window's live position exactly as fast as the window itself moves —
# a slower poll here would make the badge visibly detach/lag behind during
# a fast drag even though the window is moving smoothly.
POLL_S = max(0.001, SCHEDULER_TICK)
BADGE_GAP = 4  # gap between the badge and the window's top edge

# Rough glyph-width estimate (Segoe UI ~13px) used only to proactively grow
# the window right before new text is pushed, so a widening button isn't
# clipped for the frame it takes the real ResizeObserver report to arrive.
_CHAR_W = 7.6
_BTN_PAD = 30
_DOT_W = 21
_GAP = 6
_ICON_W = 34

CARD_BORDER = "#2a2e37"
TEXT_DIM = "#9aa4b2"
FALLBACK_ACCENT = "#6c8ef5"  # used only if the system accent can't be read
WARN_FILL = "#ffb454"
WARN_TEXT = "#241a06"

_ACCENT_REFRESH_S = 3.0  # how often to re-check for a live theme/accent change
_accent_cache = {"color": FALLBACK_ACCENT, "checked_at": 0.0}


def _contrast_text(color_hex):
    """Dark or light text, whichever reads better on top of color_hex."""
    r, g, b = (int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#0f1216" if luminance > 140 else "#f5f7fa"


def system_accent_color():
    """The user's actual Windows accent color (Settings > Personalization >
    Colors), so the overlay's highlights match the rest of their desktop
    instead of some color we made up. Cheaply cached and re-read every few
    seconds so a live theme change is picked up without restarting the app."""
    now = time.monotonic()
    if now - _accent_cache["checked_at"] > _ACCENT_REFRESH_S:
        _accent_cache["checked_at"] = now
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
                value, _ = winreg.QueryValueEx(key, "AccentColor")
            # Stored as 0xAABBGGRR.
            r = value & 0xFF
            g = (value >> 8) & 0xFF
            b = (value >> 16) & 0xFF
            _accent_cache["color"] = "#%02x%02x%02x" % (r, g, b)
        except OSError:
            pass
    return _accent_cache["color"]


class _BadgeAPI:
    """Exposed to the page as ``window.pywebview.api`` — called directly from
    the JS click handlers and the ResizeObserver in badge.html."""

    def __init__(self, manager):
        self._manager = manager

    def main_click(self):
        self._manager._main_click()

    def secondary_click(self):
        self._manager._secondary_click()

    def delete_click(self):
        self._manager._delete_click()

    def report_size(self, w, h):
        self._manager._on_report_size(w, h)


class OverlayManager:
    def __init__(self, group_manager, badge_url):
        self.group_manager = group_manager
        self._badge_url = badge_url
        self._stop = threading.Event()
        self._thread = None
        self._window = None

        self._pending = set()  # hwnds marked "add to group", awaiting Link
        self._hwnd = None
        self._mode = None
        self._group_id = None

        self._visible = False
        self._anchor_rect = None
        self._last_state = None
        self._last_pos = None
        self._badge_w = 20
        self._badge_h = 20
        self._badge_hwnd = None
        self._region_wh = None

    def start(self):
        import webview  # imports pythonnet/CLR — only touch this on a machine that has WebView2

        self._window = webview.create_window(
            "badge",
            self._badge_url,
            width=self._badge_w,
            height=self._badge_h,
            x=-1000,
            y=-1000,
            min_size=(1, 1),
            frameless=True,
            easy_drag=False,
            focus=False,
            on_top=True,
            background_color="#1b1e25",
            hidden=True,
            resizable=False,
            js_api=_BadgeAPI(self),
        )
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass

    # -- background loop --------------------------------------------------

    def _loop(self):
        try:
            self._window.events.shown.wait(20)
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                self._refresh()
            except Exception:
                pass
            time.sleep(POLL_S)

    # -- click / resize callbacks (invoked from the JS bridge thread) -----

    def _main_click(self):
        if self._hwnd is None:
            return
        if self._mode == "grouped":
            self._toggle_lock(self._group_id)
        else:
            self._toggle_pending(self._hwnd)

    def _secondary_click(self):
        if self._mode == "grouped":
            self._add_pending_to_group(self._group_id)
        else:
            self._link_pending()

    def _delete_click(self):
        if self._mode == "grouped":
            self._delete_group(self._group_id)

    def _on_report_size(self, w, h):
        w, h = int(w), int(h)
        if (w, h) != (self._badge_w, self._badge_h):
            self._badge_w, self._badge_h = w, h
            self._last_pos = None  # size changed, force a reposition
            try:
                self._window.resize(w, h)
            except Exception:
                pass
            self._apply_region(w, h)
        if self._anchor_rect is not None:
            self._reposition()

    def _find_badge_hwnd(self):
        if self._badge_hwnd and win32gui.IsWindow(self._badge_hwnd):
            return self._badge_hwnd
        hwnd = win32gui.FindWindow(None, "badge")
        self._badge_hwnd = hwnd or None
        return self._badge_hwnd

    def _apply_region(self, w, h):
        # Real per-pixel alpha isn't available (see module docstring), so the
        # window is hard-clipped to the pill shape instead — SetWindowRgn,
        # proven safe to call cross-thread by the old Tk-based badge.
        if (w, h) == self._region_wh:
            return
        hwnd = self._find_badge_hwnd()
        if not hwnd:
            return
        try:
            radius = max(1, int(h / 2))
            rgn = win32gui.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius, radius)
            win32gui.SetWindowRgn(hwnd, rgn, True)
            self._region_wh = (w, h)
        except Exception:
            pass

    def _estimate_width(self, state):
        total = _DOT_W
        total += len(state["main"]["text"]) * _CHAR_W + _BTN_PAD + _GAP
        if state["secondary"]:
            total += len(state["secondary"]["text"]) * _CHAR_W + _BTN_PAD + _GAP
        if state["showDelete"]:
            total += _ICON_W + _GAP
        return int(total) + 16  # safety margin for font-metric estimate error

    def _toggle_lock(self, group_id):
        group = self.group_manager.get_group(group_id)
        if group is not None:
            self.group_manager.set_group_locked(group_id, not group.locked)

    def _toggle_pending(self, hwnd):
        if hwnd in self._pending:
            self._pending.discard(hwnd)
        else:
            self._pending.add(hwnd)

    def _link_pending(self):
        hwnds = [h for h in self._pending if win_api.is_window_valid(h)]
        if len(hwnds) >= 2:
            self.group_manager.create_group(hwnds)
        self._pending.clear()

    def _add_pending_to_group(self, group_id):
        hwnds = [h for h in self._pending if win_api.is_window_valid(h)]
        if hwnds:
            self.group_manager.add_to_group(group_id, hwnds)
        self._pending.clear()

    def _delete_group(self, group_id):
        self.group_manager.remove_group(group_id)

    # -- state / positioning ----------------------------------------------

    def _hide(self):
        if self._visible:
            self._visible = False
            try:
                self._window.hide()
            except Exception:
                pass
        self._hwnd = None
        self._mode = None
        self._group_id = None

    def _show(self, state, rect):
        if state != self._last_state:
            self._last_state = state
            # Grow (never shrink pre-emptively) using a rough text-width
            # estimate before the new content lands, so a widening button
            # doesn't sit clipped for the frame it takes the real
            # ResizeObserver report to arrive. Shrinking is left to that
            # report alone, once the exact new size is known.
            estimated_w = self._estimate_width(state)
            if estimated_w > self._badge_w:
                self._badge_w = estimated_w
                self._last_pos = None
                try:
                    self._window.resize(self._badge_w, self._badge_h)
                except Exception:
                    pass
                self._apply_region(self._badge_w, self._badge_h)
            try:
                self._window.evaluate_js(f"applyState({json.dumps(state)})")
            except Exception:
                pass
        self._anchor_rect = rect
        self._reposition()
        if not self._visible:
            self._apply_region(self._badge_w, self._badge_h)
            self._visible = True
            try:
                self._window.show()
            except Exception:
                pass

    def _reposition(self):
        rect = self._anchor_rect
        x = max(rect.left, 0)
        y = rect.top - self._badge_h - BADGE_GAP
        if y < 0:
            # No room above (window flush with the screen's top edge) — fall
            # back to a small inset instead of floating off-screen.
            y = rect.top + BADGE_GAP
        pos = (int(x), int(y))
        if pos != self._last_pos:
            self._last_pos = pos
            try:
                self._window.move(*pos)
            except Exception:
                pass

    def _build_grouped_state(self, group, pending_count):
        # The ring is the system accent color while the group is behaving
        # normally (locked), matching the rest of the user's Windows theme;
        # it switches to the same warning amber as the web UI's unlocked
        # state so "be careful, not pinned right now" still reads instantly.
        if group.locked:
            border = system_accent_color()
            main = {"text": "Открепить", "filled": False, "dim": False}
        else:
            border = WARN_FILL
            main = {
                "text": "Закрепить", "filled": True,
                "fillColor": WARN_FILL, "fillText": WARN_TEXT,
            }

        secondary = None
        if pending_count:
            accent = system_accent_color()
            secondary = {
                "text": f"+ Добавить ({pending_count})",
                "fillColor": accent, "fillText": _contrast_text(accent),
            }

        return {
            "borderColor": border,
            "dotColor": group.color,
            "dotHollow": False,
            "main": main,
            "secondary": secondary,
            "showDelete": True,
        }

    def _build_ungrouped_state(self, selected, can_link, pending_count):
        accent = system_accent_color()
        if selected:
            border = accent
            dot_color = accent
            dot_hollow = False
            main = {"text": "Убрать", "filled": False, "dim": False}
        else:
            border = CARD_BORDER
            dot_color = TEXT_DIM
            dot_hollow = True
            main = {"text": "Добавить в группу", "filled": False, "dim": True}

        secondary = None
        if can_link:
            secondary = {
                "text": f"Связать ({pending_count})",
                "fillColor": accent, "fillText": _contrast_text(accent),
            }

        return {
            "borderColor": border,
            "dotColor": dot_color,
            "dotHollow": dot_hollow,
            "main": main,
            "secondary": secondary,
            "showDelete": False,
        }

    def _refresh(self):
        # Drop any pending selection that's no longer valid or that already
        # belongs to a group by other means (e.g. created via the web UI).
        for hwnd in list(self._pending):
            if not win_api.is_window_valid(hwnd) or self.group_manager.group_for_hwnd(hwnd) is not None:
                self._pending.discard(hwnd)

        hwnd = win_api.get_foreground_window()
        if not hwnd or not win_api.is_window_valid(hwnd):
            self._hide()
            return
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            self._hide()
            return

        group = self.group_manager.group_for_hwnd(hwnd)
        if group is None and not win_api.is_candidate_window(hwnd):
            self._hide()
            return

        rect = win_api.get_window_rect(hwnd)
        if rect is None:
            self._hide()
            return

        self._hwnd = hwnd
        if group is not None:
            self._mode = "grouped"
            self._group_id = group.id
            state = self._build_grouped_state(group, len(self._pending))
        else:
            self._mode = "ungrouped"
            self._group_id = None
            selected = hwnd in self._pending
            can_link = selected and len(self._pending) >= 2
            state = self._build_ungrouped_state(selected, can_link, len(self._pending))

        self._show(state, rect)
