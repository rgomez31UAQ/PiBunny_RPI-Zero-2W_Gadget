#!/usr/bin/env python3
"""PiBunny hardware layer for the Waveshare LCD HAT (ST7735S 128x128).

This driver is a faithful port of the Raspyjack display driver
(7h30th3r0n3/Raspyjack, LCD_1in44.py + LCD_Config.py) which is known to
work on this exact HAT. The HAT ships as the 1.44" 128x128 ST7735S panel
(Raspyjack's default "ST7735_128"), not the 1.3" ST7789 240x240 - driving the
240x240 ST7789 dialect into a 128x128 ST7735S produced the persistent
white/garbage screens seen before.

Pins (BCM) for the HAT:
  LCD:   SCLK=11, MOSI=10, CS=8 (spi0.0), DC=25, RST=27, BL=24
  Keys:  KEY1=21, KEY2=20, KEY3=16            (active low, internal pull-up)
  Joystick: UP=6, DOWN=19, LEFT=5, RIGHT=26, PRESS=13  (active low)

Uses python3-spidev for the display and python3-libgpiod for GPIO, with a
polling-based /sys/class/gpio fallback for GPIO.
"""

import os
import sys
import time
import json
import threading
import traceback

# ----------------------------------------------------------------------------
# Configuration (BCM gpio numbers)
# ----------------------------------------------------------------------------
CONFIG = {
    "lcd": {"sclk": 11, "mosi": 10, "cs": 8, "dc": 25, "rst": 27, "bl": 24},
    "panel": "st7735_128",   # default panel type (Raspyjack's 1.44" ST7735S)
    "keys": {"KEY1": 21, "KEY2": 20, "KEY3": 16},
    "joy": {"JU": 6, "JD": 19, "JL": 5, "JR": 26, "JP": 13},
    "width": 128,
    "height": 128,
    "rotate": 0,
    # SPI speed mirrors Raspyjack: 9 MHz on ST7735 (their proven setting),
    # 62 MHz on ST7789. Mode 0, CS managed by the spidev driver.
    "spi_speed": 9000000,
}

# Panel catalog. For ST7735_128 the offsets are the GRAM start adjustments
# (CASET +1, RASET +2) exactly as Raspyjack computes them for scan dir
# U2D_R2L (MADCTL 0x60 | 0x08 BGR = 0x68). ST7789_240 is kept as an
# alternative in case the HAT is ever the 240x240 controller.
PANELS = {
    "st7735_128": {"w": 128, "h": 128, "spi": 9000000,
                   "madctl": 0x68, "inv": False, "cx": 1, "cy": 2},
    "st7789_240": {"w": 240, "h": 240, "spi": 62000000,
                   "madctl": 0x60, "inv": True, "cx": 0, "cy": 0},
}

KEY_EVENTS = {21: "K1", 20: "K2", 16: "K3"}
JOY_EVENTS = {6: "JU", 19: "JD", 5: "JL", 26: "JR", 13: "JP"}

# Display calibration is persisted here so a panel/variant selected through
# the on-screen CAL menu survives reboots (see ui.py "CAL" row).
CAL_PATH = "/usr/local/bunny/ui/cal.json"


def load_cal(path=CAL_PATH):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def save_cal(panel, madctl, inv, offx, offy, colmod=0x05, path=CAL_PATH):
    data = {"panel": str(panel), "madctl": int(madctl), "inv": bool(inv),
            "offx": int(offx), "offy": int(offy),
            "colmod": int(colmod)}
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass
    return data


# ----------------------------------------------------------------------------
# GPIO backend
# ----------------------------------------------------------------------------
class GpioError(Exception):
    pass


def _gpiod22():
    """Return libgpiod >= 2.2 bindings (gpiod, line, line_settings) or None.

    Verified against python3-libgpiod 2.2.x (Debian trixie): the enums live
    under `gpiod.line.*` but `LineSettings` lives at `gpiod.LineSettings`
    and `gpiod.line_settings.LineSettings` (NOT `gpiod.line.LineSettings`).
    API: `Chip(path)`, `chip.request_lines(config={offset: LineSettings(...)},
    consumer=...)`, `LineRequest.get_value/set_value(offset)`.
    """
    try:
        import gpiod
        from gpiod import line
        settings = getattr(gpiod, "LineSettings", None)
        if settings is None:
            import gpiod.line_settings as ls
            settings = ls.LineSettings
        return gpiod, line, settings
    except Exception:
        return None


