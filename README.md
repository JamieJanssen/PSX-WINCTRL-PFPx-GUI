# PSX WINCTRL PFPx Bridge

PSX WINCTRL PFPx Bridge connects an **Aerowinx PSX** CDU to a **WINCTRL / Winwing PFP CDU**.

The bridge sends key presses from the hardware CDU to PSX and shows the selected PSX CDU display, annunciators and brightness on the connected hardware unit.

The Aerowinx MCDU font is included in the application.  
Font by **Martin and Hardy**.

## Requirements

- Aerowinx PSX running with its TCP server enabled
- A supported WINCTRL / Winwing PFP CDU connected by USB
- Python 3.13 or newer when running the `.py` file, or the packaged application

## Installation and files

Keep the bridge application and its configuration file in the same folder:

```text
psx_winctrl_pfp.exe
psx_winctrl_pfp.py
psx_winctrl_pfp.ini
```

On macOS, unzip the application and place it in the Applications folder if preferred.

## Starting the bridge

Start PSX first, then start the bridge.

Python:

```bash
python psx_winctrl_pfp.py
```

Windows packaged application:

```text
psx_winctrl_pfp.exe
```

After startup, the connected CDU briefly shows the bridge welcome screen before the selected PSX CDU display is shown.

## Main window

The main window shows the connection and activity log, the active CDU selection and the application controls.

### CDU selection

Use the **CDU L**, **CDU C** and **CDU R** buttons to select the PSX Left, Center or Right CDU. The selected CDU is highlighted and its display is sent to the connected hardware CDU.

The selected CDU is remembered after a normal shutdown.

### Mini mode

Select **Mini** to reduce the application to a small always-on-top control window. This keeps the CDU L, CDU C and CDU R buttons available while taking very little screen space.

To return to the normal window, double-click an unused brown area of the Mini window. The free brown area can also be used to drag the Mini window.

### Menu

Use the **…** menu in the main window for:

- **Copy log** — copies the complete current log to the clipboard.
- **Debug logging** — enables or disables additional diagnostic messages.
- **About** — shows application and font information. While About is open, the hardware CDU shows the bridge welcome screen. Closing About restores the current PSX CDU display.

### Closing the bridge

Use the **Quit** button for a normal shutdown. This saves the selected CDU and brightness setting.

When running the Python version from a terminal, `CTRL+C` also requests a clean shutdown.

## Configuration

Edit `psx_winctrl_pfp.ini` to select the PSX connection and the connected CDU.

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
```

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

`ACTIVE_CDU` and `BRIGHTNESS` are normally saved when the bridge is closed with the Quit button. `ATC_KEY` is saved immediately when changed through a scratchpad command.

## Supported devices

The WINCTRL Vendor ID is fixed at `4098`. Set `pid` and `did` in `psx_winctrl_pfp.ini` for the connected CDU.

| Device | Position | `pid` | `did` |
| --- | --- | ---: | ---: |
| PFP7 | Captain | `BB37` | `33BB` |
| PFP7 | Observer | `BB3B` | `33BB` |
| PFP7 | First Officer | `BB3F` | `33BB` |
| PFP4 | Captain | `BB38` | `34BB` |
| PFP4 | Observer | `BB3C` | `34BB` |
| PFP4 | First Officer | `BB40` | `34BB` |
| PFP3N | Captain | `BB35` | `31BB` |
| PFP3N | Observer | `BB39` | `31BB` |
| PFP3N | First Officer | `BB3D` | `31BB` |
| MCDU | Captain | `BB36` | `32BB` |
| MCDU | Observer | `BB3A` | `32BB` |
| MCDU | First Officer | `BB3E` | `32BB` |

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

## Brightness

Use the hardware CDU **BRT+** and **BRT-** keys to adjust the screen and key backlight brightness.

A temporary brightness indicator is shown on the CDU scratchpad line while changing the setting. Brightness levels range from `0` to `23` and the last selected level is remembered after a normal shutdown.

## USB disconnect

If the CDU is disconnected while the bridge is running, the bridge stops safely and shows:

```text
[HID] CDU disconnected
[END] Please restart
```

Reconnect the CDU and restart the bridge. The bridge does not automatically reconnect to a CDU that has been unplugged.

## Notes

- Use the Quit button whenever possible so the selected CDU and brightness are saved.
- The bridge can be used with the full window or the compact always-on-top Mini window.
