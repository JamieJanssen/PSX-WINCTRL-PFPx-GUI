import os
import sys
import time
import shutil
import threading
import configparser

import psx_winctrl_pfp_core as core

VERSION = "1.57"
core.VERSION = VERSION


class _BridgeControlEvent:
    """Present one Event-like object to the legacy bridge, with a separate restart flag."""

    def __init__(self):
        self.shutdown_event = threading.Event()
        self.restart_event = threading.Event()

    def set(self):
        self.shutdown_event.set()

    def clear(self):
        self.shutdown_event.clear()

    def is_set(self):
        return self.shutdown_event.is_set() or self.restart_event.is_set()

    def wait(self, timeout=None):
        if self.is_set():
            return True

        if timeout is None:
            while not self.is_set():
                time.sleep(0.05)
            return True

        end_time = time.monotonic() + max(0.0, float(timeout))
        while not self.is_set():
            remaining = end_time - time.monotonic()
            if remaining <= 0:
                return False
            self.shutdown_event.wait(min(0.05, remaining))
        return True


BRIDGE_CONTROL = _BridgeControlEvent()
core.SHUTDOWN_REQUESTED = BRIDGE_CONTROL


def _configure_config_path():
    """Use Application Support for the editable INI on macOS; keep Windows unchanged."""
    legacy_file = os.path.join(core.get_app_dir(), "psx_winctrl_pfp.ini")

    if sys.platform != "darwin":
        core.CONFIG_FILE = legacy_file
        return

    config_dir = os.path.expanduser(
        "~/Library/Application Support/PSX WINCTRL PFPx"
    )
    os.makedirs(config_dir, exist_ok=True)
    config_file = os.path.join(config_dir, "psx_winctrl_pfp.ini")

    if not os.path.exists(config_file) and os.path.exists(legacy_file):
        try:
            shutil.copy2(legacy_file, config_file)
        except OSError:
            pass

    core.CONFIG_FILE = config_file


_configure_config_path()


DEVICE_CHOICES = (
    ("PFP3N Captain", 0xBB35, "31BB"),
    ("PFP3N Observer", 0xBB39, "31BB"),
    ("PFP3N First Officer", 0xBB3D, "31BB"),
    ("MCDU Captain", 0xBB36, "32BB"),
    ("MCDU Observer", 0xBB3A, "32BB"),
    ("MCDU First Officer", 0xBB3E, "32BB"),
    ("PFP7 Captain", 0xBB37, "33BB"),
    ("PFP7 Observer", 0xBB3B, "33BB"),
    ("PFP7 First Officer", 0xBB3F, "33BB"),
    ("PFP4 Captain", 0xBB38, "34BB"),
    ("PFP4 Observer", 0xBB3C, "34BB"),
    ("PFP4 First Officer", 0xBB40, "34BB"),
)
DEVICE_CHOICE_BY_PID = {
    pid: (label, pid, did) for label, pid, did in DEVICE_CHOICES
}


_ORIGINAL_GUI_INIT = core.BridgeGui.__init__
_ORIGINAL_DRAW = core.BridgeGui._draw
_ORIGINAL_ON_CLICK = core.BridgeGui._on_click
_ORIGINAL_TOGGLE_MINI = core.BridgeGui._toggle_mini_mode
_ORIGINAL_SHOW_ABOUT = core.BridgeGui._show_about
_ORIGINAL_LOG = core.log


def _patched_log(message):
    if (
        message == "[END]"
        and BRIDGE_CONTROL.restart_event.is_set()
        and not BRIDGE_CONTROL.shutdown_event.is_set()
    ):
        return
    _ORIGINAL_LOG(message)


core.log = _patched_log


def _gui_init(self):
    _ORIGINAL_GUI_INIT(self)
    self.settings_open = False
    self.device_dropdown_open = False
    self.settings_panel_bounds = None
    self.device_field_bounds = None
    self.device_option_bounds = []


def _configured_device_choice(self):
    cfg = configparser.ConfigParser()
    try:
        cfg.read(core.CONFIG_FILE, encoding="utf-8")
        pid = int(
            cfg.get("FMC", "PID", fallback=f"{core.PFP_PRODUCT_ID:04X}"),
            16,
        )
    except (ValueError, configparser.Error, OSError):
        pid = core.PFP_PRODUCT_ID

    did = f"{core.PFP_DEST[0]:02X}{core.PFP_DEST[1]:02X}"
    return DEVICE_CHOICE_BY_PID.get(
        pid,
        (core.pfp_device_label(pid), pid, did),
    )