class Gpio:
    """Poll-based GPIO reader. Tries libgpiod >= 2.2, falls back to sysfs."""

    def __init__(self, lines, pull_up=True):
        # lines: dict name -> bcm number
        self.lines = dict(lines)
        self.names = {v: k for k, v in self.lines.items()}
        self._backend = None
        self._backend_name = None
        if self._try_gpiod(pull_up) is None:
            self._try_sysfs()

    # -- libgpiod (>= 2.2) ----------------------------------------------------
    def _try_gpiod(self, pull_up):
        got = _gpiod22()
        if got is None:
            return None
        gpiod, glin, gset = got
        try:
            chip = gpiod.Chip("/dev/gpiochip0")
            st = gset(direction=glin.Direction.INPUT,
                      bias=glin.Bias.PULL_UP if pull_up else glin.Bias.AS_IS)
            config = {}
            nm = {}
            for name, off in self.lines.items():
                config[off] = st
                nm[name] = off
            req = chip.request_lines(config=config, consumer="pibunny")
        except Exception:
            traceback.print_exc()
            return None
        self._backend = _Gpiod22In(req, nm, glin.Value)
        self._backend_name = "gpiod"
        return self._backend

    # -- sysfs fallback -------------------------------------------------------
    def _try_sysfs(self):
        base = "/sys/class/gpio"
        for num in set(self.lines.values()):
            gp = "%s/gpio%d" % (base, num)
            if not os.path.isdir(gp):
                with open("%s/export" % base, "w") as f:
                    f.write(str(num))
                time.sleep(0.1)
            with open("%s/direction" % gp, "w") as f:
                f.write("in")
        self._backend = _Sysfs(self.lines, base)
        self._backend_name = "sysfs"
        return self._backend

    def read(self, name):
        return self._backend.read(name)

    def read_all(self):
        return self._backend.read_all()


class _Gpiod22In:
    def __init__(self, req, nm, value_enum):
        self._req = req
        self._nm = nm                       # name -> offset
        self._V = value_enum

    def read(self, name):
        try:
            v = self._req.get_value(self._nm[name])
            if v == self._V.ACTIVE:
                return 1
            if v == self._V.INACTIVE:
                return 0
            return None
        except Exception:
            return None

    def read_all(self):
        return [(name, self.read(name)) for name in self._nm]


class _Sysfs:
    def __init__(self, lines, base):
        self._lines = lines
        self._base = base
        self._paths = {}
        for name, num in lines.items():
            gp = "%s/gpio%d/value" % (base, num)
            if os.path.isfile(gp):
                self._paths[name] = gp
        self._unknown = {}

    def read(self, name):
        if name in self._paths:
            try:
                with open(self._paths[name]) as f:
                    return int(f.read().strip())
            except Exception:
                pass
        return self._unknown.get(name)

    def read_all(self):
        return [(name, self.read(name)) for name in self._lines]


