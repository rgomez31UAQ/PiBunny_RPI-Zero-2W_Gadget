#!/usr/bin/env python3
# Simple program to read HID output reports (LED / numlock status).
# Converts output to HEX and stores the latest output in a file.
# Python 3 port of the Hak5 bunny hid_reader.

HID_DEV = "/dev/hidg0"
OUT_FILE = "/tmp/hid_out"


def poller():
    while True:
        with open(HID_DEV, "rb") as r:
            output = r.read(1).hex()
            with open(OUT_FILE, "w") as w:
                w.write(output)


if __name__ == "__main__":
    poller()