# Hardware & Protocol Specification (`vs_play` V3.4.9 / `FH8626V100`)

This document describes the hardware architecture and reverse-engineered wire protocol for the **Heliway `HLW1801A` / Odyssey Toys Spy Rover FPV** (`FCC ID: 2AG7SHLW1801A`) Wi-Fi camera module and compatible **Vison Electronics (`vs_play`)** boards.

---

## 1. Hardware Teardown

| Component | Part / Marking | Notes |
| :--- | :--- | :--- |
| **Main SoC** | **Fullhan `FH8626V100`** (`UF5858-4`) | 88-pin QFP camera SoC running embedded BusyBox Linux (`fh-linux`) with hardware H.264/JPEG encoder |
| **Image Sensor** | **Vison `VS-H62`** (`SOI JX-H62`) | 1280×720 (720p) MIPI CMOS sensor |
| **Wi-Fi Module** | **LB-Link `BL-R8801MS1 V1.3`** | **Marvell / NXP `88W8801-NMD2`** 2.4 GHz 802.11n SDIO Wi-Fi chip (`uap0`, AP IP `172.16.10.1`) |
| **Storage** | microSD slot | Mounted at `/sdcard` (records `/sdcard/video` & `/sdcard/picture` when card is present) |

### Male Micro-USB Cable Pinout (3 Wires Only)
Although the camera terminates in a **male Micro-USB plug**, opening the housing confirms only **3 wires** are soldered to the PCB:
- **Red (`VBUS`):** `+5V` power input
- **Black (`GND`):** Ground
- **Blue:** 3.3V UART2 (`/dev/ttyS2`) serial link to the RC car's motor/receiver board

Because USB 2.0 requires 4 wires (`+5V`, `GND`, `D+`, `D-`), the camera **cannot** enumerate as a USB webcam or mass-storage device. However, `+5V` and `GND` match standard Micro-USB power pins, so you can safely power the camera on a bench using any female Micro-USB power port (such as a **Raspberry Pi Zero**'s `USB` port while powering the Pi via `PWR IN`).

---

## 2. Network & Service Map

When powered on, the camera creates an open 2.4 GHz Wi-Fi Access Point (`172.16.10.1/16`) with DHCP (`udhcpd`, assigning clients `172.16.10.2+`).

| Port | Protocol | Service |
| :--- | :--- | :--- |
| **`8888`** | **TCP** | Live 720p H.264 video stream + TCP control commands (`/app/vs_play`) |
| **`8080`** | **UDP** | Quick status queries, stream config, and car drive commands (`/app/vs_play`) |
| **`8626`** | **TCP** | Fullhan DBI debug thread |
| **`23`** | **TCP** | BusyBox `telnetd` shell |
| **`21`** | **TCP** | BusyBox FTP (`inetd`) |

---

## 3. TCP `8888` Video Stream Protocol

Unlike older Vison firmwares that use a 106-byte `ITd` handshake, `vs_play` V3.4.9 rejects packets larger than `30` bytes on the `stream_fd` handler and expects **repeated 2-byte opcodes**:

| Command Bytes | ASCII | Action |
| :--- | :--- | :--- |
| **`0x28 0x28 0x00`** | `((\0` | **Start / Keep-Alive Video Stream.** Registers socket in `stream_fd[0]` and sets `stream_state[0] = 5`. |
| **`0x29 0x29 0x00`** | `))\0` | **Stop Video Stream.** Unregisters `stream_fd[0]`. |
| **`0x11 0x11 0x00`** | — | Take photo to `/sdcard` (returns `no card\n` if empty). |
| **`0x12 0x12 0x00`** | — | Start recording to `/sdcard`. |
| **`0x13 0x13 0x00`** | — | Stop recording to `/sdcard`. |
| **`0x25 0x25 0x00`** | `%%\0` | Legacy keep-alive (triggers `noact\r\n`). |

> **Important Keep-Alive Timer:** An internal 1 Hz counter decrements `stream_state[0]` from `5 -> 4 -> 3 -> 2 -> 1` and pauses transmission at `1` (~4 seconds). Clients **must** re-send `0x28 0x28 0x00` every ~1.2 seconds to keep the stream active continuously.

### 44-Byte Frame Header (`stHead`)
Every encoded H.264 frame sent over TCP `8888` is prefixed with a 44-byte (`0x2c`) little-endian header:

- **Bytes `0..3` (Magic):**
  - `00 00 01 a1` — **I-Frame (Keyframe)**
  - `00 00 01 a0` — **P-Frame (Delta Frame)**
- **Bytes `16..19` (`uint32` LE):** `fhFrameLength` (exact byte length of the H.264 payload following the 44-byte header).
- **Bytes `24..27` (`uint16` LE × 2):** Frame resolution (`0x02d0` = `720` height, `0x0500` = `1280` width — or `d0 02 00 05`).

#### Keyframe (`00 00 01 a1`) Payload Layout
Inside an I-Frame payload, Vison places `SPS` and `PPS` in fixed zero-padded slots before the `IDR` slice:
- `0..32`: `00 00 00 01 67 4d 00 1f ...` (`SPS`, Main Profile @ Level 3.1, zero-padded to offset 32)
- `32..59`: `00 00 00 01 68 ee 38 80 ...` (`PPS`, zero-padded to offset 59)
- `59..fhFrameLength`: `00 00 00 01 65 ...` (`IDR` slice)

Stripping the trailing `0x00` padding from the `SPS` and `PPS` slots produces a standard H.264 Annex-B elementary stream compatible with browser **WebCodecs (`avc1.4d001f`)**, **FFmpeg**, and **VLC**.

---

## 4. UDP `8080` Opcodes

| Request | Response | Meaning |
| :--- | :--- | :--- |
| `0x42 0x76` (`"Bv"`) | `drone0\n` | Enable Wi-Fi control session (`Bv` / `0x42 0x77` disables) |
| `0x0f` | `UDP720P` | Query stream firmware capability |
| `0x01` / `0x02` | `ok1` / `ok2` | Select stream profile 1 / 2 |
| `0x1a` | `mirror=1` | Query inverted-mount sensor flag (1 = upside-down mount; rotate 180° in client) |
| `0x20` / `0x21` | `ok\0` / `okv` | Status / video readiness ping |
| `0x12` / `0x13` | `no card\n` | SD card record / photo trigger |
| `0x66 <steer> <throttle> 0x80 0x80 <flags> <xor> 0x99` | — | 8-byte Vison RC control packet forwarded via `/dev/ttyS2` (Blue wire) |
