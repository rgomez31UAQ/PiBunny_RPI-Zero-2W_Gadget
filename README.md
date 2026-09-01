# PiBunny (pibash) — Bash Bunny firmware ported to a Raspberry Pi Zero 2 W

> 🐰 A working, fully-open port of the **Hak5 Bash Bunny MK2 firmware (v1.7)**
> onto a **Raspberry Pi Zero 2 W** + **Waveshare 1.3" LCD HAT** (ST7789 240x240,
> joystick + 3 keys) that acts as both the status "LED" and the control UI.
> Same Duckyscript engine, same `ATTACKMODE` modes, same VID/PID table — now
> with a nicer retro neon **on-device menu** and a write-your-own payload UI.

## Features

* **Full Duckyscript 1.0 → 3.0 engine** (`QUACK`) — `DEFINE`, `VAR`/`CONST`,
  `IF/ELIF/ELSE`, `WHILE`, `FUNCTION`, `STRINGLN`, `EXTENSION`,
  `WAIT_FOR_BUTTON_PRESS`, `STOP_PAYLOAD`, `HIDE_PAYLOAD`, `$_RANDOM_*`,
  keyboard-lock-state helper functions, and more.
* **USB gadget re-implemented in configfs** (`arm.py`) on the Pi's DWC2
  controller — HID keyboard, Mass Storage, RNDIS/ECM ethernet, CDC-ACM serial.
* **Seen as a plain USB keyboard on Linux / Windows / macOS** — standard
  boot-protocol HID report descriptor (63 bytes), no drivers needed.
* **Retro neon LCD UI** with a scrollable payload library: joystick to
  navigate, **K3 = OK** (run), **K2 = BACK** — pick from 16 payloads without
  re-flashing.
* **On-device screenshot-friendly status "LED"** (the LCD renders `LED` states).
* 16 ready payloads: net recon, HID pwn, sys/cred/browser recon, wifi grab,
  keylogger, hunter, ducky_runner, plus Hak5 ports — and a **`hid_test`**
  payload that proves keyboard enumeration on any OS in one go.

## Hardware

* Raspberry Pi **Zero 2 W** (also works on Zero W — anything with DWC2).
* **Waveshare 1.3" LCD HAT** (ST7735 128x128 / ST7789 240x240).

