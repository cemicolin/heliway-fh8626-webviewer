#!/usr/bin/env python3
"""
test_vslcam.py — Sends the exact `0x28 0x28` (`b"((\\x00"`) stream-start command
decoded from `vs_play` (`0xa530 -> 0x983c -> 0xaa30`) on TCP 8888.
"""

from __future__ import annotations

import os
import socket
import time

IP = "172.16.10.1"


def main() -> None:
    # Optional UDP 8080 wake-up first
    usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    usock.settimeout(0.3)
    for cmd in (b"\x42\x76", b"\x0f", b"\x01"):
        usock.sendto(cmd, (IP, 8080))
        try:
            usock.recvfrom(1024)
        except socket.timeout:
            pass
    usock.close()

    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.settimeout(2.0)
    tcp.connect((IP, 8888))
    print("[+] Connected to TCP 8888, sending 0x28 0x28 ('((') stream-start...")
    tcp.sendall(b"((\x00")

    tcp.settimeout(1.0)
    buf = bytearray()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.5 and len(buf) < 512 * 1024:
        try:
            chunk = tcp.recv(32768)
            if not chunk:
                break
            buf.extend(chunk)
        except socket.timeout:
            if buf:
                break
    tcp.close()

    print(f"[TCP 8888] Received {len(buf)} bytes | first 64 bytes hex:\n  {bytes(buf[:64]).hex()}")
    if len(buf) > 0:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture_stream.bin")
        with open(out, "wb") as f:
            f.write(buf)
        print(f"[+] Saved stream capture to: {out}")


if __name__ == "__main__":
    main()