def _show_settings(self):
    self.menu_open = False
    self.about_open = False
    self.settings_open = True
    self.device_dropdown_open = False
    self._draw()


def _close_settings(self):
    self.settings_open = False
    self.device_dropdown_open = False
    self._draw()


def _show_about(self):
    self.settings_open = False
    self.device_dropdown_open = False
    _ORIGINAL_SHOW_ABOUT(self)


def _toggle_mini_mode(self):
    self.settings_open = False
    self.device_dropdown_open = False
    _ORIGINAL_TOGGLE_MINI(self)


def _bridge_thread_main():
    while not BRIDGE_CONTROL.shutdown_event.is_set():
        BRIDGE_CONTROL.restart_event.clear()
        core.bridge_main()

        core.BRIDGE_PSX = None
        if core.GUI_APP is not None:
            core.GUI_APP.set_psx_sender(None)

        if BRIDGE_CONTROL.shutdown_event.is_set():
            break
        if BRIDGE_CONTROL.restart_event.is_set():
            continue
        break


def _start_bridge_thread(self):
    if self.stopping or BRIDGE_CONTROL.shutdown_event.is_set():
        return
    if self.bridge_thread is not None and self.bridge_thread.is_alive():
        return

    bridge_thread = threading.Thread(
        target=_bridge_thread_main,
        name="PSX Bridge",
    )
    self.set_bridge_thread(bridge_thread)
    bridge_thread.start()


def _select_device(self, choice):
    label, pid, did = choice

    # save_ini_value changes only the matching key line and therefore keeps
    # comments, ordering, blank lines, and all unrelated INI content intact.
    core.save_ini_value("FMC", "PID", f"{pid:04X}")
    core.save_ini_value("FMC", "DID", did)

    core.PFP_PRODUCT_ID = pid
    core.PFP_DEST = bytes([int(did[:2], 16), int(did[2:], 16)])
    core.PFP_DEVICE_LABEL = core.pfp_device_label(pid)
    self.root.title(self._display_title())

    core.log(
        f"[CONFIG] Device set to {label} "
        f"(PID={pid:04X} DID={did})"
    )

    self.settings_open = False
    self.device_dropdown_open = False

    if self.bridge_thread is not None and self.bridge_thread.is_alive():
        core.log("[HID] switching device...")
        BRIDGE_CONTROL.restart_event.set()
    else:
        BRIDGE_CONTROL.restart_event.clear()
        self._start_bridge_thread()

    self._draw()


def _draw_menu(self, right):
    if self.mini_mode or not self.menu_open:
        self.menu_bounds = None
        return

    x2 = right
    x1 = x2 - 170
    y1 = 42
    y2 = y1 + 120
    self.canvas.create_rectangle(
        x1, y1, x2, y2,
        fill=self.MENU_BG,
        outline=self.BORDER_FG,
        width=1,
    )

    check = "✓ " if core.DEBUG else "   "
    items = (
        "Copy log",
        f"{check}Debug logging",
        "Settings",
        "About",
    )
    for index, item in enumerate(items):
        line_y = y1 + 15 + index * 30
        self.canvas.create_text(
            x1 + 10,
            line_y,
            text=item,
            anchor="w",
            fill=self.TEXT_FG,
            font=("Helvetica", 11),
        )
        if index < len(items) - 1:
            divider_y = y1 + (index + 1) * 30
            self.canvas.create_line(
                x1, divider_y, x2, divider_y,
                fill=self.BORDER_FG,
            )

    self.menu_bounds = (x1, y1, x2, y2)