# ----------------------------------------------------------------------------
# Display (ST7735S over SPI, Raspyjack-faithful)
# ----------------------------------------------------------------------------
class Display:
    def __init__(self, cfg=CONFIG, fb=None):
        self.cfg = cfg["lcd"]
        self.rotate = int(cfg.get("rotate", 0)) & 3
        self.inv = False
        self.offx = 0
        self.offy = 0
        self.colmod = 0x05
        self.madctl = 0x68
        # panel comes from cal.json first (a saved variant beats CONFIG)
        cal = load_cal()
        panel = str(cfg.get("panel", "st7735_128"))
        if cal and cal.get("panel"):
            panel = str(cal["panel"])
        self._apply_panel(panel)
        # then apply the saved tuning on top of the panel defaults
        if cal:
            if "madctl" in cal:
                self.madctl = int(cal["madctl"]) & 0xFF
            if "inv" in cal:
                self.inv = bool(cal["inv"])
            if "offx" in cal:
                self.offx = int(cal["offx"])
            if "offy" in cal:
                self.offy = int(cal["offy"])
            if "colmod" in cal:
                self.colmod = int(cal["colmod"])
        if fb is not None:
            self.framebuf = fb
        else:
            self.framebuf = bytearray(self.w * self.h * 2)
        self._spi = None
        self._gpio = None
        self._init_spi()
        self._init_gpio()
        self._full_init()

    # -- config ----------------------------------------------------------------
    def _apply_panel(self, name):
        if name not in PANELS:
            name = "st7735_128"
        p = PANELS[name]
        self.panel = name
        self.w = int(p["w"])
        self.h = int(p["h"])
        self.spi_speed = int(p["spi"])
        self._cx = int(p["cx"])          # CASET offset adjustment
        self._cy = int(p["cy"])          # RASET offset adjustment
        self._def_madctl = int(p["madctl"])
        self._def_inv = bool(p["inv"])
        self.madctl = self._def_madctl
        self.inv = self._def_inv
        self.framebuf = bytearray(self.w * self.h * 2)

    # -- spi / gpio --------------------------------------------------------------
    def _init_spi(self):
        try:
            import spidev
        except Exception:
            raise GpioError("python3-spidev not installed")
        spi = spidev.SpiDev()
        spi.open(0, 0)
        spi.max_speed_hz = self.spi_speed
        spi.mode = 0
        self._spi = spi

    def _init_gpio(self):
        pins = ["dc", "rst", "bl"]
        names = {p: self.cfg[p] for p in pins}
        bak = None
        got = _gpiod22()
        if got is not None:
            gpiod, glin, gset = got
            try:
                chip = gpiod.Chip("/dev/gpiochip0")
                config = {}
                for p, off in names.items():
                    config[off] = gset(direction=glin.Direction.OUTPUT)
                req = chip.request_lines(config=config, consumer="pibunny")
                bak = _Gpiod22Out(req, names, glin.Value)
            except Exception as e:
                sys.stderr.write("pibunny gpio (out): %r\n" % (e,))
                bak = None
        if bak is None:
            try:
                bak = _OutSysfs(names)
            except Exception:
                raise GpioError("no usable GPIO backend for the LCD")
        self._gpio = bak

    # -- low level ----------------------------------------------------------------
    def _write(self, buf):
        # Half-duplex writebytes, chunked at 4096 bytes exactly like
        # Raspyjack. The spidev driver keeps CS asserted across each
        # writebytes call; per-chunk CS toggling is what Raspyjack ships
        # with and it works on this HAT.
        try:
            if isinstance(buf, (bytes, bytearray)) or buf is bytes:
                self._spi.writebytes(bytes(buf))
            else:
                self._spi.writebytes(list(buf))
        except (TypeError, OverflowError):
            self._spi.writebytes(list(buf))

    def _cmd(self, byte):
        self._gpio.out("dc", 0)
        self._write([byte])

    def _data(self, buf):
        self._gpio.out("dc", 1)
        self._write(buf)

    def _write_chunks(self, buf):
        self._gpio.out("dc", 1)
        n = len(buf)
        i = 0
        while i < n:
            self._write(buf[i:i + 4096])
            i += 4096

    # -- reset / init (Raspyjack order & timings) -----------------------------------
    def _reset(self):
        self._gpio.out("rst", 1)
        time.sleep(0.10)
        self._gpio.out("rst", 0)
        time.sleep(0.10)
        self._gpio.out("rst", 1)
        time.sleep(0.02)

    def _init_regs(self):
        if self.panel == "st7789_240":
            self._init_regs_st7789()
        else:
            self._init_regs_st7735()

    def _init_regs_st7735(self):
        s = self
        s._cmd(0xB1); s._data([0x01, 0x2C, 0x2D])
        s._cmd(0xB2); s._data([0x01, 0x2C, 0x2D])
        s._cmd(0xB3); s._data([0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D])
        s._cmd(0xB4); s._data([0x07])
        s._cmd(0xC0); s._data([0xA2, 0x02, 0x84])
        s._cmd(0xC1); s._data([0xC5])
        s._cmd(0xC2); s._data([0x0A, 0x00])
        s._cmd(0xC3); s._data([0x8A, 0x2A])
        s._cmd(0xC4); s._data([0x8A, 0xEE])
        s._cmd(0xC5); s._data([0x0E])
        s._cmd(0xE0); s._data([0x0F, 0x1A, 0x0F, 0x18, 0x2F, 0x28, 0x20,
                               0x22, 0x1F, 0x1B, 0x23, 0x37, 0x00, 0x07,
                               0x02, 0x10])
        s._cmd(0xE1); s._data([0x0F, 0x1B, 0x0F, 0x17, 0x33, 0x2C, 0x29,
                               0x2E, 0x30, 0x30, 0x39, 0x3F, 0x00, 0x07,
                               0x03, 0x10])
        s._cmd(0xF0); s._data([0x01])
        s._cmd(0xF6); s._data([0x00])
        s._cmd(0x3A); s._data([0x05])              # 65k RGB565 mode

    def _init_regs_st7789(self):
        s = self
        s._cmd(0x36); s._data([0x00])
        s._cmd(0x3A); s._data([0x05])              # 16bpp
        s._cmd(0xB2); s._data([0x0C, 0x0C, 0x00, 0x33, 0x33])
        s._cmd(0xB7); s._data([0x35])
        s._cmd(0xBB); s._data([0x2B])
        s._cmd(0xC0); s._data([0x2C])
        s._cmd(0xC2); s._data([0x01])
        s._cmd(0xC3); s._data([0x15])
        s._cmd(0xC4); s._data([0x20])
        s._cmd(0xC6); s._data([0x01])
        s._cmd(0xD0); s._data([0xA4, 0xA1])
        s._cmd(0xE0); s._data([0xD0, 0x04, 0x0D, 0x11, 0x13, 0x2B, 0x3F,
                               0x54, 0x4C, 0x18, 0x0D, 0x0B, 0x1F, 0x23])
        s._cmd(0xE1); s._data([0xD0, 0x04, 0x0C, 0x11, 0x13, 0x2C, 0x3F,
                               0x44, 0x51, 0x2F, 0x1F, 0x1F, 0x20, 0x23])
        s._cmd(0x21)                                # INVON (ST7789 needs this)

    def _full_init(self):
        # backlight on first, then reset, then register init, then scan way,
        # then SLPOUT/DISPON - mirroring Raspyjack's LCD_Init exactly.
        try:
            self.backlight(1)
        except Exception:
            pass
        self._reset()
        self._init_regs()
        self._cmd(0x36)
        self._data([self._def_madctl])
        time.sleep(0.20)
        self._cmd(0x11)                             # SLPOUT
        time.sleep(0.12)
        self._cmd(0x29)                             # DISPON

    def apply_variant(self, panel=None, madctl=None, inv=None,
                      offx=None, offy=None, colmod=None):
        """Switch panel/calibration live (CAL screen). Panel change = full
        re-init + framebuffer resize."""
        changed_panel = panel is not None and str(panel) != self.panel
        if panel is not None:
            self._apply_panel(str(panel))
        if madctl is not None:
            self.madctl = int(madctl) & 0xFF
        if inv is not None:
            self.inv = bool(inv)
        if offx is not None:
            self.offx = int(offx)
        if offy is not None:
            self.offy = int(offy)
        if colmod is not None:
            self.colmod = int(colmod)
        if changed_panel:
            self._init_spi()
            self._full_init()
            return
        self._cmd(0x36)
        self._data([self.madctl])
        self._cmd(0x3A)
        self._data([self.colmod])
        self._cmd(0x21 if self.inv else 0x20)
        time.sleep(0.01)
        self._cmd(0x11)
        time.sleep(0.02)
        self._cmd(0x29)

    def backlight(self, on):
        try:
            self._gpio.out("bl", on)
        except Exception:
            pass

    def _expand18(self):
        """RGB565 framebuffer -> RGB666/18bpp byte stream (3 bytes/px)."""
        fb = self.framebuf
        n = self.w * self.h
        out = bytearray(n * 3)
        j = 0
        for i in range(0, n * 2, 2):
            v = (fb[i] << 8) | fb[i + 1]
            r = (v >> 11) & 0x1F
            g = (v >> 5) & 0x3F
            b = v & 0x1F
            out[j] = (r << 3) | (r >> 2)
            out[j + 1] = (g << 2) | (g >> 4)
            out[j + 2] = (b << 3) | (b >> 2)
            j += 3
        return out

    def _set_window(self, x0, y0, x1, y1):
        ox, oy = self.offx, self.offy
        caset_x0 = x0 + self._cx + ox
        caset_x1 = x1 + self._cx + ox
        raset_y0 = y0 + self._cy + oy
        raset_y1 = y1 + self._cy + oy
        self._cmd(0x2A)
        self._data([(caset_x0 >> 8) & 0xFF, caset_x0 & 0xFF,
                    (caset_x1 >> 8) & 0xFF, caset_x1 & 0xFF])
        self._cmd(0x2B)
        self._data([(raset_y0 >> 8) & 0xFF, raset_y0 & 0xFF,
                    (raset_y1 >> 8) & 0xFF, raset_y1 & 0xFF])
        self._cmd(0x2C)

    def show(self):
        # Raspyjack pushes every whole-frame render this way: set window,
        # then stream RGB565 bytes. No DISPOFF dance (the HAT panel stays
        # live; on a 128x128 ST7735S a full frame at 9 MHz is ~29 ms so
        # tearing is negligible).
        self._set_window(0, 0, self.w - 1, self.h - 1)
        if self.colmod == 0x06:
            self._write_chunks(self._expand18())
        else:
            self._write_chunks(self.framebuf)


