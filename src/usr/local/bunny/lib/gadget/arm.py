#!/usr/bin/env python3
"""arm.py -- configfs USB composite gadget manager for the PiBunny port.

Replaces the Hak5-bunny bunny_gadget.ko kernel module on the Raspberry Pi
(dwc2 controller, libcomposite). Implements the same composite functions:
Keyboard HID, Mass Storage, RNDIS/CDC-ECM ethernet and CDC-ACM serial, with
the same VID/PID conventions and insmod-style parameters.

Hak5 bash bunny gadget params (original):
  insmod bunny_gadget.ko is_hid=1 is_storage=1 is_rndis=1 is_cdc_ecm=1
      is_cdc_serial=1 file=/dev/... stall=0 removable=1 nofua=0 ro=1
      host_addr=00:11:22:33:44:55 dev_addr=5a:00:00:5a:5a:00
      idVendor=0xF000 idProduct=0xFFxx iSerialNumber=... rndis_speed=...
"""

import os
import sys
import glob
import shutil
import time

GADGET = "/sys/kernel/config/usb_gadget"
NAME = "pibunny"

# Keyboard boot-protocol report descriptor with a 1-byte LED (output) report,
# so the host's NumLock/CapsLock state can be read back via hid_reader.
REPORT_DESC = bytes([
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01,            # GenericDesktop, Keyboard, App
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7,            # Keyboard, Ctl..GUI
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,  # 8 modifier bits
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,            # 1 reserved byte (const)
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,  # LEDs out
    0x95, 0x01, 0x75, 0x03, 0x91, 0x01,            # LED pad bits
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65, 0x05, 0x07,
    0x19, 0x00, 0x29, 0x65, 0x81, 0x00,            # 6 key codes
    0xC0,
])

DEFAULT_HOST_ADDR = "00:11:22:33:44:55"
DEFAULT_DEV_ADDR = "5a:00:00:5a:5a:00"

# One HID input report = 1 modifier byte + 1 reserved byte + 6 key scancodes.
# f_hid's "report_length" attribute is the per-report byte count the host
# reads/writes on this interface (NOT the descriptor length above).
REPORT_INPUT_LEN = 8


def modprobe(mod):
    os.system("modprobe %s 2>/dev/null" % mod)


def get_udc():
    udcs = sorted(glob.glob("/sys/class/udc/*"))
    if not udcs:
        return None
    return os.path.basename(udcs[0])


def log(msg):
    print(msg)


def write(path, value):
    if isinstance(value, (bytes, bytearray)):
        with open(path, "wb") as f:
            f.write(bytes(value))
    else:
        with open(path, "w") as f:
            f.write(str(value))


def exists(path):
    return os.path.exists(path)


def rmtree(path):
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


class GadgetError(Exception):
    pass


def now_armed():
    return os.path.isdir(os.path.join(GADGET, NAME))


def disarm():
    g = os.path.join(GADGET, NAME)
    if not os.path.isdir(g):
        return
    cfg = os.path.join(g, "configs", "c.1")
    udc = os.path.join(g, "UDC")
    try:
        write(udc, "")
    except OSError:
        pass
    time.sleep(0.3)
    if os.path.isdir(cfg):
        for link in sorted(os.listdir(cfg)):
            if link not in ("strings",):
                p = os.path.join(cfg, link)
                if os.path.islink(p):
                    os.unlink(p)
    funcs = os.path.join(g, "functions")
    if os.path.isdir(funcs):
        for fn in sorted(os.listdir(funcs)):
            rmtree(os.path.join(funcs, fn))
    for sub in ("configs/c.1/strings/0x409", "configs/c.1", "strings/0x409",
                "functions", "configs", "strings"):
        p = os.path.join(g, sub)
        try:
            rmtree(p)
        except OSError:
            pass
    try:
        os.rmdir(g)
    except OSError:
        # configfs: attributes don't block rmdir, so this is only reached
        # if a real file system entry remains (e.g. our unit-test sandbox).
        shutil.rmtree(g, ignore_errors=True)


def set_gadget_ids(vid, pid, sn, man, product):
    g = os.path.join(GADGET, NAME)
    write(os.path.join(g, "idVendor"), "0x%04x" % vid)
    write(os.path.join(g, "idProduct"), "0x%04x" % pid)
    os.makedirs(os.path.join(g, "strings", "0x409"), exist_ok=True)
    write(os.path.join(g, "strings", "0x409", "serialnumber"), sn)
    write(os.path.join(g, "strings", "0x409", "manufacturer"), man)
    write(os.path.join(g, "strings", "0x409", "product"), product)
    os.makedirs(os.path.join(g, "configs", "c.1", "strings", "0x409"), exist_ok=True)
    write(os.path.join(g, "configs", "c.1", "strings", "0x409", "configuration"), "PiBunny Config 1")
    write(os.path.join(g, "configs", "c.1", "MaxPower"), "250")


