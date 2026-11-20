"""Optional desktop notification. Failure here never fails a deploy."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess

from deploy_butler.security import mask_text


def notify_os(title: str, body: str) -> None:
    title = mask_text(title)[:140]
    body = mask_text(body)[:300]
    system = platform.system()
    env = os.environ.copy()
    env["BUTLER_TITLE"] = title
    env["BUTLER_BODY"] = body
    try:
        if system == "Darwin":
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    'display notification (system attribute "BUTLER_BODY") '
                    'with title (system attribute "BUTLER_TITLE")',
                ],
                check=False,
                timeout=5,
                capture_output=True,
                env=env,
            )
            return
        if system == "Windows":
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                "$n.Visible = $true; "
                "$n.ShowBalloonTip(5000, $env:BUTLER_TITLE, $env:BUTLER_BODY, 'Info'); "
                "Start-Sleep -Seconds 2; $n.Dispose()"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                check=False,
                timeout=8,
                capture_output=True,
                env=env,
            )
            return
        if shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "--", title, body],
                check=False,
                timeout=5,
                capture_output=True,
                env=env,
            )
    except (OSError, subprocess.SubprocessError):
        return
