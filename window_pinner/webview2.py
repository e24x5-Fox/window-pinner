"""Detect whether the Microsoft Edge WebView2 Runtime is installed, so we
can fall back to opening the default browser on machines that don't have it
(rare on modern Windows, but not guaranteed on older/stripped installs)."""

import winreg

_WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_KEY_PATHS = (
    (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_GUID}"),
    (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_GUID}"),
    (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_GUID}"),
)


def is_available():
    for root, path in _KEY_PATHS:
        try:
            with winreg.OpenKey(root, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version:
                    return True
        except OSError:
            continue
    return False
