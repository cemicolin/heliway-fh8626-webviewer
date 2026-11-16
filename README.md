# heliway-fh8626-webviewer

Local FPV Web Viewer, Recorder & Controller for Odyssey / Heliway Wi-Fi RC Cameras.

Revive dead-app Wi-Fi FPV RC car & drone cameras (**Odyssey Toys Spy Rover FPV**, **Heliway `HLW1801A`**, and compatible **Vison `vs_play` / Fullhan `FH8626V100`** modules) directly from your browser with **zero external dependencies**.

- **Zero Python dependencies:** Pure Python 3.10+ standard library backend + HTML5 `<canvas>` and hardware-accelerated **WebCodecs H.264 (`1280×720`)** decoding in Chrome/Edge.
- **Cross-platform:** Works on Windows, Linux, and macOS (and can run headless on a Raspberry Pi with `--host 0.0.0.0`).
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
   *(Ignore any "No Internet" warning—this is a direct peer-to-peer Wi-Fi link to `172.16.10.1`.)*
3. **Start the FPV station:**
   ```bash
   python rcamera_app.py
   ```
4. Open **`http://127.0.0.1:8000`** in **Chrome** or **Edge** (WebCodecs H.264 support required).

### CLI Options
```text
python rcamera_app.py [--ip 172.16.10.1] [--host 127.0.0.1] [--port 8000] [--no-browser]
```
- **`--ip`**: Manually specify camera IP (defaults to auto-detection / `172.16.10.1`).
- **`--host 0.0.0.0 --no-browser`**: Bind to all interfaces (useful when running headless on a Raspberry Pi).
- **`--port`**: Change local web UI port (default `8000`).

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
