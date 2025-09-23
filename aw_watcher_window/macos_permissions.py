import logging
from multiprocessing import Process

logger = logging.getLogger(__name__)


def background_ensure_permissions() -> None:
    permission_process = Process(target=ensure_permissions, args=(()))
    permission_process.start()
    return


def ensure_permissions() -> None:
    # noreorder
    from AppKit import (  # fmt: skip
        NSURL,
        NSAlert,
        NSAlertFirstButtonReturn,
        NSWorkspace,
    )
    from ApplicationServices import AXIsProcessTrusted  # fmt: skip

    accessibility_permissions = AXIsProcessTrusted()
    if not accessibility_permissions:
        title = "Samay Needs Accessibility Permissions"
        info = "Please enable accessibility permissions in System Settings to track window activity."

        alert = NSAlert.new()
        alert.setMessageText_(title)
        alert.setInformativeText_(info)

        alert.addButtonWithTitle_("Open System Settings")
        alert.addButtonWithTitle_("Continue")

        choice = alert.runModal()
        if choice == NSAlertFirstButtonReturn:
            NSWorkspace.sharedWorkspace().openURL_(
                NSURL.URLWithString_(
                    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
                )
            )
