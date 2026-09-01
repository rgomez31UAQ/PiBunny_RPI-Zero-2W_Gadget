#!/usr/bin/env python3
"""PiBunny UI daemon - NEON edition.

Renders the Waveshare LCD HAT (ST7735S 128x128, alternatively ST7789 240x240)
as a retro CRT terminal in phosphor-green-on-black:

  * splash      -- animated boot sequence with moving scan bar
  * switch      -- pick switch1/2/3 (K1/K2/K3) or open the payload menu (JOY)
  * menu        -- select/edit payload per switch, browser + sysinfo short-cuts
  * run         -- live payload terminal (tails /run/bunny/out.log)
  * output      -- scrollable payload-output viewer
  * loot        -- browse the loot folder, view captured files
  * sys         -- system info (host, kernel, uptime, ip)
  * cal         -- display calibration sweep (kept for the CAL row)

All layout is authored on a 128x128 base and scaled to the real panel with
S(). Publishing input events mirrors the framework contract (see /usr/local/bunny).
"""

import os
import sys
import json
import time
import queue
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BOOTMARK = "/var/log/bunny-ui-boot.txt"


def _mk(tag, extra=""):
    try:
        with open(BOOTMARK, "a") as f:
            f.write("PK:%s %d%s\n" % (tag, int(time.time()), extra))
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def _hook(t, v, tb):
    try:
        import traceback as _tb
        with open(BOOTMARK, "a") as f:
            f.write("PK:CRASH %d\n%s\n" % (int(time.time()),
                                           "".join(_tb.format_exception(t, v, tb))))
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    sys.__excepthook__(t, v, tb)


sys.excepthook = _hook
_mk("start")

import hardware
_mk("hw")

import font
_mk("font")

RUN = "/run/bunny"
LED_STATE = os.path.join(RUN, "led.state")
INFO = os.path.join(RUN, "info")
SWITCH = os.path.join(RUN, "switch")
OUT_LOG = os.path.join(RUN, "out.log")
FIFO = os.path.join(RUN, "input.fifo")
FIFO_FLAGS = os.O_RDWR | os.O_NONBLOCK     # RDWR so a write never ENXIO/EPIPEs