class _Gpiod22Out:
    def __init__(self, req, names, value_enum):
        self._r = req
        self._n = names
        self._V = value_enum

    def out(self, name, val):
        self._r.set_value(self._n[name],
                          self._V.ACTIVE if val else self._V.INACTIVE)


class _OutSysfs:
    def __init__(self, names):
        self._paths = {}
        base = "/sys/class/gpio"
        for name, num in names.items():
            gp = "%s/gpio%d" % (base, num)
            if not os.path.isdir(gp):
                with open("%s/export" % base, "w") as f:
                    f.write(str(num))
                    time.sleep(0.05)
            with open("%s/direction" % gp, "w") as f:
                f.write("out")
            self._paths[name] = "%s/value" % gp

    def out(self, name, val):
        with open(self._paths[name], "w") as f:
            f.write("1" if val else "0")


# ----------------------------------------------------------------------------
# Input (polling, debounced)
# ----------------------------------------------------------------------------
class Input:
    def __init__(self, cfg=CONFIG, output=None, on_event=None):
        self.keys = cfg["keys"]
        self.joy = cfg["joy"]
        all_lines = {}
        all_lines.update(self.keys)
        all_lines.update(self.joy)
        self._gpio = Gpio(all_lines, pull_up=True)
        self._output = output            # callable name->event string
        self._on_event = on_event        # callback(event_string)
        self._last = {}
        self._stable_since = {}
        self._debounce = 0.045
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _emit(self, event):
        if self._on_event:
            try:
                self._on_event(event)
            except Exception:
                pass
        if self._output:
            try:
                self._output(event)
            except Exception:
                pass

    def _loop(self):
        for name in list(self.keys) + list(self.joy):
            self._last[name] = 1
            self._stable_since[name] = time.time()
        while not self._stop.is_set():
            try:
                pairs = self._gpio.read_all()
            except Exception:
                pairs = []
            now = time.time()
            for name, val in pairs:
                if val is None:
                    continue
                # active-low: pressed == 0
                pressed = (val == 0)
                if pressed != (self._last[name] == 0):
                    self._stable_since[name] = now
                    self._last[name] = val
                else:
                    if pressed and (now - self._stable_since[name]) > self._debounce:
                        if name in self.keys:
                            self._emit(KEY_EVENTS[self.keys[name]])
                            self._stable_since[name] = now + 0.25  # rate limit
                        elif name in self.joy:
                            self._emit(JOY_EVENTS[self.joy[name]])
                            self._stable_since[name] = now + 0.25
            time.sleep(0.01)

    def stop(self):
        self._stop.set()


def rgb255(r, g, b):
    """RGB565 big-endian bytes (same order Raspyjack pushes)."""
    v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return (v >> 8) & 0xFF, v & 0xFF