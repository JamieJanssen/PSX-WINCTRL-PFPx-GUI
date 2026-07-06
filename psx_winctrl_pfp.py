import os
import sys
import signal
import configparser
import base64
import socket
import time
import threading
import queue
import re
os.environ["TK_SILENCE_DEPRECATION"] = "1"
import tkinter as tk
import tkinter.font as tkfont
from collections import defaultdict
import hid


def get_app_dir():
    """Return the executable/script folder used for editable settings."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)

    return os.path.dirname(os.path.abspath(__file__))






# ============================================================
# Version / debug
# ============================================================

VERSION = "1.54"
APPLICATION_TITLE = "PSX WINCTRL PFPx Bridge"
GUI_APPLICATION_TITLE = "PSX PFPx Bridge"
LOG_FONT_FAMILY = "Menlo" if sys.platform == "darwin" else "Consolas"
DEBUG = "--debug" in [arg.lower() for arg in sys.argv[1:]]

SHUTDOWN_REQUESTED = threading.Event()


def _handle_sigint(signum, frame):
    """Request a clean shutdown without raising KeyboardInterrupt.

    PyInstaller/macOS can otherwise raise repeated KeyboardInterrupt exceptions
    during cleanup, which may abort before settings are saved.
    """
    SHUTDOWN_REQUESTED.set()


# ============================================================
# Application files
# ============================================================

SCRIPT_DIR = get_app_dir()
CONFIG_FILE = os.path.join(SCRIPT_DIR, "psx_winctrl_pfp.ini")


# ============================================================
# HID / timing
# ============================================================

READ_SIZE = 64
BYTE_LIMIT = 17

# Active CDU can be changed at runtime from the scratchpad:
#   CDU-L = Left CDU
#   CDU-C = Center CDU
#   CDU-R = Right CDU
DEFAULT_CDU = "L"

CDU_CONFIGS = {
    "L": {
        "label": "Left",
        "key_qh": "Qh401",
        "screen_qs_lines": list(range(62, 76)),
        "lights_qi": 86,
        "blank_qi": 89,
        "lcd_qi248_bit": 1 << 19,
    },
    "C": {
        "label": "Center",
        "key_qh": "Qh402",
        "screen_qs_lines": list(range(76, 90)),
        "lights_qi": 87,
        "blank_qi": 90,
        "lcd_qi248_bit": 1 << 21,
    },
    "R": {
        "label": "Right",
        "key_qh": "Qh403",
        "screen_qs_lines": list(range(90, 104)),
        "lights_qi": 88,
        "blank_qi": 91,
        "lcd_qi248_bit": 1 << 20,
    },
}

MIN_SEND_INTERVAL = 0.02
STABLE_FRAMES = 2
RISING_COOLDOWN = 0.10

# ============================================================
# Display / runtime defaults
# ============================================================


DEFAULT_CDU_COLOR = "w"
DEFAULT_CDU_ATC_ALTN = 1  # 1 = replace ATC key, 0 = keep original ATC key
CDU_ATC_ALTN_SEQUENCE = [65, 42] #Key sequence FMC COMM + LSK2 Left > opens ALTN Page

CDU_SWITCH_CLEAR_SEQUENCE = [39, -1]

# Bridge scratchpad commands are removed by holding CLR briefly.
# PSX treats a held CLR key as "clear scratchpad".
CDU_COMMAND_CLEAR_HOLD_SECONDS = 1.00


class RuntimeConfig:
    def __init__(self):
        self.lock = threading.Lock()
        self.active_cdu = DEFAULT_CDU
        self.active_cdu_dirty = False
        self.brightness_step = 16
        self.brightness_dirty = False
        self.cdu_color = DEFAULT_CDU_COLOR
        self.cdu_atc_altn = DEFAULT_CDU_ATC_ALTN
        self.cdu_atc_altn_user = DEFAULT_CDU_ATC_ALTN
        self.nextgen_fmc = True
        self.mode = "DEFAULT"

    def set_ng(self):
        with self.lock:
            self.cdu_color = "w"
            self.nextgen_fmc = True
            self.cdu_atc_altn = self.cdu_atc_altn_user
            self.mode = "FMC_NG"

    def set_legacy(self):
        with self.lock:
            self.cdu_color = "g"
            self.nextgen_fmc = False
            self.cdu_atc_altn = 0
            self.mode = "FMC_LEGACY"

    def set_active_cdu(self, cdu, mark_dirty=True):
        cdu = cdu.upper()

        if cdu not in CDU_CONFIGS:
            return False

        with self.lock:
            if self.active_cdu != cdu:
                self.active_cdu = cdu
                if mark_dirty:
                    self.active_cdu_dirty = True

        return True

    def get_active_cdu(self):
        with self.lock:
            return self.active_cdu

    def is_active_cdu_dirty(self):
        with self.lock:
            return self.active_cdu_dirty

    def set_brightness_step(self, step, mark_dirty=True):
        step = max(0, min(PFP_SCREEN_BRIGHTNESS_STEPS - 1, int(step)))

        with self.lock:
            if self.brightness_step != step:
                self.brightness_step = step
                if mark_dirty:
                    self.brightness_dirty = True

        return step

    def get_brightness_step(self):
        with self.lock:
            return self.brightness_step

    def is_brightness_dirty(self):
        with self.lock:
            return self.brightness_dirty

    def get_active_cdu_config(self):
        with self.lock:
            return CDU_CONFIGS[self.active_cdu]

    def set_from_qi248(self, nextgen_fmc, cdu_lcd):
        with self.lock:
            self.cdu_color = "w" if cdu_lcd else "g"
            self.nextgen_fmc = bool(nextgen_fmc)

            # ATC/ALTN behavior is user-configurable when NG FMC is active.
            # When Legacy FMC is active, force the original ATC key at runtime only.
            # Do not save this forced runtime change to psx_winctrl_pfp.ini.
            self.cdu_atc_altn = self.cdu_atc_altn_user if self.nextgen_fmc else 0

            active_cdu = self.active_cdu

            if nextgen_fmc and cdu_lcd:
                self.mode = f"QI248_{active_cdu}_NG_LCD"
            elif nextgen_fmc:
                self.mode = f"QI248_{active_cdu}_NG_CRT"
            elif cdu_lcd:
                self.mode = f"QI248_{active_cdu}_LEGACY_LCD"
            else:
                self.mode = f"QI248_{active_cdu}_LEGACY_CRT"

    def get_cdu_color(self):
        with self.lock:
            return self.cdu_color

    def set_cdu_atc_altn(self, atc_altn):
        with self.lock:
            self.cdu_atc_altn_user = 1 if int(atc_altn) == 1 else 0
            self.cdu_atc_altn = self.cdu_atc_altn_user if self.nextgen_fmc else 0


    def get_cdu_atc_altn(self):
        with self.lock:
            return self.cdu_atc_altn

    def get_cdu_atc_altn_user(self):
        with self.lock:
            return self.cdu_atc_altn_user


    def get_mode(self):
        with self.lock:
            return self.mode


RUNTIME_CONFIG = RuntimeConfig()

FMC_WIDTH = 24
FMC_HEIGHT = 14

# PSX CDU LCD color strings. Each CDU has 14 Qs lines:
#   Ti, 1s, 1b, 2s, 2b, ... 6s, 6b, Sp
# These map directly to the 14 displayed CDU rows.
CDU_COLOR_QS_START = {
    "L": 500,
    "C": 514,
    "R": 528,
}
CDU_COLOR_QS_RANGE = range(500, 542)

# Convert PSX LCD colour codes to the corresponding PFP display colour codes.
# The PFP font uses "o" for blue and "e" for grey.
PSX_TO_PFP_COLOR = {
    "a": "a",  # amber
    "b": "o",  # blue
    "c": "c",  # cyan
    "g": "g",  # green
    "m": "m",  # magenta
    "r": "r",  # red
    "w": "w",  # white
}

# PSX CDU light bitmask:
# Left CDU / Captain = Qi86
# Center CDU        = Qi87
# Right CDU / FO    = Qi88
#
# PSX FMC/CDU options bitmask:
# Qi248 bit 13 = Next Gen FMC
# Qi248 bit 19 = Left CDU LCD
# Qi248 bit 21 = Center CDU LCD
# Qi248 bit 20 = Right CDU LCD
PSX_FMC_CONFIG_QI = 248
QI248_NEXTGEN_FMC_BIT = 1 << 13

# PSX CDU blanking timers:
# Qi89 = BlankTimeCduL, Qi90 = BlankTimeCduC, Qi91 = BlankTimeCduR.
# The CDU screen must be blanked whenever the related value is non-zero.
CDU_BLANK_QI = {
    "L": 89,
    "C": 90,
    "R": 91,
}
CDU_BLANK_QI_TO_CDU = {qi: cdu for cdu, qi in CDU_BLANK_QI.items()}

MDT_ALL_LIGHTS_BIT = 0x2000  # 8192

PSX_LIGHT_BITS_TO_PFP = {
    0x0001: "EXEC",
    0x0002: "DSPY",
    0x0004: "FAIL",
    0x0008: "MSG",
    0x0010: "OFST",
}

NEED_RELEASE = {39, 60}  # CLR, ATC require explicit release on hardware key release

# WINCTRL USB Vendor ID
WINCTRL_VENDOR_ID = 0x4098

# Default PFPx product and destination IDs.
PFP_PRODUCT_ID = 0xBB37
PFP_DEST = bytes([0x33, 0xBB])

# GUI title suffixes, determined from the configured [FMC] pid.
PFP_DEVICE_LABELS = {
    0xBB35: "PFP3N CPT",
    0xBB39: "PFP3N OBS",
    0xBB3D: "PFP3N FO",
    0xBB36: "MCDU CPT",
    0xBB3A: "MCDU OBS",
    0xBB3E: "MCDU FO",
    0xBB37: "PFP7 CPT",
    0xBB3B: "PFP7 OBS",
    0xBB3F: "PFP7 FO",
    0xBB38: "PFP4 CPT",
    0xBB3C: "PFP4 OBS",
    0xBB40: "PFP4 FO",
}
PFP_DEVICE_LABEL = PFP_DEVICE_LABELS[PFP_PRODUCT_ID]


def pfp_device_label(pid):
    """Return a concise PFPx model/position label for a configured PID."""
    return PFP_DEVICE_LABELS.get(pid, f"PID {pid:04X}")

# PFPx annunciator LED channels.
PFP_LEDS = {
    "DSPY": 0x03,
    "FAIL": 0x04,
    "MSG":  0x05,
    "OFST": 0x06,
    "EXEC": 0x07,
}

PFP_LED_INTENSITY = 255

# PFPx screen and key backlight settings.
PFP_SCREEN_BACKLIGHT_CHANNEL = 0x01
PFP_KEY_BACKLIGHT_CHANNEL = 0x00
PFP_SCREEN_BRIGHTNESS_MIN = 10
PFP_SCREEN_BRIGHTNESS_MAX = 255
PFP_SCREEN_BRIGHTNESS_STEPS = 24
PFP_SCREEN_BRIGHTNESS_DEFAULT_STEP = 16

# BRT+/- hold and repeat timing.
PFP_BRT_HOLD_DELAY = 0.5
PFP_BRT_FULL_RANGE_SECONDS = 2.6
PFP_BRT_REPEAT_INTERVAL = PFP_BRT_FULL_RANGE_SECONDS / (PFP_SCREEN_BRIGHTNESS_STEPS - 1)

# Periodically re-send the current backlight values to keep the HID output side active.
HID_BRIGHTNESS_KEEPALIVE_SECONDS = 15.0

# Temporary scratchpad brightness indication.
BRIGHTNESS_OVERLAY_SECONDS = 2.2
BRIGHTNESS_BLOCK = "\u2588"  # U+2588 FULL BLOCK


def brightness_value_from_step(step):
    step = max(0, min(PFP_SCREEN_BRIGHTNESS_STEPS - 1, step))

    span = PFP_SCREEN_BRIGHTNESS_MAX - PFP_SCREEN_BRIGHTNESS_MIN
    value = PFP_SCREEN_BRIGHTNESS_MIN + round(
        span * step / (PFP_SCREEN_BRIGHTNESS_STEPS - 1)
    )

    return max(0, min(255, value))

PSX_NAME_TO_CODE = {
    "LSKL1": 41, "LSKL2": 42, "LSKL3": 43, "LSKL4": 44, "LSKL5": 45, "LSKL6": 46,
    "LSKR1": 51, "LSKR2": 52, "LSKR3": 53, "LSKR4": 54, "LSKR5": 55, "LSKR6": 56,
    "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16, "H": 17, "I": 18, "J": 19,
    "K": 20, "L": 21, "M": 22, "N": 23, "O": 24, "P": 25, "Q": 26, "R": 27, "S": 28, "T": 29,
    "U": 30, "V": 31, "W": 32, "X": 33, "Y": 34, "Z": 35,
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "SP": 36, "DEL": 37, "/": 38, "CLR": 39,
    "+/-": 68, "+": 68, ".": 67,
    "INIT": 57, "INITREF": 57, "INIT REF": 57,
    "ROUTE": 58, "DEPARR": 59, "DEP/ARR": 59, "ATC": 60, "VNAV": 61, "FIX": 62,
    "LEGS": 63, "HOLD": 64, "FMC": 65, "PROG": 66,
    "MENU": 47, "NAVRAD": 48, "NAV/RAD": 48, "PREV": 49, "NEXT": 50,
    "EXEC": 40,
}

def clear_console():
    os.system("cls" if os.name == "nt" else "clear")


clear_console()


class StatusLog:
    def __init__(self):
        self.lock = threading.Lock()

    @staticmethod
    def _header_text():
        return (
            f"{APPLICATION_TITLE}  v{VERSION}\n"
            "Jamie Janssen © 2026"
        )

    def header(self):
        self.log(self._header_text())

    def reconnect_screen(self):
        self.log("[PSX] reconnecting...")

    def start(self):
        self.header()
        self.log("Starting...")

    def log(self, message):
        message = str(message)
        with self.lock:
            if GUI_APP is not None:
                GUI_APP.add_log(message)
            else:
                print(message, flush=True)


STATUS = StatusLog()
GUI_APP = None
BRIDGE_PSX = None


def log(message):
    STATUS.log(message)


def log_debug(message):
    if DEBUG:
        log(message)


def log_highlight(message):
    log(message)


class BridgeGui:
    """Canvas-based status window with an optional always-on-top mini mode."""

    WINDOW_BG = "#79553C"
    PANEL_BG = "#E9E1D4"
    TEXT_FG = "#000000"
    MUTED_FG = "#5A4030"
    BORDER_FG = "#B39C82"
    TITLE_FG = "#FFF8EE"
    BUTTON_BG = "#9A7550"
    BUTTON_ACTIVE_BG = "#2F7D4A"
    BUTTON_ACTIVE_TEXT = "#FFF8EE"
    BUTTON_TEXT = "#FFF8EE"
    MENU_BG = "#E9E1D4"

    FULL_WIDTH = 460
    FULL_HEIGHT = 365
    MINI_WIDTH = 258
    MINI_HEIGHT = 46

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(self._display_title())
        self.root.geometry(f"{self.FULL_WIDTH}x{self.FULL_HEIGHT}")
        self.root.minsize(self.FULL_WIDTH, self.FULL_HEIGHT)
        self.root.configure(background=self.WINDOW_BG)
        self.root.protocol("WM_DELETE_WINDOW", self.request_stop)

        self.bridge_thread = None
        self.psx_sender = None
        self.close_when_stopped = False
        self.stopping = False
        self.mini_mode = False
        self.full_geometry = f"{self.FULL_WIDTH}x{self.FULL_HEIGHT}"
        self.mini_geometry = None
        self.mini_drag_anchor = None
        self.log_queue = queue.Queue()
        self.log_lines = []
        self.log_scroll_offset = 0
        self.log_thumb_bounds = None
        self.log_track_bounds = None
        self.log_dragging = False
        self.menu_bounds = None
        self.menu_button_bounds = None
        self.menu_open = False
        self.about_open = False
        self.button_bounds = {}
        self.log_font = tkfont.Font(family=LOG_FONT_FAMILY, size=10)
        self.log_content_width = 0

        self.canvas = tk.Canvas(
            self.root,
            background=self.WINDOW_BG,
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._draw)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", lambda _event: self._scroll_log(-1))
        self.canvas.bind("<Button-5>", lambda _event: self._scroll_log(+1))

        self.root.after(150, self._refresh)

    def _display_title(self):
        return f"{GUI_APPLICATION_TITLE} - {PFP_DEVICE_LABEL}"

    def set_bridge_thread(self, bridge_thread):
        self.bridge_thread = bridge_thread

    def set_psx_sender(self, psx_sender):
        self.psx_sender = psx_sender

    def add_log(self, message):
        self.log_queue.put(str(message))

    def _append_log(self, message):
        message = str(message).rstrip()
        if not message:
            return

        lines = [line for line in message.splitlines() if line.strip()]
        if not lines:
            return

        was_at_bottom = self.log_scroll_offset == 0
        self.log_lines.extend(lines)
        self.log_lines = self.log_lines[-500:]

        if was_at_bottom:
            self.log_scroll_offset = 0

    def request_stop(self):
        if self.stopping:
            self.close_when_stopped = True
            return

        self.stopping = True
        self.close_when_stopped = True
        SHUTDOWN_REQUESTED.set()
        self._draw()

    def _set_windows_toolwindow(self, enabled):
        """Hide Mini mode from the Windows taskbar without affecting other platforms."""
        if sys.platform != "win32":
            return

        try:
            self.root.attributes("-toolwindow", bool(enabled))
        except tk.TclError:
            pass

    def _toggle_mini_mode(self):
        self.menu_open = False
        self.about_open = False
        self.root.update_idletasks()

        if not self.mini_mode:
            # Preserve the original Full-window geometry. Full always returns
            # here, regardless of where Mini is dragged afterwards.
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            size = self.root.geometry().split("+", 1)[0]
            self.full_geometry = f"{size}+{x}+{y}"

            # The first Mini opens at Full's position. Later Mini sessions in
            # the same running application use the last dragged Mini position.
            if self.mini_geometry:
                _, _, mini_x, mini_y = self._split_geometry(self.mini_geometry)
            else:
                mini_x, mini_y = x, y

            # Withdraw while changing the native window style. On Windows this
            # prevents the taskbar from retaining a second-looking window entry.
            self.root.withdraw()
            self.mini_mode = True
            self.root.resizable(False, False)
            self.root.minsize(self.MINI_WIDTH, self.MINI_HEIGHT)
            self.root.maxsize(self.MINI_WIDTH, self.MINI_HEIGHT)
            self.root.overrideredirect(True)
            self._set_windows_toolwindow(True)
            self.root.attributes("-topmost", True)
            self.root.geometry(
                f"{self.MINI_WIDTH}x{self.MINI_HEIGHT}+{mini_x}+{mini_y}"
            )
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()
            if sys.platform == "darwin":
                self.root.after_idle(
                    lambda: self.root.attributes("-topmost", True)
                )
        else:
            # Remember Mini's latest screen position only for this run.
            mini_x = self.root.winfo_x()
            mini_y = self.root.winfo_y()
            self.mini_geometry = (
                f"{self.MINI_WIDTH}x{self.MINI_HEIGHT}+{mini_x}+{mini_y}"
            )

            # Always restore the original Full-window geometry. Mini may have
            # been dragged onto the taskbar, but that must not affect Full.
            self.root.withdraw()
            self.mini_mode = False
            self.root.attributes("-topmost", False)
            self.root.overrideredirect(False)
            self._set_windows_toolwindow(False)
            self.root.resizable(True, True)
            self.root.minsize(self.FULL_WIDTH, self.FULL_HEIGHT)
            self.root.maxsize(10000, 10000)
            self.root.geometry(self.full_geometry)
            self.root.deiconify()
            self.root.lift()
            try:
                self.root.focus_force()
            except tk.TclError:
                pass

        self.root.update_idletasks()
        self._draw()

    @staticmethod
    def _split_geometry(geometry):
        """Return width, height, x and y from a standard Tk geometry string."""
        match = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", geometry)
        if not match:
            raise ValueError(f"Invalid Tk geometry: {geometry!r}")
        width, height, x, y = match.groups()
        return int(width), int(height), int(x), int(y)

    def _select_cdu(self, cdu):
        if self.stopping or self.psx_sender is None:
            return

        if self.psx_sender.set_active_cdu(cdu):
            self._draw()

    def _copy_log_to_clipboard(self):
        log_text = "\n".join(self.log_lines)
        self.root.clipboard_clear()
        self.root.clipboard_append(log_text)
        self.root.update()
        self.menu_open = False
        self._draw()

    def _toggle_debug(self):
        global DEBUG
        DEBUG = not DEBUG
        log(f"[CONFIG] Debug logging {'enabled' if DEBUG else 'disabled'}")
        self.menu_open = False
        self._draw()

    def _show_about(self):
        self.menu_open = False
        self.about_open = True
        if self.psx_sender is not None:
            self.psx_sender.show_about_overlay()
        self._draw()

    def _close_about(self):
        if not self.about_open:
            return

        self.about_open = False
        if self.psx_sender is not None:
            self.psx_sender.hide_about_overlay()
        self._draw()

    def _scroll_log(self, delta):
        if self.mini_mode:
            return

        visible = self._visible_log_line_count()
        max_offset = max(0, len(self._wrapped_log_lines()) - visible)
        self.log_scroll_offset = max(
            0,
            min(max_offset, self.log_scroll_offset + int(delta)),
        )
        self._draw()

    def _on_mousewheel(self, event):
        if self.mini_mode or not self.log_track_bounds:
            return
        steps = -1 if event.delta > 0 else 1
        self._scroll_log(steps * 3)

    def _on_click(self, event):
        if self.mini_mode:
            for name, bounds in self.button_bounds.items():
                x1, y1, x2, y2 = bounds
                if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                    if name == "MODE":
                        self._toggle_mini_mode()
                    else:
                        self._select_cdu(name)
                    return

            # The unused brown area is deliberately draggable in Mini mode,
            # replacing the native title bar that has been hidden.
            self.mini_drag_anchor = (event.x, event.y)
            return

        if self.about_open:
            self._close_about()
            return

        if not self.mini_mode and self.menu_button_bounds:
            x1, y1, x2, y2 = self.menu_button_bounds
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.menu_open = not self.menu_open
                self._draw()
                return

        if not self.mini_mode and self.menu_open:
            if self.menu_bounds:
                x1, y1, x2, y2 = self.menu_bounds
                if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                    item = int((event.y - y1) // 30)
                    if item == 0:
                        self._copy_log_to_clipboard()
                    elif item == 1:
                        self._toggle_debug()
                    else:
                        self._show_about()
                    return
            self.menu_open = False
            self._draw()
            return

        if not self.mini_mode and self.log_thumb_bounds:
            x1, y1, x2, y2 = self.log_thumb_bounds
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.log_dragging = True
                return

        if not self.mini_mode and self.log_track_bounds:
            x1, y1, x2, y2 = self.log_track_bounds
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                if self.log_thumb_bounds:
                    _, thumb_y1, _, thumb_y2 = self.log_thumb_bounds
                    self._scroll_log(-3 if event.y < thumb_y1 else 3)
                return

        for name, bounds in self.button_bounds.items():
            x1, y1, x2, y2 = bounds
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                if name == "QUIT":
                    if self.bridge_thread is not None and not self.bridge_thread.is_alive():
                        self.root.destroy()
                    else:
                        self.request_stop()
                elif name == "MODE":
                    self._toggle_mini_mode()
                else:
                    self._select_cdu(name)
                return

    def _on_double_click(self, event):
        if not self.mini_mode:
            return

        # Only a free brown area restores Full mode. Double-clicks on a CDU
        # button retain their normal CDU-selection behaviour.
        for bounds in self.button_bounds.values():
            x1, y1, x2, y2 = bounds
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                return

        self.mini_drag_anchor = None
        self._toggle_mini_mode()

    def _on_release(self, _event):
        self.log_dragging = False
        self.mini_drag_anchor = None

    def _on_drag(self, event):
        if self.mini_mode:
            if self.mini_drag_anchor is None:
                return

            anchor_x, anchor_y = self.mini_drag_anchor
            x = self.root.winfo_pointerx() - anchor_x
            y = self.root.winfo_pointery() - anchor_y
            self.root.geometry(f"+{x}+{y}")
            return

        if not self.log_dragging or not self.log_track_bounds:
            return

        x1, y1, x2, y2 = self.log_track_bounds
        visible = self._visible_log_line_count()
        visual_count = len(self._wrapped_log_lines())
        max_offset = max(0, visual_count - visible)
        if max_offset == 0:
            return

        track_height = max(1, y2 - y1)
        thumb_height = max(20, int(track_height * visible / visual_count))
        usable_height = max(1, track_height - thumb_height)
        thumb_top = max(y1, min(y2 - thumb_height, event.y - thumb_height // 2))
        ratio = (thumb_top - y1) / usable_height
        self.log_scroll_offset = round((1.0 - ratio) * max_offset)
        self._draw()

    def _on_motion(self, event):
        if not self.mini_mode and self.menu_button_bounds:
            x1, y1, x2, y2 = self.menu_button_bounds
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.canvas.configure(cursor="hand2")
                return

        for bounds in self.button_bounds.values():
            x1, y1, x2, y2 = bounds
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.canvas.configure(cursor="hand2")
                return

        if not self.mini_mode and self.log_thumb_bounds:
            x1, y1, x2, y2 = self.log_thumb_bounds
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.canvas.configure(cursor="hand2")
                return

        self.canvas.configure(cursor="")

    def _panel(self, x1, y1, x2, y2):
        self.canvas.create_rectangle(
            x1, y1, x2, y2,
            fill=self.PANEL_BG,
            outline=self.BORDER_FG,
            width=1,
        )

    def _draw_button(self, name, x1, y1, x2, y2, label, active=False):
        fill = self.BUTTON_ACTIVE_BG if active else self.BUTTON_BG
        text_color = self.BUTTON_ACTIVE_TEXT if active else self.BUTTON_TEXT
        self.canvas.create_rectangle(
            x1, y1, x2, y2,
            fill=fill,
            outline=self.BORDER_FG,
            width=1,
        )
        self.canvas.create_text(
            (x1 + x2) / 2,
            (y1 + y2) / 2,
            text=label,
            anchor="center",
            fill=text_color,
            font=("Helvetica", 12, "bold" if active else "normal"),
        )
        self.button_bounds[name] = (x1, y1, x2, y2)

    def _visible_log_line_count(self):
        if not self.log_track_bounds:
            return 10
        _, y1, _, y2 = self.log_track_bounds
        return max(1, (y2 - y1 - 8) // 17)

    def _wrap_log_line(self, text, max_width):
        """Split one logical log entry into Canvas-sized visual lines."""
        if not text:
            return [""]

        # Keep ASCII/Unicode box-drawing rows intact so their columns remain
        # aligned in the monospace log font.
        if text.startswith(("+", "|", "┌", "├", "│", "└")):
            return [text]

        words = text.split()
        if not words:
            return [""]

        wrapped = []
        current = ""

        for word in words:
            candidate = word if not current else f"{current} {word}"
            if self.log_font.measure(candidate) <= max_width:
                current = candidate
                continue

            if current:
                wrapped.append(current)
                current = ""

            while self.log_font.measure(word) > max_width and len(word) > 1:
                cut = len(word)
                while cut > 1 and self.log_font.measure(word[:cut]) > max_width:
                    cut -= 1
                wrapped.append(word[:cut])
                word = word[cut:]

            current = word

        if current:
            wrapped.append(current)

        return wrapped or [""]

    def _wrapped_log_lines(self):
        max_width = max(80, self.log_content_width)
        visual_lines = []
        for line in self.log_lines:
            visual_lines.extend(self._wrap_log_line(line, max_width))
        return visual_lines

    def _draw_log(self, margin, right, log_top, log_bottom):
        self._panel(margin, log_top, right, log_bottom)

        log_line_height = 17
        content_x1 = margin + 12
        content_x2 = right - 30
        content_y1 = log_top + 18
        content_y2 = log_bottom - 8
        self.log_content_width = max(80, content_x2 - content_x1)
        self.log_track_bounds = (right - 18, content_y1, right - 8, content_y2)

        visual_lines = self._wrapped_log_lines()
        visible_count = max(1, (content_y2 - content_y1) // log_line_height)
        max_offset = max(0, len(visual_lines) - visible_count)
        self.log_scroll_offset = max(0, min(max_offset, self.log_scroll_offset))
        first = max(0, len(visual_lines) - visible_count - self.log_scroll_offset)
        shown_lines = visual_lines[first:first + visible_count]

        if shown_lines:
            y = content_y1
            for line in shown_lines:
                self.canvas.create_text(
                    content_x1,
                    y,
                    text=line,
                    anchor="nw",
                    fill=self.TEXT_FG,
                    font=self.log_font,
                )
                y += log_line_height
        else:
            self.canvas.create_text(
                content_x1,
                content_y1,
                text="No log messages yet.",
                anchor="nw",
                fill=self.MUTED_FG,
                font=("Helvetica", 11),
            )

        track_x1, track_y1, track_x2, track_y2 = self.log_track_bounds
        self.canvas.create_rectangle(
            track_x1, track_y1, track_x2, track_y2,
            fill="#D7CABB",
            outline=self.BORDER_FG,
            width=1,
        )

        if len(visual_lines) > visible_count:
            track_height = track_y2 - track_y1
            thumb_height = max(20, int(track_height * visible_count / len(visual_lines)))
            usable_height = track_height - thumb_height
            ratio = 1.0 - (self.log_scroll_offset / max_offset)
            thumb_y1 = track_y1 + int(usable_height * ratio)
            thumb_y2 = thumb_y1 + thumb_height
            self.canvas.create_rectangle(
                track_x1 + 1, thumb_y1, track_x2 - 1, thumb_y2,
                fill="#9B8975",
                outline="#796A5B",
                width=1,
            )
            self.log_thumb_bounds = (track_x1, thumb_y1, track_x2, thumb_y2)
        else:
            self.log_thumb_bounds = None

    def _draw_menu(self, right):
        if self.mini_mode or not self.menu_open:
            self.menu_bounds = None
            return

        x2 = right
        x1 = x2 - 170
        y1 = 42
        y2 = y1 + 90
        self.canvas.create_rectangle(
            x1, y1, x2, y2,
            fill=self.MENU_BG,
            outline=self.BORDER_FG,
            width=1,
        )
        check = "✓ " if DEBUG else "   "
        items = (
            "Copy log",
            f"{check}Debug logging",
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
                    x1,
                    divider_y,
                    x2,
                    divider_y,
                    fill=self.BORDER_FG,
                )
        self.menu_bounds = (x1, y1, x2, y2)

    def _draw_about(self, width, height):
        if self.mini_mode or not self.about_open:
            return

        x1 = 52
        y1 = 105
        x2 = width - 52
        y2 = height - 80
        self.canvas.create_rectangle(
            x1, y1, x2, y2,
            fill=self.PANEL_BG,
            outline=self.BORDER_FG,
            width=1,
        )
        self.canvas.create_text(
            (x1 + x2) / 2,
            y1 + 32,
            text=APPLICATION_TITLE,
            anchor="center",
            fill=self.TEXT_FG,
            font=("Helvetica", 14, "bold"),
        )
        self.canvas.create_text(
            (x1 + x2) / 2,
            y1 + 62,
            text=f"Version {VERSION}",
            anchor="center",
            fill=self.MUTED_FG,
            font=("Helvetica", 11),
        )
        self.canvas.create_text(
            (x1 + x2) / 2,
            y1 + 100,
            text="Aerowinx MCDU font by Martin and Hardy",
            anchor="center",
            fill=self.TEXT_FG,
            font=("Helvetica", 11),
        )
        self.canvas.create_text(
            (x1 + x2) / 2,
            y1 + 126,
            text="Jamie Janssen © 2026",
            anchor="center",
            fill=self.MUTED_FG,
            font=("Helvetica", 11),
        )
        self.canvas.create_text(
            (x1 + x2) / 2,
            y2 - 20,
            text="Click anywhere to close",
            anchor="center",
            fill=self.MUTED_FG,
            font=("Helvetica", 10),
        )

    def _draw_cdu_buttons(self, margin, button_y1, button_y2, include_mode=True):
        cdu_button_width = 70
        gap = 8
        active_cdu = RUNTIME_CONFIG.get_active_cdu()

        for index, cdu in enumerate(("L", "C", "R")):
            x1 = margin + index * (cdu_button_width + gap)
            self._draw_button(
                cdu,
                x1,
                button_y1,
                x1 + cdu_button_width,
                button_y2,
                f"CDU {cdu}",
                active=(active_cdu == cdu),
            )

        if include_mode:
            mode_x1 = margin + 3 * (cdu_button_width + gap) + 18
            self._draw_button(
                "MODE",
                mode_x1,
                button_y1,
                mode_x1 + 70,
                button_y2,
                "Mini",
            )

    def _draw(self, _event=None):
        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()

        if self.mini_mode:
            width = max(canvas_width, self.MINI_WIDTH)
            height = max(canvas_height, self.MINI_HEIGHT)
        else:
            width = max(canvas_width, self.FULL_WIDTH)
            height = max(canvas_height, self.FULL_HEIGHT)

        self.canvas.delete("all")
        self.button_bounds = {}
        self.log_thumb_bounds = None
        self.log_track_bounds = None
        self.menu_button_bounds = None

        margin = 16
        right = width - margin

        if self.mini_mode:
            button_y1 = 8
            button_y2 = 38
            self._draw_cdu_buttons(
                margin,
                button_y1,
                button_y2,
                include_mode=False,
            )
            return

        header_y = 25
        self.canvas.create_text(
            margin,
            header_y,
            text=self._display_title(),
            anchor="w",
            fill=self.TITLE_FG,
            font=("Helvetica", 15, "bold"),
        )
        self.canvas.create_text(
            right - 28,
            header_y,
            text=f"v{VERSION}",
            anchor="e",
            fill=self.TITLE_FG,
            font=("Helvetica", 15),
        )
        self.canvas.create_text(
            right,
            header_y,
            text="⋯",
            anchor="e",
            fill=self.TITLE_FG,
            font=("Helvetica", 18, "bold"),
        )
        self.menu_button_bounds = (right - 22, 10, right + 2, 40)

        button_y1 = height - 46
        button_y2 = height - 16
        log_top = 49
        log_bottom = button_y1 - 13
        self._draw_log(margin, right, log_top, log_bottom)
        self._draw_cdu_buttons(
            margin,
            button_y1,
            button_y2,
            include_mode=True,
        )

        quit_x2 = right
        quit_x1 = quit_x2 - 88
        quit_label = "Close" if (
            self.bridge_thread is not None and not self.bridge_thread.is_alive()
        ) else "Quit"
        self._draw_button("QUIT", quit_x1, button_y1, quit_x2, button_y2, quit_label)

        self._draw_menu(right)
        self._draw_about(width, height)

    def _refresh(self):
        if not self.root.winfo_exists():
            return

        while True:
            try:
                self._append_log(self.log_queue.get_nowait())
            except queue.Empty:
                break

        self.root.title(self._display_title())

        if self.bridge_thread is not None and not self.bridge_thread.is_alive():
            if self.close_when_stopped:
                self.root.after(100, self.root.destroy)
                return
        self._draw()
        self.root.after(150, self._refresh)

    def run(self):
        self.root.mainloop()


def save_ini_value(section, key, value):
    """Update one value in psx_winctrl_pfp.ini while preserving comments and layout."""
    section_header = f"[{section}]"
    key_lower = str(key).strip().lower()
    value = str(value)

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        cfg = configparser.ConfigParser()
        cfg.add_section(section)
        cfg.set(section, key, value)

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            cfg.write(f)

        return

    in_section = False
    section_found = False
    key_found = False
    insert_index = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Section header
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not key_found:
                insert_index = i

            in_section = stripped.lower() == section_header.lower()

            if in_section:
                section_found = True
                insert_index = i + 1

            continue

        if in_section:
            insert_index = i + 1

            # Preserve empty/comment lines
            if not stripped or stripped.startswith(("#", ";")):
                continue

            if "=" in line:
                existing_key = line.split("=", 1)[0].strip().lower()

                if existing_key == key_lower:
                    newline = "\n" if line.endswith("\n") else ""
                    lines[i] = f"{key} = {value}{newline}"
                    key_found = True
                    break

    if not section_found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"

        lines.append(f"\n[{section}]\n")
        lines.append(f"{key} = {value}\n")

    elif not key_found:
        if insert_index is None:
            insert_index = len(lines)

        lines.insert(insert_index, f"{key} = {value}\n")

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)


def atc_key_value_to_altn(value):
    """Return 1 for ALTN mode and 0 for original ATC mode."""
    value = str(value or "").strip().upper()

    if value == "ALTN":
        return 1

    return 0


def atc_altn_to_ini_value(atc_altn):
    return "ALTN" if int(atc_altn) == 1 else "ATC"


def connect_with_retry(host, port, name, stop_evt=None, retry_delay=5.0):
    """Connect until available and report that the connection attempt is active.

    Returns:
        (socket_or_none, was_unavailable)
    """
    connecting_reported = False

    while stop_evt is None or not stop_evt.is_set():
        sock = None

        try:
            if not connecting_reported:
                log(f"[{name}] connecting...")
                connecting_reported = True

            log_debug(f"[{name}] connecting {host}:{port}...")

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(1.0)

            return sock, False

        except KeyboardInterrupt:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
            raise

        except (ConnectionRefusedError, TimeoutError, OSError) as e:
            log_debug(f"[{name}] connect error: {repr(e)}")
            log_debug(f"[{name}] retrying in {retry_delay:.0f} seconds...")

            try:
                if sock:
                    sock.close()
            except Exception:
                pass

            end_time = time.time() + retry_delay

            while time.time() < end_time:
                if stop_evt is not None and stop_evt.is_set():
                    return None, False

                time.sleep(0.1)

    return None, False


class PfpLedController:
    """Direct HID LED and backlight controller for WINCTRL PFPx/PFP4 devices.

    HID light-control message:
      02 dest0 dest1 00 00 03 49 light_type value 00 00 00 00 00

    For the connected PFPx device:
      dest = 33 BB
      light_type: backlight=01, DSPY=03, FAIL=04, MSG=05, OFST=06, EXEC=07
    """

    def __init__(self, hid_device, hid_lock=None, dest=None):
        self.dev = hid_device
        self.hid_lock = hid_lock or threading.Lock()
        self.dest = bytes(dest or PFP_DEST)
        self.lock = threading.Lock()
        self.last_states = {}
        self.screen_brightness_step = RUNTIME_CONFIG.get_brightness_step()
        self.connected = False

    def start(self):
        with self.lock:
            self.connected = True
            log_debug("[PFP LED] direct HID connected")

            self._set_screen_brightness_step_unlocked(
                self.screen_brightness_step,
                force=True
            )

            for led_name in PFP_LEDS:
                self._set_led_unlocked(led_name, False, force=True)

    def force_blackout(self):
        """Force all PFP lighting to black while the HID handle is still open."""
        with self.lock:
            # Backlights to black
            self._write_light_control_message(PFP_KEY_BACKLIGHT_CHANNEL, 0)
            self._write_light_control_message(PFP_SCREEN_BACKLIGHT_CHANNEL, 0)

            # Annunciator LEDs off
            for led_name in PFP_LEDS:
                self._set_led_unlocked(led_name, False, force=True)

    def stop(self):
        """Switch annunciators and backlights off during shutdown.

        CTRL+C can close the HID handle while shutdown is already in progress.
        ValueError('not open') is harmless in that case and is ignored.
        """
        try:
            self.force_blackout()

        except ValueError as e:
            if "not open" not in str(e).lower():
                log(f"[PFP LED] shutdown error: {repr(e)}")

        except KeyboardInterrupt:
            pass

        except Exception as e:
            log(f"[PFP LED] shutdown error: {repr(e)}")

    def _write_light_control_message(self, light_type, value):
        value = int(value) & 0xFF

        msg = bytes([
            0x02, self.dest[0], self.dest[1],
            0x00, 0x00,
            0x03, 0x49,
            int(light_type) & 0xFF,
            value,
            0x00, 0x00, 0x00, 0x00, 0x00,
        ])

        with self.hid_lock:
            return self.dev.write(msg)

    def set_led(self, name, on, intensity=PFP_LED_INTENSITY):
        name = name.upper()

        if name not in PFP_LEDS:
            log_debug(f"[PFP LED] unknown LED: {name}")
            return

        with self.lock:
            self._set_led_unlocked(name, on, intensity=intensity)

    def change_screen_brightness_step(self, delta):
        with self.lock:
            new_step = self.screen_brightness_step + delta
            self._set_screen_brightness_step_unlocked(new_step)

    def get_screen_brightness_step(self):
        with self.lock:
            return self.screen_brightness_step

    def refresh_screen_brightness_keepalive(self):
        """Re-send the current brightness without changing any saved setting."""
        with self.lock:
            value = brightness_value_from_step(self.screen_brightness_step)

            screen_written = self._write_light_control_message(
                PFP_SCREEN_BACKLIGHT_CHANNEL,
                value,
            )
            key_written = self._write_light_control_message(
                PFP_KEY_BACKLIGHT_CHANNEL,
                value,
            )

            if screen_written is not None and screen_written <= 0:
                raise OSError("HID brightness keepalive write returned 0 bytes")
            if key_written is not None and key_written <= 0:
                raise OSError("HID brightness keepalive write returned 0 bytes")

    def _set_screen_brightness_step_unlocked(self, step, force=False):
        step = max(0, min(PFP_SCREEN_BRIGHTNESS_STEPS - 1, step))
        value = brightness_value_from_step(step)

        if not force and step == self.screen_brightness_step:
            return

        self.screen_brightness_step = step
        RUNTIME_CONFIG.set_brightness_step(step, mark_dirty=not force)

        self._write_light_control_message(
            PFP_SCREEN_BACKLIGHT_CHANNEL,
            value
        )
        self._write_light_control_message(
            PFP_KEY_BACKLIGHT_CHANNEL,
            value
        )

        log_debug(
            f"[PFP BRT] screen/key backlight "
            f"step {step + 1}/{PFP_SCREEN_BRIGHTNESS_STEPS} value={value}"
        )

    def apply_psx_cdu_lights_bitmask(self, state):
        md_t_all = bool(state & MDT_ALL_LIGHTS_BIT)

        with self.lock:
            for bit, led_name in PSX_LIGHT_BITS_TO_PFP.items():
                on = md_t_all or bool(state & bit)
                self._set_led_unlocked(led_name, on)

    def _set_led_unlocked(self, name, on, intensity=PFP_LED_INTENSITY, force=False):
        name = name.upper()

        value = 1 if on else 0

        if not force and self.last_states.get(name) == value:
            return

        self.last_states[name] = value

        self._write_light_control_message(
            PFP_LEDS[name],
            value
        )

        log_debug(f"[PFP LED] {name} {'ON' if on else 'OFF'}")



def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")

    host = cfg.get("PSX", "host", fallback="127.0.0.1").strip()
    port = cfg.getint("PSX", "port", fallback=10747)
    vid = WINCTRL_VENDOR_ID
    pid = int(cfg.get("FMC", "PID", fallback="0xBB37"), 16)
    did = cfg.get("FMC", "DID", fallback="33BB").strip().upper().replace("0X", "")

    if len(did) != 4:
        log(f"[CONFIG] invalid FMC DID={did!r}; using 33BB")
        did = "33BB"

    global PFP_PRODUCT_ID, PFP_DEST, PFP_DEVICE_LABEL
    PFP_PRODUCT_ID = pid
    PFP_DEST = bytes([int(did[0:2], 16), int(did[2:4], 16)])
    PFP_DEVICE_LABEL = pfp_device_label(pid)

    atc_key = cfg.get("FMC", "ATC_KEY", fallback="ALTN").strip().upper()
    if atc_key not in ("ATC", "ALTN"):
        log(f"[CONFIG] invalid FMC ATC_KEY={atc_key!r}; using ALTN")
        atc_key = "ALTN"

    RUNTIME_CONFIG.set_cdu_atc_altn(atc_key_value_to_altn(atc_key))

    active_cdu = cfg.get("FMC", "ACTIVE_CDU", fallback=DEFAULT_CDU).strip().upper()
    if active_cdu not in CDU_CONFIGS:
        log(f"[CONFIG] invalid FMC ACTIVE_CDU={active_cdu!r}; using {DEFAULT_CDU}")
        active_cdu = DEFAULT_CDU

    RUNTIME_CONFIG.set_active_cdu(active_cdu, mark_dirty=False)

    brightness_step = cfg.getint(
        "FMC",
        "BRIGHTNESS",
        fallback=PFP_SCREEN_BRIGHTNESS_DEFAULT_STEP
    )
    brightness_step = max(0, min(PFP_SCREEN_BRIGHTNESS_STEPS - 1, brightness_step))
    RUNTIME_CONFIG.set_brightness_step(brightness_step, mark_dirty=False)

    log_debug(f"[CONFIG] PSX {host}:{port}")
    log_debug(f"[CONFIG] FMC VID={hex(vid)} PID={hex(pid)} DID={did}")
    log_debug(f"[CONFIG] FMC ATC_KEY={atc_altn_to_ini_value(RUNTIME_CONFIG.get_cdu_atc_altn_user())}")
    log_debug(f"[CONFIG] FMC ACTIVE_CDU={RUNTIME_CONFIG.get_active_cdu()}")
    log_debug(f"[CONFIG] FMC BRIGHTNESS={RUNTIME_CONFIG.get_brightness_step()}")
    log_debug("[CONFIG] Display control: direct HID")
    log_debug("[CONFIG] LED/backlight control: direct HID")

    return host, port, vid, pid


# WINCTRL key map: HID byte/bit positions to CDU key names.
BUILTIN_MAP = {
    "2,4": "INIT REF",
    "2,5": "ROUTE",
    "2,6": "DEPARR",
    "2,7": "ATC",
    "3,0": "VNAV",
    "3,3": "FIX",
    "3,4": "LEGS",
    "3,5": "HOLD",
    "3,7": "PROG",
    "4,0": "EXEC",
    "4,1": "MENU",
    "4,3": "PREV",
    "4,4": "NEXT",
    "6,1": "A",
    "6,2": "B",
    "6,3": "C",
    "6,4": "D",
    "6,5": "E",
    "6,6": "F",
    "6,7": "G",
    "7,0": "H",
    "7,1": "I",
    "7,2": "J",
    "7,3": "K",
    "7,4": "L",
    "7,5": "M",
    "7,6": "N",
    "7,7": "O",
    "4,6": "2",
    "4,7": "3",
    "5,0": "4",
    "5,1": "5",
    "5,2": "6",
    "5,3": "7",
    "5,4": "8",
    "5,5": "9",
    "5,6": ".",
    "5,7": "0",
    "8,0": "P",
    "8,1": "Q",
    "8,2": "R",
    "8,3": "S",
    "8,4": "T",
    "8,5": "U",
    "8,6": "V",
    "8,7": "W",
    "9,0": "X",
    "9,1": "Y",
    "9,2": "Z",
    "9,3": "SP",
    "9,4": "DEL",
    "9,5": "/",
    "9,6": "CLR",
    "6,0": "+",
    "1,0": "LSKL1",
    "1,1": "LSKL2",
    "1,2": "LSKL3",
    "1,3": "LSKL4",
    "1,4": "LSKL5",
    "1,5": "LSKL6",
    "1,6": "LSKR1",
    "1,7": "LSKR2",
    "2,0": "LSKR3",
    "2,1": "LSKR4",
    "2,2": "LSKR5",
    "2,3": "LSKR6",
    "3,6": "FMC",
    "4,5": "1",
    "4,2": "NAVRAD",
    "3,2": "BRT+",
    "3,1": "BRT-"
}


def load_map():
    mapping = {}

    for k, v in BUILTIN_MAP.items():
        by, bi = map(int, k.split(","))

        if by < BYTE_LIMIT:
            mapping[(by, bi)] = str(v).strip()

    log_debug(f"[MAP] loaded {len(mapping)} built-in bitpos (<{BYTE_LIMIT})")
    return mapping


def pressed_from_mapping(frame, mapped_bps):
    pressed = set()
    flen = min(len(frame), BYTE_LIMIT)

    for by, bi in mapped_bps:
        if by < flen and (frame[by] & (1 << bi)):
            pressed.add((by, bi))

    return pressed


# Direct HID display timing.
# These small delays make font preload more reliable after quit/restart cycles.
DISPLAY_STARTUP_SETTLE_SECONDS = 0.30
DISPLAY_FONT_HEAD_DELAY_SECONDS = 0.05
DISPLAY_FONT_DATA_DELAY_SECONDS = 0.20
DISPLAY_INIT_DELAY_SECONDS = 0.10
DISPLAY_CLEAR_DELAY_SECONDS = 0.10
DISPLAY_SHUTDOWN_CLEAR_DELAY_SECONDS = 0.05

# HID display command headers for the WINCTRL CDU/PFP.
DISPLAY_CMD_HEADERS = {
    "0301": bytes([0x03, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x00, 0, 0, 0]),
    "0401": bytes([0x04, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x01, 0, 0, 0]),
    "0601": bytes([0x06, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x19, 0, 0, 0]),
    "0701": bytes([0x07, 0x01, 0, 0, 0, 0, 0, 0, 0]),
    "1001": bytes([0x10, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x08, 0, 0, 0]),
    "1201": bytes([0x12, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x04, 0, 0, 0]),
    "1301": bytes([0x13, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x04, 0, 0, 0]),
    "1801": bytes([0x18, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x08, 0, 0, 0]),
    "1901": bytes([0x19, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x0E, 0, 0, 0]),
    "1A01": bytes([0x1A, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x01, 0, 0, 0]),
    "1C01": bytes([0x1C, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x00, 0, 0, 0]),
    "1E01": bytes([0x1E, 0x01, 0, 0, 0, 0, 0, 0, 0, 0x00, 0, 0, 0]),
}


# Direct HID display text format.
COLOR_STEP = 0x21
COLOR_INVERT_OFFSET = 0x1B
COLOR_SMALL_OFFSET = 0x16B
FORMAT_TABLE = {
    "a": 0x00,                    # amber/base
    "w": 0x00 + COLOR_STEP * 2,   # white
    "c": 0x00 + COLOR_STEP * 3,   # cyan
    "g": 0x00 + COLOR_STEP * 4,   # green
    "m": 0x00 + COLOR_STEP * 5,   # magenta
    "r": 0x00 + COLOR_STEP * 6,   # red
    "y": 0x00 + COLOR_STEP * 7,   # yellow
    "o": 0x00 + COLOR_STEP * 8,   # blue/orange naming differs in older code
    "e": 0x00 + COLOR_STEP * 9,   # grey
    "k": 0x00 + COLOR_STEP * 10,  # khaki
}


class DirectHidDisplaySender:
    """Direct HID display controller."""

    def __init__(self, hid_device, hid_lock, dest=None):
        self.dev = hid_device
        self.hid_lock = hid_lock
        self.dest = bytes(dest or PFP_DEST)
        self.counter = 0
        self.stop_evt = threading.Event()
        self.started = False

    def start(self):
        # Give the device a brief moment after opening/restarting before font preload.
        time.sleep(DISPLAY_STARTUP_SETTLE_SECONDS)

        log("[CDU] Loading Aerowinx MCDU fonts by Martin and Hardy")
        commands = self.load_embedded_font_commands()

        self.send_display_commands(commands["large_head"])
        time.sleep(DISPLAY_FONT_HEAD_DELAY_SECONDS)

        self.send_display_commands(commands["large_data"])
        time.sleep(DISPLAY_FONT_DATA_DELAY_SECONDS)

        self.send_display_commands(commands["small_head"])
        time.sleep(DISPLAY_FONT_HEAD_DELAY_SECONDS)

        self.send_display_commands(commands["small_data"])
        time.sleep(DISPLAY_FONT_DATA_DELAY_SECONDS)

        self.send_display_commands(self.pfp_init_commands())
        time.sleep(DISPLAY_INIT_DELAY_SECONDS)

        self.send_display_commands(self.clear_commands())
        time.sleep(DISPLAY_CLEAR_DELAY_SECONDS)

        self.started = True
        log("[CDU] ready")

    def show_welcome_screen(self, version, seconds=None):
        """Show the centered welcome screen; optional seconds keeps the startup pause."""
        if not self.started:
            return

        display = self.make_blank_display()

        title = "PSX WINCTRL PFPx"
        subtitle = f"BRIDGE v{version}"
        name = "Jamie  Janssen"
        font_credit_1 = "AEROWINX MCDU FONT BY"
        font_credit_2 = "MARTIN AND HARDY"

        self._put_centered_colored_text(display, 3, title, ["c", "g", "y", "w", "m"])
        self._put_centered_colored_text(display, 5, subtitle, ["g", "c", "w", "y"])
        self._put_centered_colored_text(display, 8, name, ["y", "w", "c", "g", "m"])

        # Small-font acknowledgement for the Aerowinx MCDU font source.
        self._put_colored_text(display, 11, 0, font_credit_1, ["w"], small=True)
        self._put_colored_text(
            display,
            12,
            FMC_WIDTH - len(font_credit_2),
            font_credit_2,
            ["w"],
            small=True,
        )

        self.send_cdu_display_bytes(self.display_to_bytes(display))
        if seconds:
            time.sleep(seconds)

    def make_blank_display(self):
        return [[" ", "w", 0, False] for _ in range(FMC_WIDTH * FMC_HEIGHT)]

    def _put_centered_colored_text(self, display, row, text, colors, small=False):
        col = max(0, (FMC_WIDTH - len(text)) // 2)
        self._put_colored_text(display, row, col, text, colors, small=small)

    def _put_colored_text(self, display, row, col, text, colors, small=False):
        for i, ch in enumerate(text[:FMC_WIDTH]):
            idx = row * FMC_WIDTH + col + i
            if 0 <= idx < len(display):
                color = colors[i % len(colors)] if ch != " " else "w"
                display[idx] = [ch, color, 1 if small else 0, False]


    def force_blank_display(self):
        """Blank the CDU text while the HID handle is still open.

        Shutdown must be quick and robust. Do not send the heavier clear_commands()
        sequence here; the screen/key backlights are switched off separately.
        """
        self.send_cdu_display_bytes(self.display_to_bytes(self.make_blank_display()))

    def stop(self):
        self.stop_evt.set()
        try:
            self.force_blank_display()
        except ValueError as e:
            if "not open" not in str(e).lower():
                log_debug(f"[CDU] shutdown error: {repr(e)}")
        except KeyboardInterrupt:
            pass
        except Exception as e:
            log_debug(f"[CDU] shutdown error: {repr(e)}")

    def send_lines(self, lines, color_lines=None):
        if not self.started:
            return

        display = self._lines_to_display_cells((lines, color_lines))
        byte_data = self.display_to_bytes(display)
        self.send_cdu_display_bytes(byte_data)
        log_debug("[CDU] direct HID frame sent")

    def _new_message(self):
        msg = bytearray(64)
        msg[0] = 0xF0
        self.counter = (self.counter + 1) & 0xFF
        msg[2] = self.counter
        return msg

    @staticmethod
    def _time_bytes():
        now = time.localtime()
        ms_byte = int((time.time() % 1) * 1000 / 4) & 0xFF
        sec_byte = (now.tm_sec * 3) & 0xFF
        min_byte = now.tm_min & 0xFF
        return bytes([ms_byte, sec_byte, min_byte])

    def _write_report(self, report):
        if len(report) != 64:
            raise ValueError(f"HID report must be 64 bytes, got {len(report)}")

        with self.hid_lock:
            written = self.dev.write(report)

        if written <= 0:
            raise OSError("hid.write returned 0 bytes")

    def send_display_commands(self, commands):
        commands = list(commands)
        if not commands:
            return

        index_header_end = 3
        time_id = self._time_bytes()
        msg = self._new_message()
        current_index = index_header_end

        for cmd_index, command in enumerate(commands):
            for i, b in enumerate(command):
                current_index += 1

                # The device protocol uses rolling bytes 8, 9 and 10 in each command.
                if i == 8:
                    msg[current_index] = time_id[0]
                elif i == 9:
                    msg[current_index] = time_id[1]
                elif i == 10:
                    msg[current_index] = time_id[2]
                else:
                    msg[current_index] = b

                is_last = cmd_index == len(commands) - 1 and i == len(command) - 1
                if current_index == 59 or is_last:
                    msg[index_header_end] = current_index - index_header_end
                    self._write_report(bytes(msg))
                    msg = self._new_message()
                    current_index = index_header_end

    def _full_command(self, header_name, payload=b""):
        return self.dest + b"\x00\x00" + DISPLAY_CMD_HEADERS[header_name] + payload


    def load_embedded_font_commands(self):
        """Decode the embedded PFP font-command payload."""
        try:
            payload = base64.b64decode("".join(EMBEDDED_PFP_FONT_PAYLOAD_B64.split()), validate=True)
            if not payload.startswith(b"PFPF1"):
                raise ValueError("invalid font payload header")

            position = 5
            sections = {}

            for section_name in ("large_head", "large_data", "small_head", "small_data"):
                count = payload[position]
                position += 1
                commands = []

                for _ in range(count):
                    size = int.from_bytes(payload[position:position + 2], "little")
                    position += 2
                    command_tail = payload[position:position + size]
                    position += size

                    if len(command_tail) != size:
                        raise ValueError("truncated font command")

                    commands.append(self.dest + command_tail)

                sections[section_name] = commands

            if position != len(payload):
                raise ValueError("unexpected trailing font data")

            return sections

        except (ValueError, IndexError) as e:
            raise RuntimeError(
                f"Embedded Aerowinx MCDU font could not be loaded: {e!r}"
            ) from e

    def pfp_init_commands(self):
        init = []

        init.append(self._full_command("1E01"))
        init.append(self._full_command("1801", bytes([
            0x32, 0x00, 0x13, 0x00, 0x0E, 0x00, 0x18, 0x00
        ])))
        init.append(self._full_command("1901", bytes([
            0x01, 0x00, 0x01, 0x00, 0x00, 0x00, 0x02, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00
        ])))
        init.append(self._full_command("1901", bytes([
            0x01, 0x00, 0x02, 0x00, 0x00, 0x00, 0x03, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00
        ])))

        color_commands = [
            [0x02, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x04, 0x00, 0, 0, 0, 0, 0, 0],
            [0x02, 0x00, 0x00, 0xA5, 0xFF, 0xFF, 0x05, 0x00, 0, 0, 0, 0, 0, 0],
            [0x02, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x06, 0x00, 0, 0, 0, 0, 0, 0],
            [0x02, 0x00, 0xFF, 0xFF, 0x00, 0xFF, 0x07, 0x00, 0, 0, 0, 0, 0, 0],
            [0x02, 0x00, 0x3D, 0xFF, 0x00, 0xFF, 0x08, 0x00, 0, 0, 0, 0, 0, 0],
            [0x02, 0x00, 0xFF, 0x63, 0xFF, 0xFF, 0x09, 0x00, 0, 0, 0, 0, 0, 0],
            [0x02, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0x0A, 0x00, 0, 0, 0, 0, 0, 0],
            [0x02, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0x0B, 0x00, 0, 0, 0, 0, 0, 0],
            [0x02, 0x00, 0xFF, 0x99, 0x00, 0xFF, 0x0C, 0x00, 0, 0, 0, 0, 0, 0],
            [0x02, 0x00, 0x77, 0x77, 0x77, 0xFF, 0x0D, 0x00, 0, 0, 0, 0, 0, 0],
            [0x02, 0x00, 0x5E, 0x73, 0x79, 0xFF, 0x0E, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x0F, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0x00, 0xA5, 0xFF, 0xFF, 0x10, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x11, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0xFF, 0xFF, 0x00, 0xFF, 0x12, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0x3D, 0xFF, 0x00, 0xFF, 0x13, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0xFF, 0x63, 0xFF, 0xFF, 0x14, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0x15, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0x16, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0xFF, 0x99, 0x00, 0xFF, 0x17, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0x77, 0x77, 0x77, 0xFF, 0x18, 0x00, 0, 0, 0, 0, 0, 0],
            [0x03, 0x00, 0x5E, 0x73, 0x79, 0xFF, 0x19, 0x00, 0, 0, 0, 0, 0, 0],
            [0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x1A, 0x00, 0, 0, 0, 0, 0, 0],
            [0x04, 0x00, 0x01, 0x00, 0x00, 0x00, 0x1B, 0x00, 0, 0, 0, 0, 0, 0],
            [0x04, 0x00, 0x02, 0x00, 0x00, 0x00, 0x1C, 0x00, 0, 0, 0, 0, 0, 0],
        ]

        for c in color_commands:
            init.append(self._full_command("1901", bytes(c)))

        init.append(self._full_command("1A01", bytes([0x02])))
        init.append(self._full_command("1C01"))
        return init

    def clear_commands(self):
        return [
            self._full_command("0401", bytes([0x0E])),
            self._full_command("0301"),
            self._full_command("1201", bytes([0xFF, 0x06, 0x07, 0x0D])),
            self._full_command("1301", bytes([0xFF, 0x06, 0x07, 0x0D])),
            self._full_command("1001", bytes([0x00, 0x00, 0x00, 0x00, 0x80, 0x02, 0xE0, 0x01])),
            self._full_command("0301"),
        ]

    def send_cdu_display_bytes(self, byte_data):
        pos = 0
        while pos < len(byte_data):
            chunk = byte_data[pos:pos + 63]
            report = bytes([0xF2]) + chunk
            report = report.ljust(64, b"\x00")
            self._write_report(report)
            pos += 63

    def _lines_to_display_cells(self, item):
        display = []
        default_cdu_color = RUNTIME_CONFIG.get_cdu_color()

        if isinstance(item, tuple):
            lines, color_lines = item
        else:
            lines = item
            color_lines = None

        clean_lines = list(lines)[:FMC_HEIGHT]
        while len(clean_lines) < FMC_HEIGHT:
            clean_lines.append("")

        clean_color_lines = list(color_lines or [])[:FMC_HEIGHT]
        while len(clean_color_lines) < FMC_HEIGHT:
            clean_color_lines.append("")

        for row_index, line in enumerate(clean_lines):
            line = line.rstrip("\r\n")

            text_part = line[:FMC_WIDTH]
            text_part = text_part.ljust(FMC_WIDTH)

            size_part = line[FMC_WIDTH:FMC_WIDTH * 2]

            if size_part:
                last_size = size_part[-1]
                size_part = size_part.ljust(FMC_WIDTH, last_size)
            else:
                if row_index in (0, 2, 4, 6, 8, 10, 12, 13):
                    size_part = "+" * FMC_WIDTH
                else:
                    size_part = "-" * FMC_WIDTH

            color_part = clean_color_lines[row_index].strip()

            if color_part:
                last_color = color_part[-1]
                color_part = color_part.ljust(FMC_WIDTH, last_color)
            else:
                color_part = default_cdu_color * FMC_WIDTH

            for ch, size_symbol, psx_color in zip(text_part, size_part, color_part):
                # Convert PSX CDU placeholders to Aerowinx MCDU Unicode glyphs.
                ch = normalize_psx_display_char(ch)

                psx_color = psx_color.lower()

                # In PSX LCD colour strings, y means a grey background.
                # Render it as a white PFP glyph with the inverse format flag.
                #
                # PSX underscores represent blank CDU cells. Keep the inverse
                # flag on those cells too, so highlighted fields remain one
                # continuous block across spaces and underscores.
                inverted = psx_color == "y"

                if ch in ("_", " "):
                    display.append([" ", "w", 0, inverted])
                else:
                    small = 0 if size_symbol == "+" else 1
                    cdu_color = (
                        "w"
                        if inverted
                        else PSX_TO_PFP_COLOR.get(psx_color, default_cdu_color)
                    )
                    display.append([ch, cdu_color, small, inverted])

        while len(display) < FMC_WIDTH * FMC_HEIGHT:
            display.append([" ", default_cdu_color, 0, False])

        return display[:FMC_WIDTH * FMC_HEIGHT]

    @staticmethod
    def cell_to_bytes(cell, index, total):
        current_char = " "
        color_value = FORMAT_TABLE["w"]

        if cell and cell[0] is not None:
            current_char = str(cell[0])[:1] or " "
            fmt = cell[1] if len(cell) > 1 else "w"
            small = bool(cell[2]) if len(cell) > 2 else False
            inverted = bool(cell[3]) if len(cell) > 3 else False

            color_value = FORMAT_TABLE.get(str(fmt).lower(), FORMAT_TABLE["e"])
            if inverted:
                color_value += COLOR_INVERT_OFFSET
            if small:
                color_value += COLOR_SMALL_OFFSET

        low = color_value & 0xFF
        high = (color_value >> 8) & 0xFF

        if index == 0:
            low = (low + 0x01) & 0xFF
        elif index == total - 1:
            low = (low + 0x02) & 0xFF

        try:
            char_bytes = current_char.encode("utf-8")
        except UnicodeEncodeError:
            char_bytes = b"?"

        return bytes([low, high]) + char_bytes

    def display_to_bytes(self, display):
        out = bytearray()
        total = len(display)
        for i, cell in enumerate(display):
            out.extend(self.cell_to_bytes(cell, i, total))
        return bytes(out)


# Convert PSX CDU placeholders to Aerowinx MCDU Unicode glyphs.
PSX_CHAR_MAP = {
    "o": "\u00b0",  # degree symbol
    "b": "\u2610",  # box
    "l": "\u2190",  # left arrow
    "r": "\u2192",  # right arrow
}

def normalize_psx_display_char(ch):
    return PSX_CHAR_MAP.get(ch, ch)

class PsxSender:
    def __init__(self, host, port, pfp_display, pfp_leds, min_interval_s=0.03):
        self.host = host
        self.port = port
        self.pfp_display = pfp_display
        self.pfp_leds = pfp_leds
        self.min_interval = float(min_interval_s)

        self.q = queue.Queue()
        self.stop_evt = threading.Event()
        self.sock = None
        self.sock_lock = threading.Lock()
        self.connect_lock = threading.Lock()

        self.tx_thread = threading.Thread(target=self._tx_loop, daemon=True)
        self.rx_thread = threading.Thread(target=self._rx_drain, daemon=True)

        self.last_send = 0.0
        self.tx_count = 0

        self.rx_buffer = ""

        # Cache all three PSX CDU screens.
        # PSX normally sends Qs62-Qs103 on connect; keeping these values
        # lets us switch CDU screens without using a reload key sequence.
        self.all_fmc_lines = {q: "" for q in range(62, 104)}
        self.all_fmc_color_lines = {q: "" for q in CDU_COLOR_QS_RANGE}
        self.fmc_lines = self.all_fmc_lines
        self.fmc_dirty = False
        self.fmc_timer = None
        self.fmc_lock = threading.Lock()

        # Temporary BRT+/BRT- feedback. The real PSX scratchpad is never changed;
        # this is only a rendered overlay on the final CDU row.
        self.brightness_overlay_step = None
        self.brightness_overlay_until = 0.0
        self.brightness_overlay_timer = None

        # The About dialog temporarily owns the physical CDU display.
        self.about_overlay_active = False

        self.cdu_lights_state = {}
        self.cdu_blank_state = {"L": 0, "C": 0, "R": 0}
        self.qi248_state = None

    def _active_config(self):
        return RUNTIME_CONFIG.get_active_cdu_config()

    def _active_qs_lines(self):
        return self._active_config()["screen_qs_lines"]

    def _active_color_qs_lines(self):
        active_cdu = RUNTIME_CONFIG.get_active_cdu()
        start = CDU_COLOR_QS_START[active_cdu]
        return list(range(start, start + FMC_HEIGHT))

    def _active_qh_name(self):
        return self._active_config()["key_qh"]

    def _active_lights_qi(self):
        return self._active_config()["lights_qi"]

    def _active_blank_qi(self):
        return self._active_config()["blank_qi"]

    def _reset_fmc_lines_for_active_cdu(self):
        # Kept for compatibility, but no longer clears the screen.
        # The active CDU display is now selected from self.all_fmc_lines.
        self.fmc_lines = self.all_fmc_lines


    def set_active_cdu(self, cdu):
        cdu = cdu.upper()

        if not RUNTIME_CONFIG.set_active_cdu(cdu):
            return False

        self.cdu_lights_state = {}

        # Re-apply the last known Qi248 value for the newly selected CDU.
        # Qi248 itself may not change when switching CDU, but the relevant
        # LCD bit does change:
        #   Left   = bit 19
        #   Center = bit 21
        #   Right  = bit 20
        if self.qi248_state is not None:
            self._apply_qi248_state(self.qi248_state, force_log=True)

        # Immediately draw the newly selected CDU from the cached Qs lines.
        with self.fmc_lock:
            self.fmc_dirty = True

        self._send_fmc_frame()

        return True


    def clear_command_from_source_cdu(self, source_qh_name):
        for code in CDU_SWITCH_CLEAR_SEQUENCE:
            self.send_raw_psx_line(f"{source_qh_name}={code}")
            time.sleep(0.03)

    def clear_scratchpad_command(self, source_qh_name):
        # Hold CLR briefly to clear the complete scratchpad command.
        # This is used after bridge commands such as CDU-L, CDU-C, CDU-R,
        # CDU-ATC, and CDU-ALTN.
        self.send_raw_psx_line(f"{source_qh_name}=39")
        time.sleep(CDU_COMMAND_CLEAR_HOLD_SECONDS)
        self.send_raw_psx_line(f"{source_qh_name}=-1")

    def clear_scratchpad_command_async(self, source_qh_name):
        # Do not block the CDU switch or display redraw while CLR is held.
        # The command is cleared on the source CDU while the bridge can already
        # switch to/render the selected CDU.
        t = threading.Thread(
            target=self.clear_scratchpad_command,
            args=(source_qh_name,),
            daemon=True
        )
        t.start()

    def set_atc_key_mode(self, mode):
        mode = str(mode or "").strip().upper()

        if mode not in ("ATC", "ALTN"):
            return False

        atc_altn = atc_key_value_to_altn(mode)
        RUNTIME_CONFIG.set_cdu_atc_altn(atc_altn)
        save_ini_value("FMC", "ATC_KEY", atc_altn_to_ini_value(atc_altn))

        log(f"[CONFIG] FMC ATC_KEY={atc_altn_to_ini_value(atc_altn)} saved to psx_winctrl_pfp.ini")
        return True

    def start(self):
        self.rx_thread.start()
        self.tx_thread.start()

    def stop(self):
        self.stop_evt.set()
        self.q.put(None)
        self.tx_thread.join(timeout=1.0)
        self.rx_thread.join(timeout=1.0)
        self._close()

    def send_code(self, code):
        self.q.put(code)

    def send_codes(self, codes):
        for code in codes:
            self.q.put(code)

    def send_raw_psx_line(self, line):
        with self.sock_lock:
            s = self.sock

        if not s:
            return False

        if not line.endswith("\n"):
            line += "\n"

        try:
            s.sendall(line.encode("ascii", errors="ignore"))
            return True
        except Exception:
            self._close()
            return False

    def _connect(self):
        with self.sock_lock:
            if self.sock:
                return True

        with self.connect_lock:
            with self.sock_lock:
                if self.sock:
                    return True

            s, was_unavailable = connect_with_retry(
                self.host,
                self.port,
                "PSX",
                stop_evt=self.stop_evt,
                retry_delay=5.0
            )

            if s is None:
                return False

            if was_unavailable:
                STATUS.reconnect_screen()

            log("[PSX] connected")

            try:
                s.sendall(
                    f"clientName={GUI_APPLICATION_TITLE} - {PFP_DEVICE_LABEL}\n"
                    .encode("utf-8")
                )

                # The bridge uses ECON variables only. PSX sends the required
                # CDU text, colors, lights, blanking state and Qi248 itself
                # when the network connection starts and whenever they change.

            except Exception as e:
                log(f"[PSX] setup failed after connect: {repr(e)}")
                try:
                    s.close()
                except Exception:
                    pass
                return False

            with self.sock_lock:
                if self.sock:
                    try:
                        s.close()
                    except Exception:
                        pass
                    return True

                self.sock = s

            return True

    def _close(self):
        with self.sock_lock:
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _rx_drain(self):
        while not self.stop_evt.is_set():
            try:
                if not self._connect():
                    continue

                with self.sock_lock:
                    s = self.sock

                if not s:
                    time.sleep(0.1)
                    continue

                data = s.recv(4096)

                if not data:
                    # Debug: PSX disconnect without exception
                    log_debug("[PSX RX] disconnected")
                    self._close()
                    continue

                text = data.decode("utf-8", errors="replace")
                self._handle_psx_text(text)

            except socket.timeout:
                continue

            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
                if not self.stop_evt.is_set():
                    log(f"[PSX RX] connection lost: {repr(e)}")
                self._close()
                time.sleep(1.0)

            except Exception as e:
                if not self.stop_evt.is_set():
                    log(f"[PSX RX] error: {repr(e)}")
                self._close()
                time.sleep(1.0)

    def _handle_psx_text(self, text):
        self.rx_buffer += text

        while "\n" in self.rx_buffer:
            line, self.rx_buffer = self.rx_buffer.split("\n", 1)
            line = line.rstrip("\r")

            if not line:
                continue

            if line.startswith(f"Qi{self._active_lights_qi()}="):
                self._handle_cdu_lights(line)
                continue

            if line.startswith("Qi") and "=" in line:
                try:
                    qi_num = int(line[2:].split("=", 1)[0])
                except ValueError:
                    qi_num = None

                if qi_num in CDU_BLANK_QI_TO_CDU:
                    self._handle_cdu_blank(line, qi_num)
                    continue

            if line.startswith(f"Qi{PSX_FMC_CONFIG_QI}="):
                self._handle_qi248(line)
                continue

            if line.startswith("Qs") and "=" in line:
                self._handle_fmc_line(line)

    def _handle_cdu_lights(self, line):
        _, value = line.split("=", 1)

        try:
            state = int(value.strip())
        except ValueError:
            return

        lights_qi = self._active_lights_qi()

        if state == self.cdu_lights_state.get(lights_qi):
            return

        self.cdu_lights_state[lights_qi] = state

        log_debug(f"[CDU LIGHTS] Qi{lights_qi}={state} / 0x{state:04X}")
        self.pfp_leds.apply_psx_cdu_lights_bitmask(state)

    def _handle_cdu_blank(self, line, qi_num):
        _, value = line.split("=", 1)

        try:
            state = int(value.strip())
        except ValueError:
            return

        cdu = CDU_BLANK_QI_TO_CDU.get(qi_num)
        if not cdu:
            return

        if state == self.cdu_blank_state.get(cdu, 0):
            return

        self.cdu_blank_state[cdu] = state

        log_debug(f"[CDU BLANK] Qi{qi_num} CDU {cdu}={state}")

        if cdu == RUNTIME_CONFIG.get_active_cdu():
            with self.fmc_lock:
                self.fmc_dirty = True

            self._send_fmc_frame()

    def _apply_qi248_state(self, state, force_log=False):
        cfg = self._active_config()

        nextgen_fmc = bool(state & QI248_NEXTGEN_FMC_BIT)
        cdu_lcd = bool(state & cfg["lcd_qi248_bit"])

        RUNTIME_CONFIG.set_from_qi248(nextgen_fmc, cdu_lcd)

        color = "w" if cdu_lcd else "g"
        atc_altn = RUNTIME_CONFIG.get_cdu_atc_altn()

        if force_log:
            log_debug(
                f"[CONFIG] Qi248={state} / 0x{state:08X} -> "
                f"CDU {cfg['label']}, CDU_COLOR={color}, CDU_ATC_ALTN={atc_altn}"
            )
        else:
            log_debug(
                f"[CONFIG] Qi248 reapplied: CDU {cfg['label']}, "
                f"CDU_COLOR={color}, CDU_ATC_ALTN={atc_altn}"
            )

    def _handle_qi248(self, line):
        _, value = line.split("=", 1)

        try:
            state = int(value.strip())
        except ValueError:
            return

        if state == self.qi248_state:
            return

        self.qi248_state = state
        self._apply_qi248_state(state, force_log=True)

        # Redraw because Qi248 can switch the active CDU between CRT and LCD.
        # That changes the default color and whether PSX LCD color strings matter.
        with self.fmc_lock:
            self.fmc_dirty = True

        self._send_fmc_frame()

    def show_about_overlay(self):
        """Keep the startup welcome display visible while GUI About is open."""
        with self.fmc_lock:
            self.about_overlay_active = True
        self.pfp_display.show_welcome_screen(VERSION)

    def hide_about_overlay(self):
        """Restore the latest cached active PSX CDU display."""
        with self.fmc_lock:
            self.about_overlay_active = False
            self.fmc_dirty = True
        self._send_fmc_frame()

    def _brightness_overlay_line(self, step):
        """Return the 24-column PSX-style BRT indicator.

        Step 0 shows one block; step 23 shows all 24 blocks. While the
        brightness is below maximum, the plus sign stays at the far-right
        scratchpad position. At maximum, the 24th block replaces the plus sign.
        """
        step = max(0, min(PFP_SCREEN_BRIGHTNESS_STEPS - 1, int(step)))
        block_count = step + 1

        if block_count >= FMC_WIDTH:
            return BRIGHTNESS_BLOCK * FMC_WIDTH

        return (
            (BRIGHTNESS_BLOCK * block_count)
            + (" " * (FMC_WIDTH - block_count - 1))
            + "+"
        )

    def _brightness_overlay_expired(self):
        with self.fmc_lock:
            remaining = self.brightness_overlay_until - time.monotonic()

            if remaining > 0:
                timer = threading.Timer(remaining, self._brightness_overlay_expired)
                timer.daemon = True
                self.brightness_overlay_timer = timer
                timer.start()
                return

            self.brightness_overlay_step = None
            self.brightness_overlay_until = 0.0
            self.brightness_overlay_timer = None
            self.fmc_dirty = True

        # Redraw the cached, current PSX scratchpad after the overlay disappears.
        self._send_fmc_frame()

    def show_brightness_overlay(self, step):
        """Show updated brightness feedback for 2.2 seconds and reset its timer."""
        step = max(0, min(PFP_SCREEN_BRIGHTNESS_STEPS - 1, int(step)))

        with self.fmc_lock:
            self.brightness_overlay_step = step
            self.brightness_overlay_until = time.monotonic() + BRIGHTNESS_OVERLAY_SECONDS
            self.fmc_dirty = True

            if self.brightness_overlay_timer:
                self.brightness_overlay_timer.cancel()

            timer = threading.Timer(
                BRIGHTNESS_OVERLAY_SECONDS,
                self._brightness_overlay_expired
            )
            timer.daemon = True
            self.brightness_overlay_timer = timer
            timer.start()

        self._send_fmc_frame()

    def _send_fmc_frame(self):
        with self.fmc_lock:
            if not self.fmc_dirty or self.about_overlay_active:
                return

            self.fmc_dirty = False

        active_cdu = RUNTIME_CONFIG.get_active_cdu()

        if self.cdu_blank_state.get(active_cdu, 0) != 0:
            # PSX BlankTimeCdu* is active: keep caching incoming CDU text,
            # but render the active hardware CDU as blank until the timer is 0.
            ordered_lines = [""] * FMC_HEIGHT
            ordered_color_lines = None
            log_debug(
                f"[CDU BLANK] CDU {active_cdu} screen blanked "
                f"value={self.cdu_blank_state.get(active_cdu, 0)}"
            )
        else:
            ordered_lines = [self.fmc_lines.get(q, "") for q in self._active_qs_lines()]
            if RUNTIME_CONFIG.get_cdu_color() == "w":
                # LCD mode: use PSX per-character LCD color strings.
                ordered_color_lines = [
                    self.all_fmc_color_lines.get(q, "")
                    for q in self._active_color_qs_lines()
                ]
            else:
                # CRT/legacy mode: keep the existing all-green behavior.
                ordered_color_lines = None

        # Render a temporary brightness overlay on the scratchpad row only.
        # Incoming PSX scratchpad updates remain cached and will reappear later.
        with self.fmc_lock:
            overlay_active = (
                self.brightness_overlay_step is not None
                and time.monotonic() < self.brightness_overlay_until
            )
            overlay_step = self.brightness_overlay_step

        if overlay_active:
            ordered_lines[-1] = self._brightness_overlay_line(overlay_step)

            if ordered_color_lines is not None:
                ordered_color_lines = list(ordered_color_lines)
                ordered_color_lines[-1] = "w" * FMC_WIDTH

        # Debug: FMC frame queued for direct HID display
        log_debug("[FMC] frame queued")
        self.pfp_display.send_lines(ordered_lines, ordered_color_lines)

    def _handle_fmc_line(self, line):
        left, value = line.split("=", 1)

        try:
            qnum = int(left[2:])
        except ValueError:
            return

        if qnum in CDU_COLOR_QS_RANGE:
            self.all_fmc_color_lines[qnum] = value

            # Only redraw the hardware CDU when the colour update belongs to the active CDU.
            if qnum in self._active_color_qs_lines():
                with self.fmc_lock:
                    self.fmc_dirty = True

                    if self.fmc_timer:
                        self.fmc_timer.cancel()

                    self.fmc_timer = threading.Timer(0.05, self._send_fmc_frame)
                    self.fmc_timer.daemon = True
                    self.fmc_timer.start()

            return

        if qnum < 62 or qnum > 103:
            return

        # Scratchpad command line for active CDU selection:
        #   CDU L -> Left CDU
        #   CDU C -> Center CDU
        #   CDU R -> Right CDU
        if qnum == self._active_qs_lines()[-1]:
            command = value.strip().upper()

            target_cdu = None

            if command in ("CDU-L", "CDU LEFT"):
                target_cdu = "L"

            elif command in ("CDU-C", "CDU CENTER", "CDU CENTRE"):
                target_cdu = "C"

            elif command in ("CDU-R", "CDU RIGHT"):
                target_cdu = "R"

            if target_cdu:
                source_qh_name = self._active_qh_name()
                self.set_active_cdu(target_cdu)
                self.clear_scratchpad_command_async(source_qh_name)
                return

            if command in ("CDU-ATC", "CDU ATC"):
                source_qh_name = self._active_qh_name()

                # Start clearing immediately, before saving the ini setting.
                # This keeps scratchpad cleanup independent from ATC/ALTN mode changes.
                self.clear_scratchpad_command_async(source_qh_name)

                # Also clear the local display cache immediately so the hardware CDU
                # does not keep showing the command while PSX processes CLR.
                self.all_fmc_lines[qnum] = ""
                with self.fmc_lock:
                    self.fmc_dirty = True
                self._send_fmc_frame()

                self.set_atc_key_mode("ATC")
                return

            if command in ("CDU-ALTN", "CDU ALTN"):
                source_qh_name = self._active_qh_name()

                # Start clearing immediately, before saving the ini setting.
                # This keeps scratchpad cleanup independent from ATC/ALTN mode changes.
                self.clear_scratchpad_command_async(source_qh_name)

                # Also clear the local display cache immediately so the hardware CDU
                # does not keep showing the command while PSX processes CLR.
                self.all_fmc_lines[qnum] = ""
                with self.fmc_lock:
                    self.fmc_dirty = True
                self._send_fmc_frame()

                self.set_atc_key_mode("ALTN")
                return

        # Store every CDU line, even when it is not the currently displayed CDU.
        self.all_fmc_lines[qnum] = value

        # Only redraw the hardware CDU when the updated line belongs to the active CDU.
        if qnum in self._active_qs_lines():
            with self.fmc_lock:
                self.fmc_dirty = True

                if self.fmc_timer:
                    self.fmc_timer.cancel()

                self.fmc_timer = threading.Timer(0.05, self._send_fmc_frame)
                self.fmc_timer.daemon = True
                self.fmc_timer.start()

    def _tx_loop(self):
        while not self.stop_evt.is_set():
            code = self.q.get()
            if code is None:
                break

            now = time.monotonic()
            dt = now - self.last_send
            if dt < self.min_interval:
                time.sleep(self.min_interval - dt)

            line = f"{self._active_qh_name()}={code}\n".encode("ascii", errors="ignore")

            try:
                if not self._connect():
                    continue

                with self.sock_lock:
                    s = self.sock

                if not s:
                    continue

                self.tx_count += 1
                # Debug: raw PSX keyboard output
                log_debug(f"[PSX] TX#{self.tx_count} -> {line!r}")
                s.sendall(line)
                self.last_send = time.monotonic()

            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as e:
                if not self.stop_evt.is_set():
                    log(f"[PSX] send connection lost: {repr(e)} -> reconnect")
                self._close()

            except Exception as e:
                if not self.stop_evt.is_set():
                    log(f"[PSX] send error: {repr(e)} -> reconnect")
                self._close()


def bridge_main():
    if threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGINT, _handle_sigint)
        except Exception:
            pass

    STATUS.start()

    if DEBUG:
        log("[CONFIG] Debug logging enabled")

    psx_host, psx_port, VID, PID = load_config()
    mapping = load_map()

    lsk_bps = {bp for bp, name in mapping.items() if name.upper().startswith("LSK")}
    log_debug(f"[MAP] LSK bitpos: {sorted(lsk_bps)}")

    bp_to_code = {}
    for bp, name in mapping.items():
        code = PSX_NAME_TO_CODE.get(name.upper())
        if code is not None:
            bp_to_code[bp] = code

    log_debug(f"[MAP] resolvable to PSX codes: {len(bp_to_code)}")
    # Use all mapped HID buttons, including BRT+/BRT- which have no PSX code
    mapped_bps = set(mapping.keys())

    devs = hid.enumerate(VID, PID)
    if not devs:
        log("")
        log("[ERROR] WINCTRL CDU not found.")
        log(f"[ERROR] Expected VID={VID:04X} PID={PID:04X}")
        log("")
        log("[ERROR] Check the [FMC] pid setting in psx_winctrl_pfp.ini.")
        log("[ERROR] Also check that the CDU is connected and visible in Windows.")
        log("")

        return

    d = devs[0]
    # Debug: selected HID device details
    log_debug(
        f"[HID] Using '{d.get('product_string')}' "
        f"if={d.get('interface_number')} "
        f"usage_page={d.get('usage_page')} "
        f"usage={d.get('usage')}"
    )

    h = hid.device()
    h.open_path(d["path"])

    try:
        h.set_nonblocking(True)
    except Exception:
        try:
            h.nonblocking = True
        except Exception:
            pass

    hid_lock = threading.Lock()

    pfp_leds = PfpLedController(h, hid_lock, PFP_DEST)
    pfp_leds.start()

    display = DirectHidDisplaySender(h, hid_lock, PFP_DEST)
    try:
        display.start()
    except RuntimeError as e:
        log("")
        log(f"[ERROR] {e}")
        log("")
        return

    display.show_welcome_screen(VERSION, seconds=3.0)

    psx = PsxSender(psx_host, psx_port, display, pfp_leds, MIN_SEND_INTERVAL)
    psx.start()

    global BRIDGE_PSX
    BRIDGE_PSX = psx
    if GUI_APP is not None:
        GUI_APP.set_psx_sender(psx)

    log_debug("[RUN] HID keys -> active PSX CDU, active PSX CDU screen -> direct HID display, active PSX CDU lights -> direct HID LEDs.")
    log_highlight("")
    log_highlight("┌─────────────────────────────┐")
    log_highlight("│ Scratchpad commands         │")
    log_highlight("├─────────────────────────────┤")
    log_highlight("│ CDU-L     Left CDU          │")
    log_highlight("│ CDU-C     Center CDU        │")
    log_highlight("│ CDU-R     Right CDU         │")
    log_highlight("│ CDU-ATC   ATC key mode      │")
    log_highlight("│ CDU-ALTN  ALTN page mode    │")
    log_highlight("└─────────────────────────────┘")
    
    prev_pressed = set()
    stable_count = defaultdict(int)
    last_rise_time = {}

    brt_hold_direction = 0
    brt_hold_start_time = None
    brt_last_repeat_time = 0.0
    hid_disconnected = False
    last_brightness_keepalive = time.monotonic()

    try:
        while not SHUTDOWN_REQUESTED.is_set():
            try:
                with hid_lock:
                    data = h.read(READ_SIZE)
            except OSError:
                hid_disconnected = True
                log("[HID] CDU disconnected")
                SHUTDOWN_REQUESTED.set()
                break

            now = time.monotonic()
            if now - last_brightness_keepalive >= HID_BRIGHTNESS_KEEPALIVE_SECONDS:
                try:
                    pfp_leds.refresh_screen_brightness_keepalive()
                except (OSError, ValueError):
                    hid_disconnected = True
                    log("[HID] CDU disconnected")
                    SHUTDOWN_REQUESTED.set()
                    break
                last_brightness_keepalive = now

            if not data:
                if SHUTDOWN_REQUESTED.wait(0.001):
                    break
                continue

            frame = bytes(data)

            if len(frame) < 1 or frame[0] != 0x01:
                continue

            cur_pressed_raw = pressed_from_mapping(frame, mapped_bps)

            cur_pressed = set()
            for bp in cur_pressed_raw:
                if bp in lsk_bps:
                    cur_pressed.add(bp)
                    continue

                stable_count[bp] += 1
                if stable_count[bp] >= STABLE_FRAMES:
                    cur_pressed.add(bp)

            for bp in list(stable_count):
                if bp not in cur_pressed_raw:
                    stable_count.pop(bp, None)

            rising = cur_pressed - prev_pressed
            falling = prev_pressed - cur_pressed
            prev_pressed = cur_pressed

            if (
                brt_hold_direction != 0
                and brt_hold_start_time is not None
                and now - brt_hold_start_time >= PFP_BRT_HOLD_DELAY
                and now - brt_last_repeat_time >= PFP_BRT_REPEAT_INTERVAL
            ):
                pfp_leds.change_screen_brightness_step(brt_hold_direction)
                psx.show_brightness_overlay(pfp_leds.get_screen_brightness_step())
                brt_last_repeat_time = now

            # Hardware key release handling.
            # PSX automatically releases most CDU keys internally, but CLR (39)
            # and ATC (60) must be explicitly released by sending -1 when the
            # physical key is released.
            for bp in falling:
                name = mapping.get(bp, "").upper()

                if name in ("BRT+", "BRT-"):
                    brt_hold_direction = 0
                    brt_hold_start_time = None
                    brt_last_repeat_time = 0.0
                    continue

                code = bp_to_code.get(bp)

                if code in NEED_RELEASE:
                    # Debug: HID release for keys requiring explicit PSX release
                    log_debug(f"[HID] release {mapping.get(bp)} -> -1")
                    psx.send_code(-1)

            # Hardware key press handling.
            for bp in rising:
                if (now - last_rise_time.get(bp, 0.0)) < RISING_COOLDOWN:
                    continue

                last_rise_time[bp] = now

                name = mapping.get(bp, "").upper()

                if name == "BRT+":
                    pfp_leds.change_screen_brightness_step(+1)
                    psx.show_brightness_overlay(pfp_leds.get_screen_brightness_step())
                    brt_hold_direction = +1
                    brt_hold_start_time = now
                    brt_last_repeat_time = now
                    continue

                if name == "BRT-":
                    pfp_leds.change_screen_brightness_step(-1)
                    psx.show_brightness_overlay(pfp_leds.get_screen_brightness_step())
                    brt_hold_direction = -1
                    brt_hold_start_time = now
                    brt_last_repeat_time = now
                    continue

                code = bp_to_code.get(bp)
                if code is None:
                    # Debug: mapped HID key has no PSX keycode
                    log_debug(f"[HID] {bp} {mapping.get(bp)} -> NO PSX CODE")
                    continue

                # Debug: keyboard output to PSX
                log_debug(f"[HID] {bp} {mapping.get(bp)} -> {code}")

                if code == 60 and RUNTIME_CONFIG.get_cdu_atc_altn() == 1:
                    # Debug: ATC alternate translation
                    log_debug("[ATC ALTN] ATC key replaced by FMC COMM, LSKL2")
                    psx.send_codes(CDU_ATC_ALTN_SEQUENCE)
                else:
                    psx.send_code(code)

    except KeyboardInterrupt:
        SHUTDOWN_REQUESTED.set()

    finally:
        # Ignore further CTRL+C during cleanup. One CTRL+C is enough.
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass

        # Save runtime choices first. This must happen before any HID cleanup,
        # because CTRL+C during HID cleanup may interrupt shutdown on macOS/PyInstaller.
        try:
            save_ini_value("FMC", "ACTIVE_CDU", RUNTIME_CONFIG.get_active_cdu())
            save_ini_value("FMC", "BRIGHTNESS", pfp_leds.get_screen_brightness_step())
            log_debug(
                f"[CONFIG] saved ACTIVE_CDU={RUNTIME_CONFIG.get_active_cdu()} "
                f"BRIGHTNESS={pfp_leds.get_screen_brightness_step()}"
            )
        except BaseException as e:
            # Do not let a shutdown-save problem cause a PyInstaller crash.
            # KeyboardInterrupt is also covered here.
            if not isinstance(e, KeyboardInterrupt):
                log(f"[CONFIG] failed to save runtime settings: {repr(e)}")

        # Hardware blackout while HID is still open. Keep this simple and fast:
        # backlights/annunciators first, optional display blank after.
        try:
            pfp_leds.force_blackout()
        except BaseException as e:
            if not isinstance(e, KeyboardInterrupt):
                if "not open" not in str(e).lower():
                    log_debug(f"[PFP LED] shutdown error: {repr(e)}")

        try:
            display.force_blank_display()
        except BaseException as e:
            if not isinstance(e, KeyboardInterrupt):
                if "not open" not in str(e).lower():
                    log_debug(f"[CDU] shutdown error: {repr(e)}")

        # Stop threads and close handles. These are intentionally best-effort.
        try:
            psx.stop()
        except BaseException:
            pass

        try:
            display.stop()
        except BaseException:
            pass

        try:
            pfp_leds.stop()
        except BaseException:
            pass

        try:
            h.close()
        except BaseException:
            pass

        log("[END] Please restart" if hid_disconnected else "[END]")





# ============================================================
# Embedded resources
# ============================================================

# Embedded Aerowinx MCDU font commands.
EMBEDDED_PFP_FONT_PAYLOAD_B64 = """
UEZQRjEBKAAAAAYBAAAAAAAAABkAAAABAAAAFwAgAGQAAAB0AAAAAAAAAGktAAAAFxsCAAAHAQAA
AAAAAAAMAgAAAQAAAAAAAAAAAgAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAACEAAAAAAAAAAAAAAAAAAAB///z///7///7AAAbAAAbAAAbAAAbAAAbAAAbAAAbAAAbA
AAbAAAbAAAbAAAbAAAbAAAbAAAbAAAbAAAbAAAbAAAb///7///5///wAAAAAAAAAAAAiAAAAAe8A
Ae8AAe8AAe8AAe8AAe8AAccAAccAAccAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIwAAAAAAAAAAAAAAAADgcADgcADg
cADgcAHA4AHA4AHA4H///H///H///AOBwAOBwAMBgAcDgAcDgAcDgH///H///H///A4HAA4HAA4G
ABwOABwOABwOABwOAAAAAAAAAAAAACQAAAAAAAAAAAAAAAAAAAAAPAAAPAAAfgAB/4AD/8AHvcAH
PMAHPAAHvAAHvAAD/AAB/AAA/gAAP4AAP8AAPeAAPOAAPOAAPOAGPOAHPeAHv8AD/4AB/wAAPAAA
PAAAAAAAAAAlAAAAAAAAAAAAAAAbAgAABwEAAAAAAAAADAIAAAEAAAAAAgAAAAIAAAAOADAfgDA/
gHA5gOA5gOA/gcAfg4AOAwAABwAADgAADAAAHAAAOAAAcAAAcAAA4AABwAABwAADgcAHA+AGB/AO
BjAcBjAYB/A4A+AwAcAAAAAAAAAAAAAmAAAAAAAAAAAAAAAAAAAAAPwAAf4AA94AA48AB4cAB4cA
A48AA84AA94AAfwAAPgAAfAAA/gAB7xADzxwDx7wDg/gDg/gHgfADgfADw/AD5/gB/7wA/jgAABA
AAAAAAAAAAAAJwAAAAAAAAAAAAAAAAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACgAAAAA
AAAAAAAAAAAADwAAPwAAfwAA8AAB4AABwAADgAADgAADgAADAAADAAADAAADAAADAAADAAADAAAD
AAADAAADgAADgAABwAAB4AAA+AAAfwAAPwAADwAAAAAAAAAAAAApAAAAAAAAAAAAAAAAA8AAA/AA
A/gAADwAAB4AAA8AAAcAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAcAAA8A
AB4AADwAA/gAA/AAA8AAAAAAAAAAAAAAKgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGwIAAAcBAAAA
AAAAAAwCAAABAAAAAAQAAAACAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHAAAHAAAHAAAHAADHGA
H//AH//AB/8AAPgAAPwAAdwAA94AB48AAwYAAAAAAAAAAAAAAAAAKwAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAD//8D//8AAwAAAwAAAwAAAwAAAwAAAw
AAAwAAAwAAAAAAAAAAAAAAAAAAAAAAAAACwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPAAAPAAAPAAAPAAAPAAA
fAAAeAAAAAAAAAAtAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAP//wP//wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAALgAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAB4AAD8AADsAADsAAD8AAA4AAAAAAAAAAAAAC8AAAAAAAAAAAAAAAAAADAA
ADAAAHAAAOAAAMAAAcAAA4AAAxsCAAAHAQAAAAAAAAAMAgAAAQAAAAAGAAAAAgAAAAAHAAAOAAAc
AAAcAAA4AABwAABwAADgAAHAAAGAAAOAAAcAAAYAAA4AABwAABgAADgAADAAAAAAAAAAAAAAADAA
AAAAAAAAAAAAAAAA/AAD/zAH//APA+AeAeAcAfA4A/A4AzAwBzAwDjAwDDAwHDAwODAwcDAwYDAw
4DAxwDA5gDA7gDA/AHAeAPAeAeAfg8A//4Az/wAw/AAAAAAAAAAAAAAxAAAAAAAAAAAAAAAAABgA
ADgAAHgAAHgAAPgAANgAABgAABgAABgAABgAABgAABgAABgAABgAABgAABgAABgAABgAABgAABgA
ABgAABgAABgAABgAAP8AAP8AAAAAAAAAAAAAMgAAAAAAAAAAAAAAAAD+AAP/gAf/wA+B4B4A8BwA
cDgAMDgAMBAAcAAA4AAB4AADwAAHgAAPAAAeAAA8AAB4AAHwAAPAAAeAAA8AAB4AADwAAD//8D//
8B//8AAAAAAAAAAAADMAAAAAAAAAAAAAAAAA/gAD/4AH/8APAeAOAHAcAHAYADAYADAAADAAAHAA
APAAAeAAH8AAH8AAH8AAAeAAAHAAADAYADAYADAcADAcAHAPAOAH5+AD/4AA/wAAAAAAAAAAAAA0
AAAAAAAAAAAAAAAAAAYAAA4AAB4AAB4AAD4AAHYAAGYAAOYAAcYAAYYAA4YABwYbAgAABwEAAAAA
AAAADAIAAAEAAAAACAAAAAIAAAAGBgAOBgAMBgAYBgA///A///Af//AABgAABgAABgAABgAABgAA
BgAABgAAAAAAAAAAAAA1AAAAAAAAAAAAAAAAH//wP//wP//wMAAAMAAAMAAAMAAAMAAAMAAAMAAA
MAAAP/wAP/8AH//AAAPgAADgAABwAABwAAAwOAAwOABwHABwHwHgD//AB/+AAf4AAAAAAAAAAAAA
NgAAAAAAAAAAAAAAAAP/8A//8B//8B4AADwAADgAADgAADgAADAAADAAADAAADAAADH+ADP/gD//
wD4B4DwAcDgAMDAAMDAAMDgAMDwAcB4B4B//4Af/gAH+AAAAAAAAAAAAADcAAAAAAAAAAAAAAAA/
//A///AAAHAAAOAAAMAAAcAAA4AAA4AABwAABgAADgAAHAAAGAAAOAAAMAAAcAAA4AAAwAABwAAD
gAADgAAHAAAGAAAOAAAMAAAIAAAAAAAAAAAAAAA4AAAAAAAAAAAAAAAAAf4AB//AD//gHgHwHABw
OAAwMAAwMAAwMAAwOABwHADwHwHgD//AB/+AD//AHgHgGABwOAAwMAAwMAAwOAAwPABwHgHwD//g
B//AAf4AAAAAAAAAAAAAOQAAAAAAAAAAAAAAAAH+AAf/wA//4B4B8BwAcDgAMDAAMDAAMDAAMDgA
cBwA8B8B8A//8Af/sAH+MAAAGwIAAAcBAAAAAAAAAAwCAAABAAAAAAoAAAACAAAwAAAwAAAwAAAw
AAAwAABwAADwAAHwH//gP//AP/8AAAAAAAAAAAAAOgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB8
AAB8AAB8AAB8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB8AAB8AAB8AAB8
AAAAAAAAAAAAAAAAAAAAADsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPAAAPAAAPAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPAAAPAAAPAAAPAAAPAAAfAAAeAAAAAAA
AAA8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAAAcAAA8AAB4AADwAAHgAAPAAAeAAA4AAA4AA
A8AAAeAAAPAAAHgAADwAAB4AAA8AAAcAAAMAAAAAAAAAAAAAAAAAAAAAAAAAPQAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAA//4A//4A//4AAAAAAAAAAAAAAAAAAAAA//4A//4A//4AAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD
AAADgAABwAAA4AAAcAAAOAAAHAAADgAABwAAAwAABwAADgAAHAAAOAAAcAAA4BsCAAAHAQAAAAAA
AAAMAgAAAQAAAAAMAAAAAgAAAAHAAAOAAAMAAAAAAAAAAAAAAAAAAAAAAAAAAD8AAAAD/gAD/wAD
/wAAB4AAB4AAB4AAB4AAB4AAB4AAD4AAHwAAHgAAPgAAfAAAeAAAeAAAeAAAeAAAeAAAAAAAAAAA
AAAAAAAA+AAA+AAA+AAA+AAAAAAAAAAAAAAAAAAAAABAAAAAD//gH//wHgDwHABwHABwHABwHH9w
HP9wHe9wHc9wHc9wHc9wHc9wHc9wHc9wHc9wHc9wHc9wHe9wHf9wHP/wHAPgHAAAHgAAH/4AD/4A
Af4AAAAAAAAAAAAAAAAAAAAAQQAAAAAAAAAAAAAAAAAwAAAwAAA4AAB4AAB4AAB8AADsAADMAADO
AAHGAAGGAAGHAAODAAMDAAMDgAcBgAf/gAf/wA4AwAwAwAwAYBwAYBgAcDgAMDAAMDAAMAAAAAAA
AAAAAEIAAAAAAAAAAAAAAAA//wA//4A//8AwAeAwAHAwAHAwADAwADAwADAwAHAwAHAwAeA//8A/
/4A//8AwAeAwAHAwAHAwADAwADAwADAwAHAwAGAwAeA//8A//wAAAAAAAAAAAABDAAAAAAAAAAAA
AAAAAPwAA/8AB/+ADwPAHgDgHABwOABwOAAwOAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAA
OAAgOABwHABwHgDgDwHgB/8bAgAABwEAAAAAAAAADAIAAAEAAAAADgAAAAIAAMAD/wAA/AAAAAAA
AAAAAABEAAAAAAAAAAAAAAAAP/wAP/+AMAfAMAHgMABwMABwMAAwMAAwMAAwMAAwMAAwMAAwMAAw
MAAwMAAwMAAwMAAwMAAwMAAwMAAwMABwMADwMAPgP//AP/+AH/wAAAAAAAAAAAAARQAAAAAAAAAA
AAAAAD//8D//8DAAADAAADAAADAAADAAADAAADAAADAAADAAADAAAD//gD//gDAAADAAADAAADAA
ADAAADAAADAAADAAADAAAD//8D//8D//8AAAAAAAAAAAAEYAAAAAAAAAAAAAAAA///A///AwAAAw
AAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAA//4A//4AwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAw
AAAwAAAwAAAwAAAAAAAAAAAAAABHAAAAAAAAAAAAAAAAAPwAA/8AB/+ADwHAHgDgHABwOABwOAAw
OAAAMAAAMAAAMAAAMAAAMAAAMH/wMH/wMAAwMAAwOAAwOAAwHABwHgDgDwPgB//AA/8AAPwAAAAA
AAAAAAAASAAAAAAAAAAAAAAAADAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMD//
8D//8DAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMAAAAAAAGwIAAAcBAAAAAAAA
AAwCAAABAAAAABAAAAACAAAAAAAASQAAAAAAAAAAAAAAAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAw
AAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAA
AAAAAAAAAEoAAAAAAAAAAAAAAAAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAA
ADAAADAAADAAADAAADAQADAwADA4AHAcAGAeAOAPA8AH/4AD/wAA/AAAAAAAAAAAAABLAAAAAAAA
AAAAAAAAMAAwMABwMADwMAHAMAOAMAcAMB4AMDwAMHgAMOAAMcAAM4AAPwAAP4AAPcAAOOAAMHAA
MDgAMBwAMA4AMAcAMAOAMAHAMADgMABwMAAQAAAAAAAAAAAATAAAAAAAAAAAAAAAADAAADAAADAA
ADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAA
ADAAADAAAD//8B//8AAAAAAAAAAAAE0AAAAAAAAAAAAAAAAwABA4ADA4AHA8APA+APA+AfA3A7Az
g7AzhzAxzjAw7jAw/DAweDAwODAwEDAwADAwADAwADAwADAwADAwADAwADAwADAwADAwADAwADAA
AAAAAAAAAABOAAAAAAAAABsCAAAHAQAAAAAAAAAMAgAAAQAAAAASAAAAAgAAAAAAAAAwADA4ADA4
ADA8ADA+ADA+ADA3ADAzgDAzgDAxwDAw4DAw4DAwcDAwODAwODAwHDAwDjAwDjAwBzAwA7AwA7Aw
AfAwAPAwAPAwAHAwADAAAAAAAAAAAABPAAAAAAAAAAAAAAAAAPwAA/8AB/+AD4PAHgHgHADwOABw
OAAwMAAwMAAwMAAwMAAwMAAwMAAwMAAwMAAwMAAwMAAwOAAwOABwHADwHgHgDwPAB/+AA/8AAPwA
AAAAAAAAAAAAUAAAAAAAAAAAAAAAAD//AD//gDABwDAA4DAAcDAAMDAAMDAAMDAAMDAAcDAAcDAB
4D//wD//gD/+ADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAAAAAAAAAAAAAAFEAAAAA
AAAAAAAAAAAA/AAD/wAH/4APg8AeAeAcAPA4AHA4ADAwADAwADAwADAwADAwADAwADAwADAwADAw
ADAwAjA4BzA4A/AcAfAeAeAPA+AH//AD/3AA/DAAAAAAAAAAAABSAAAAAAAAAAAAAAAAP/8AP/+A
P//AMAHgMABwMABwMAAwMAAwMAAwMABwMABwMAHgP//gP//AP/4AMA4AMAcAMAcAMAOAMAHAMAHA
MADgMADgMABwMABwMAAwAAAAAAAAAAAAUwAAAAAAAAAAAAAAAAH8AAf/AA8bAgAABwEAAAAAAAAA
DAIAAAEAAAAAFAAAAAIAAP+AHgfAHAHgOADgMABwMAAwMAAgOAAAOAAAHgAAH/4AB/+AAf/AAAHg
AADgAABwAAAwMAAwOABwHABwHwHgD//AB/+AAf4AAAAAAAAAAAAAVAAAAAAAAAAAAAAAAD//8D//
8AAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAw
AAAwAAAwAAAwAAAwAAAwAAAAAAAAAAAAAFUAAAAAAAAAAAAAAAAwADAwADAwADAwADAwADAwADAw
ADAwADAwADAwADAwADAwADAwADAwADAwADAwADAwADAwADA4ADA4AHAcAHAeAOAPA8AH/4AD/wAA
/AAAAAAAAAAAAABWAAAAAAAAAAAAAAAAMAAwOAAwOABwGABgHABgDADgDADADgDABgHABgGABgGA
AwOAAwMAAwMAAYcAAYYAAYYAAM4AAMwAAMwAAHwAAHgAAHgAADgAADAAADAAAAAAAAAAAAAAVwAA
AAAAAAAAAAAAADAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAAMDAQMDA4MDB4MDD8MDDu
MDHOMDOHMDODsDcDsD4B8D4A8DwA8DgAcDgAMDAAEAAAAAAAAAAAAFgAAAAAAAAAAAAAAAAYABgY
ADgcADAOAHAGAOAHAMADGwIAAAcBAAAAAAAAAAwCAAABAAAAABYAAAACAACBwAGDgAHDAADnAABu
AAB8AAA4AAA4AAB8AABuAADnAAHDAAGDgAOBwAcAwAYA4A4AcBwAMBgAOBgAGAAAAAAAAAAAAFkA
AAAAAAAAAAAAAAAwADA4ADAYAHAcAOAOAMAGAcAHA4ADgwABhwABzgAA7AAAfAAAeAAAMAAAMAAA
MAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAAAAAAAAAAABaAAAAAAAAAAAAAAAAP//w
P//wAADgAAHAAAHAAAOAAAOAAAcAAA4AAA4AABwAADgAADgAAHAAAOAAAOAAAcAAA4AAA4AABwAA
BgAADgAAHAAAHAAAP//wP//wAAAAAAAAAAAAWwAAAAH/AAH/AAHAAAHAAAHAAAHAAAHAAAHAAAHA
AAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAH/
AAH/AAAAAAAAAAAAAFwAAAAAAAAAAAAAAAAAAABwAABwAAB4AAA4AAAcAAAeAAAOAAAPAAAHgAAD
gAADwAABwAAA4AAA8AAAcAAAeAAAOAAAHAAAHgAADgAADwAAB4AAA4AAA8AAAIAAAAAAAAAAAABd
AAAAAH/AAH/AAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAABsCAAAHAQAAAAAAAAAM
AgAAAQAAAAAYAAAAAgAAAcAAAcAAAcAAAcAAAcAAAcAAAcAAAcAAAcAAAcAAAcAAAcAAAcAAAcAA
f8AAf8AAAAAAAAAAAABeAAAAAAAAAAAAAAAAAAAAAAAAADgAADgAAHwAAHwAAP4AAO4AAe8AAccA
A4OAA4OAAQEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
XwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGAAAAAAAAAAAAAAYAAA
4AAA8AAAcAAAOAAAOAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABhAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAPwAA/8AB++AAwPAAAHAAAHgAAHgAADgAP/gA//gB4DgDwHgDwHgDgHgDwPgDwfgB//g
A/3gACAAAAAAAAAAAAAAYgAAAAAAAAAAAAAAAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8+AA//gA//
wA/DwA+B4A+B4A8B4A8bAgAABwEAAAAAAAAADAIAAAEAAAAAGgAAAAIAAADgDwDgDwDgDwDgDwDg
DwHgD4HgD4PAD8fAD/+ADv8AAAgAAAAAAAAAAAAAYwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAA+AAD/gAH/4APB4AeAwAeAAAcAAA8AAA8AAA8AAA8AAA8AAAcAAAeAAAfAwAPh4AH/
wAD/gAAIAAAAAAAAAAAAAGQAAAAAAAAAAAAAAAAAEAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAA
OAAAOAAAOAAAOAAAOAAAOAAIOCAcOHAeOPAPOeAHu8AD/4AB/wAA/wAAfgAAPAAAGAAAAAAAAAAA
AABlAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHwAAf8AA+eAB4PABwHADwHgDwHg
DgHgD//gD//gDgAADwAADwAABwAAB4CAA8HAA//AAP+AAAgAAAAAAAAAAAAAZgAAAAAAAAAAAAAA
AAAfwAB/4AB48ADwYADwAADgAADgAADgAA//gA//gADgAADgAADgAADgAADgAADgAADgAADgAADg
AADgAADgAADgAADgAADgAADgAADgAAAAAAAAAAAAAGcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAeAAB/AABGwIAAAcBAAAAAAAAAAwC
AAABAAAAABwAAAACAAD+AAOOAAMGAAMGAAOOAAH+AAH8AAB4AAAAAAAAAAAAAGgAAAAAAAAAAAAA
AAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHPwAH/4AH48AHw8AHgcAHgcAHAcAHAcAHAcAHAcAH
AcAHAcAHAcAHAcAHAcAHAcAHAcAHAcAAAAAAAAAAAABpAAAAAAAAAAAAAAAAADgAAHwAAHwAADgA
AAAAAAAAAAAAAAAAA/gAA/gAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgA
A/+AA/+AA/+AAAAAAAAAAAAAagAAAAAAAAAAAAAAAAAAAAAPAAAPgAAPAAAGAAAAAAAAAAAAAAH/
AAH/AAH/AAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAA
AAAAAGsAAAAAAAAAAAAAAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHAAAHA8AHB4AHDwAHHgAHPAAH
eAAH+AAH+AAH+AAHvAAHHgAHHwAHDwAHB4AHA8AHA+AHAeAAAIAAAAAAAAAAAABsAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAAAcAAA8AAB4AADwAAHgAAPAAAf//4///8///4
eAAAPAAAHgAADwAABxsCAAAHAQAAAAAAAAAMAgAAAQAAAAAeAAAAAgAAgAADwAAB4AAAwAAAAAAA
AAAAAABtAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/fgD//gDzzwDjzwDjjw
DjjwDjjwDjjwDjjwDjjwDjjwDjjwDjjwDjjwDjjwDjjwDjjwDjjwAAAAAAAAAAAAbgAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAc/AAf/gAfnwAfDwAeBwAcBwAcBwAcBwAcBwAcB
wAcBwAcBwAcBwAcBwAcBwAcBwAcBwAcBwAAAAAAAAAAAAG8AAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAfAAB/wAD/4AHg8AHg8APAeAPAeAOAOAOAOAOAOAOAOAOAOAPAeAPAeAHg8AH
x8AD/4AB/wAAEAAAAAAAAAAAAABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
D34AD/+AD+fAD8PAD4HgDwHgDwDgDwDgDwDgDwDgDwDgDwDgDwHgD4HgD4PAD+fAD/+AD38ADwAA
AAAAAAAAcQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD94AH/4APH4AeD4A8B
4A8B4A4B4A4B4A4B4A4B4A4B4A8B4A8B4A8D4AeD4AfP4AP/4AD94AAbAgAABwEAAAAAAAAADAIA
AAEAAAAAIAAAAAIAAAHgAAAAAAAAcgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAMAAAOAAAPAAAHgAADwAAB4AAA8H//+P///H///AAA+AAA8AAB4AADwAAHgAAPAAAeAAAMAAAA
AAAAAAAAAHMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/gAD/4ADx8AHgcAHgIAH
gAADwAAD8AAA/gAAP4AAD8AAA8AAAeAGAeAHAcAPg8AH/4AB/wAAEAAAAAAAAAAAAAB0AAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAADAAADgAAHgAAHwAAPwAAP4A
Af4AAf8AA/8AA/+AB/+AB//AD//AD//gH//gAAAAAAAAAAAAdQAAAAAAAAAAAAAAAAAwAAB4AAD8
AAH+AAH/AAP/gAe7wA854B448Bw4cAg4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4
AAA4AAA4AAA4AAAQAAAAAAAAAAAAAHYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAOAOAPAOAHAeAHAcAHgcADg8ADg4ADw4ABx4AB5wAB7wAA7gAA/gAAfgAAfAAAfAAAOAAAOAAA
AAAAAAAAAAB3AAAAGwIAAAcBAAAAAAAAAAwCAAABAAAAACIAAAACAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAcAHAcAHAcOHAeOHAOPHAOfOAOfOAOfuAPfuAHbuAH7uAH58AH58AH
x8AHx8ADw8ADw8ADggAAAAAAAAAAAAB4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAADwHAB4PAA4eAA8cAAe8AAO4AAP4AAHwAAHgAAHwAAP4AAP4AAe8AA8eAA8eAB4PADwHgDwHg
AAAAAAAAAAAAeQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA8B4A8B4AcBwAeB
wAeDwAODwAPDgAHDgAHHgAHnAADnAADvAAD+AAB+AAB+AAB8AAA8AAA8AAA4AAAAAAAAAHoAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH/8AH/8AAA8AAB4AADwAADgAAHgAAPAAA
eAAAeAAA8AAB4AADwAADwAAHgAAP/+AP/+AP/+AAAAAAAAAAAAB7AAAAAHgAAPAAAPAAAOAAAOAA
AOAAAOAAAOAAAOAAAOAAAeAAD8AAD4AAD8AAAeAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAA
APAAAPgAAH/AAD/AAAHAAAAAAAAAAAAAfAAAAAA4AAA4AAA4AAA4ABsCAAAHAQAAAAAAAAAMAgAA
AQAAAAAkAAAAAgAAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgA
ADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAAAAAAAAAAAAAfQAAAAB4AAA4AAA8AAA8AAA8
AAA8AAA8AAA8AAA8AAAcAAAcAAAPwAAHwAAPwAAeAAAcAAA8AAA8AAA8AAA8AAA8AAA8AAA8AAA8
AAA8AAB4AA/4AA/wAAwAAAAAAAAAAAAAAH4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAEAD8MAH/8AH/8AGH4AEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAACwAAAAAAAAAAAAAAAAAeAAB/gAB/wADhwADhwAHg4AHg4ADhwADzwAB/gAA/AA
AEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAECYA
AAAAAAAAAAAAAAAAAH//+H//+H//+HAAOHAAOHAAOHAAOHAAOHAAOHAAOHAAOHAAOHAAOHAAOHAA
OHAAOHAAOHAAOHAAOHAAOHAAOHAAOH//+H//+D//8AAAAAAAAAAAAJAhAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAbAgAABwEAAAAAAAAADAIAAAEAAAAAJgAAAAIAAAAAAAAAAAAwAABwAADgAADA
AAGAAAMAAAcAAA//4AcAAAOAAAHAAADgAABgAABwAAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJEh
AAAAAAAAAAAAAAAAAAAAOAAAeAAA/AAB/gABuwADOYAHOcAGOMAAOAAAOAAAOAAAOAAAOAAAOAAA
OAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAAAAAAAAAAACSIQAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAABgAABwAAA4AAAcAAAMAAAGAD//AD//gAAHAAAOAAAcAAA4AAAwA
ABgAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAkyEAAAAAAAAAAAAAAAAAAAA4AAA4AAA4AAA4AAA4
AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAY4wAc5wAO7gAH/AAD+AAB8AAA4
AAAQAAAAAAAAAAAAAJQDAAAAAAAAAAAAAAAAeAAAfAAAfAAA/gAA7gAB7wAB7wADxwADx4AHg4AH
g8APg8APAeAPAeAeAPAeAPA+APg8AHh8AHx4ADz///7///7///7///4AAAAAAAAAAAAAAAAAAAAh
KwAAAAAAAAAAADgAAHwAAP4AA/8AB8fAD4PgHwHwPgD4PAB4OAA4GwIAAAcBAAAAAAAAAAwCAAAB
AAAAACgAAAACAAA4ADg4ADg4ADg4ADg4ADg4ADg4ADg8AHg+APgfAfAPg+AHx8AD/wAA/gAAfAAA
OAAAEAAAAAAAAAAAAADAJQAAAAAAAAAAAAAAAAAYAAA4AAD4AAH4AAf4AA/4AD/4AH/4Af/4A//4
D//4H//4H//4B//4A//4AP/4AH/4AB/4AA/4AAP4AAH4AAB4AAA4AAAIAAAAAAAAAAAAAAAAAAAA
tiUAAAAAAAAAAAAAABAAABgAAB4AAB8AAB/AAB/gAB/4AB/8AB//AB//gB//4B//+B//8B//4B//
gB//AB/8AB/4AB/gAB/AAB8AAB4AABgAABAAAAAAAAAAAAAAAAAAAAAAAIglAAAAAAAAAAAAAAAP
/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AP
/+AP/+AP/+AP/+AP/+AP/+AP/+AP/+AAAAAAAACyJQAAAAAAAAAAAAAAAAAAAAAAABAAADgAADgA
AHwAAHwAAP4AAP4AAf8AAf8AA/+AB//AB//AD//gD//gH//wH//wP//4f//8f//8///+AAAAAAAA
AAAAAAAAAAAAAAAAAAAAvCUAAAAAAAAAAAAAAAAAAAAAAP///n///D//+D//+B//8B//8A//4A//
4Af/wAf/wAP/gBsCAAAHAQAAAAAAAAAMAgAAAQAAAAAqAAAAAgAAAf8AAf8AAP4AAP4AAHwAAHwA
ADgAADgAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoCUAAAAAAAAAAAAAAP///v///v///v///v//
/v///v///v///v///v///v///v///v///v///v///v///v///v///v///v///v///v///v///gAA
AgAAAAAAAAAAAAAAAAAAAKElAAAAAAA///g///g///g8AHg8AHg8AHg8AHg8AHg8AHg8AHg8AHg8
AHg8AHg8AHg8AHg8AHg8AHg8AHg8AHg8AHg8ADg8ADg8ADg8ADg8ADg///g///g///g///gAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABrAQAABwEAAAAAAAAAXAEAAAEA
AAAALAAAUAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAEoAAAABgEAAAAAAAAAGQAAAAIAAAAXACAAZAAAAHQAAAAAAAAAaS0AAAAXGwIAAAcBAAAA
AAAAAAwCAAACAAAAAAAAAAACAAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAIQAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4
AAA4AAA4AAA4AAA4AAA4AAAAAAAAAAAAAAA4AAA4AAA4AAAAAAAAAAAAAAAAAAAAACIAAAAAAAAA
AAAAAAAAAAAAAAAAAAAA7gAA7gAA7gAA7gAA7gAA7gAA7gAA7gAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAjAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AO4AAO4AAO4AAO4AAO4AA/+AA/+AAO4AAO4AAO4AAO4AAO4AAO4AA/+AA/+AAO4AAO4AAO4AAO4A
AO4AAO4AAAAAAAAAAAAAAAAAAAAAJAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY
AAAYAAB+AAD/AAHbgAGZAAGYAAHYAAH4AAD8AAA/AAAfgAAbgAAZgAAZgAGbgAPfAAH/AAA4AAAY
AAAAAAAAACUAAAAAAAAAAAAAABsCAAAHAQAAAAAAAAAMAgAAAgAAAAACAAAAAgAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAcBgA+BgA+DgA+HAA+HAAcOAAAcAAAcAAA4AABwAABwAADgAAHCAAHPgAOP
gAcNgAYPgA4PgAwHAAAAAAAAAAAAACYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
eAAA/AABzgABzgABzgABzgAA/AAA/AAA8AAB+AAB+YADncADj4ADD4ADhwADj4AD/4AB+YAAAQAA
AAAAAAAAAAAnAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAADgAADgAADgAADgAADgA
ADgAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcAAB8AAD8AADgAAHAAAHAAAHAAAHAAAHAAAHA
AAHAAAHAAAHAAAHAAAHAAADgAAD4AAB8AAAcAAAAAAAAAAAAACkAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAB4AAB8AAA+AAAPAAAHAAADAAADAAADgAADgAADgAADgAADgAADgAADAAA
DAAAHAAA+AAB8AAB4AAAAAAAAAAAAAAqAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgbAgAABwEAAAAA
AAAADAIAAAIAAAAABAAAAAIAAAAAOAAAOAADOYAD/4AD/4AAOAAAfAAA7gAB7wABxgAARAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAArAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABgAABgAADgAADgAADgAB//AB//AA/+AADgAADgAADgA
ABgAABgAAAAAAAAAAAAAAAAAAAAAAAAALAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA8AAA8AAA8AAA4
AAA4AAAAAAAAAC0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAD/4AH/8AH/8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAuAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAHAAAPgAAPgAAPgAAPgAAHAAAAAAAAAAAAAALwAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAGwIAAAcBAAAAAAAAAAwCAAACAAAAAAYAAAACAACAAAHAAAOAAAOA
AAcAAA4AAA4AABwAADgAADgAAHAAAOAAAOAAAcAAA4AAA4AABwAADgAABgAAAAAAAAAAAAAAMAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD5gAH/gAP/AAcHAA4PgA4fgA4fgA47gA47
gA5zgA7jgA7jgA/DgA/DgA+DgAeHAAf+AA/+AAz4AAAAAAAAAAAAADEAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAMAAAcAAA+AAA+AAA+AAAuAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAA
OAAAOAAAOAAAfAAA/gAA/gAAAAAAAAAAAAAyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAHwAAf8AA/+AA4HABwHABgHAAAHAAAOAAAeAAA8AAB4AADwAAHgAAPAAAeAAA8AAB/+AB//A
A//AAAAAAAAAAAAAMwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB8AAH/AAP/gAOD
wAcBwAYBwAABwAABgAAPgAAfAAAfAAADgAABwAYAwAcBwAOBwAP/gAH/AAB+AAAAAAAAAAAAADQA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABgAADgAAHgAAHgAAPhsCAAAHAQAAAAAA
AAAMAgAAAgAAAAAIAAAAAgAAAAB+AAB+AADuAAHOAAHOAAOOAAf/gAf/wAf/wAAOAAAOAAAOAAAG
AAAGAAAAAAAAAAAAADUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH/8AH/8AH/4AH
AAAHAAAHAAAHAAAHAAAH/gAH/4AD/4AAAcAAAcAAAcAHAcAHAcAD/4AB/wAA/gAAAAAAAAAAAAA2
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/AA//AA/+ABwAABwAABwAABwAABwAA
BzgAB/8AB/+AB4OABwHABwHABwHABwHAA/+AA/8AAP4AAAAAAAAAAAAANwAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAf/wAf/wAP/wAADgAADAAAHAAAOAAAOAAAcAAAYAAA4AABwAABw
AADgAADAAAHAAAOAAAOAAAMAAAAAAAAAAAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAA/AAB/4AD/4AHAcAHAcAHAcAHAcAHA8AD/4AB/wAB/wADg4AHAcAHAcAHAcAHAcAD/4AB
/4AA/gAAAAAAAAAAAAA5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP4AAf+AA/+A
BwHABwHABwHABwHAA4PAA/8bAgAABwEAAAAAAAAADAIAAAIAAAAACgAAAAIAAMAB/8AAOcAAAcAA
AcAAAcAAAcAAAcAD/4AH/4AH/gAAAAAAAAAAAAA6AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAADgAADgAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAADgAADgA
AAAAAAAAAAAAAAAAAAAAOwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAA8AAA8AAA8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA8AAA8AAA8AAA4AAA4AAAAAAAA
ADwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABgAADgAAHAAAOAAAcAAA
4AABwAABwAAA4AAAcAAAOAAAHAAADgAABgAAAAAAAAAAAAAAAAAAAAAAAAA9AAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/+AA/+AAAAAAAAAAAAAA/+AA/+AA/+A
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAGAAAHAAADgAABwAAA4AAAcAAAOAAAOAAAcAAA4AABwGwIAAAcBAAAAAAAA
AAwCAAACAAAAAAwAAAACAAAAAOAAAcAAAYAAAAAAAAAAAAAAAAAAAAAAAAAAPwAAAAAAAAAAAAAA
AAAAAAAAAAAAAAH4AAH+AAH+AAAOAAAPAAAPAAAPAAAOAAAeAAAcAAA4AAB4AABwAABwAABwAAAA
AAAAAAAAAAB4AAB4AAB4AAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB/wAD
/4AHAcAHAcAHAcAHf8AH/8AH78AH78AH78AH78AH78AH78AH78AH/8AHf8AHB8AHAAAHAAAD/AAA
/AAAAAAAAAAAAAAAAAAAAABBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAADgA
ADgAADwAAHwAAHwAAHwAAO4AAO4AAMYAAccAAf8AAf8AA/+AA4OAAwGABwHABwHABgDAAAAAAAAA
AAAAQgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/8AA/+AA//AA4DgA4DgA4BgA4B
gA4DgA//gA//AA//gA4HgA4DgA4BgA4BgA4DgA//AA//AA/8AAAAAAAAAAAAAEMAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA+AAB/gAD/wAHB4AOA4AOAYAOAAAOAAAOAAAOAAAOAAAO
AAAOAAAOAQAOA4AHB4AH/xsCAAAHAQAAAAAAAAAMAgAAAgAAAAAOAAAAAgAAAAH+AAD4AAAAAAAA
AAAAAEQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/AAH/wAH/4AHA8AHAcAHAcAH
AcAHAcAHAcAHAcAHAcAHAcAHAcAHAcAHAcAHA8AH/4AH/wAH/AAAAAAAAAAAAABFAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA//AB//AB/+ABwAABwAABwAABwAABwAAB/4AB/8AB/8A
BwAABwAABwAABwAABwAAB/+AB//AB//AAAAAAAAAAAAARgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAf/wAf/wAf/gAcAAAcAAAcAAAcAAAcAAAf+AAf/AAf/AAcAAAcAAAcAAAcAAAcA
AAcAAAcAAAYAAAAAAAAAAAAAAEcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAfAAA
/wAB/4ADg4AHAcAHAIAHAAAHAAAHAAAHH8AHP8AHH8AHAcAHAcAHAcADg4AD/wAB/wAAfAAAAAAA
AAAAAABIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABgDABgDABwHABwHABwHABwHA
BwHABwHAB//AB//AB//ABwHABwHABwHABwHABwHABwHABgDABgDAAAAAAAAbAgAABwEAAAAAAAAA
DAIAAAIAAAAAEAAAAAIAAAAAAABJAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADAA
ADAAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADAAADAAAAAA
AAAAAAAASgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABgAABgAABwAABwAABwAAB
wAABwAABwAABwAABwAABwAABwAABgAYBgAYDgAcHgAP/AAH+AAD4AAAAAAAAAAAAAEsAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAYAMA4AOB4AODwAOHgAOPAAOeAAO8AAP4AAPwAAP
wAAP4AAOeAAOPAAOHgAODwAOB4AMA4AMAYAAAAAAAAAAAABMAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAABgAABgAABwAABwAABwAABwAABwAABwAABwAABwAABwAABwAABwAABwAABwAA
BwAABwAAB//AA//AAAAAAAAAAAAATQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAYA
wAcBwAeDwAeDwAfHwAfHwAfvwAd9wAc5wAc5wAcRwAcBwAcBwAcBwAcBwAcBwAcBwAYAwAYAwAAA
AAAAAAAAAE4AAAAAAAAAGwIAAAcBAAAAAAAAAAwCAAACAAAAABIAAAACAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAwBgA4BgA4BwA8BwA+BwA+BwA/BwA/hwA7hwA5xwA55wA45wA4dwA4dwA4P
wA4HwA4HgAwDgAwBgAAAAAAAAAAAAE8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
fAAA/gAB/wADg4AHAcAHAcAHAcAHAcAHAcAHAcAHAcAHAcAHAcAHAcAHAcADg4AD/wAB/wAAfAAA
AAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB/4AB/8AB/+ABwHABwHA
BwDABwDABwHAB/+AB/8AB/4ABwAABwAABwAABwAABwAABwAABgAABgAAAAAAAAAAAAAAUQAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB+AAH/AAP/gAODgAMBwAcBwAcBwAcBwAcBwAcB
wAcBwAcFwAcPwAcPwAMHwAOHgAP/gAH/wAB8wAAAAAAAAAAAAFIAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAH/gAH/wAH/4AHAcAHAcAHAMAHAMAHAcAH/4AH/4AH/gAHDAAHDgAHBwAH
BwAHA4AHA4AHAcAGAMAAAAAAAAAAAABTAAAAAAAAAAAAAAAAAAAAAAAAABsCAAAHAQAAAAAAAAAM
AgAAAgAAAAAUAAAAAgAAAAAAAAAAAAAAAAAAAAAAfAAB/wAD/4AHA4AHAcAHAMAHAAAHAAAD8AAD
/wAA/4AAA4AAAcAAAcAGAcAHAcAD/4AB/wAA/gAAAAAAAAAAAABUAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAB//AB//AA/+AADgAADgAADgAADgAADgAADgAADgAADgAADgAADgAADgA
ADgAADgAADgAABgAABgAAAAAAAAAAAAAVQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAYAwAYAwAcBwAcBwAcBwAcBwAcBwAcBwAcBwAcBwAcBwAcBwAcBwAcBwAcBwAODgAP/gAH/AAB8
AAAAAAAAAAAAAFYAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGAMAHAcAHAcADAYAD
g4ADg4ABgwABxwABxwAAxgAA7gAA7gAAbAAAfAAAfAAAOAAAOAAAOAAAEAAAAAAAAAAAAABXAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABgDABgDABwHABwHABwHABwHABwHABwHABxHA
BznABznAB33AB+/AB8fAB8fAB4PAB4PABwHABgDAAAAAAAAAAAAAWAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAbAgAABwEAAAAAAAAADAIAAAIAAAAAFgAAAAIAAAAABgDABwHABwHAA4OA
AccAAccAAO4AAHwAAHwAADgAAHwAAHwAAO4AAccAAccAA4OAAwHABwHABgDAAAAAAAAAAAAAWQAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAYAwAcBwAcDgAODgAOHAAHOAADuAAD8AAB8
AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAAwAAAwAAAAAAAAAAAAAFoAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAH/4AH/8AD/8AAA4AABwAADwAADgAAHAAAHAAAOAAAcAAAcAAA4AAB
wAABwAADgAAH/4AH/8AH/8AAAAAAAAAAAABbAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP4AAP4A
AMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAMAAAP4A
AP4AAAAAAAAAAAAAXAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAwAAA4AAAcA
AAcAAAOAAAGAAAHAAADgAADgAABwAAAwAAA4AAAcAAAcAAAOAAAGAAAHAAACAAAAAAAAAAAAAF0A
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAfwAAfwAAAwAAAwAAAwAAAwAAGwIAAAcBAAAAAAAAAAwC
AAACAAAAABgAAAACAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAADAAB/
AAB/AAAAAAAAAAAAAF4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAOAAAOAAA
fAAAbAAA7gAAxgABxwAAggAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABf
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAYAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAgAABgAABwAAAwAAA4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAA/gAB7wABgwAAA4AAA4AAP4AA/4ABw4ADg4ADg4ADh4ADj4AB
/4AAIAAAAAAAAAAAAABiAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAA4AAA4AAA4AA
A4AAA4AAA/4AA/8AAxsCAAAHAQAAAAAAAAAMAgAAAgAAAAAaAAAAAgAAx4ADw4ADg4ADg4ADg4AD
g4ADg4ADg4ADx4AD7wAD/gAAEAAAAAAAAAAAAABjAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAH8AAP+AAcOAAcEAA4AAA4AAA4AAA4AAA4AAAcAAAcEAAPeA
AH8AAAgAAAAAAAAAAAAAZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAA4AAA4
AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAQ4gAY5wAc7gAO/AAH+AAD8AAB4AAAwAAAAAAAAAAAA
AGUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAfgAB7wAB
xwADg4ADg4AD/4AD/4ADgAADgAADgAABwQAB54AA/wAACAAAAAAAAAAAAABmAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAB8AAD+AAHGAAHCAAHAAAHAAA/4AA/4AAHAAAHAAAHAAAHAAAHAA
AHAAAHAAAHAAAHAAAHAAAHAAAHAAAAAAAAAAAAAAZwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAbAgAABwEAAAAAAAAADAIA
AAIAAAAAHAAAAAIAADgAAHwAAP4AAO4AAO4AAP4AAHwAADgAAAAAAAAAAAAAaAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAOAAAOAAAOAAAOAAAOAAAOAAAO+AAP/AAPnAAPDgAODgAODgAOD
gAODgAODgAODgAODgAODgAODgAODgAAAAAAAAAAAAGkAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAOAAAOAAAEAAAAAAAAAAB+AAB+AAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAA
OAAB/wAB/wAAAAAAAAAAAABqAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAA4A
AA4AAAAAAAAAAAAAAP4AAP4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAA4AAAAA
AAAAawAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAAAOAAAOAAAOAAAOAAAOAAAODgAOH
AAOOAAOcAAO4AAPwAAP4AAP8AAOcAAOOAAOPAAOHAAODgAACAAAAAAAAAAAAAGwAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAADgAAHAAAOAAAcAAA/
//g///gcAAAOAAAHGwIAAAcBAAAAAAAAAAwCAAACAAAAAB4AAAACAAAAAAOAAAHAAACAAAAAAAAA
AAAAAG0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD94AD
/4ADvcADOcADOcADOcADOcADOcADOcADOcADOcADOcADOcADOcAAAAAAAAAAAABuAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA74AA/8AA+cAA8MAA4OAA4OA
A4OAA4OAA4OAA4OAA4OAA4OAA4OAA4OAAAAAAAAAAAAAbwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB8AAH/AAHHAAODgAODgAODgAODgAODgAODgAODgAPH
gAHvAAD+AAAQAAAAAAAAAAAAAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAD/gAD7wADw4ADw4ADg4ADgYADgYADg4ADg4ADg4ADx4AD/wAD/gADgAAA
AAAAAABxAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AP+AAe+AA4eAA4OAA4OAA4OAA4OAA4OAA4OAA4eAAceAAf+AAP+AABsCAAAHAQAAAAAAAAAMAgAA
AgAAAAAgAAAAAgAAA4AAAAAAAAByAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAcAAAOAAAHAAADgAABwP//4P//4AABwAADgAAHAAAOAAAcAAAIAAAAA
AAAAAAAAcwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD+
AAHvAAHDAAHAAAHgAAD4AAB+AAAPAAADgAEDgAGDgAPHAAH+AAAQAAAAAAAAAAAAAHQAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAMAAAOAAAeAAA
fAAA/AAA/gAB/gAB/wAD/wAD/4AH/4AH/8AAAAAAAAAAAAB1AAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAADAAAHgAAPwAAf4AA78ABzuABjmABDiAADgAADgAADgAADgAADgAADgAADgA
ADgAADgAADgAADAAAAAAAAAAAAAAdgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAODgAODgAODgAHDAAHHAAHHAADmAADuAABuAAB8AAB8AAA4AAA4AAA4AAAA
AAAAAAAAAHcAAAAbAgAABwEAAAAAAAAADAIAAAIAAAAAIgAAAAIAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcBwAcBwAc5wAM5wAM5gAO9gAP9gAPtgAPvgAHv
gAHvAAHnAAHHAAHHAAAAAAAAAAAAAHgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAADg4ABxwABxgAA7gAA/AAAfAAAOAAAOAAAfAAA7gAA7gABxwADg4ADg4AA
AAAAAAAAAAB5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAA4OAA4OAAcMAAccAAccAAOYAAO4AAG4AAH4AAHwAADwAADwAADgAADgAAAAAAAAAegAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/AAP/AAAHAAAOAAAe
AAAcAAA4AABwAADwAADgAAHAAAPAAAP/gAP/gAAAAAAAAAAAAHsAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAPwAAeAAAcAAAcAAAcAAAcAAAcAAAcAAAYAAA4AADgAAB4AAA4AAAYAAAcAAAcAAAcAAAYAAA
cAAAcAAAeAAAPwAAAwAAAAAAAAAAAAB8AAAAAAAAAAAAAAAAAAAAGwIAAAcBAAAAAAAAAAwCAAAC
AAAAACQAAAACAAAAAAAAAAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAA
OAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAOAAAAAAAAAAAAAB9AAAAAAAAAAAAAAAAAAAAAAAA
AAAAA/AAAHgAADgAADgAADgAADgAADgAABgAABgAABwAAAcAAB4AABwAABgAADgAADgAABgAABgA
ADgAADgAAHgAA/AAAwAAAAAAAAAAAAAAfgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAADzAAH/AAG/AAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAALAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB8AAD+AADnAADHAAD
HAADHAADuAAB+AAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQJgAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD//AD//gDADgDADgDADgDADgDADgDADgDADg
DADgDADgDADgDADgDADgDADgDADgDADgD//gB//AAAAAAAAAAAAAkCEAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAABsCAAAHAQAAAAAAAAAMAgAAAgAAAAAmAAAAAgAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAADAAAGAAAOAAAcAAAYAAA/+AAYAAAMAAAGAAADAAABAAAAAAAAAAAAAAAAAAAAAAAAAAkSEA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAA4AAB8AAD2AAGzAAExgAAwAAAwAAAw
AAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAwAAAAAAAAAAAAAJIhAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGAAADAAABgAABwAD/4AD/4AAAwAABgAA
DAAAGAAAEAAAAAAAAAAAAAAAAAAAAAAAAACTIQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAABgAABgAABgAABgAABgAABgAABgAABgAABgAABgAABgAABgAABgAAZuAAd8AAP4AAHwAADgA
ABAAAAAAAAAAAAAAlAMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAB4AAB8AAD8AADu
AADuAAHHAAHHAAOHgAODgAeDwAcBwA8BwA8B4A4A4B//8B//8B//8AAAAAAAAAAAAAAAAAAAACEr
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAOAAAfAAB7wAbAgAABwEAAAAAAAAADAIAAAIA
AAAAKAAAAAIAAAPHgAeDwAcBwAYAwAYAwAYAwAYAwAYAwAYAwAYAwAcDwAOHgAHvAAD+AAB8AAA4
AAAQAAAAAAAAAAAAAMAlAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAcAAA8AAD8AA
H8AAf8AA/8AB/8AH/8AD/8AB/8AAf8AAP8AAD8AAB8AAA8AAAMAAAEAAAAAAAAAAAAAAAAAAAAC2
JQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAABwAAB4AAB+AAB/AAB/wAB/4AB/+AB//A
B//AB/8AB/4AB/gAB/AAB8AAB4AABgAABAAAAAAAAAAAAAAAAAAAAAAAiCUAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAP/gAP/gAP/gAP/gAP/gAP/gAP/gAP/gAP/gAP/gAP/gAP/gAP/gAP/
gAP/gAP/gAP/gAP/gAP/gAP/gAP/gAAAAAAAALIlAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAEAAAOAAAOAAAfAAAfAAA/gAA/gAB/wAB/wAD/4AH/8AH/8AP/+AP/+Af//AAAAAA
AAAAAAAAAAAAAAAAAAC8JQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH//wD//g
B//AB//AA/+AGwIAAAcBAAAAAAAAAAwCAAACAAAAACoAAAACAAAD/4AB/wAB/wAA/gAA/gAAfAAA
fAAAOAAAOAAAEAAAAAAAAAAAAAAAAAAAAAAAAACgJQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAH//wH//wH//wH//wH//wH//wH//wH//wH//wH//wH//wH//wH//wH//wH//wH//wH//wAAAQ
AAAAAAAAAAAAAAAAAAAAoSUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA//4A//4A4A4A4A4A4A
4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A4A//4A//4A//4AAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAGsBAAAHAQAAAAAAAABcAQAAAgAA
AAAsAABQAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAA
"""

def main():
    global GUI_APP

    GUI_APP = BridgeGui()
    bridge_thread = threading.Thread(target=bridge_main, name="PSX Bridge")
    GUI_APP.set_bridge_thread(bridge_thread)
    GUI_APP.root.after(100, bridge_thread.start)
    GUI_APP.run()


if __name__ == "__main__":
    main()
