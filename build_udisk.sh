#!/bin/bash
# build_udisk.sh -- create/recreate the loop-backed "BashBunny" visible disk.
# Populated from /opt/bunny/skeleton. Size configurable via UDISK_SIZE_M.
set -e

SIZE_M="${UDISK_SIZE_M:-256}"
IMG="/opt/bunny/udisk.img"
SKEL="/opt/bunny/skeleton"

mkdir -p /opt/bunny
if [ ! -f "$IMG" ]; then
	echo "[*] Creating $IMG (${SIZE_M}MiB)"
	dd if=/dev/zero of="$IMG" bs=1M count="$SIZE_M" status=none
	mkfs.vfat -F 32 -n "BashBunny" "$IMG" &> /dev/null
fi

MP="$(mktemp -d)"
mount -o loop "$IMG" "$MP"
mkdir -p "$MP/loot" "$MP/tools"
cp -rn "$SKEL"/. "$MP/" &> /dev/null || true
chmod -R u+rwX "$MP" 2>/dev/null || true
sync
umount "$MP"
rmdir "$MP"
echo "[*] udisk image ready: $IMG"