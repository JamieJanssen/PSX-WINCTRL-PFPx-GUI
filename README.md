# PSX WINCTRL PFPx Bridge

PSX WINCTRL PFPx Bridge connects an **Aerowinx PSX** CDU to a supported **WINCTRL / Winwing PFP CDU**.

The bridge sends hardware key presses to PSX and shows the selected PSX CDU display, annunciators and brightness on the connected hardware unit.

The Aerowinx MCDU font is embedded in the application.  
Font by **Martin and Hardy**.

## Requirements

- Aerowinx PSX running with its TCP server enabled
- A supported WINCTRL / Winwing CDU
- Python 3.13 or newer when running the `.py` files, or the packaged application

The CDU does **not** have to be connected before the bridge is started. The bridge can wait for the device and connect automatically when it becomes available.

## Installation and files

The Python source is split into two files:

```text
psx_winctrl_pfp.py
psx_winctrl_pfp_core.py
```

`psx_winctrl_pfp.py` is the **only application entry point** and is the file that should be started by the user. It contains the GUI integration and starts the bridge core.

`psx_winctrl_pfp_core.py` contains the bridge, PSX, HID, display and embedded-font implementation. It is imported by `psx_winctrl_pfp.py` and is **not intended to be started directly**.

Both Python files must therefore be kept together in the same directory when running from source.

On Windows, the packaged application normally uses an INI file next to the executable:

```text
psx_winctrl_pfp.exe
psx_winctrl_pfp.ini
```

On macOS, the configuration file is stored at:

```text
~/Library/Application Support/PSX WINCTRL PFPx/psx_winctrl_pfp.ini
```

If that macOS configuration does not exist yet and a legacy INI is found next to the application, it is copied to the new location once.

## Starting the bridge

Start PSX and then start the bridge.

Python:

```bash
python psx_winctrl_pfp.py
```

Do not start `psx_winctrl_pfp_core.py` directly.

Windows packaged application:

```text
psx_winctrl_pfp.exe
```

If the configured CDU is already connected, the bridge opens it automatically. The CDU briefly shows the bridge welcome screen before the selected PSX CDU display is shown.

If no supported CDU is available yet, the application remains open and waits for one to appear. USB detection is retried automatically about every 3 seconds.

Typical startup messages are:

```text
[HID] Waiting for WINCTRL CDU...
[HID] Configured device: PFP7 Captain (PID=BB37)
```

When a supported CDU becomes available, the bridge connects automatically and continues normal operation without requiring an application restart.

## USB autodetect and reconnect

The bridge includes automatic WINCTRL CDU detection and reconnect support.

- The application remains running if the CDU is not connected at startup.
- USB detection is retried automatically about every 3 seconds.
- If the configured PID is available, that device is used.
- If the configured PID is not present but another supported WINCTRL CDU is connected, the first supported device in the built-in device order can be selected automatically.
- When an automatically selected device differs from the saved device, its PID and DID are stored in the INI file.
- If the USB cable is disconnected while the bridge is running, the GUI remains open.
- When the CDU is connected again, the bridge reconnects automatically.
- Detailed retry messages are only shown when Debug logging is enabled.

## Main window

The main window shows the connection and activity log, the active CDU selection and the application controls.

### CDU selection

Use the **CDU L**, **CDU C** and **CDU R** buttons to select the PSX Left, Center or Right CDU. The selected CDU is highlighted in green and its display is sent to the connected hardware CDU.

The selected CDU is remembered after a normal shutdown.

### Mini mode

Select **Mini** to reduce the application to a small always-on-top control window. This keeps the CDU L, CDU C and CDU R buttons available while taking very little screen space.

The active CDU selector remains highlighted in green.

Mini controls:

- Left-click **L**, **C** or **R** to select the active CDU.
- Drag an unused brown area to move the Mini window.
- Double-click an unused brown area to return to Full mode.
- Secondary/right-click anywhere in the Mini window to return to Full mode.
- On macOS, Ctrl-click also returns to Full mode.

The Mini window position is saved. When restored, its position is validated and clamped so the window cannot remain unreachable outside the visible screen area.

### Menu

Use the **…** menu in the main window for:

- **Copy log** — copies the complete current log to the clipboard.
- **Debug logging** — enables or disables additional diagnostic messages.
- **Settings** — selects one of the supported WINCTRL CDU models and positions. Connected devices are marked **Connected** in green. Changing the selection saves the corresponding PID/DID and reconnects the bridge.
- **About** — shows application and font information. While About is open, the hardware CDU shows the bridge welcome screen. Closing About restores the cached PSX CDU display.

