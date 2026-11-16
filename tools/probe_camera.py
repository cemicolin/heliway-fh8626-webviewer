#!/usr/bin/env python3
"""
probe_camera.py — Automated Discovery & Protocol Probe for Odyssey / Heliway (Vison VS-H62 + Fullhan FH8626V100 + Marvell 88W8801) Wi-Fi FPV Camera.

Usage:
  1. Run: python probe_camera.py --wait
  2. Switch your PC's Wi-Fi to the RC camera's Wi-Fi network (e.g., VS-FPV-xxx, Heliway-xxx, WiFi-xxx).
  3. The script will automatically detect the network switch, probe the camera, capture sample video packets,
     save `probe_report.json` + `probe_capture.bin`, and tell you when you can switch back to your home Wi-Fi.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
from typing import Any

# 106-byte Vison (VS FPV / FH8626 / 172.16.10.1:8888) TCP magic initialization sequence ("ITd...")
VISON_MAGIC_HANDSHAKE = bytes.fromhex(
    "495464000000580000009bf89049c926884d4f922b3b33ba7eceacef63f7"
    "7157ab2f53e3f768ecd9e18547b8c22e21d01bfb6b3de325a27b8fb3acef63"
    "f77157ab2f53e3f768ecd9e185eb20be383aab05a8c2a71f2c906d93f72a85"
    "e7356effe1b8f5af097f9147f87e"
)

# Short Vison / Lewei / Jieli wake-up payloads
ALT_HANDSHAKES: dict[str, bytes] = {
    "vison_magic_106": VISON_MAGIC_HANDSHAKE,
    "vison_query_0f": b"\x0f",
    "vison_udp_bv": b"\x42\x76",
    "vison_udp_ef": b"\xef\x00\x04\x00",
    "vison_init_3066": b"\x30\x66",
    "jieli_heartbeat_0101": b"\x01\x01",
    "lewei_cmd_header": b"lewei_cmd\x00",
}

COMMON_CAMERA_IPS = [
    "172.16.10.1",
    "192.168.1.1",
    "192.168.0.1",
    "192.168.4.1",
    "192.168.80.1",
    "192.168.100.1",
]

TCP_PORTS_TO_SCAN = [23, 80, 554, 5000, 7070, 8080, 8554, 8800, 8888, 9060]
UDP_PORTS_TO_SCAN = [5000, 7070, 7099, 8080, 8800, 8888, 50000]


def get_network_interfaces() -> list[dict[str, str]]:
    """Parse `ipconfig` on Windows to find active IPv4 addresses and Default Gateways."""
    interfaces: list[dict[str, str]] = []
    try:
        out = subprocess.check_output(["ipconfig"], text=True, errors="ignore")
    except Exception:
        return interfaces

    current_adapter = "Unknown"
    current_ip = ""
    current_gw = ""

    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not raw_line.startswith(" ") and line.endswith(":"):
            if current_ip:
                interfaces.append({
                    "adapter": current_adapter,
                    "ip": current_ip,
                    "gateway": current_gw,
                })
            current_adapter = line[:-1].strip()
            current_ip = ""
            current_gw = ""
        elif "IPv4 Address" in line or "IP Address" in line:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                current_ip = m.group(1)
        elif "Default Gateway" in line:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                current_gw = m.group(1)

    if current_ip:
        interfaces.append({
            "adapter": current_adapter,
            "ip": current_ip,
            "gateway": current_gw,
        })
    return interfaces


def scan_tcp_port(ip: str, port: int, timeout: float = 0.6) -> dict[str, Any]:
    """Check if a TCP port is open and grab any immediate banner."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.monotonic()
    try:
        s.connect((ip, port))
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        banner = b""
        try:
            s.settimeout(0.35)
            banner = s.recv(256)
        except socket.timeout:
            pass
        return {
            "port": port,
            "open": True,
            "latency_ms": latency_ms,
            "banner_hex": banner.hex(),
            "banner_ascii": banner.decode("latin1", errors="replace") if banner else "",
        }
    except Exception as e:
        return {"port": port, "open": False, "error": str(e)}
    finally:
        s.close()


