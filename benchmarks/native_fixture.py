#!/usr/bin/env python3
"""A real, single-window AppKit form. Only its Save click writes the oracle.

This file intentionally has no model, browser, automation or IPC action handler.
The ready file describes process/window bounds only, never expected form values.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys


TITLE = "ComputerUSE Native Benchmark"


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Open a dedicated native AppKit benchmark form; Save writes local JSON.")
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This fixture requires macOS and PyObjC AppKit.")
    ready_path, oracle_path = args.ready.resolve(), args.output.resolve()
    if ready_path.exists() or oracle_path.exists():
        parser.error("Use new ready/output paths; existing benchmark results are never overwritten at launch.")
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    oracle_path.parent.mkdir(parents=True, exist_ok=True)

    import AppKit as A
    import Foundation as F

    class FixtureDelegate(A.NSObject):
        def applicationDidFinishLaunching_(self, notification):
            # A programmatic AppKit app has no nib-provided Edit menu. Standard
            # Cocoa editing key equivalents need these responder-chain actions.
            menu = A.NSMenu.alloc().initWithTitle_("Main Menu")
            application_item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Application", None, "")
            application_item.setSubmenu_(A.NSMenu.alloc().initWithTitle_(TITLE))
            menu.addItem_(application_item)
            edit_item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
            edit_menu = A.NSMenu.alloc().initWithTitle_("Edit")
            for title, action, key in (("Select All", "selectAll:", "a"), ("Paste", "paste:", "v"), ("Copy", "copy:", "c"), ("Cut", "cut:", "x")):
                item = A.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
                item.setKeyEquivalentModifierMask_(A.NSEventModifierFlagCommand)
                edit_menu.addItem_(item)
            edit_item.setSubmenu_(edit_menu)
            menu.addItem_(edit_item)
            A.NSApp.setMainMenu_(menu)
            screen = A.NSScreen.screens()[0]
            visible = screen.visibleFrame()
            width, height = 720.0, 420.0
            origin = (visible.origin.x + (visible.size.width - width) / 2, visible.origin.y + (visible.size.height - height) / 2)
            self.window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                (origin, (width, height)), A.NSWindowStyleMaskTitled, A.NSBackingStoreBuffered, False
            )
            self.window.setTitle_(TITLE)
            self.window.setMovable_(False)
            self.window.setReleasedWhenClosed_(False)
            self.window.setDelegate_(self)
            self.window.setBackgroundColor_(A.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.97, 0.965, 0.945, 1))
            content = self.window.contentView()

            def label(text, frame, size=16):
                view = A.NSTextField.labelWithString_(text)
                view.setFrame_(frame)
                view.setFont_(A.NSFont.systemFontOfSize_(size))
                view.setAccessibilityLabel_(text)
                content.addSubview_(view)
                return view

            label("Project settings", ((40, 350), (640, 42)), 29)
            label("Project name", ((42, 300), (500, 25)), 17)
            self.name_field = A.NSTextField.textFieldWithString_("Untitled project")
            self.name_field.setFrame_(((40, 249), (638, 42)))
            self.name_field.setFont_(A.NSFont.systemFontOfSize_(21))
            self.name_field.setAccessibilityLabel_("Project name")
            self.name_field.setAccessibilityIdentifier_("benchmark-project-name")
            content.addSubview_(self.name_field)

            self.notifications = A.NSButton.alloc().initWithFrame_(((40, 181), (450, 36)))
            self.notifications.setButtonType_(A.NSSwitchButton)
            self.notifications.setTitle_("Enable notifications")
            self.notifications.setFont_(A.NSFont.systemFontOfSize_(19))
            self.notifications.setState_(A.NSControlStateValueOff)
            self.notifications.setAccessibilityLabel_("Enable notifications")
            self.notifications.setAccessibilityIdentifier_("benchmark-notifications")
            content.addSubview_(self.notifications)

            self.status = label("Not saved", ((40, 118), (640, 32)), 18)
            self.status.setAccessibilityIdentifier_("benchmark-save-status")
            self.save_button = A.NSButton.alloc().initWithFrame_(((495, 53), (185, 45)))
            self.save_button.setTitle_("Save project")
            self.save_button.setBezelStyle_(A.NSBezelStyleRounded)
            self.save_button.setFont_(A.NSFont.systemFontOfSize_(19))
            self.save_button.setTarget_(self)
            self.save_button.setAction_("saveProject:")
            self.save_button.setKeyEquivalent_("")
            self.save_button.setAccessibilityLabel_("Save project")
            self.save_button.setAccessibilityIdentifier_("benchmark-save")
            content.addSubview_(self.save_button)
            label("Local benchmark fixture — changes stay in this window.", ((40, 14), (640, 23)), 13)
            self.save_count = 0
            self.window.makeKeyAndOrderFront_(None)
            self.window.makeFirstResponder_(self.name_field)
            A.NSApp.activateIgnoringOtherApps_(True)

            def record_event(event):
                # Numeric transport diagnostics only; no text or oracle writes.
                kind = int(event.type())
                if kind in (A.NSEventTypeLeftMouseDown, A.NSEventTypeLeftMouseUp):
                    point = event.locationInWindow()
                    print(json.dumps({"event_type": kind, "window_id": int(event.windowNumber()), "local_x": float(point.x), "local_y": float(point.y)}), flush=True)
                return event
            self.event_monitor = A.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(A.NSEventMaskLeftMouseDown | A.NSEventMaskLeftMouseUp, record_event)

            frame = self.window.frame()
            rect = self.window.contentRectForFrameRect_(frame)
            screen_height = screen.frame().size.height

            def top_left(rectangle):
                return {"x": float(rectangle.origin.x), "y": float(screen_height - rectangle.origin.y - rectangle.size.height), "width": float(rectangle.size.width), "height": float(rectangle.size.height)}

            atomic_json(ready_path, {"pid": os.getpid(), "window_id": int(self.window.windowNumber()), "title": TITLE,
                                     "nonce": args.nonce, "window_bounds": top_left(frame), "content_bounds": top_left(rect),
                                     "coordinate_space": "primary_screen_logical_pixels"})

        def saveProject_(self, sender):
            # A keyboard shortcut, AXPress or direct script call is not the oracle.
            # This benchmark requires the actual native button's mouse-up action.
            event = A.NSApp.currentEvent()
            if sender != self.save_button or event is None or event.type() != A.NSEventTypeLeftMouseUp:
                self.status.setStringValue_("Click Save project to save")
                return
            name = str(self.name_field.stringValue()).strip()
            if not name:
                self.status.setStringValue_("Project name is required")
                return
            enabled = self.notifications.state() == A.NSControlStateValueOn
            self.save_count += 1
            atomic_json(oracle_path, {"nonce": args.nonce, "writer_pid": os.getpid(), "source": "native_save_button_mouse_up",
                                     "project_name": name, "notifications_enabled": bool(enabled), "save_count": self.save_count,
                                     "saved_at": datetime.now(timezone.utc).isoformat()})
            self.status.setStringValue_("Saved successfully · Notifications " + ("enabled" if enabled else "disabled"))
            self.status.setAccessibilityLabel_(str(self.status.stringValue()))

        def applicationShouldTerminateAfterLastWindowClosed_(self, app):
            return True

        def applicationSupportsSecureRestorableState_(self, app):
            return True

        def applicationDidResignActive_(self, notification):
            event = A.NSApp.currentEvent()
            event_type = int(event.type()) if event is not None else None
            print(f"fixture_resigned_active event_type={event_type}", flush=True)

    F.NSProcessInfo.processInfo().setProcessName_(TITLE)
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
    delegate = FixtureDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
