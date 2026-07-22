"""Group model: whichever window you grab becomes the mover, every other
window in its group just goes along with it.

Two behaviours are combined:

1. Instant follow (fallback): whichever window moves, all others in its
   group are translated by the same delta immediately. This is what happens
   for windows that don't go through a standard interactive drag (e.g.
   Aero Snap, or a move that skips the MOVESIZESTART/END hooks).

2. Drag / settle effect (the default, nicer-looking path): while a window is
   being interactively dragged (between EVENT_SYSTEM_MOVESIZESTART and
   EVENT_SYSTEM_MOVESIZEEND), the rest of its group simply stays put — no
   real-time following. Once the drag ends, they glide (ease-out) to their
   exact correct position over ``return_ms``.
"""

import json
import os
import threading
import time

from . import win_api

PALETTE = [
    "#6C8EF5",  # blue
    "#F2795C",  # coral
    "#4FC28C",  # green
    "#E7B84B",  # amber
    "#B084F2",  # violet
    "#3FBFC0",  # teal
    "#F27BAE",  # pink
    "#8DA3B0",  # slate
]

DEFAULT_SETTINGS = {"return_ms": 250}
SCHEDULER_TICK = 0.015  # ~66 Hz
ECHO_GRACE = 0.3  # ignore MOVESIZESTART on a non-mover only if we touched it this recently
STALE_DRAG_TIMEOUT = 1.5  # auto-finalize a drag if the mover stops reporting (missed MOVESIZEEND)


class _DragSession:
    """Tracks one active drag. Whichever window you grab becomes the mover;
    every other member of its group just stays put until you release, then
    eases into its exact correct position."""

    __slots__ = ("mover", "base_rects", "last_seen", "dragging", "settle")

    def __init__(self, mover, base_rects):
        self.mover = mover
        self.base_rects = base_rects  # hwnd -> Rect, snapshot at drag start
        self.last_seen = time.monotonic()  # last time the mover reported a move
        self.dragging = True
        self.settle = None  # dict(start, duration, followers={hwnd: (start_rect, target_rect)})


class Group:
    def __init__(self, group_id, members, color=None, locked=True):
        """members: dict hwnd -> {"title": str, "class": str}"""
        self.id = group_id
        self.members = members  # hwnd -> meta
        self.color = color or PALETTE[(group_id - 1) % len(PALETTE)]
        self.locked = locked  # while unlocked, windows move independently (no pinning)

    def hwnds(self):
        return list(self.members.keys())

    def to_dict(self):
        return {
            "id": self.id,
            "color": self.color,
            "locked": self.locked,
            "members": [
                {"hwnd": hwnd, "title": meta["title"], "class": meta["class"]}
                for hwnd, meta in self.members.items()
            ],
        }