def arm(opts):
    g = os.path.join(GADGET, NAME)
    os.makedirs(g, exist_ok=True)

    vid = opts.get("vid", 0xF000)
    pid = opts.get("pid", 0xFEFF)
    sn = opts.get("sn", "ch000001")
    man = opts.get("man", "Hak5")
    product = opts.get("product", "Bash Bunny")

    set_gadget_ids(vid, pid, sn, man, product)

    funcs = os.path.join(g, "functions")
    cfg = os.path.join(g, "configs", "c.1")

    def add_func(fn, kind):
        d = os.path.join(funcs, fn)
        os.makedirs(d, exist_ok=True)
        return d

    def bind(fn):
        os.symlink(os.path.join(funcs, fn), os.path.join(cfg, fn))

    host_addr = opts.get("host_addr", DEFAULT_HOST_ADDR)
    dev_addr = opts.get("dev_addr", DEFAULT_DEV_ADDR)

    if opts.get("hid"):
        d = add_func("hid.usb0", "hid")
        write(os.path.join(d, "report_desc"), REPORT_DESC)
        write(os.path.join(d, "report_length"), REPORT_INPUT_LEN)
        # advertise boot-protocol keyboard (interface subclass 1/protocol 1)
        write(os.path.join(d, "subclass"), 1)
        write(os.path.join(d, "protocol"), 1)
        bind("hid.usb0")

    if opts.get("storage"):
        d = add_func("mass_storage.usb0", "storage")
        lun = os.path.join(d, "lun.0")
        if exists(os.path.join(lun, "file")):
            write(os.path.join(lun, "file"), opts.get("storage_file", "/dev/loop0p1"))
        if exists(os.path.join(lun, "removable")):
            write(os.path.join(lun, "removable"), 1)
        if exists(os.path.join(lun, "nofua")):
            write(os.path.join(lun, "nofua"), 0)
        if exists(os.path.join(lun, "ro")):
            write(os.path.join(lun, "ro"), 1 if opts.get("ro") else 0)
        bind("mass_storage.usb0")

    if opts.get("rndis"):
        d = add_func("rndis.usb0", "rndis")
        if exists(os.path.join(d, "host_addr")):
            write(os.path.join(d, "host_addr"), host_addr)
            write(os.path.join(d, "dev_addr"), dev_addr)
        # RNDIS_SPEED_ from stock bunny had no configfs counterpart
        # (the gadget negotiates link speed itself); accepted and ignored.
        bind("rndis.usb0")

    if opts.get("ecm"):
        d = add_func("ecm.usb0", "ecm")
        if exists(os.path.join(d, "host_addr")):
            write(os.path.join(d, "host_addr"), host_addr)
            write(os.path.join(d, "dev_addr"), dev_addr)
        bind("ecm.usb0")

    if opts.get("serial"):
        add_func("acm.usb0", "serial")
        bind("acm.usb0")

    udc = get_udc()
    if not udc:
        raise GadgetError("no UDC found (dwc2 not in device mode?)")
    write(os.path.join(g, "UDC"), udc)
    log("gadget armed: vid=%04x pid=%04x functions=%s udc=%s"
        % (vid, pid, ",".join(sorted(os.listdir(cfg))), udc))


def parse_args(argv):
    opts = {"bool": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--hid", "--storage", "--rndis", "--ecm", "--serial", "--ro"):
            opts[a[2:]] = True
        elif a.startswith("--"):
            key = a[2:]
            if a[2:] == "storage-file":
                key = "storage_file"
            i += 1
            opts[key] = argv[i]
        i += 1
    if opts.get("vid"):
        opts["vid"] = int(str(opts["vid"]), 0)
    if opts.get("pid"):
        opts["pid"] = int(str(opts["pid"]), 0)
    if opts.get("rndis_speed"):
        opts["rndis_speed"] = int(opts["rndis_speed"])
    return opts


def main():
    opts = parse_args(sys.argv[1:])
    # dwc2 must be loaded for the UDC to appear (dr_mode=peripheral set in
    # config.txt). If the legacy g_* modules grabbed the controller first they
    # will own /sys/class/udc and configfs can't bind, so drop them first.
    os.system("rmmod g_hid g_multi g_ether 2>/dev/null")
    modprobe("dwc2")
    modprobe("usb_f_hid")
    modprobe("usb_f_mass_storage")
    modprobe("usb_f_rndis")
    modprobe("usb_f_ecm")
    modprobe("usb_f_acm")
    disarm()
    arm(opts)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("arm.py: %s" % e)
        sys.exit(1)