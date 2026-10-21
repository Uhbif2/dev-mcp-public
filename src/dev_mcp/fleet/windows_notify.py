"""Windows toast transport. A notification never opens the owner's application.

The caller provides an already registered Start-menu AppUserModelID. Windows
requires that registration for desktop toasts; this module does not install a
shortcut or change Windows settings. All toast text is fixed and non-private.
"""
from __future__ import annotations

import base64
import ctypes
import re
import subprocess
import sys
from xml.etree import ElementTree as ET


_AUMID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}\Z")


def gui_session_available() -> bool:
    """Interactive desktop with Windows toast notifications not disabled.

    GetShellWindow alone is insufficient: ToastEnabled=0 suppresses display
    while WinRT can still accept a Show call. Do not spend a durable outbox
    attempt until the owner's notification setting permits delivery.
    """
    if sys.platform != "win32":
        return False
    try:
        # GetShellWindow is zero in a noninteractive Windows station.
        if not ctypes.windll.user32.GetShellWindow():
            return False
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\PushNotifications",
            ) as key:
                try:
                    enabled, kind = winreg.QueryValueEx(key, "ToastEnabled")
                except FileNotFoundError:
                    return True  # Windows defaults to enabled when unset.
        except FileNotFoundError:
            return True
        return kind == winreg.REG_DWORD and type(enabled) is int and enabled == 1
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def toast_xml(overview_url: str, *, notice_kind: str = 'done') -> str:
    """Only a prevalidated local URL may become a protocol activation target."""
    from .handoff import validate_overview_url

    url = validate_overview_url(overview_url)
    root = ET.Element("toast", {"activationType": "protocol", "launch": url})
    visual = ET.SubElement(root, "visual")
    binding = ET.SubElement(visual, "binding", {"template": "ToastGeneric"})
    if notice_kind not in {'done', 'needs'}:
        raise ValueError('invalid notice kind')
    ET.SubElement(binding, "text").text = ("Флоту требуется ваше решение" if notice_kind == 'needs'
                                           else "Результат готов")
    ET.SubElement(binding, "text").text = "Откройте локальный обзор результатов"
    return ET.tostring(root, encoding="unicode")


class WindowsToast:
    """Show once, returning False when WinRT or the desktop is unavailable.

    This intentionally does not attempt to register AUMIDs, install shortcuts,
    launch the dashboard, or display private result names and paths.
    """

    def __init__(self, app_id: str):
        if not isinstance(app_id, str) or not _AUMID.fullmatch(app_id):
            raise ValueError("invalid registered AppUserModelID")
        self.app_id = app_id

    def show(self, overview_url: str, *, notice_kind: str = 'done') -> bool:
        xml = toast_xml(overview_url, notice_kind=notice_kind)
        if not gui_session_available():
            return False
        # XML is passed as a UTF-16 encoded command, not shell-concatenated
        # user data. URL and AUMID are separately validated; quotes are XML
        # escaped and single quotes never occur in accepted URL/AUMID syntax.
        script = (
            "$ErrorActionPreference='Stop';"
            "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
            "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] | Out-Null;"
            "$doc=New-Object Windows.Data.Xml.Dom.XmlDocument;"
            "$doc.LoadXml('" + xml.replace("'", "''") + "');"
            "$toast=New-Object Windows.UI.Notifications.ToastNotification $doc;"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('"
            + self.app_id + "').Show($toast);"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                timeout=12, check=False, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
