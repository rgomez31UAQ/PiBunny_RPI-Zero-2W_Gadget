#!/bin/bash
# install.sh -- PiBunny installer (run as root on Raspberry Pi OS Lite,
# 32-bit, on a Zero 2 W). Enables the Waveshare 1.3inch LCD HAT gadget, adds
# the ported bunny layer and services. Reboot when done.
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
export DEBIAN_FRONTEND=noninteractive

# Boot filesystem location (Bookworm moved it)
if [ -d /boot/firmware ]; then
	BOOT=/boot/firmware
else
	BOOT=/boot
fi

echo "[*] Installing packages..."
apt-get update
apt-get install -y python3 python3-spidev python3-libgpiod \
	dnsmasq dosfstools curl

echo "[*] Installing bunny layer -> /usr/local/bunny"
install -d /usr/local/bunny
cp -r "$REPO_DIR/src/usr/local/bunny"/. /usr/local/bunny/
chmod +x /usr/local/bunny/bin/* /usr/local/bunny/hid_reader.py \
	/usr/local/bunny/lib/gadget/arm.py
ln -sfn QUACK /usr/local/bunny/bin/Q

echo "[*] Building udisk skeleton + image"
install -d /opt/bunny
rm -rf /opt/bunny/skeleton
cp -r "$REPO_DIR/udisk" /opt/bunny/skeleton
if [ ! -f /opt/bunny/udisk.img ]; then
	bash "$REPO_DIR/build_udisk.sh"
fi

echo "[*] Network / DHCP config"
install -d /etc/dnsmasq.d
install -m 644 "$REPO_DIR/src/etc/dnsmasq.d/pibunny.conf" /etc/dnsmasq.d/pibunny.conf
install -d /etc/network/interfaces.d
install -m 644 "$REPO_DIR/src/etc/network/interfaces.d/usb0" /etc/network/interfaces.d/usb0
if [ -e /etc/dhcpcd.conf ]; then
	grep -q '^denyinterfaces usb0' /etc/dhcpcd.conf \
		|| echo 'denyinterfaces usb0' >> /etc/dhcpcd.conf
fi
# don't auto-start the DHCP server; ATTACKMODE starts it when the ethernet
# gadget is armed
systemctl disable dnsmasq.service 2>/dev/null || true

echo "[*] Installing services"
install -m 644 "$REPO_DIR/src/etc/systemd/system/bunny-ui.service" /etc/systemd/system/
install -m 644 "$REPO_DIR/src/etc/systemd/system/bunny.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable bunny-ui.service bunny.service

echo "[*] Writing $BOOT/config.txt (PiBunny block)"
if ! grep -q '^# PiBunny' "$BOOT/config.txt"; then
	cat >> "$BOOT/config.txt" <<EOF

# PiBunny
# USB gadget mode: the Pi is always the attached device (micro-USB -> target)
dtoverlay=dwc2,dr_mode=peripheral
# SPI for the Waveshare 1.3inch LCD HAT
dtparam=spi=on
# HAT keys/joystick pull-ups come from libgpiod BIAS_PULL_UP in hardware.py
EOF
fi

echo "[*] Writing $BOOT/cmdline.txt (load gadget modules at boot)"
if ! grep -q 'modules-load=dwc2,libcomposite' "$BOOT/cmdline.txt"; then
	sed -i '1 s/$/ modules-load=dwc2,libcomposite/' "$BOOT/cmdline.txt"
fi

echo
echo "[+] Done. Reboot the Pi, then plug the micro-USB port into a target."
echo "    Screen: KEY1/2/3 = select switch1/2/3, joystick = payload menu."