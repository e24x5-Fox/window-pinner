"""Entry point: wires together the WinEvent hook, group manager, the local
web UI (Flask) and the tray icon. No console window, no Tk window, no
external browser — the control surface is a native window with an embedded
WebView2 control (via pywebview), owned entirely by this app."""

import logging
import os
import socket
import threading
import webbrowser

from werkzeug.serving import make_server

from . import webview2, win_api
from .groups import GroupManager
from .overlay import OverlayManager
from .tray import TrayIcon
from .webapp import create_app

PREFERRED_PORT = 57732


def _config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "WindowPinner")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")


def _find_port(preferred):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    group_manager = GroupManager(_config_path())
    group_manager.load()
    group_manager.start()  # lag-trail / settle animation scheduler thread

    watcher = win_api.WinEventWatcher(
        on_location_changed=group_manager.on_location_changed,
        on_destroyed=group_manager.on_window_destroyed,
        on_foreground_changed=group_manager.on_foreground_changed,
        on_move_start=group_manager.on_move_start,
        on_move_end=group_manager.on_move_end,
    )
    watcher.start()

    overlay = OverlayManager(group_manager)
    overlay.start()

    port = _find_port(PREFERRED_PORT)
    url = f"http://127.0.0.1:{port}/"
    app = create_app(group_manager)
    server = make_server("127.0.0.1", port, app)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    if webview2.is_available():
        _run_with_embedded_window(url, watcher, group_manager, overlay, server)
    else:
        # No WebView2 Runtime found (rare on modern Windows, but not
        # guaranteed on older/stripped installs) — degrade gracefully to
        # opening the UI in the system's default browser instead of crashing.
        _run_with_default_browser(url, watcher, group_manager, overlay, server)


def _run_with_embedded_window(url, watcher, group_manager, overlay, server):
    import webview  # imports pythonnet/CLR — only touch this on a machine that has WebView2

    window = webview.create_window(
        "Window Pinner", url, width=1040, height=800, min_size=(760, 560)
    )

    def on_closing():
        # Clicking the window's own close button minimizes to tray instead
        # of quitting the whole app.
        window.hide()
        return False

    window.events.closing += on_closing

    def open_ui():
        window.show()

    def do_exit():
        watcher.stop()
        group_manager.stop()
        overlay.stop()
        server.shutdown()
        window.events.closing -= on_closing
        window.destroy()
        tray.stop()

    tray = TrayIcon(on_show=open_ui, on_exit=do_exit)
    tray_thread = threading.Thread(target=tray.run, daemon=True)
    tray_thread.start()

    webview.start()  # blocks the main thread until the window is destroyed


def _run_with_default_browser(url, watcher, group_manager, overlay, server):
    def open_ui():
        webbrowser.open(url)

    def do_exit():
        watcher.stop()
        group_manager.stop()
        overlay.stop()
        server.shutdown()
        tray.stop()

    tray = TrayIcon(on_show=open_ui, on_exit=do_exit)
    open_ui()
    tray.run()  # blocks the main thread until "Exit" is chosen


if __name__ == "__main__":
    main()
