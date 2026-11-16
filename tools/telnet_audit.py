#!/usr/bin/env python3
"""
telnet_audit.py — Non-destructive Telnet audit for Fullhan FH8626V100 camera (172.16.10.1:23).
Tries common Fullhan/BusyBox credentials, runs read-only inspection commands,
and saves the output to `telnet_audit.txt`.
"""

from __future__ import annotations

import os
import socket
import time

HOST = "172.16.10.1"
PORT = 23

CREDENTIALS = [
    ("user", "user123456"),
    ("root", "123456"),
    ("root", ""),
    ("root", "admin"),
    ("root", "admin123456"),
    ("admin", "admin123456"),
    ("root", "888888"),
    ("root", "vison"),
    ("root", "fh8626"),
    ("root", "heliway"),
    ("root", "xc3511"),
    ("root", "vizxv"),
]


def recv_available(sock: socket.socket, wait: float = 0.5) -> str:
    sock.settimeout(wait)
    buf = bytearray()
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            # Strip Telnet IAC sequences (0xFF xx yy)
            i = 0
            while i < len(chunk):
                if chunk[i] == 0xFF and i + 2 < len(chunk):
                    i += 3
                else:
                    buf.append(chunk[i])
                    i += 1
        except socket.timeout:
            break
    return buf.decode("latin1", errors="replace")


def try_login(user: str, pwd: str) -> tuple[bool, str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect((HOST, PORT))
        banner = recv_available(s, 0.5)
        if "login:" not in banner.lower():
            s.sendall(b"\r\n")
            banner += recv_available(s, 0.5)

        if "login:" not in banner.lower() and any(p in banner for p in ("#", "$", "/ #")):
            # Already logged in without auth!
            return True, run_audit_commands(s, banner)

        s.sendall(user.encode("ascii") + b"\r\n")
        resp = recv_available(s, 0.6)
        if "password:" in resp.lower():
            s.sendall(pwd.encode("ascii") + b"\r\n")
            resp = recv_available(s, 0.8)

        if any(p in resp for p in ("#", "$", "~", "/")) and "incorrect" not in resp.lower() and "login:" not in resp.lower():
            return True, run_audit_commands(s, banner + "\n" + resp)
        return False, resp
    except Exception as e:
        return False, str(e)
    finally:
        s.close()


def run_audit_commands(s: socket.socket, header: str) -> str:
    commands = [
        "uname -a",
        "ps",
        "netstat -an 2>/dev/null || cat /proc/net/tcp /proc/net/udp",
        "ls -la / /app /mnt /etc/init.d 2>/dev/null",
        "cat /etc/init.d/rcS 2>/dev/null",
        "cat /etc/passwd 2>/dev/null",
    ]
    out = [f"=== BANNER ===\n{header.strip()}\n"]
    for cmd in commands:
        s.sendall(cmd.encode("ascii") + b"\r\n")
        res = recv_available(s, 0.8)
        out.append(f"=== CMD: {cmd} ===\n{res.strip()}\n")
    return "\n".join(out)


def main() -> None:
    print(f"[*] Auditing Telnet on {HOST}:{PORT} ...")
    for user, pwd in CREDENTIALS:
        print(f"    Trying {user}:{pwd!r} ...", end=" ", flush=True)
        ok, output = try_login(user, pwd)
        if ok:
            print("SUCCESS!")
            out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telnet_audit.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(f"Login: {user}:{pwd}\n\n{output}\n")
            print(f"[+] Saved full audit to {out_path}")
            return
        print("failed")

    print("\n[!] None of the common default credentials matched.")
    print("    We can still capture the exact video stream via `python probe_camera.py --ip 172.16.10.1`")
    print("    or use a microSD card with `upgrade/iu.sh` if we want root shell access.")


if __name__ == "__main__":
    main()