LOG_PATH = "/var/log/pibunny-ui.log"


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg)
    sys.stderr.write(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass

UDISK_LIB = "/root/udisk/payloads/library"
UDISK_SW = "/root/udisk/payloads"
LOOT_DIR = "/root/udisk/loot"
MENU_CFG = "/root/udisk/.menucfg"
MENU_ASK = os.path.join(RUN, "menu.ask")
MENU_RESULT = os.path.join(RUN, "menu.result")

# LED state -> screen color (Hak5 convention kept)
COLORS = {
    "R": (255, 0, 0), "G": (0, 255, 0), "B": (0, 0, 255),
    "Y": (255, 255, 0), "C": (0, 255, 255), "M": (255, 0, 255),
    "W": (255, 255, 255),
}

# ---------------------------------------------------------------------------
# NEON palette (packed RGB565)
# ---------------------------------------------------------------------------
BLACK = hardware.rgb255(0, 0, 0)
WHITE = hardware.rgb255(255, 255, 255)
GREEN = hardware.rgb255(0, 255, 80)        # phosphor green (primary)
GREEN_BR = hardware.rgb255(120, 255, 150)  # hot green (emphasis)
GREEN_DIM = hardware.rgb255(0, 140, 50)    # dim green (secondary)
CYAN = hardware.rgb255(0, 255, 255)
CYAN_DIM = hardware.rgb255(0, 120, 120)
MAGENTA = hardware.rgb255(255, 0, 255)
MAGENTA_DIM = hardware.rgb255(150, 0, 150)
YEL = hardware.rgb255(255, 255, 0)
GREY = hardware.rgb255(120, 120, 120)
EDGE = hardware.rgb255(0, 90, 40)          # neon frame
ARS = hardware.rgb255(206, 16, 32)         # Arasaka crimson accent

SPLASH_TOTAL = 5.0

# (label, panel, MADCTL, invert, col off, row off, COLMOD)
VARIANTS = [
    ("ST7735 DFLT",       "st7735_128", 0x68, False, 0, 0, 0x05),
    ("ST7735 INV+",       "st7735_128", 0x68, True,  0, 0, 0x05),
    ("ST7735 MADCTL-00",  "st7735_128", 0x00, False, 0, 0, 0x05),
    ("ST7789 DFLT",       "st7789_240", 0x60, True,  0, 0, 0x05),
    ("ST7789 MADCTL-00",  "st7789_240", 0x00, True,  0, 0, 0x05),
]

# boot-splash script sequence: one line per stage
BOOT_SEQ = [
    "INIT SPI DRIVER",
    "MOUNT UDISK",
    "GPIO KEYBLOCK",
    "ARM GADGET",
    "SYSINFO",
]

MODE = {"name": "splash"}   # splash|switch|menu|run|output|loot|sys|cal


def mdir(p):
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        pass


def read_text(path, dflt=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return dflt


def write_text(path, data):
    try:
        with open(path, "w") as f:
            f.write(data)
    except Exception:
        pass


def wrap(text, width):
    """Greedy char wrap (scale-1). Returns list of lines <= width chars."""
    text = text.rstrip()
    if len(text) <= width:
        return [text]
    out = []
    while text:
        out.append(text[:width])
        text = text[width:]
    return out


def tail(path, nbytes=8192, nlines=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read().decode(errors="replace")
        return data.splitlines()[-nlines:]
    except Exception:
        return []


class Led:
    def __init__(self):
        self.state = {"color": (180, 0, 255), "pattern": "solid",
                      "count": 1, "on_ms": 0, "off_ms": 0, "invert": 0}
        self.mtime = 0
        self.seq = 0
        self.reload()

    def reload(self):
        try:
            m = os.path.getmtime(LED_STATE)
            with open(LED_STATE) as f:
                st = json.load(f)
            seq = int(st.get("seq", 0))
            if seq == self.seq and m == self.mtime:
                return False
            self.state = st
            self.mtime = m
            self.seq = seq
            return True
        except Exception:
            return False

    def frame(self, t):
        """Return (rgb, animating)."""
        d = self.state
        col = tuple(d.get("color", (0, 0, 0)))
        pat = d.get("pattern", "solid")
        on = int(d.get("on_ms", 0))
        off = int(d.get("off_ms", 0))
        cnt = int(d.get("count", 1))
        inv = int(d.get("invert", 0))

        if pat == "off":
            return (0, 0, 0), False
        if pat == "solid":
            return col, False
        if pat == "blink":
            period = max(1, on + off)
            k = int((t * 1000) % period)
            return (col if k < on else (0, 0, 0)), True
        if pat == "success":
            k = int((t * 1000) % 2000)
            if k < 400:
                return (col if int(k / 80) % 2 == 0 else (0, 0, 0)), True
            return col, True
        if pat == "count":
            period = cnt * (on + off) + (off if inv == 0 else on)
            period = max(1, period)
            k = int((t * 1000) % period)
            if inv == 0:
                if k < cnt * (on + off):
                    seg = k % max(1, on + off)
                    return (col if seg < on else (0, 0, 0)), True
                return (0, 0, 0), True
            else:
                if k < cnt * (on + off):
                    seg = k % max(1, on + off)
                    return (col if seg >= on else (0, 0, 0)), True
                return col, True
        return col, False


class App:
    def __init__(self):
        _mk("app-init")
        mdir(RUN)
        _mk("run-dir")
        self.disp = hardware.Display()
        _mk("display-object")
        self.sc_x = float(self.disp.w) / 128.0
        self.sc_y = float(self.disp.h) / 128.0
        self.fill(BLACK)
        self.disp.show()
        sys.stderr.write("bunny-ui: LCD initialized, black frame on panel\n")
        _mk("black-frame")
        self.led = Led()
        _mk("led")
        self.events = queue.Queue()
        self._poly = []
        self._fifo_fd = None
        self._open_fifo()
        self.menu_row = 1
        self.menu_scroll = 0
        self.menu_options = {}
        self.menu_cursor = 0
        self._splash_start = None
        self._splash_t0 = time.monotonic()
        self._menu_last_reload = None
        self.cal_idx = 0
        self._cal_ready = False
        self.last_info = ""
        self.last_switch = ""
        self._info_mtime = 0
        self._on_event = None
        # feature-screens state
        self._out_scroll = 0
        self._loot_files = []
        self._loot_idx = 0
        self._loot_view = None      # filename being viewed
        self._loot_lines = []
        self._loot_scroll = 0
        self._sys_cache = {}


        def handler(ev):
            try:
                self.events.put_nowait(ev)
            except Exception:
                pass
            self._publish(ev)

        self.input = hardware.Input(output=self._publish, on_event=handler)
        bn = getattr(getattr(self.input, "_gpio", None), "_backend_name", "?")
        sys.stderr.write("bunny-ui: input backend=%s\n" % (bn,))
        _mk("input-backend", ": " + bn)
        self._hbeat = 0.0
        self._hb = 0.0

    # -- layout scaling (128-base -> real panel) ----------------------------
    def S(self, v):
        return max(0, int(round(v * self.sc_x)))

    def FS(self, v):
        """Font scale for a 128-base design scale."""
        return max(1, int(round(v * self.sc_x)))

    # -- fifo ----------------------------------------------------------------
    def _open_fifo(self):
        try:
            if not os.path.exists(FIFO):
                os.mkfifo(FIFO)
            self._fifo_fd = os.open(FIFO, FIFO_FLAGS)
        except OSError:
            self._fifo_fd = None

    def _publish(self, ev):
        if self._fifo_fd is None:
            return
        try:
            os.write(self._fifo_fd, (ev + "\n").encode())
        except OSError:
            try:
                self._fifo_fd = os.open(FIFO, FIFO_FLAGS)
                os.write(self._fifo_fd, (ev + "\n").encode())
            except OSError:
                pass

    # -- drawing primitives ---------------------------------------------------
    def _pack(self, rgb):
        if len(rgb) == 2:
            return rgb
        return hardware.rgb255(*rgb)

    def fill(self, rgb):
        hi, lo = self._pack(rgb)
        w = self.disp.w
        row = bytes([hi, lo]) * w
        fb = self.disp.framebuf
        stride = w * 2
        h = self.disp.h
        for yy in range(h):
            fb[yy * stride:(yy + 1) * stride] = row

    def strip(self, y, h, rgb):
        hi, lo = self._pack(rgb)
        w = self.disp.w
        row = bytes([hi, lo]) * w
        fb = self.disp.framebuf
        stride = w * 2
        for yy in range(y, y + h):
            if 0 <= yy < self.disp.h:
                fb[yy * stride:(yy + 1) * stride] = row

    def box(self, x0, y0, x1, y1, rgb):
        hi, lo = self._pack(rgb)
        w = self.disp.w
        fb = self.disp.framebuf
        stride = w * 2
        for yy in range(max(0, y0), min(y1, self.disp.h)):
            base = yy * stride
            for xx in range(max(0, x0), min(x1, w)):
                p = base + xx * 2
                fb[p] = hi
                fb[p + 1] = lo

    def border(self, color=EDGE):
        w = self.disp.w
        stride = w * 2
        hi, lo = self._pack(color)
        row = bytes([hi, lo]) * w
        fb = self.disp.framebuf
        fb[0:stride] = row
        fb[(self.disp.h - 1) * stride:self.disp.h * stride] = row
        for yy in range(self.disp.h):
            fb[yy * stride] = hi
            fb[yy * stride + 1] = lo
            fb[yy * stride + (w - 1) * 2] = hi
            fb[yy * stride + (w - 1) * 2 + 1] = lo

    def text(self, x, y, s, color, scale=1, bg=None):
        font.draw_text(self.disp.framebuf, self.disp.w, self.disp.h,
                       self.S(x), self.S(y), s, self._pack(color), self.FS(scale),
                       self._pack(bg) if bg is not None else None)

    def center(self, y, text, color, scale, bg=None):
        w = self.disp.w
        tw = font.text_width(text, scale) * self.FS(scale)
        x = max(0, (w - tw) // 2)
        self.text(x, y, text, color, scale, bg)

    def _blink(self, t, ms=500):
        return int((t * 1000) / ms) % 2 == 0

    # -- render ---------------------------------------------------------------
    def render(self, t):
        m = MODE["name"]
        self.fill(BLACK)
        if m == "splash":
            self.render_splash(t)
        elif m == "switch":
            self.render_menu(t)   # K3=OK / K2=BACK / joystick navigates
        elif m == "menu":
            self.render_menu(t)
        elif m == "run":
            self.render_run(t)
        elif m == "output":
            self.render_output(t)
        elif m == "loot":
            self.render_loot(t)
        elif m == "sys":
            self.render_sys(t)
        elif m == "cal":
            self.render_cal(t)
        self.border(EDGE)

    # -------- splash ----------------------------------------------------------
    def render_splash(self, t):
        S = self.S
        el = t - self._splash_t0
        self.center(S(8), "PIBUNNY", GREEN_BR, 2)
        self.center(S(28), "ARASAKA", ARS, 1)
        self.center(S(40), "// BACKDOOR TERMINAL //", CYAN_DIM, 1)
        # boot sequence
        y = 56
        for i, line in enumerate(BOOT_SEQ):
            tt = el - i * 0.55
            if tt < 0:
                break
            ok = tt > 0.30
            col = GREEN if ok else GREEN_DIM
            self.text(S(8), S(y), line.ljust(18, "."), col, 1)
            if ok:
                self.text(S(8) + self.S(18 * 6), S(y), " OK", GREEN_BR, 1)
            y += 10
        # progress track
        p = min(1.0, el / SPLASH_TOTAL)
        self.box(S(8), S(112), S(120), S(118), (0, 60, 24))
        fx = S(8) + int((self.disp.w - S(16)) * p)
        if fx > S(8):
            self.box(S(8), S(112), fx, S(118), GREEN)
        # moving scan bar
        ybar = S(48) + int((t * S(2)) % (self.disp.h - S(70)))
        self.strip(ybar, S(1), CYAN_DIM)

    # -------- switch ----------------------------------------------------------
    # -------- menu ------------------------------------------------------------
    def menu_items(self):
        """Ordered (key, base-label) rows: .menucfg switches, then tools."""
        keys = []
        if os.path.isfile(MENU_CFG):
            try:
                with open(MENU_CFG) as f:
                    for line in f:
                        p = line.split()
                        if p and p[0].startswith("switch") and \
                                p[0][6:].isdigit():
                            keys.append(p[0])
            except Exception:
                pass
        if not keys:
            keys = ["switch%d" % i for i in range(1, 4)]
        items = [(k, "SW%s" % k[6:]) for k in keys]
        items += [("armed", "ARM"), ("loot", "LOOT"), ("sys", "SYS"),
                  ("cal", "CAL"), ("back", "BACK")]
        return items

    def render_menu(self, t=None):
        S = self.S
        self.text(S(4), S(4), "> PIBUNNY//CTRL", CYAN, 1)
        items = self.menu_items()
        total = len(items)
        vis = 6
        top = max(0, min(self.menu_row, total - vis))
        self.menu_scroll = top
        y = 14
        for i in range(top, min(total, top + vis)):
            key, base = items[i]
            sel = (i == self.menu_row)
            if key.startswith("switch"):
                cfg = self.read_cfg(key).replace(
                    "library/", "").replace(".txt", "")
                label = "%s %s" % (base, (cfg or "?")[:11])
            elif key == "armed":
                label = "%s RUN" % base
            elif key == "back":
                label = "%s <<" % base
            else:
                label = "%s ---" % base
            txt = label[:16]
            if sel:
                self.strip(S(y - 1), S(9), GREEN)
                self.text(S(4), S(y), "> " + txt, BLACK, 1)
            else:
                self.text(S(4), S(y), "  " + txt, GREEN_DIM, 1)
            y += 16
        page = ""
        if total > vis:
            page = "  %d/%d" % (self.menu_row + 1, total)
        self.text(S(4), S(116),
                  "K3 OK  K2 BACK%s" % page, MAGENTA_DIM, 1)

    # -------- run (live payload terminal) -------------------------------------
    def render_run(self, t):
        S = self.S
        sw = read_text(SWITCH).upper() or "?"
        self.text(S(4), S(4), "> ARMED :: %s" % sw[:10], GREEN_BR, 1)
        info = read_text(INFO)
        if info:
            self.text(S(4), S(14), info[:20].upper(), CYAN, 1)
        lines = tail(OUT_LOG, 8192, 12)
        if not lines:
            lines = ["[ waiting for output ]"]
        y = 28
        pending = lines[-6:]
        for ln in pending:
            for piece in wrap(ln, 20):
                if y >= S(108):
                    break
                self.text(S(4), S(y), piece[:20], GREEN_DIM, 1)
                y += 9
        # blinking cursor on last output line
        if self._blink(t, 500) and y < S(112):
            self.text(S(4) + S(19 * 6), S(y - 9), "_", GREEN, 1)
        rgb, _ = self.led.frame(t)
        self.strip(self.disp.h - S(3), S(3), rgb)
        self.text(S(4), S(self.disp.h - S(10)),
                  "K3 OUT  K2 BACK", MAGENTA_DIM, 1)

    # -------- output viewer ----------------------------------------------------
    def render_output(self, t):
        S = self.S
        self.text(S(4), S(4), "> OUTPUT", CYAN, 1)
        all_lines = tail(OUT_LOG, 16384, 200)
        if not all_lines:
            all_lines = ["[ empty ]"]
        n = max(1, int((100 / 9)))
        lines = all_lines[self._out_scroll:self._out_scroll + n]
        y = 16
        for ln in lines:
            for piece in wrap(ln, 20):
                if y >= S(106):
                    break
                self.text(S(4), S(y), piece[:20], GREEN_DIM, 1)
                y += 9
        footer = "U/D SCR  K1 MENU  K2 RUN"
        self.text(S(4), S(116), footer, MAGENTA_DIM, 1)

    # -------- loot browser -----------------------------------------------------
    def render_loot(self, t):
        S = self.S
        if self._loot_view:
            self.text(S(4), S(4), "> LOOT :: %s" % self._loot_view[:14], CYAN, 1)
            lines = self._loot_lines
            visible = list(range(8))
            y = 16
            for i in visible:
                ln = self._loot_lines[self._loot_scroll + i] if self._loot_lines else ""
                for piece in wrap(ln[:20], 20):
                    if y >= S(110):
                        break
                    self.text(S(4), S(y), piece[:20], GREEN_DIM, 1)
                    y += 9
            self.text(S(4), S(116), "U/D SCR  K1 BACK", MAGENTA_DIM, 1)
        else:
            self.text(S(4), S(4), "> LOOT FILES", CYAN, 1)
            y = 16
            files = self._loot_files or ["[ empty ]"]
            for i in range(8):
                if i >= len(files):
                    break
                sel = (i == self._loot_idx % max(1, len(files)))
                name = files[i][:19]
                if sel:
                    self.strip(S(y - 1), S(9), GREEN)
                    self.text(S(4), S(y), "> " + name[:16], BLACK, 1)
                else:
                    self.text(S(4), S(y), "  " + name[:16], GREEN_DIM, 1)
                y += 16
            self.text(S(4), S(116), "U/D MOVE  JP VIEW  K1 BACK", MAGENTA_DIM, 1)

    # -------- system info ------------------------------------------------------
    def render_sys(self, t):
        S = self.S
        self.text(S(4), S(4), "> SYSINFO", CYAN, 1)
        self._cache_sys()
        rows = [
            ("HOST", self._sys_cache.get("host", "?")),
            ("KERN", self._sys_cache.get("kern", "?")),
            ("UPTM", self._sys_cache.get("uptime", "?")),
            ("IP", self._sys_cache.get("ip", "?")),
            ("SW", read_text(SWITCH) or "none"),
        ]
        y = 16
        for k, v in rows:
            self.text(S(4), S(y), "%s %s" % (k, v[:15]), GREEN, 1)
            y += 15
        self.text(S(4), S(116), "K1 BACK", MAGENTA_DIM, 1)

    def _cache_sys(self):
        if self._sys_cache.get("_t", 0) and \
                time.time() - self._sys_cache["_t"] < 5:
            return
        try:
            host = read_text("/proc/sys/kernel/hostname") or "?"
        except Exception:
            host = "?"
        try:
            with open("/proc/version") as f:
                kern = f.read().split()[2] or "?"
        except Exception:
            kern = "?"
        try:
            with open("/proc/uptime") as f:
                up = int(float(f.read().split()[0]))
            up = "%dh%02dm" % (up // 3600, (up % 3600) // 60)
        except Exception:
            up = "?"
        ips = []
        try:
            out = subprocess.run(["ip", "-4", "addr", "show"],
                                 capture_output=True, text=True, timeout=2).stdout
            for line in out.splitlines():
                if "inet " in line:
                    ips.append(line.split()[1].split("/")[0])
        except Exception:
            ip = "?"
        self._sys_cache = {"host": host, "kern": kern, "uptime": up,
                           "ip": ",".join(ips[:3])
                               if ips else (ip if "ip" in dir() else "?"),
                           "_t": time.time()}

    # -------- calibrate --------------------------------------------------------
    def render_cal(self, t):
        S = self.S
        v = VARIANTS[self.cal_idx % len(VARIANTS)]
        # quadrant calibration (colors stay honest for judging the panel)
        self.box(0, S(0), S(63), S(56), hardware.rgb255(220, 30, 30))
        self.box(S(64), S(0), S(127), S(56), hardware.rgb255(30, 220, 30))
        self.box(0, S(57), S(63), S(100), hardware.rgb255(30, 30, 220))
        self.box(S(64), S(57), S(127), S(100), WHITE)
        self.text(S(4), S(4), "> CAL %d/%d" % (
            self.cal_idx % len(VARIANTS) + 1, len(VARIANTS)), WHITE, 1)
        lab = v[0][:15]
        self.text(S(4), S(14), lab, WHITE, 1)
        self.text(S(4), S(114), "JP NXT  K1 SAVE  K2 EXIT", MAGENTA, 1)

    # -- mode transitions -------------------------------------------------------
    def enter_cal(self):
        self.cal_idx = max(0, self.cal_idx % len(VARIANTS))
        self._cal_apply()
        self._cal_ready = True
        MODE["name"] = "cal"

    def _cal_apply(self):
        v = VARIANTS[self.cal_idx % len(VARIANTS)]
        self.disp.apply_variant(v[1], v[2], v[3], v[4], v[5], v[6])
        self.sc_x = float(self.disp.w) / 128.0
        self.sc_y = float(self.disp.h) / 128.0

    def _cal_save(self):
        v = VARIANTS[self.cal_idx % len(VARIANTS)]
        hardware.save_cal(v[1], v[2], v[3], v[4], v[5], v[6])
        sys.stderr.write("bunny-ui: CAL saved %s\n" % (v[0],))
        MODE["name"] = "menu"

    def _cal_exit(self):
        MODE["name"] = "menu"

    # -- menu data -------------------------------------------------------------
    def load_menu(self):
        opts = {}
        for sw in ("switch%d" % i for i in range(1, 20)):
            base = os.path.join(UDISK_SW, sw)
            items = []
            if os.path.isdir(base):
                for f in sorted(os.listdir(base)):
                    if f.endswith(".txt"):
                        items.append(f)
            if os.path.isdir(UDISK_LIB):
                for f in sorted(os.listdir(UDISK_LIB)):
                    if f.endswith(".txt"):
                        items.append("library/" + f)
            seen = set()
            opts[sw] = []
            for it in items:
                if it not in seen:
                    seen.add(it)
                    opts[sw].append(it)
            if not opts[sw]:
                opts[sw] = ["(none)"]
        self.menu_options = opts

    def current(self, key):
        opts = self.menu_options.get(key, ["payload.txt"])
        cfg = self.read_cfg(key)
        try:
            return opts.index(cfg)
        except ValueError:
            return 0

    def read_cfg(self, sw):
        if os.path.isfile(MENU_CFG):
            try:
                with open(MENU_CFG) as f:
                    for line in f:
                        parts = line.split()
                        if parts and parts[0] == sw:
                            return parts[1]
            except Exception:
                pass
        return "payload.txt"

    def write_cfg(self, sw, val):
        lines = []
        if os.path.isfile(MENU_CFG):
            try:
                with open(MENU_CFG) as f:
                    lines = f.read().splitlines()
            except Exception:
                pass
        out = []
        hit = False
        for line in lines:
            parts = line.split()
            if parts and parts[0] == sw:
                out.append("%s %s" % (sw, val))
                hit = True
            else:
                out.append(line)
        if not hit:
            out.append("%s %s" % (sw, val))
        write_text(MENU_CFG, "\n".join(out) + "\n")

    def _refresh_loot(self):
        try:
            names = sorted(
                f for f in os.listdir(LOOT_DIR)
                if os.path.isfile(os.path.join(LOOT_DIR, f)))
        except Exception:
            names = []
        self._loot_files = names
        if self._loot_idx >= len(names):
            self._loot_idx = 0

    def _open_loot_file(self, name):
        p = os.path.join(LOOT_DIR, name)
        try:
            with open(p, errors="replace") as f:
                data = f.read()
        except Exception:
            data = "[ unreadable ]"
        self._loot_lines = []
        for ln in data.splitlines():
            for piece in wrap(ln, 20):
                self._loot_lines.append(piece)
        if not self._loot_lines:
            self._loot_lines = ["[ empty ]"]
        self._loot_scroll = 0

    # -- events -----------------------------------------------------------------
    def commit_row(self, key):
        if not self.menu_options or not any(
                v and v != ["(none)"] for v in self.menu_options.values()):
            self.load_menu()
        opts = self.menu_options.get(key, ["payload.txt"])
        cur = self.current(key)
        if opts[cur] != "(none)":
            self.write_cfg(key, opts[cur])
        MODE["name"] = "run"
        write_text(MENU_RESULT, key)
        try:
            os.unlink(MENU_ASK)
        except OSError:
            pass

    def quick_run(self, key):
        MODE["name"] = "run"
        write_text(MENU_RESULT, key)
        try:
            os.unlink(MENU_ASK)
        except OSError:
            pass

    def handle(self, ev):
        m = MODE["name"]
        if m == "splash":
            self._splash_start = time.monotonic() + 1000
            MODE["name"] = "switch"
        elif m == "switch":
            # Primary interface: a single navigable list.
            #   JU/JD = move highlight; K3 / JP = OK (select/run);
            #   K2 = BACK (off / no-op at top level, backs out of tools).
            items = self.menu_items()
            if ev == "JU":
                self.menu_row = (self.menu_row - 1) % len(items)
            elif ev == "JD":
                self.menu_row = (self.menu_row + 1) % len(items)
            elif ev == "K3" or ev == "JP":
                self.activate_row()
        elif m == "run":
            if ev == "K3" or ev == "JP":
                MODE["name"] = "output"
            elif ev == "K2":
                self.return_to_menu()
        elif m == "output":
            if ev == "K2":
                MODE["name"] = "run"
            elif ev == "K3" or ev == "JP" or ev == "K1":
                self.return_to_menu()
            elif ev == "JU" or ev == "JL":
                self._out_scroll = max(0, self._out_scroll - 3)
            elif ev == "JD" or ev == "JR":
                self._out_scroll += 3
        elif m == "loot":
            if self._loot_view:
                if ev == "K2":
                    self._loot_view = None
                elif ev == "K3" or ev == "JP" or ev == "K1":
                    self._loot_view = None
                elif ev in ("JU", "JL"):
                    self._loot_scroll = max(0, self._loot_scroll - 1)
                elif ev in ("JD", "JR"):
                    self._loot_scroll += 1
            else:
                if ev == "K2":
                    self.return_to_menu()
                elif ev == "K3" or ev == "JP":
                    if self._loot_files:
                        self._open_loot_file(self._loot_files[self._loot_idx])
                        self._loot_view = self._loot_files[self._loot_idx]
                elif ev in ("JU", "JL"):
                    self._loot_idx = (self._loot_idx - 1) % max(1, len(self._loot_files))
                elif ev in ("JD", "JR"):
                    self._loot_idx = (self._loot_idx + 1) % max(1, len(self._loot_files))
        elif m == "sys":
            if ev == "K2":
                self.return_to_menu()
            elif ev == "K3" or ev == "JP":
                self._cache_sys()
        elif m == "cal":
            if ev in ("JP", "JR", "JD"):
                self.cal_idx = (self.cal_idx + 1) % len(VARIANTS)
                self._cal_apply()
            elif ev in ("JL", "JU"):
                self.cal_idx = (self.cal_idx - 1) % len(VARIANTS)
                self._cal_apply()
            elif ev == "K1":
                self._cal_save()
            elif ev == "K2":
                self._cal_exit()

    def activate_row(self):
        """K3/JP OK action on the highlighted list row."""
        key = self.menu_items()[self.menu_row][0]
        if key == "back":
            # BACK row at the selector = just return to the list (no-op)
            return
        elif key == "armed":
            self.quick_run("arming")
        elif key == "loot":
            self._refresh_loot()
            self._loot_view = None
            MODE["name"] = "loot"
        elif key == "sys":
            self._cache_sys()
            MODE["name"] = "sys"
        elif key == "cal":
            self.enter_cal()
        elif key.startswith("switch"):
            self.commit_row(key)

    def return_to_menu(self):
        """K2 BACK from any tool/payload screen returns to the selector list."""
        MODE["name"] = "switch"
        write_text(MENU_RESULT, "none")   # tell the framework "don't run"
        try:
            os.unlink(MENU_ASK)
        except OSError:
            pass

    # -- main loop ---------------------------------------------------------------
    def run(self):
        t_last = time.monotonic()
        dirty = True
        _mk("run-loop")
        frames = 0
        while True:
            now = time.monotonic()
            if now - self._hbeat > 1.0:
                self._hbeat = now
                fb = self.disp.framebuf
                fb_sample = "".join("%02x" % b for b in fb[::1024][:4])
                nz = sum(1 for b in fb[::256])
                sys.stderr.write(
                    "bunny-ui: hb mode=%s dirty=%s fb=%s nz=%d\n"
                    % (MODE["name"], dirty, fb_sample, nz))
            try:
                while True:
                    ev = self.events.get_nowait()
                    self.handle(ev)
                    dirty = True
            except queue.Empty:
                pass
            champ = self.led.reload()
            if champ:
                dirty = True
            inf = read_text(INFO)
            if inf != self.last_info:
                self.last_info = inf
                dirty = True
            sw = read_text(SWITCH)
            if sw != self.last_switch:
                self.last_switch = sw
                if sw and MODE["name"] == "switch":
                    MODE["name"] = "run"
                dirty = True
            # framework finished the payload and is back at the selector
            if MODE["name"] in ("run", "output") and inf.startswith("SELECT"):
                MODE["name"] = "switch"
                dirty = True
            if MODE["name"] == "menu":
                nowm = time.monotonic()
                if (self._menu_last_reload is None or
                        nowm - self._menu_last_reload > 1.0):
                    self._menu_last_reload = nowm
                    had = any(v and v != ["(none)"]
                              for v in self.menu_options.values())
                    self.load_menu()
                    has = any(v and v != ["(none)"]
                              for v in self.menu_options.values())
                    if has != had:
                        dirty = True
            if MODE["name"] == "splash":
                if self._splash_start is None and \
                        now - self._splash_t0 > SPLASH_TOTAL:
                    MODE["name"] = "switch"
                    dirty = True
            t = time.monotonic()
            if dirty or MODE["name"] in ("splash", "switch", "run"):
                if (t - t_last) > 0.05:
                    t0 = time.monotonic()
                    try:
                        self.render(t)
                        self.disp.show()
                        frames += 1
                        if frames == 1:
                            _mk("first-frame")
                        log("frame mode=%s blit=%.0fms" % (
                            MODE["name"], (time.monotonic() - t0) * 1000))
                    except Exception:
                        import traceback
                        log("frame ERR:\n" + traceback.format_exc())
                    t_last = t
                    dirty = False
            if time.monotonic() - self._hb > 5.0:
                self._hb = time.monotonic()
                log("alive mode=%s" % MODE["name"])
                _mk("hb", ": " + MODE["name"])
            time.sleep(0.02)


def main():
    _mk("main")
    log("ui: start")
    try:
        app = App()
        log("ui: display ready (panel=%s madctl=%02x inv=%d colmod=%02x w=%d h=%d)"
            % (app.disp.panel, app.disp.madctl, app.disp.inv, app.disp.colmod,
               app.disp.w, app.disp.h))
        _mk("app-ready")
    except Exception as e:
        import traceback
        log("ui: FATAL init failure:\n" + traceback.format_exc())
        _mk("fatal-init")
        raise
    try:
        app.run()
    except Exception:
        import traceback
        log("ui: FATAL loop failure:\n" + traceback.format_exc())
        _mk("fatal-loop")
        raise


if __name__ == "__main__":
    main()