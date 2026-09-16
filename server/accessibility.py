"""Read the focused macOS app's native accessibility tree without UI changes.

This module uses public CoreFoundation / ApplicationServices functions directly,
so AXValue point/size conversion does not depend on PyObjC's partial AX wrapper.
"""

from __future__ import annotations

import ctypes as C
import platform
import time
from collections import deque


class _Pair(C.Structure):
    _fields_ = [("a", C.c_double), ("b", C.c_double)]


class _NativeAX:
    def __init__(self):
        self.cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.ax = C.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        pointer = C.c_void_p

        def bind(lib, name, result, *args):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = result, list(args)
            return fn

        self.release = bind(self.cf, "CFRelease", None, pointer)
        self.retain = bind(self.cf, "CFRetain", pointer, pointer)
        self.hash = bind(self.cf, "CFHash", C.c_ulong, pointer)
        self.type_id = bind(self.cf, "CFGetTypeID", C.c_ulong, pointer)
        self.string_type = bind(self.cf, "CFStringGetTypeID", C.c_ulong)()
        self.bool_type = bind(self.cf, "CFBooleanGetTypeID", C.c_ulong)()
        self.number_type = bind(self.cf, "CFNumberGetTypeID", C.c_ulong)()
        self.value_type = bind(self.ax, "AXValueGetTypeID", C.c_ulong)()
        self.make_string = bind(self.cf, "CFStringCreateWithCString", pointer, pointer, C.c_char_p, C.c_uint32)
        self.string = bind(self.cf, "CFStringGetCString", C.c_bool, pointer, pointer, C.c_long, C.c_uint32)
        self.boolean = bind(self.cf, "CFBooleanGetValue", C.c_bool, pointer)
        self.number = bind(self.cf, "CFNumberGetValue", C.c_bool, pointer, C.c_int, pointer)
        self.array_count = bind(self.cf, "CFArrayGetCount", C.c_long, pointer)
        self.array_item = bind(self.cf, "CFArrayGetValueAtIndex", pointer, pointer, C.c_long)
        self.array_create = bind(self.cf, "CFArrayCreate", pointer, pointer, pointer, C.c_long, pointer)
        self.value_kind = bind(self.ax, "AXValueGetType", C.c_int, pointer)
        self.value = bind(self.ax, "AXValueGetValue", C.c_bool, pointer, C.c_int, pointer)
        self.trusted = bind(self.ax, "AXIsProcessTrusted", C.c_bool)
        self.system = bind(self.ax, "AXUIElementCreateSystemWide", pointer)
        self.get = bind(self.ax, "AXUIElementCopyAttributeValue", C.c_int, pointer, pointer, C.POINTER(pointer))
        self.get_many = bind(self.ax, "AXUIElementCopyMultipleAttributeValues", C.c_int, pointer, pointer, C.c_uint32, C.POINTER(pointer))
        self.get_slice = bind(self.ax, "AXUIElementCopyAttributeValues", C.c_int, pointer, pointer, C.c_long, C.c_long, C.POINTER(pointer))
        self.set_timeout = bind(self.ax, "AXUIElementSetMessagingTimeout", C.c_int, pointer, C.c_float)
        self.pid = bind(self.ax, "AXUIElementGetPid", C.c_int, pointer, C.POINTER(C.c_int))
        self._strings: dict[str, int] = {}
        self.fields = ["AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPosition", "AXSize", "AXEnabled"]
        names = (pointer * len(self.fields))(*[self._name(field) for field in self.fields])
        # Attribute CFStrings are owned by _strings throughout this array's life.
        self._field_array = self.array_create(None, names, len(names), None)

    def _name(self, name):
        if name not in self._strings:
            self._strings[name] = self.make_string(None, name.encode(), 0x08000100)
        return self._strings[name]

    def attr(self, element, name):
        value = C.c_void_p()
        if self.get(element, self._name(name), C.byref(value)) != 0:
            return None
        return value.value

    def convert(self, value):
        if not value:
            return None
        type_id = self.type_id(value)
        if type_id == self.string_type:
            buffer = C.create_string_buffer(8192)
            if self.string(value, buffer, len(buffer), 0x08000100):
                return buffer.value.decode("utf-8", "replace")[:1000]
        elif type_id == self.bool_type:
            return bool(self.boolean(value))
        elif type_id == self.number_type:
            result = C.c_double()
            if self.number(value, 13, C.byref(result)):
                return result.value
        elif type_id == self.value_type:
            kind = self.value_kind(value)
            if kind in (1, 2):  # kAXValueCGPointType, kAXValueCGSizeType
                result = _Pair()
                if self.value(value, kind, C.byref(result)):
                    return result.a, result.b
        return None

    def attributes(self, element):
        array = C.c_void_p()
        if self.get_many(element, self._field_array, 0, C.byref(array)) != 0 or not array.value:
            return {}
        try:
            return {field: self.convert(self.array_item(array, i)) for i, field in enumerate(self.fields) if i < self.array_count(array)}
        finally:
            self.release(array)

    def children(self, element, maximum=60):
        array = C.c_void_p()
        if self.get_slice(element, self._name("AXChildren"), 0, maximum, C.byref(array)) != 0 or not array.value:
            return []
        try:
            return [self.retain(self.array_item(array, i)) for i in range(min(maximum, self.array_count(array)))]
        finally:
            self.release(array)

    def focused(self, timeout=0.12):
        system = self.system()
        try:
            self.set_timeout(system, timeout)
            return self.attr(system, "AXFocusedApplication")
        finally:
            self.release(system)

    def close(self):
        self.release(self._field_array)
        for string in self._strings.values():
            self.release(string)


