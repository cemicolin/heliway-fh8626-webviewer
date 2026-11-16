#!/usr/bin/env python3
"""
pull_camera_info.py — Downloads `/app/vs_play` from 172.16.10.1 using either
the camera's own FTP server (after checking `/etc/inetd.conf`) or BusyBox `ftpput`,
then extracts all protocol strings locally in Python.
"""

from __future__ import annotations

import ftplib
import os
import re
import socket
import threading
import time

HOST = "172.16.10.1"
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "camera_dump")
os.makedirs(OUT_DIR, exist_ok=True)


def detect_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((HOST, 8080))
        return s.getsockname()[0]
    except Exception:
        return "172.16.10.2"
    finally:
        s.close()


LOCAL_IP = detect_local_ip()


def telnet_session() -> tuple[socket.socket, callable]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)
    s.connect((HOST, 23))

    def recv_all(wait: float = 0.5) -> str:
        s.settimeout(wait)
        buf = bytearray()
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                c = s.recv(8192)
                if not c:
                    break
                i = 0
                while i < len(c):
                    if c[i] == 0xFF and i + 2 < len(c):
                        i += 3
                    else:
                        buf.append(c[i])
                        i += 1
            except socket.timeout:
                break
        return buf.decode("latin1", errors="replace")

    recv_all(0.4)
    s.sendall(b"root\r\n")
    recv_all(0.4)
    s.sendall(b"123456\r\n")
    recv_all(0.6)
    return s, recv_all


def start_pasv_ftp_receiver(out_path: str) -> tuple[threading.Event, int]:
    """Minimal PASV-only FTP server so BusyBox `ftpput` can upload `/app/vs_play`."""
    done = threading.Event()
    ctrl_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ctrl_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ctrl_srv.bind(("0.0.0.0", 0))
    ctrl_port = ctrl_srv.getsockname()[1]
    ctrl_srv.listen(1)
    ctrl_srv.settimeout(8.0)

    def worker() -> None:
        data_srv = None
        try:
            conn, _ = ctrl_srv.accept()
            conn.settimeout(5.0)
            conn.sendall(b"220 ready\r\n")
            while True:
                line = conn.recv(1024).decode("ascii", errors="ignore").strip()
                if not line:
                    break
                cmd = line.upper()
                if cmd.startswith("USER"):
                    conn.sendall(b"331 ok\r\n")
                elif cmd.startswith("PASS"):
                    conn.sendall(b"230 ok\r\n")
                elif cmd.startswith("TYPE"):
                    conn.sendall(b"200 ok\r\n")
                elif cmd.startswith("PASV"):
                    data_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    data_srv.bind(("0.0.0.0", 0))
                    dp = data_srv.getsockname()[1]
                    data_srv.listen(1)
                    data_srv.settimeout(5.0)
                    p1, p2 = dp >> 8, dp & 0xFF
                    ip_parts = LOCAL_IP.replace(".", ",")
                    conn.sendall(f"227 Entering Passive Mode ({ip_parts},{p1},{p2})\r\n".encode("ascii"))
                elif cmd.startswith("STOR"):
                    conn.sendall(b"150 ok\r\n")
                    if data_srv:
                        dconn, _ = data_srv.accept()
                        dconn.settimeout(3.0)
                        with open(out_path, "wb") as f:
                            while True:
                                chunk = dconn.recv(32768)
                                if not chunk:
                                    break
                                f.write(chunk)
                        dconn.close()
                    conn.sendall(b"226 done\r\n")
                elif cmd.startswith("QUIT"):
                    conn.sendall(b"221 bye\r\n")
                    break
                else:
                    conn.sendall(b"200 ok\r\n")
            conn.close()
        except Exception:
            pass
        finally:
            if data_srv:
                data_srv.close()
            ctrl_srv.close()
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    return done, ctrl_port


def extract_strings_from_binary(bin_path: str) -> None:
    with open(bin_path, "rb") as f:
        data = f.read()

    raw_strings = re.findall(rb"[ -~]{4,}", data)
    interesting: list[str] = []
    keywords = (
        "udp", "tcp", "8080", "8888", "8626", "8090", "7070", "554",
        "rtsp", "drone", "720p", "h264", "jpeg", "mjpg", "tty", "uart",
        "serial", "car", "bv", "cmd", "recv", "send", "socket", "bind",
        "172.16", "vison", "play", "heart", "live", "stream", "frame",
        "192.168", "http", "port", "0x", "itd",
    )
    for b in raw_strings:
        st = b.decode("ascii", errors="ignore")
        low = st.lower()
        if any(k in low for k in keywords):
            interesting.append(st)

    out_txt = os.path.join(OUT_DIR, "vs_play_extracted_strings.txt")
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(interesting))
    print(f"[+] Extracted {len(interesting)} protocol strings -> {out_txt}")


def main() -> None:
    s, recv_all = telnet_session()

    # Check inetd.conf so we know what FTP on port 21 is serving
    s.sendall(b"cat /etc/inetd.conf\r\n")
    inetd = recv_all(0.6)
    with open(os.path.join(OUT_DIR, "inetd_conf.txt"), "w", encoding="utf-8") as f:
        f.write(inetd)

    vs_play_local = os.path.join(OUT_DIR, "vs_play")

    # Method 1: Try BusyBox ftpput to our local PASV FTP receiver
    print("[*] Attempting download via BusyBox ftpput...")
    done, ctrl_port = start_pasv_ftp_receiver(vs_play_local)
    s.sendall(f"ftpput -P {ctrl_port} {LOCAL_IP} vs_play /app/vs_play\r\n".encode("ascii"))
    done.wait(timeout=8.0)
    recv_all(0.5)

    # Method 2: Fallback via camera's own FTP on port 21 (if inetd serves /sdcard or /tmp)
    if not (os.path.exists(vs_play_local) and os.path.getsize(vs_play_local) > 10000):
        print("[*] Trying camera FTP on port 21...")
        s.sendall(b"cp /app/vs_play /sdcard/vs_play 2>/dev/null; cp /app/vs_play /tmp/vs_play 2>/dev/null\r\n")
        recv_all(0.5)
        for user, pwd in [("root", "123456"), ("anonymous", "")]:
            try:
                ftp = ftplib.FTP(HOST, timeout=3)
                ftp.login(user, pwd)
                for remote in ("vs_play", "/app/vs_play", "/tmp/vs_play", "/sdcard/vs_play"):
                    try:
                        with open(vs_play_local, "wb") as f:
                            ftp.retrbinary(f"RETR {remote}", f.write)
                        if os.path.getsize(vs_play_local) > 10000:
                            print(f"[+] Downloaded {remote} via FTP ({os.path.getsize(vs_play_local)} bytes)")
                            break
                    except Exception:
                        pass
                ftp.quit()
                if os.path.exists(vs_play_local) and os.path.getsize(vs_play_local) > 10000:
                    break
            except Exception as e:
                print(f"    FTP {user} error: {e}")

    s.close()

    if os.path.exists(vs_play_local) and os.path.getsize(vs_play_local) > 10000:
        print(f"[+] Successfully downloaded /app/vs_play ({os.path.getsize(vs_play_local)} bytes)")
        extract_strings_from_binary(vs_play_local)
    else:
        print("[!] Could not download /app/vs_play; check camera_dump/inetd_conf.txt")


if __name__ == "__main__":
    main()