def _draw_settings(self, width, height):
    self.settings_panel_bounds = None
    self.device_field_bounds = None
    self.device_option_bounds = []

    if self.mini_mode or not self.settings_open:
        return

    x1 = 52
    y1 = 55
    x2 = width - 52
    y2 = height - 30
    self.canvas.create_rectangle(
        x1, y1, x2, y2,
        fill=self.PANEL_BG,
        outline=self.BORDER_FG,
        width=1,
    )
    self.settings_panel_bounds = (x1, y1, x2, y2)

    self.canvas.create_text(
        (x1 + x2) / 2,
        y1 + 24,
        text="Settings",
        anchor="center",
        fill=self.TEXT_FG,
        font=("Helvetica", 14, "bold"),
    )
    self.canvas.create_text(
        x1 + 18,
        y1 + 57,
        text="Device",
        anchor="w",
        fill=self.TEXT_FG,
        font=("Helvetica", 11, "bold"),
    )

    field_x1 = x1 + 88
    field_y1 = y1 + 42
    field_x2 = x2 - 18
    field_y2 = field_y1 + 28
    current_label = self._configured_device_choice()[0]

    self.canvas.create_rectangle(
        field_x1, field_y1, field_x2, field_y2,
        fill="#FFFFFF",
        outline=self.BORDER_FG,
        width=1,
    )
    self.canvas.create_text(
        field_x1 + 9,
        (field_y1 + field_y2) / 2,
        text=current_label,
        anchor="w",
        fill=self.TEXT_FG,
        font=("Helvetica", 10),
    )
    self.canvas.create_text(
        field_x2 - 10,
        (field_y1 + field_y2) / 2,
        text="▼",
        anchor="center",
        fill=self.MUTED_FG,
        font=("Helvetica", 9),
    )
    self.device_field_bounds = (
        field_x1, field_y1, field_x2, field_y2
    )

    if self.device_dropdown_open:
        row_h = 17
        list_y1 = field_y2
        list_y2 = list_y1 + row_h * len(DEVICE_CHOICES)
        self.canvas.create_rectangle(
            field_x1, list_y1, field_x2, list_y2,
            fill="#FFFFFF",
            outline=self.BORDER_FG,
            width=1,
        )

        for index, choice in enumerate(DEVICE_CHOICES):
            row_y1 = list_y1 + index * row_h
            row_y2 = row_y1 + row_h
            if choice[0] == current_label:
                self.canvas.create_rectangle(
                    field_x1 + 1, row_y1,
                    field_x2 - 1, row_y2,
                    fill="#E3D8C9",
                    outline="",
                )
            self.canvas.create_text(
                field_x1 + 9,
                (row_y1 + row_y2) / 2,
                text=choice[0],
                anchor="w",
                fill=self.TEXT_FG,
                font=("Helvetica", 9),
            )
            self.device_option_bounds.append(
                (choice, (field_x1, row_y1, field_x2, row_y2))
            )
    else:
        self.canvas.create_text(
            (x1 + x2) / 2,
            y2 - 18,
            text="Click outside to close",
            anchor="center",
            fill=self.MUTED_FG,
            font=("Helvetica", 10),
        )


def _draw(self, event=None):
    _ORIGINAL_DRAW(self, event)
    if not self.mini_mode:
        width = max(self.canvas.winfo_width(), self.FULL_WIDTH)
        height = max(self.canvas.winfo_height(), self.FULL_HEIGHT)
        self._draw_settings(width, height)


def _on_click(self, event):
    if self.mini_mode:
        return _ORIGINAL_ON_CLICK(self, event)

    if self.settings_open:
        if self.device_dropdown_open:
            for choice, bounds in self.device_option_bounds:
                x1, y1, x2, y2 = bounds
                if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                    self._select_device(choice)
                    return

        if self.device_field_bounds:
            x1, y1, x2, y2 = self.device_field_bounds
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.device_dropdown_open = not self.device_dropdown_open
                self._draw()
                return

        if self.settings_panel_bounds:
            x1, y1, x2, y2 = self.settings_panel_bounds
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                return

        self._close_settings()
        return

    if self.about_open:
        return _ORIGINAL_ON_CLICK(self, event)

    if self.menu_open and self.menu_bounds:
        x1, y1, x2, y2 = self.menu_bounds
        if x1 <= event.x <= x2 and y1 <= event.y <= y2:
            item = int((event.y - y1) // 30)
            if item == 2:
                self._show_settings()
                return
            if item == 3:
                self._show_about()
                return

    return _ORIGINAL_ON_CLICK(self, event)


core.BridgeGui.__init__ = _gui_init
core.BridgeGui._configured_device_choice = _configured_device_choice
core.BridgeGui._show_settings = _show_settings
core.BridgeGui._close_settings = _close_settings
core.BridgeGui._show_about = _show_about
core.BridgeGui._toggle_mini_mode = _toggle_mini_mode
core.BridgeGui._start_bridge_thread = _start_bridge_thread
core.BridgeGui._select_device = _select_device
core.BridgeGui._draw_menu = _draw_menu
core.BridgeGui._draw_settings = _draw_settings
core.BridgeGui._draw = _draw
core.BridgeGui._on_click = _on_click


def main():
    core.GUI_APP = core.BridgeGui()
    core.GUI_APP.root.after(100, core.GUI_APP._start_bridge_thread)
    core.GUI_APP.run()


if __name__ == "__main__":
    main()
