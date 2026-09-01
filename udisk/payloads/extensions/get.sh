#!/bin/bash

function GET() {
  case $1 in
    "TARGET_IP")
      # PiBunny hands addresses out with dnsmasq (ATTACKMODE *_ETHERNET);
      # leases are at /var/lib/misc/dnsmasq.leases with
      # fields: <epoch> <mac> <ip> <hostname> <client-id>.
      if [ -s /var/lib/misc/dnsmasq.leases ]; then
        export TARGET_IP=$(awk '{print $3}' /var/lib/misc/dnsmasq.leases | sort -u | paste -sd,)
      else
        export TARGET_IP=$(cat /var/lib/dhcp/dhcpd.leases | grep ^lease | awk '{ print $2 }' | sort | uniq)
      fi
      ;;
    "TARGET_HOSTNAME")
      if [ -s /var/lib/misc/dnsmasq.leases ]; then
        export TARGET_HOSTNAME=$(awk '{print $4}' /var/lib/misc/dnsmasq.leases | sort -u | tail -n1 | tr -d '"')
      else
        export TARGET_HOSTNAME=$(cat /var/lib/dhcp/dhcpd.leases | grep hostname | awk '{print $2 }' | sort | uniq | tail -n1 | sed "s/^[ \t]*//" | sed 's/\"//g' | sed 's/;//')
      fi
      ;;
    "HOST_IP")
      export HOST_IP=$(cat /etc/network/interfaces.d/usb0 | grep address | awk {'print $2'})
      ;;
    "SWITCH_POSITION")
      # PiBunny: read the switch position that was active at boot
      SWITCH_POSITION=$(cat /run/bunny/switch 2>/dev/null)
      export SWITCH_POSITION="$SWITCH_POSITION"
      ;;
    "TARGET_OS")
      GET TARGET_IP
      ScanForOS=$(nmap -Pn -O $TARGET_IP -p1 -v2 2>/dev/null)
      [[ $ScanForOS == *"Too many fingerprints"* ]] && ScanForOS=$(nmap -Pn -O $TARGET_IP --osscan-guess -v2 2>/dev/null)
      [[ "${ScanForOS,,}" == *"windows"* ]] && export TARGET_OS='WINDOWS' && return
      [[ "${ScanForOS,,}" == *"apple"* ]] && export TARGET_OS='MACOS' && return
      [[ "${ScanForOS,,}" == *"linux"* ]] && export TARGET_OS='LINUX' && return
      export TARGET_OS='UNKNOWN'
      ;;
  esac
}

export -f GET