def probe_udp_port(ip: str, port: int, timeout: float = 0.45) -> dict[str, Any]:
    """Send known discovery datagrams to a UDP port and capture any response."""
    results: list[dict[str, Any]] = []
    for name, payload in ALT_HANDSHAKES.items():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(payload, (ip, port))
            data, addr = sock.recvfrom(4096)
            results.append({
                "handshake": name,
                "response_len": len(data),
                "from": f"{addr[0]}:{addr[1]}",
                "response_hex_prefix": data[:64].hex(),
                "response_ascii_prefix": data[:64].decode("latin1", errors="replace"),
            })
        except socket.timeout:
            pass
        except Exception as e:
            results.append({"handshake": name, "error": str(e)})
        finally:
            sock.close()
    return {"port": port, "responses": results}


def analyze_stream_bytes(buf: bytes) -> dict[str, Any]:
    """Inspect captured stream bytes for H.264 NAL units, JPEG frames, or Vison headers."""
    info: dict[str, Any] = {
        "total_bytes": len(buf),
        "first_64_hex": buf[:64].hex(),
        "first_64_ascii": buf[:64].decode("latin1", errors="replace"),
        "contains_jpeg_soi": b"\xff\xd8\xff" in buf,
        "contains_jpeg_eoi": b"\xff\xd9" in buf,
        "contains_h264_annexb_4": b"\x00\x00\x00\x01" in buf,
        "contains_h264_annexb_3": b"\x00\x00\x01" in buf,
        "contains_vison_itd": b"ITd" in buf,
        "h264_nal_types": [],
    }

    # Find H.264 NAL unit types if Annex-B start codes exist
    nal_types_seen: set[str] = set()
    idx = 0
    while True:
        pos = buf.find(b"\x00\x00\x00\x01", idx)
        if pos == -1 or pos + 4 >= len(buf):
            break
        nal_byte = buf[pos + 4]
        nal_type = nal_byte & 0x1F
        nal_names = {
            1: "Non-IDR Slice (1)",
            5: "IDR Keyframe (5)",
            6: "SEI (6)",
            7: "SPS (7)",
            8: "PPS (8)",
            9: "AUD (9)",
        }
        nal_types_seen.add(nal_names.get(nal_type, f"Type {nal_type} (0x{nal_byte:02x})"))
        idx = pos + 4

    info["h264_nal_types"] = sorted(nal_types_seen)

    # Extract a sample JPEG if present
    soi = buf.find(b"\xff\xd8\xff")
    if soi != -1:
        eoi = buf.find(b"\xff\xd9", soi + 3)
        if eoi != -1:
            jpeg_bytes = buf[soi : eoi + 2]
            sample_jpg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_frame.jpg")
            with open(sample_jpg_path, "wb") as f:
                f.write(jpeg_bytes)
            info["extracted_sample_jpeg"] = sample_jpg_path
            info["extracted_sample_jpeg_bytes"] = len(jpeg_bytes)

    return info


def probe_vison_tcp_stream(ip: str, port: int = 8888, duration_sec: float = 3.5) -> dict[str, Any]:
    """Connect to TCP port (default 8888), send Vison 106-byte magic handshake, and capture stream."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    captured = bytearray()
    chunks_received = 0
    try:
        s.connect((ip, port))
        s.sendall(VISON_MAGIC_HANDSHAKE)
        s.settimeout(0.5)
        deadline = time.monotonic() + duration_sec
        while time.monotonic() < deadline and len(captured) < 512 * 1024:
            try:
                chunk = s.recv(16384)
                if not chunk:
                    break
                captured.extend(chunk)
                chunks_received += 1
            except socket.timeout:
                if captured:
                    break
    except Exception as e:
        return {"port": port, "success": False, "error": str(e)}
    finally:
        s.close()

    if not captured:
        return {"port": port, "success": False, "chunks_received": 0, "total_bytes": 0}

    capture_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"probe_capture_{port}.bin")
    with open(capture_path, "wb") as f:
        f.write(captured)

    analysis = analyze_stream_bytes(bytes(captured))
    return {
        "port": port,
        "success": True,
        "chunks_received": chunks_received,
        "capture_file": capture_path,
        "analysis": analysis,
    }


def probe_rtsp(ip: str, port: int) -> dict[str, Any]:
    """Send an RTSP OPTIONS request to check if an RTSP server (common on Fullhan FH8626) is listening."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect((ip, port))
        req = (
            f"OPTIONS rtsp://{ip}:{port}/ RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"User-Agent: rcamera-probe/1.0\r\n\r\n"
        ).encode("ascii")
        s.sendall(req)
        resp = s.recv(2048)
        return {
            "port": port,
            "responded": bool(resp),
            "response": resp.decode("latin1", errors="replace"),
        }
    except Exception as e:
        return {"port": port, "responded": False, "error": str(e)}
    finally:
        s.close()