class GroupManager:
    def __init__(self, config_path):
        self.config_path = config_path
        self.groups = {}  # id -> Group
        self._last_rect = {}  # hwnd -> Rect (our authoritative cache)
        self._lock = threading.Lock()
        self._next_id = 1
        self._enabled = True
        self._settings = dict(DEFAULT_SETTINGS)
        self._drag = {}  # group_id -> _DragSession
        self._last_programmatic_move = {}  # hwnd -> monotonic time we last moved it ourselves
        self._last_foreground = 0
        self._scheduler_thread = None
        self._scheduler_stop = threading.Event()

    # ---------------------------------------------------------- persistence
    def load(self):
        if not os.path.exists(self.config_path):
            return []
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        unresolved = []
        with self._lock:
            settings = data.get("settings", {})
            if isinstance(settings, dict):
                for key in ("return_ms",):
                    if key in settings:
                        try:
                            self._settings[key] = max(0, min(int(settings[key]), 3000))
                        except (TypeError, ValueError):
                            pass

            for group_data in data.get("groups", []):
                members = {}
                for m in group_data["members"]:
                    hwnd = win_api.find_window_by_title_class(m["title"], m["class"])
                    if hwnd and win_api.is_window_valid(hwnd):
                        members[hwnd] = {"title": m["title"], "class": m["class"]}
                    else:
                        unresolved.append(m["title"])
                if len(members) >= 2:
                    gid = self._next_id
                    self._next_id += 1
                    self.groups[gid] = Group(gid, members, color=group_data.get("color"))
                    for hwnd in members:
                        rect = win_api.get_window_rect(hwnd)
                        if rect:
                            self._last_rect[hwnd] = rect
        return unresolved

    def save(self):
        with self._lock:
            data = {
                "settings": dict(self._settings),
                "groups": [
                    {
                        "color": group.color,
                        "members": [
                            {"title": meta["title"], "class": meta["class"]}
                            for meta in group.members.values()
                        ],
                    }
                    for group in self.groups.values()
                ],
            }
        tmp_path = self.config_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.config_path)

    # ------------------------------------------------------------- editing
    def create_group(self, hwnd_list):
        """hwnd_list: list of hwnds (>=2) to link together."""
        if len(hwnd_list) < 2:
            return None
        with self._lock:
            members = {}
            for hwnd in hwnd_list:
                title = None
                cls = None
                try:
                    import win32gui
                    title = win32gui.GetWindowText(hwnd)
                    cls = win32gui.GetClassName(hwnd)
                except Exception:
                    pass
                members[hwnd] = {"title": title or "", "class": cls or ""}
                rect = win_api.get_window_rect(hwnd)
                if rect:
                    self._last_rect[hwnd] = rect
            gid = self._next_id
            self._next_id += 1
            group = Group(gid, members)
            self.groups[gid] = group
        self.save()
        return group

    def remove_group(self, group_id):
        with self._lock:
            self.groups.pop(group_id, None)
            self._drag.pop(group_id, None)
        self.save()

    def list_groups(self):
        with self._lock:
            return list(self.groups.values())

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)

    def is_enabled(self):
        return self._enabled

    def group_for_hwnd(self, hwnd):
        with self._lock:
            for group in self.groups.values():
                if hwnd in group.members:
                    return group
        return None

    def get_group(self, group_id):
        with self._lock:
            return self.groups.get(group_id)

    def set_group_locked(self, group_id, locked):
        """Unlocking pauses pinning for this group entirely — its windows can
        be freely rearranged. Re-locking captures wherever they currently are
        as the new baseline, so future moves propagate correctly from there."""
        with self._lock:
            group = self.groups.get(group_id)
            if group is None:
                return None
            group.locked = bool(locked)
            if not group.locked:
                self._drag.pop(group_id, None)
            else:
                for hwnd in group.members:
                    rect = win_api.get_window_rect(hwnd)
                    if rect:
                        self._last_rect[hwnd] = rect
            return group

    # -------------------------------------------------------------- settings
    def get_settings(self):
        with self._lock:
            return dict(self._settings)

    def set_settings(self, return_ms=None):
        with self._lock:
            if return_ms is not None:
                self._settings["return_ms"] = max(0, min(int(return_ms), 3000))
            result = dict(self._settings)
        self.save()
        return result

    # --------------------------------------------------------------- events
    def on_window_destroyed(self, hwnd):
        with self._lock:
            self._last_rect.pop(hwnd, None)
            changed = False
            for group in list(self.groups.values()):
                if hwnd in group.members:
                    del group.members[hwnd]
                    changed = True
                    self._drag.pop(group.id, None)
                    if len(group.members) < 2:
                        del self.groups[group.id]
        if changed:
            self.save()

    def on_foreground_changed(self, hwnd):
        """When a window becomes the active one, bring the rest of its group
        just below it in z-order so they don't get left behind other apps."""
        self._raise_group_siblings(hwnd)

    def _raise_group_siblings(self, hwnd):
        if not self._enabled:
            return
        with self._lock:
            owner_group = None
            for group in self.groups.values():
                if hwnd in group.members:
                    owner_group = group
                    break
            if owner_group is None or not owner_group.locked:
                return
            other_hwnds = [h for h in owner_group.members if h != hwnd]

        insert_after = hwnd
        for other_hwnd in other_hwnds:
            win_api.raise_below(other_hwnd, insert_after)
            insert_after = other_hwnd

    def on_move_start(self, hwnd):
        """User grabbed a window's title bar (or resize border). If it's
        part of a locked group, snapshot everyone's current position so we
        can compute where they need to end up once you release."""
        if not self._enabled:
            return
        with self._lock:
            owner_group = None
            for group in self.groups.values():
                if hwnd in group.members:
                    owner_group = group
                    break
            if owner_group is None or not owner_group.locked:
                return

            existing = self._drag.get(owner_group.id)
            if existing is not None and hwnd != existing.mover:
                # Windows can fire MOVESIZESTART/END for a window we are
                # currently repositioning ourselves via rapid SetWindowPos
                # calls (the settle glide), even though nobody is actually
                # dragging it. Only treat this as that kind of echo
                # if we genuinely touched this exact window very recently —
                # otherwise a stale/stuck session (e.g. a missed MOVESIZEEND)
                # would permanently block every OTHER window in the group
                # from ever becoming the mover again.
                last_touch = self._last_programmatic_move.get(hwnd, 0.0)
                if time.monotonic() - last_touch < ECHO_GRACE:
                    return
                self._drag.pop(owner_group.id, None)
                existing = None

            base_rects = {}
            for h in owner_group.members:
                rect = self._last_rect.get(h) or win_api.get_window_rect(h)
                if rect is None:
                    return  # can't establish a baseline for this group right now
                base_rects[h] = rect

            self._drag[owner_group.id] = _DragSession(hwnd, base_rects)

    def on_move_end(self, hwnd):
        """User released the window. Smoothly glide every other group
        member to its exact correct position."""
        if not self._enabled:
            return
        with self._lock:
            for gid, session in list(self._drag.items()):
                if session.mover != hwnd or not session.dragging:
                    continue
                final_rect = win_api.get_window_rect(hwnd) or session.base_rects[hwnd]
                self._finalize_drag(gid, session, final_rect)

    def _finalize_drag(self, gid, session, final_mover_rect):
        """Assumes self._lock is held. Either snaps or starts the eased
        glide of every follower to its exact correct position, based on the
        mover's final rect."""
        hwnd = session.mover
        base_mover = session.base_rects[hwnd]
        dx = final_mover_rect.left - base_mover.left
        dy = final_mover_rect.top - base_mover.top
        self._last_rect[hwnd] = final_mover_rect

        followers = {}
        for h, base in session.base_rects.items():
            if h == hwnd:
                continue
            current = self._last_rect.get(h) or win_api.get_window_rect(h) or base
            target = base.translated(dx, dy)
            followers[h] = (current, target)

        duration = max(self._settings.get("return_ms", 0), 0) / 1000.0
        if duration <= 0 or not followers:
            moves = list(followers.items())
            for h, (_, target) in moves:
                self._last_rect[h] = target
            win_api.move_windows_batch([(h, target) for h, (_, target) in moves])
            self._drag.pop(gid, None)
        else:
            session.dragging = False
            session.settle = {
                "start": time.monotonic(),
                "duration": duration,
                "followers": followers,
            }

    def on_location_changed(self, hwnd):
        if not self._enabled:
            return

        with self._lock:
            owner_group = None
            for group in self.groups.values():
                if hwnd in group.members:
                    owner_group = group
                    break
            if owner_group is None or not owner_group.locked:
                return

            new_rect = win_api.get_window_rect(hwnd)
            if new_rect is None:
                return
            old_rect = self._last_rect.get(hwnd)
            if old_rect is None:
                self._last_rect[hwnd] = new_rect
                return

            session = self._drag.get(owner_group.id)

            if session is not None:
                if session.dragging and session.mover == hwnd:
                    # The mover is being dragged; everyone else just stays
                    # put until release, so just track liveness for the
                    # stale-drag watchdog below — no propagation.
                    session.last_seen = time.monotonic()
                    self._last_rect[hwnd] = new_rect
                    return
                # Any other movement of a group member while a session owns
                # this group must be OUR OWN scheduler doing its job (settle
                # animation) — never re-propagate it, even if it doesn't
                # exactly match our cache (rounding/timing). Doing so would
                # feed this echo back into the whole group as if it were a
                # fresh user move, causing visible jitter.
                self._last_rect[hwnd] = new_rect
                return

            dx = new_rect.left - old_rect.left
            dy = new_rect.top - old_rect.top
            if dx == 0 and dy == 0:
                return  # size-only change, or just an echo of our own move

            # Classic instant fallback: no managed drag session for this
            # group at all (a non-standard move that skipped the
            # MOVESIZESTART/END hooks entirely, e.g. Aero Snap).
            self._last_rect[hwnd] = new_rect
            moves = []
            for other_hwnd in list(owner_group.members.keys()):
                if other_hwnd == hwnd:
                    continue
                other_rect = self._last_rect.get(other_hwnd)
                if other_rect is None:
                    other_rect = win_api.get_window_rect(other_hwnd)
                    if other_rect is None:
                        continue
                target = other_rect.translated(dx, dy)
                moves.append((other_hwnd, target))
                self._last_rect[other_hwnd] = target

        # Move every follower together in one frame instead of one
        # SetWindowPos call at a time (see move_windows_batch docstring).
        win_api.move_windows_batch(moves)

    # ------------------------------------------------------------ scheduler
    def start(self):
        """Start the background thread driving the settle animation.
        Independent of the WinEvent hook thread."""
        self._scheduler_stop.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def stop(self):
        self._scheduler_stop.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=2)

    def _scheduler_loop(self):
        while not self._scheduler_stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._scheduler_stop.wait(SCHEDULER_TICK)

    def _poll_foreground(self):
        """EVENT_SYSTEM_FOREGROUND doesn't fire reliably for every window /
        app (same class of gap as MOVESIZESTART/END), so back the WinEvent
        hook with a cheap poll here as a catch-all."""
        fg = win_api.get_foreground_window()
        if fg != self._last_foreground:
            self._last_foreground = fg
            self._raise_group_siblings(fg)

    def _tick(self):
        self._poll_foreground()
        now = time.monotonic()
        moves = []  # (hwnd, Rect) across every active session this tick
        with self._lock:
            finished = []
            for gid, session in list(self._drag.items()):
                if session.dragging:
                    if now - session.last_seen > STALE_DRAG_TIMEOUT:
                        # MOVESIZEEND was likely dropped (missed WinEvent) —
                        # without this, the group would stay locked to this
                        # mover forever and no other window could take over.
                        last_rect = win_api.get_window_rect(session.mover) or session.base_rects[session.mover]
                        self._finalize_drag(gid, session, last_rect)
                    continue
                elif session.settle is not None:
                    settle = session.settle
                    duration = settle["duration"]
                    elapsed = now - settle["start"]
                    frac = 1.0 if duration <= 0 else min(elapsed / duration, 1.0)
                    eased = 1 - (1 - frac) ** 3
                    for h, (start_rect, target_rect) in settle["followers"].items():
                        cur = target_rect if frac >= 1.0 else start_rect.lerp(target_rect, eased).rounded()
                        moves.append((h, cur))
                        self._last_rect[h] = cur
                        self._last_programmatic_move[h] = now
                    if frac >= 1.0:
                        finished.append(gid)
            for gid in finished:
                self._drag.pop(gid, None)

        # All windows moving this tick — across every active group — land
        # together in one DeferWindowPos batch instead of a visible stagger.
        win_api.move_windows_batch(moves)