## Table of contents
1. [Build](#build)
2. [Wiring](#wiring-waveshare-13-lcd-hat-on-the-zero-2-w-40-pin-header)
3. [Usage](#usage)
4. [Writing payloads](#writing-payloads)
5. [Development layout](#development-layout)

## What was reused vs rewritten

Reused **1:1** from `upgrade/cherry.rootfs.tar`:
* Duckyscript engine language files (`usr/local/bunny/lib/languages/*.json`)
* Payload extensions (`QUACK` helpers: `GET`, `RUN`, `SETKB`, `DUCKY_LANG`,
  `WAIT*`, `DEBUG`, `requiretool`, ...)
* Built-in payloads, `config.txt`, version, docs, Windows CDC-ACM inf
* The `ATTACKMODE` mode/PID semantics (same VID 0xF000 / PID table)

Rewritten for the Pi (DWC2 + configfs instead of the sunxi `bunny_gadget.ko`):
| Stock bunny                | PiBunny                                        |
|----------------------------|------------------------------------------------|
| `bunny_gadget.ko` (insmod) | `lib/gadget/arm.py` (configfs composite gadget)|
| 3-position slide switch    | Waveshare keys: KEY1/KEY2/KEY3 = switch1/2/3   |
| RGB status LED             | LCD renders the `LED` command states           |
| —                          | Joystick payload-select menu (new)             |
| `/dev/nandf` udisk         | loop image `/opt/bunny/udisk.img`              |
| py2 QUACK / hid_reader     | Python 3 ports                                 |

## Build

1. Flash **Raspberry Pi OS Lite (32-bit / armhf)** onto an SD card.
   (The UI drives the LCD directly from the kernel's SPI interface — no
   desktop needed and boot is fast.)
2. Copy this `rpi-bunny/` folder onto the Pi (e.g. via the `boot` partition
   or `scp`), then run as `root`:
   ```bash
   sudo ./install.sh
   sudo reboot
   ```
   `install.sh` installs `python3-spidev`, `python3-libgpiod`,
   `dnsmasq`, installs `/usr/local/bunny`, builds the udisk image,
   adds the systemd services and edits `/boot*/config.txt` + `cmdline.txt`
   (dwc2 peripheral gadget, SPI on, key/joystick pull-ups).

## Wiring (Waveshare 1.3" LCD HAT on the Zero 2 W 40-pin header)

Nothing to wire by hand — the HAT stacks directly. Pin map used by the port:

| Function        | BCM GPIO |
|-----------------|----------|
| LCD SPI (CLK/MOSI/CS) | 11 / 10 / 8 |
| LCD DC / RST / BL | 25 / 27 / 24 |
| KEY1 / KEY2 / KEY3 | 21 / 20 / 16 |
| Joystick UP/DOWN/LEFT/RIGHT/PRESS | 6 / 19 / 5 / 26 / 13 |

All keys/joystick are active-low with internal pull-ups.

*Display driver:* the port uses `spidev` + `libgpiod` from userspace, so no
kernel `fbtft`/DRM driver is required and it keeps full control of the panel
(it also lets us GPU-free render a status "LED" + menus).

## Usage

On boot the screen shows the **payload selector** (retro neon list). The
selection is a single scrollable list driven from `.menucfg` — every payload
is a row, plus `ARM` (arming mode), `LOOT`, `SYS`, `CAL` and `BACK` tools:

* **Joystick UP / DOWN** — move the highlight
* **K3 (OK)** or **joystick press** — run / open the highlighted row
* **K2 (BACK)** — back out of a tool or running payload to the selector

Each row shows the actual payload name pulled from `.menucfg`, so what you
highlight is what runs — no more guessing which switch fires which attack.

Payloads live on the udisk (mounted at `/root/udisk`), exactly like stock:
```
payloads/switch1/payload.txt   (your own ducky scripts per switch)
payloads/switch2/payload.txt
payloads/switch3/payload.txt   (arming/setup fallback)
payloads/extensions/*.sh       (QUACK helper functions)
payloads/library/*.txt         (selectable from the on-screen library menu)
config.txt                      (e.g. DUCKY_LANG us)
loot/                           (exfil directory)
tools/                          (optional .deb / scripts run at boot in arming)
```

Payload files are mapped to the on-screen menu with `.menucfg`:
```
switch1 library/net_recon.txt
switch2 library/hid_pwn.txt
switch3 library/sys_recon.txt
...
switch16 library/hid_test.txt
```

# Writing payloads

Inside a payload it's stock-bunny syntax (a bash file that calls `ATTACKMODE`
and `QUACK`):
```bash
LED SETUP        # screen goes magenta
ATTACKMODE HID STORAGE RNDIS_ETHERNET
QUACK GUI r
QUACK DELAY 300
QUACK STRING notepad
QUACK ENTER
QUACK DELAY 1000
QUACK STRING hello from pibunny
LED FINISH
```
Supported `ATTACKMODE` modes and 1:1 same VIDs/PIDs:
`HID STORAGE SERIAL RNDIS_ETHERNET ECM_ETHERNET AUTO_ETHERNET ARMING`,
plus `VID_0x… PID_0x… SN_ MAN_ RNDIS_SPEED_ ETHERNET_TIMEOUT_` overrides.
RNDIS/ECM get IP `192.168.1.1/24` on `usb0` and a DHCP server hands the
target `192.168.1.x`, so the usual `GET TARGET_IP` etc. work over
`sock.sh`/port forwarding like on stock.

## Development layout

```
rpi-bunny/
├── install.sh                # Pi-side installer
├── build_udisk.sh            # creates /opt/bunny/udisk.img (empty drive)
├── udisk/                    # udisk skeleton: payloads, extensions, config...
├── src/usr/local/bunny/
│   ├── bin/                  # ATTACKMODE, LED, QUACK, bunny_framework, udisk...
│   ├── lib/gadget/arm.py     # configfs composite gadget (replaces the .ko)
│   ├── lib/languages/        # keyboard layouts (from stock firmware)
│   ├── ui/                   # hardware.py, font.py, ui.py (LCD + input daemon)
│   └── hid_reader.py         # HID LED/numlock report reader (py3)
└── src/etc/                  # systemd units, dnsmasq/usb0 config
```

## Field bring-up checklist (first boot on the Pi)

Do this once, in order, while the Pi is **not** plugged into a target yet:

1. **Boot + screen**: after `reboot`, the panel should show a splash then
   **SELECT SWITCH** (magenta). No screen? `systemctl status bunny-ui` —
   the common cause is `python3-spidev` (install it) or `dtoverlay=dwc2`
   taking SPI (remove it); `ls /dev/spidev0.0` must succeed.
2. **Keys / joystick** on the picker: K1/K2/K3 should select that switch and
   start running it (screen goes to RUN with a color), joystick press opens
   the payload menu; `sel`ing and committing runs the payload. Check
   `cat /run/bunny/switch` and `/run/bunny/led.state`.
3. **Gadget sanity** before touching a host:
   `dmesg | grep -i -E 'dwc2|gadget'` and
   `ls /sys/kernel/config/usb_gadget/pibunny` should exist after any
   `ATTACKMODE` call. Run `ATTACKMODE ARMING` — the Pi should present itself
   as mass storage + serial (used for `udisk`/`factory_reset_bunny`).
4. **HID**: plug into a test machine. It must enumerate as keyboard
   (VID_0xf000/PID_0xff01 after `ATTACKMODE HID`). Type-capture: `QUACK
   STRING hello` should type into notepad/editor. If nothing types, check
   `report_length` = 8 and that `/dev/hidg0` exists.
5. **Altcode path** (`ALTCODE` / numlock detection): run a payload using
   `ALTCODE` — the num-lock toggle only works while `hid_reader` is running
   (`systemctl status bunny` shows the framework; the bytes land in
   `/tmp/hid_out`).
6. **Ethernet**: `ATTACKMODE HID STORAGE RNDIS_ETHERNET`, plug into Windows
   (install `win7-win8-cdc-acm.inf` from the udisk as needed) or macOS/Linux
   (ECM + `ECM_ETHERNET`). The Pi should get `192.168.1.1` on `usb0` and the
   target a lease in `192.168.1.x`; `GET TARGET_IP` then works.

## Notes & limitations

* `QUACK` writes to `/dev/hidg0` (f_hid). The HID report descriptor includes
  the keyboard LED output report so `ALTCODE`/numlock detection still works.
* The framework runs one attack per boot (like stock). Reboot (or
  `systemctl start bunny.service`) to pick a different switch; the joystick
  menu persists payload assignments in `/root/udisk/.menucfg`.
* The udisk image is both mounted at `/root/udisk` and exposed as Mass
  Storage — same dual-use as the original `/dev/nandf` mount. Keep writes to
  the disk on one side at a time for clean sync.
* `init_bt` (Bluetooth serial module) is stubbed out — this is not the
  wireless bunny.
* For lab testing that's authorized: this is a USB attack device. Use it only
  on systems you own or are explicitly permitted to test.