def probe_http(ip: str, port: int) -> dict[str, Any]:
    """Send a basic HTTP GET / request to check for embedded web servers or MJPEG streams."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect((ip, port))
        req = f"GET / HTTP/1.0\r\nHost: {ip}\r\n\r\n".encode("ascii")
        s.sendall(req)
        resp = s.recv(2048)
        return {
            "port": port,
            "responded": bool(resp),
            "response_prefix": resp[:512].decode("latin1", errors="replace"),
        }
    except Exception as e:
        return {"port": port, "responded": False, "error": str(e)}
    finally:
        s.close()


def probe_udp_video_stream(ip: str, port: int = 8080, duration_sec: float = 2.0) -> dict[str, Any]:
    """Send Bv (0x42 0x76) unlock + UDP720P / Vison video triggers and capture any resulting stream."""
    triggers: list[tuple[str, bytes]] = [
        ("bv_4276", b"\x42\x76"),
        ("ef_000400", b"\xef\x00\x04\x00"),
        ("ef_020800", b"\xef\x02\x08\x00"),
        ("cmd_20", b"\x20"),
        ("cmd_0101", b"\x01\x01"),
        ("init_3066", b"\x30\x66"),
        ("vison_magic_106", VISON_MAGIC_HANDSHAKE),
        ("query_0f", b"\x0f"),
    ]
    # First send Bv unlock to 8080
    unlock_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        unlock_sock.sendto(b"\x42\x76", (ip, 8080))
    except Exception:
        pass
    finally:
        unlock_sock.close()

    for name, payload in triggers:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        captured = bytearray()
        packets = 0
        try:
            sock.sendto(payload, (ip, port))
            deadline = time.monotonic() + duration_sec
            while time.monotonic() < deadline and packets < 150:
                try:
                    data, _ = sock.recvfrom(65535)
                    packets += 1
                    captured.extend(data)
                except socket.timeout:
                    break
            if packets >= 3 or len(captured) > 100:
                capture_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    f"probe_udp_capture_{port}_{name}.bin",
                )
                with open(capture_path, "wb") as f:
                    f.write(captured)
                return {
                    "port": port,
                    "trigger": name,
                    "success": True,
                    "packets": packets,
                    "capture_file": capture_path,
                    "analysis": analyze_stream_bytes(bytes(captured)),
                }
        except Exception:
            pass
        finally:
            sock.close()
    return {"port": port, "success": False, "packets": 0}


def probe_telnet(ip: str, port: int = 23) -> dict[str, Any]:
    """Connect to Telnet port 23, try default Fullhan credentials, and run read-only `ps` + `netstat`."""
    creds = [
        ("user", "user123456"),
        ("root", "123456"),
        ("root", ""),
        ("root", "admin"),
        ("admin", "admin123456"),
    ]
    last_banner = ""
    for user, pwd in creds:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect((ip, port))
            time.sleep(0.35)
            initial = b""
            try:
                initial = s.recv(2048)
            except socket.timeout:
                pass
            if b"\xff" in initial:
                s.sendall(b"\xff\xfc\x01\xff\xfc\x1f\xff\xfe\x01\xff\xfe\x03")
                time.sleep(0.2)
            s.sendall(b"\r\n")
            time.sleep(0.3)
            try:
                initial += s.recv(2048)
            except socket.timeout:
                pass
            last_banner = initial.decode("latin1", errors="replace")

            if "login:" in last_banner.lower():
                s.sendall(user.encode("ascii") + b"\r\n")
                time.sleep(0.4)
                resp = s.recv(2048).decode("latin1", errors="replace")
                if "password:" in resp.lower():
                    s.sendall(pwd.encode("ascii") + b"\r\n")
                    time.sleep(0.6)
                    resp = s.recv(2048).decode("latin1", errors="replace")
                if any(p in resp for p in ("#", "$", "~", "/")) and "incorrect" not in resp.lower():
                    s.sendall(b"ps; netstat -an 2>/dev/null; ls /app 2>/dev/null\r\n")
                    time.sleep(0.8)
                    shell_out = s.recv(8192).decode("latin1", errors="replace")
                    return {
                        "port": port,
                        "login_used": f"{user}:{pwd}",
                        "banner_text": last_banner,
                        "shell_output": shell_out,
                    }
        except Exception as e:
            return {"port": port, "error": str(e)}
        finally:
            s.close()
    return {
        "port": port,
        "banner_text": last_banner,
        "shell_output": "",
        "note": "Default credentials did not unlock shell",
    }


def run_full_probe(target_ip: str | None = None) -> dict[str, Any]:
    interfaces = get_network_interfaces()
    print("\n=== Active Network Interfaces ===")
    for iface in interfaces:
        print(f"  - {iface['adapter']}: IP={iface['ip']}  Gateway={iface['gateway']}")

    candidate_ips: list[str] = []
    if target_ip:
        candidate_ips.append(target_ip)
    for iface in interfaces:
        gw = iface.get("gateway", "")
        if gw and gw not in candidate_ips:
            candidate_ips.append(gw)
    for ip in COMMON_CAMERA_IPS:
        if ip not in candidate_ips:
            candidate_ips.append(ip)

    report: dict[str, Any] = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "interfaces": interfaces,
        "probed_hosts": {},
    }

    for ip in candidate_ips:
        print(f"\n[*] Scanning candidate camera IP: {ip} ...")
        open_tcp: list[dict[str, Any]] = []
        for port in TCP_PORTS_TO_SCAN:
            res = scan_tcp_port(ip, port, timeout=0.45)
            if res.get("open"):
                print(f"    [+] TCP {ip}:{port} is OPEN! (latency {res['latency_ms']} ms)")
                open_tcp.append(res)

        udp_findings: list[dict[str, Any]] = []
        # Always check UDP 8080 and 8888 on the primary gateway or if any TCP port is open
        is_active_gw = any(iface.get("gateway") == ip for iface in interfaces)
        if open_tcp or is_active_gw or ip == target_ip:
            for port in UDP_PORTS_TO_SCAN:
                u_res = probe_udp_port(ip, port, timeout=0.35)
                if u_res["responses"]:
                    print(f"    [+] UDP {ip}:{port} responded: {u_res['responses']}")
                    udp_findings.append(u_res)

        stream_probes: list[dict[str, Any]] = []
        udp_stream_probes: list[dict[str, Any]] = []
        telnet_info: dict[str, Any] | None = None
        rtsp_probes: list[dict[str, Any]] = []
        http_probes: list[dict[str, Any]] = []

        for tcp_info in open_tcp:
            p = tcp_info["port"]
            if p == 23:
                print(f"    [*] Inspecting Telnet on {ip}:23 ...")
                telnet_info = probe_telnet(ip, 23)
            if p in (8888, 8800, 9060, 5000):
                print(f"    [*] Sending Vison 106-byte magic handshake to TCP {ip}:{p} ...")
                sp = probe_vison_tcp_stream(ip, p)
                stream_probes.append(sp)
                if sp.get("success"):
                    print(f"    [!] CAPTURED TCP VIDEO STREAM on {ip}:{p}! ({sp['analysis']['total_bytes']} bytes)")
                    print(f"        Analysis: {json.dumps(sp['analysis'], indent=2)}")
            if p in (554, 7070, 8554):
                rp = probe_rtsp(ip, p)
                rtsp_probes.append(rp)
                if rp.get("responded"):
                    print(f"    [+] RTSP response on {ip}:{p}: {rp['response'].splitlines()[0]}")
            if p in (80, 8080):
                hp = probe_http(ip, p)
                http_probes.append(hp)

        if open_tcp or udp_findings or is_active_gw:
            for udp_p in (8080, 8888, 8800, 7070):
                print(f"    [*] Probing UDP video triggers on {ip}:{udp_p} ...")
                usp = probe_udp_video_stream(ip, udp_p)
                udp_stream_probes.append(usp)
                if usp.get("success"):
                    print(f"    [!] CAPTURED UDP VIDEO STREAM on {ip}:{udp_p} ({usp['trigger']})! ({usp['analysis']['total_bytes']} bytes)")
                    print(f"        Analysis: {json.dumps(usp['analysis'], indent=2)}")

            report["probed_hosts"][ip] = {
                "is_active_gateway": is_active_gw,
                "open_tcp_ports": open_tcp,
                "udp_responses": udp_findings,
                "vison_stream_probes": stream_probes,
                "udp_stream_probes": udp_stream_probes,
                "telnet_info": telnet_info,
                "rtsp_probes": rtsp_probes,
                "http_probes": http_probes,
            }
            if open_tcp or udp_findings:
                # Found the active camera!
                break

    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\n[+] Saved full diagnostic report to: {report_path}")
    return report


def wait_for_camera_wifi(home_gateway: str | None = None, timeout_sec: int = 300) -> str | None:
    if home_gateway is None:
        # Capture current primary gateway before waiting for Wi-Fi switch
        for iface in get_network_interfaces():
            if iface.get("gateway"):
                home_gateway = iface["gateway"]
                break

    print("======================================================================")
    print(" WAITING FOR CAMERA WI-FI CONNECTION")
    print("======================================================================")
    print("1. Power on the camera (via your Pi Zero's USB port or the RC car).")
    print("2. Connect this PC's Wi-Fi to the camera's hotspot.")
    print("   (Wait ~5-10 seconds after connecting; this script will auto-detect it!)")
    print("======================================================================\n")

    t0 = time.monotonic()
    last_status = ""
    while time.monotonic() - t0 < timeout_sec:
        interfaces = get_network_interfaces()
        for iface in interfaces:
            gw = iface.get("gateway", "")
            ip = iface.get("ip", "")
            if ip.startswith(("172.16.10.", "192.168.4.", "192.168.80.", "192.168.100.")):
                guessed_gw = gw or (".".join(ip.split(".")[:3]) + ".1")
                print(f"\n[!] Detected camera subnet IP={ip} on {iface['adapter']}! Using Gateway={guessed_gw}")
                time.sleep(1.0)
                return guessed_gw
            if gw and home_gateway and gw != home_gateway:
                print(f"\n[!] Detected new Wi-Fi network on {iface['adapter']}! IP={ip}, Gateway={gw}")
                time.sleep(1.0)
                return gw

        elapsed = int(time.monotonic() - t0)
        status = f"\r[Waiting {elapsed}s/{timeout_sec}s] Still on home network ({home_gateway})..."
        if status != last_status:
            sys.stdout.write(status)
            sys.stdout.flush()
            last_status = status
        time.sleep(1.5)

    print("\n[!] Timed out waiting for Wi-Fi network change.")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Odyssey/Heliway (Vison + Fullhan FH8626V100) Wi-Fi FPV Camera")
    parser.add_argument("--ip", help="Specific camera IP to probe (e.g. 172.16.10.1)")
    parser.add_argument("--wait", action="store_true", help="Wait for Wi-Fi switch from home network to camera AP")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout in seconds when using --wait")
    args = parser.parse_args()

    target_ip = args.ip
    if args.wait and not target_ip:
        target_ip = wait_for_camera_wifi(timeout_sec=args.timeout)

    run_full_probe(target_ip=target_ip)
    print("\n======================================================================")
    print(" PROBE COMPLETE! You can now switch your Wi-Fi back to your home network.")
    print("======================================================================")


if __name__ == "__main__":
    main()