### Minimize

The `_` button minimizes the application.

On Windows this uses the normal window minimize behaviour. On macOS the bridge temporarily restores the native window decoration state as needed so minimizing and restoring also works with the borderless GUI.

### Closing the bridge

Use the **X** button for a normal shutdown. This saves the selected CDU and brightness setting and closes the bridge cleanly.

When running the Python version from a terminal, `CTRL+C` also requests a clean shutdown.

## Configuration

The bridge reads its PSX connection and CDU settings from `psx_winctrl_pfp.ini`.

Example:

```ini
[PSX]
host = 127.0.0.1
port = 10747

[FMC]
pid = BB37
did = 33BB
ATC_KEY = ALTN
ACTIVE_CDU = L
BRIGHTNESS = 16

[GUI]
FULL_X = 100
FULL_Y = 100
MINI_X = 100
MINI_Y = 100
```

The application updates individual INI values without rewriting the complete file, so comments and the existing layout are preserved.

### PSX settings

| Setting | Description |
| --- | --- |
| `host` | PSX TCP host address |
| `port` | PSX TCP port |

### FMC settings

| Setting | Description |
| --- | --- |
| `pid` | USB product ID of the selected CDU |
| `did` | WINCTRL destination ID |
| `ATC_KEY` | `ATC` for the original ATC key, or `ALTN` to open the ALTN page |
| `ACTIVE_CDU` | Selected CDU: `L`, `C` or `R` |
| `BRIGHTNESS` | Screen and key brightness level: `0` to `23` |

`ACTIVE_CDU` and `BRIGHTNESS` are normally saved during a clean shutdown. `ATC_KEY` is saved immediately when changed through a scratchpad command.

PID/DID can still be edited manually, but normal device selection can now be handled through **Settings** and USB autodetect.

## Supported devices

The WINCTRL Vendor ID is fixed at `4098` (`0x4098`).

| Device | Position | `pid` | `did` |
| --- | --- | ---: | ---: |
| PFP3N | Captain | `BB35` | `31BB` |
| PFP3N | Observer | `BB39` | `31BB` |
| PFP3N | First Officer | `BB3D` | `31BB` |
| MCDU | Captain | `BB36` | `32BB` |
| MCDU | Observer | `BB3A` | `32BB` |
| MCDU | First Officer | `BB3E` | `32BB` |
| PFP7 | Captain | `BB37` | `33BB` |
| PFP7 | Observer | `BB3B` | `33BB` |
| PFP7 | First Officer | `BB3F` | `33BB` |
| PFP4 | Captain | `BB38` | `34BB` |
| PFP4 | Observer | `BB3C` | `34BB` |
| PFP4 | First Officer | `BB40` | `34BB` |

For example, a PFP7 Captain CDU uses:

```ini
[FMC]
pid = BB37
did = 33BB
```

## Scratchpad commands

Enter these commands in the PSX CDU scratchpad:

| Command | Action |
| --- | --- |
| `CDU-L` | Select the Left CDU |
| `CDU-C` | Select the Center CDU |
| `CDU-R` | Select the Right CDU |
| `CDU-ATC` | Use the original ATC key behaviour |
| `CDU-ALTN` | Use the ATC key to open the ALTN page |

After a recognised command is handled, the scratchpad command is cleared automatically.

## Brightness

Use the hardware CDU **BRT+** and **BRT-** keys to adjust the screen and key backlight brightness.

Brightness levels range from `0` to `23`.

A temporary brightness indicator is shown on the CDU scratchpad line while changing the setting. The last selected brightness is remembered after a normal shutdown.

The bridge also periodically refreshes the hardware brightness state so long-running sessions keep the expected screen and key brightness.

## PSX connection behaviour

The bridge uses the PSX TCP interface and follows PSX CDU data as push/event-based updates. The active CDU display, LCD colours, annunciators and blanking state are kept in sync with PSX.

The selected PSX CDU determines which CDU display, annunciators and key channel are used.

## Notes

- Start the Python version with `psx_winctrl_pfp.py`; `psx_winctrl_pfp_core.py` is an internal module and has no standalone entry point.
- The bridge can be started before the WINCTRL CDU is connected.
- USB unplug/replug does not require restarting the GUI.
- Use **Settings** to select a specific supported CDU when multiple devices are available.
- Use the normal **X** button whenever possible so runtime settings are saved cleanly.
- The bridge can be used with the full window or the compact always-on-top Mini window.
