# heliway-fh8626-webviewer

Local FPV Web Viewer, Recorder & Controller for Odyssey / Heliway Wi-Fi RC Cameras.

Revive dead-app Wi-Fi FPV RC car & drone cameras (**Odyssey Toys Spy Rover FPV**, **Heliway `HLW1801A`**, and compatible **Vison `vs_play` / Fullhan `FH8626V100`** modules) directly from your browser with **zero external dependencies**.

- **Zero Python dependencies:** Pure Python 3.10+ standard library backend + HTML5 `<canvas>` and hardware-accelerated **WebCodecs H.264 (`1280×720`)** decoding in Chrome/Edge.
- **Cross-platform:** Works on Windows, Linux, and macOS (and can run headless on a multi-interface host like a Raspberry Pi with Ethernet + Wi-Fi using `--host 0.0.0.0`).
- **Features:**
  - Live 720p H.264 FPV feed (~23–30 FPS, ~2 Mbps) with automatic `b"((\x00"` keep-alive
  - Automatic 180° flip toggle (for upside-down roof-mounted sensors reporting `mirror=1`)
  - One-click **PNG snapshots**, **browser `.webm` video recording**, and **clean `.h264` elementary stream recording**
  - Optional **WASD / Arrow-key UDP drive controls** (for vehicles wired with the 3rd UART wire)

---

## Supported Hardware

Tested on:
- **Vehicle:** Odyssey Toys *Spy Rover FPV* / Heliway `HLW1801A` (`FCC ID: 2AG7SHLW1801A`)
- **Main SoC:** Fullhan `FH8626V100` (`UF5858-4`)
- **Image Sensor:** Vison `VS-H62` (SOI `JX-H62` 720p CMOS)
- **Wi-Fi Module:** LB-Link `BL-R8801MS1 V1.3` (Marvell `88W8801` 2.4 GHz SDIO)

> **Tip — Bench-Powering Without the RC Car (Male Micro-USB Cable):**
> The camera's attached male Micro-USB cable has only **3 wires** (`+5V` Red, `GND` Black, `UART TX` Blue), so it does **not** enumerate as a USB webcam. However, `+5V` and `GND` match standard Micro-USB power pins. If your car battery is charging or removed, you can power the camera by plugging its male Micro-USB cable into any female Micro-USB power source—for example, plugging a power supply into a **Raspberry Pi Zero's `PWR IN`** port and plugging the camera into the Pi Zero's **`USB`** port (no SD card needed, since the Pi Zero shares `5V`/`GND` across both ports).

---

## Quick Start

1. **Power on the RC car / camera module** and wait ~3 seconds for its Wi-Fi hotspot to appear.
2. **Connect your computer's Wi-Fi** to the camera's network (e.g., `Heliway-...`, `VSLCAM-...`, `VS-FPV-...`).
   > **Network Note:** This is a direct peer-to-peer Wi-Fi link to `172.16.10.1` (ignore any "No Internet" warning). On a **Wi-Fi-only** laptop/PC, you will lose normal Wi-Fi internet while connected and driving the car. Any machine with **both Ethernet and Wi-Fi** (a wired PC, or the headless Raspberry Pi bridge below) can stay online over Ethernet while Wi-Fi stays connected to the camera.
3. **Start the FPV station:**
   ```bash
   python rcamera_app.py
   ```
4. Open **`http://127.0.0.1:8000`** in **Chrome** or **Edge** (WebCodecs H.264 support required).

### CLI Options
```text
python rcamera_app.py [--ip 172.16.10.1] [--host 127.0.0.1] [--port 8000] [--ssl] [--no-browser]
```
- **`--ip`**: Manually specify camera IP (defaults to auto-detection / `172.16.10.1`).
- **`--host 0.0.0.0 --ssl --no-browser`**: Bind to all interfaces over HTTPS (auto-generates a self-signed cert in `certs/` via OpenSSL). Useful on a dual-interface headless host—e.g., a Raspberry Pi B+ with Ethernet + USB Wi-Fi—because remote LAN browsers require HTTPS (or an SSH tunnel to `http://127.0.0.1:8000`) for WebCodecs H.264 decoding.
- **`--cert` / `--key`**: Use your own TLS certificate and key PEM files instead of auto-generating.
- **`--port`**: Change local web UI port (default `8000`).

### Headless Raspberry Pi Bridge (Ethernet + Wi-Fi, or Dual Wi-Fi)
If you want to keep your PC on your home network while viewing the camera (or place a wireless Pi bridge in the room where you drive):
1. **Interface setup:**
   - **Ethernet + Wi-Fi (e.g., Pi B+):** Connect Ethernet to your home router and USB Wi-Fi to the camera's hotspot (`172.16.10.1`).
   - **Dual Wi-Fi (e.g., Pi 3B + USB Wi-Fi dongle):** Assign one radio (`wlan0` or `wlan1`) to your home Wi-Fi (ideally **5 GHz** on a dual-band dongle to avoid 2.4 GHz interference) and the other to the camera (`172.16.10.1`).
2. **Keep the default route on your home network** (the camera's DHCP advertises `172.16.10.1` as a gateway; on Raspberry Pi OS Bookworm / NetworkManager, pin the camera interface and disable its default route):
   ```bash
   nmcli connection modify "<Camera-SSID>" connection.interface-name wlan1 ipv4.never-default yes
   nmcli connection modify "<Home-SSID>" connection.interface-name wlan0
   ```
   *(Swap `wlan0`/`wlan1` as needed. On older `dhcpcd`-based Pi OS, add `interface wlan1` + `nogateway` in `/etc/dhcpcd.conf`.)*
3. **Install prerequisites (on Raspberry Pi OS Lite, `git` is not preinstalled) & start the server over HTTPS:**
   ```bash
   sudo apt update && sudo apt install -y git openssl
   python3 rcamera_app.py --host 0.0.0.0 --ssl --no-browser
   ```
4. Open `https://<pi-lan-ip>:8000` in Chrome/Edge (accept the self-signed certificate warning once), **or** run without `--ssl` and tunnel over SSH (`ssh -L 8000:127.0.0.1:8000 pi@<pi-lan-ip>`) to `http://127.0.0.1:8000`.

> **SD Card Wear Note:** No endurance SD card is needed. Live streaming runs entirely in RAM with zero continuous disk writes, and **Save Snapshot (PNG)** / **Record Video (`.webm`)** save directly in your client browser. Only the optional **Record Raw Stream** button writes `.h264` files to `recordings/` on the Pi.

---

## Repository Structure

```text
heliway-fh8626-webviewer/
├── rcamera_app.py          # Main cross-platform FPV web server, H.264 parser & browser UI
├── docs/
│   └── PROTOCOL.md         # Full FH8626V100 / vs_play TCP 8888 & UDP 8080 protocol spec
├── tools/                  # Optional diagnostic scripts for probing similar cameras
│   ├── probe_camera.py     # Network scanner, port prober, and stream analyzer
│   ├── test_vslcam.py      # Quick TCP 8888 (`0x28 0x28`) stream capture verification
│   ├── telnet_audit.py     # Read-only Telnet inspection helper (default Fullhan logins)
│   └── pull_camera_info.py # Extracts startup scripts / strings for protocol analysis
├── LICENSE                 # MIT License
└── .gitignore
```

---

## Security Note

Like many budget Wi-Fi FPV toy cameras, the stock firmware runs an unencrypted Wi-Fi AP with default services (`telnetd`, `ftpd`) enabled. Operate the camera only on its isolated local Wi-Fi link, and see [docs/PROTOCOL.md](docs/PROTOCOL.md) for full protocol details.