def focused_application_pid(*, attempts: int = 2, timeout: float = 0.25) -> int | None:
    """Read current AX focus with bounded retries, without Cocoa's cached state."""
    native = None
    try:
        native = _NativeAX()
        if not native.trusted():
            return None
        for attempt in range(min(max(attempts, 1), 3)):
            application = native.focused(timeout=min(max(timeout, 0.05), 0.5))
            if application:
                try:
                    pid = C.c_int()
                    if native.pid(application, C.byref(pid)) == 0 and pid.value > 0:
                        return pid.value
                finally:
                    native.release(application)
            if attempt + 1 < attempts:
                time.sleep(0.025)
        return None
    except (OSError, AttributeError, TypeError, ValueError):
        return None
    finally:
        if native:
            native.close()


def read_accessibility(width: int, height: int, *, backend=None, budget: float = 1.8) -> dict:
    """Return visible AX target boxes and text from the focused window/menu bar.

    Bounded by traversal count, depth, elapsed time and native RPC timeout. No AX
    actions are performed here; targets are later clicked in screenshot pixels.
    """
    result = {"available": False, "engine": "macOS Accessibility", "elements": [], "text": "", "title": "", "pid": None}
    if platform.system() != "Darwin" and backend is None:
        result["hint"] = "原生 Accessibility 樹目前支援 macOS；此平台使用 OCR／視覺模型。"
        return result
    owned = backend is None
    native = backend or _NativeAX()
    queue = deque()
    app = None
    started = time.monotonic()
    try:
        if not native.trusted():
            result["hint"] = "尚未授予輔助使用權限。"
            return result
        app = native.focused()
        if not app:
            result["hint"] = "目前沒有可讀取的前景應用程式。"
            return result
        result["available"] = True
        pid = C.c_int()
        if native.pid(app, C.byref(pid)) == 0:
            result["pid"] = pid.value
        app_info = native.attributes(app)
        result["title"] = app_info.get("AXTitle") or ""
        for name in ("AXFocusedWindow", "AXMenuBar"):
            root = native.attr(app, name)
            if root:
                queue.append((root, 0))
        if not queue:
            queue.append((native.retain(app), 0))
        seen, visited, lines = set(), 0, []
        while queue and visited < 600 and len(result["elements"]) < 250 and time.monotonic() - started < budget:
            node, depth = queue.popleft()
            try:
                fingerprint = native.hash(node)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                visited += 1
                info = native.attributes(node)
                position, size = info.get("AXPosition"), info.get("AXSize")
                role = info.get("AXRole") or "AXElement"
                secure = info.get("AXSubrole") == "AXSecureTextField"
                words = [info.get("AXTitle"), info.get("AXDescription")]
                if not secure and isinstance(info.get("AXValue"), (str, float, int)):
                    words.append(str(info["AXValue"]))
                text = " · ".join(dict.fromkeys(word.strip()[:300] for word in words if isinstance(word, str) and word.strip()))[:500]
                if position and size:
                    x, y = max(0, position[0]), max(0, position[1])
                    right, bottom = min(width, position[0] + size[0]), min(height, position[1] + size[1])
                    if right > x and bottom > y:
                        element_id = f"ax_{len(result['elements']) + 1}"
                        element = {"id": element_id, "role": role.removeprefix("AX"), "text": text, "x": round((x + right) / 2, 1), "y": round((y + bottom) / 2, 1), "width": round(right - x, 1), "height": round(bottom - y, 1), "source": "accessibility", "confidence": 1.0, "disabled": info.get("AXEnabled") is False}
                        value = info.get("AXValue")
                        if not secure and isinstance(value, (str, float, int, bool)):
                            element["value"] = value[:1200] if isinstance(value, str) else value
                        if role in {"AXCheckBox", "AXRadioButton"} and isinstance(value, (bool, int, float)):
                            element["checked"] = bool(value)
                        result["elements"].append(element)
                        if text:
                            lines.append(f"[{element_id}] {element['role']}: {text}")
                if depth < 14:
                    queue.extend((child, depth + 1) for child in native.children(node))
            finally:
                native.release(node)
        result["text"] = "\n".join(lines)[:20000]
        result["truncated"] = bool(queue)
        return result
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        result["hint"] = f"原生 Accessibility 暫時無法讀取（{type(exc).__name__}）。"
        return result
    finally:
        while queue:
            native.release(queue.popleft()[0])
        if app:
            native.release(app)
        if owned:
            native.close()
