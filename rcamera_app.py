#!/usr/bin/env python3
"""
rcamera_app.py — Local FPV Viewer, Recorder & Wi-Fi Controller for
Odyssey Toys Spy Rover / Heliway HLW1801A (Fullhan FH8626V100 + Vison VS-H62 + Marvell 88W8801).

Zero external Python dependencies required! Uses Python's standard library +
HTML5 Canvas & WebCodecs H.264 / MJPEG decoding in your browser.

Usage:
  python rcamera_app.py
  Then open http://127.0.0.1:8000 in Chrome or Edge.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import http.server
import json
import os
import platform
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Any

# Vison vs_play (FH8626V100) TCP 8888 stream-start and keep-alive ("((" = 0x28 0x28 resets stream_state=5)
VISON_STREAM_START = b"((\x00"
VISON_KEEPALIVE = b"((\x00"

CAMERA_SUBNET_PREFIXES = ("172.16.10.", "192.168.4.", "192.168.80.", "192.168.100.")
RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)


def detect_camera_gateway() -> str:
    """Detect the camera gateway IP across Windows, Linux, and macOS."""
    system = platform.system()
    candidates: list[str] = []
    try:
        if system == "Windows":
            out = subprocess.check_output(["ipconfig"], text=True, errors="ignore")
            for line in out.splitlines():
                s = line.strip()
                if "IPv4 Address" in s or "IP Address" in s:
                    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", s)
                    if m:
                        candidates.append(m.group(1))
        elif system == "Linux":
            out = subprocess.check_output(["ip", "-4", "addr"], text=True, errors="ignore")
            candidates.extend(re.findall(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", out))
        elif system == "Darwin":
            out = subprocess.check_output(["ifconfig"], text=True, errors="ignore")
            candidates.extend(re.findall(r"inet\s+(\d+\.\d+\.\d+\.\d+)\s", out))
    except Exception:
        pass

    for ip in candidates:
        if ip.startswith(CAMERA_SUBNET_PREFIXES):
            return ".".join(ip.split(".")[:3]) + ".1"
    return "172.16.10.1"


class CameraEngine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.connected = False
        self.camera_ip = "172.16.10.1"
        self.video_port = 8888
        self.control_port = 8080
        self.auto_detect = True
        self.codec_detected = "Waiting for stream..."
        self.bytes_received = 0
        self.frames_emitted = 0
        self.fps = 0.0
        self.bitrate_kbps = 0.0
        self.last_hex_sample = ""
        self.last_error = ""
        self.recording_file: Any = None
        self.recording_filename = ""

        # Driving state (neutral = 128 on 0..255 scale)
        self.throttle = 128  # 0=full reverse, 128=stop, 255=full forward
        self.steering = 128  # 0=full left, 128=center, 255=full right
        self.trim = 0
        self.control_enabled = False
        self.control_variant = "vison_66"  # "vison_66" or "heliway_ff"

        # Subscribers for SSE stream
        self.subscribers: list[queue.Queue[dict[str, Any]]] = []

        self._worker_thread: threading.Thread | None = None
        self._control_thread: threading.Thread | None = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._worker_thread = threading.Thread(target=self._video_loop, daemon=True)
        self._worker_thread.start()
        self._control_thread = threading.Thread(target=self._control_loop, daemon=True)
        self._control_thread.start()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=30)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def broadcast(self, event: dict[str, Any]) -> None:
        with self.lock:
            dead = []
            for q in self.subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(event)
                    except Exception:
                        dead.append(q)
            for q in dead:
                self.subscribers.remove(q)

    def toggle_raw_recording(self) -> dict[str, Any]:
        with self.lock:
            if self.recording_file:
                self.recording_file.close()
                self.recording_file = None
                fname = self.recording_filename
                self.recording_filename = ""
                return {"recording": False, "saved_file": fname}
            else:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                ext = "mjpg" if "JPEG" in self.codec_detected else "h264"
                fname = os.path.join(RECORDINGS_DIR, f"rcamera_{ts}.{ext}")
                self.recording_file = open(fname, "wb")
                self.recording_filename = fname
                return {"recording": True, "file": fname}

    def build_control_packet(self) -> bytes:
        t = max(0, min(255, int(self.throttle)))
        s = max(0, min(255, int(self.steering)))
        if self.control_variant == "heliway_ff":
            # 8-byte 0xFF 0x08packet common on Heliway / LW modules
            chk = (0x08 + s + t + 0x80 + 0x80 + 0x00) & 0xFF
            return bytes([0xFF, 0x08, s, t, 0x80, 0x80, 0x00, chk ^ 0xFF])
        else:
            # 8-byte 0x66 ... 0x99 packet common on Vison / VS FPV Car modules
            flags = 0x00
            chk = s ^ t ^ 0x80 ^ 0x80 ^ flags
            return bytes([0x66, s, t, 0x80, 0x80, flags, chk, 0x99])

    def _control_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while self.running:
            if self.connected and self.control_enabled:
                pkt = self.build_control_packet()
                try:
                    sock.sendto(pkt, (self.camera_ip, self.control_port))
                except Exception:
                    pass
            time.sleep(0.025)  # 40 Hz control rate

    def _video_loop(self) -> None:
        last_rate_time = time.monotonic()
        bytes_since_last = 0
        frames_since_last = 0

        while self.running:
            if self.auto_detect:
                self.camera_ip = detect_camera_gateway()

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            try:
                sock.connect((self.camera_ip, self.video_port))
                self.connected = True
                self.last_error = ""
                # Send UDP 8080 wake-up first, then TCP 8888 "((" (0x28 0x28) stream-start
                try:
                    usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    for ucmd in (b"\x42\x76", b"\x0f", b"\x01"):
                        usock.sendto(ucmd, (self.camera_ip, self.control_port))
                    usock.close()
                except Exception:
                    pass

                sock.sendall(VISON_STREAM_START)
                sock.settimeout(1.0)

                buf = bytearray()
                last_keepalive = time.monotonic()

                while self.running:
                    now = time.monotonic()
                    if now - last_keepalive > 1.2:
                        try:
                            sock.sendall(VISON_KEEPALIVE)
                        except Exception:
                            break
                        last_keepalive = now

                    try:
                        chunk = sock.recv(32768)
                        if not chunk:
                            self.last_error = "Camera closed stream socket; reconnecting..."
                            break
                    except socket.timeout:
                        continue

                    self.bytes_received += len(chunk)
                    bytes_since_last += len(chunk)
                    if not self.last_hex_sample or (frames_since_last % 30 == 0):
                        self.last_hex_sample = chunk[:48].hex(" ")

                    buf.extend(chunk)

                    # Parse Vison 44-byte stHead frames: 00 00 01 a1 (I-Frame) and 00 00 01 a0 (P-Frame)
                    while True:
                        idx_p = buf.find(b"\x00\x00\x01\xa0")
                        idx_i = buf.find(b"\x00\x00\x01\xa1")
                        candidates = [i for i in (idx_p, idx_i) if i != -1]
                        if not candidates:
                            break
                        pos = min(candidates)
                        if pos > 0:
                            del buf[:pos]
                        if len(buf) < 44:
                            break
                        is_key = buf[3] == 0xA1
                        fh_len = struct.unpack_from("<I", buf, 16)[0]
                        if fh_len == 0 or fh_len > 512 * 1024:
                            # Invalid length; skip magic
                            del buf[:4]
                            continue
                        if len(buf) < 44 + fh_len:
                            break

                        raw_payload = bytes(buf[44 : 44 + fh_len])
                        del buf[: 44 + fh_len]

                        if is_key and len(raw_payload) > 64 and raw_payload[:5] == b"\x00\x00\x00\x01\x67":
                            # Strip Vison fixed-slot zero padding after SPS (0..32) and PPS (32..59); IDR starts at 59
                            sps = raw_payload[:32].rstrip(b"\x00")
                            pps = raw_payload[32:59].rstrip(b"\x00")
                            idr = raw_payload[59:]
                            clean_frame = sps + pps + idr
                        else:
                            clean_frame = raw_payload

                        with self.lock:
                            if self.recording_file:
                                try:
                                    self.recording_file.write(clean_frame)
                                except Exception:
                                    pass

                        self.codec_detected = "H.264 720p (1280x720 Main@L3.1)"
                        self.frames_emitted += 1
                        frames_since_last += 1
                        self.broadcast({
                            "type": "h264_frame",
                            "is_key": is_key,
                            "data": base64.b64encode(clean_frame).decode("ascii"),
                            "ts": time.time(),
                        })

                    # Prevent unbounded buffer growth
                    if len(buf) > 512 * 1024:
                        del buf[:-65536]

                    dt = now - last_rate_time
                    if dt >= 1.0:
                        self.fps = round(frames_since_last / dt, 1)
                        self.bitrate_kbps = round((bytes_since_last * 8 / 1000.0) / dt, 1)
                        bytes_since_last = 0
                        frames_since_last = 0
                        last_rate_time = now

            except Exception as e:
                self.connected = False
                self.fps = 0.0
                self.bitrate_kbps = 0.0
                self.last_error = f"{type(e).__name__}: {e}"
                time.sleep(1.2)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass


ENGINE = CameraEngine()

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Odyssey / Heliway RC Camera FPV Station (FH8626V100 + VS-H62)</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #2f81f7;
    --success: #238636;
    --danger: #da3633;
    --warn: #d29922;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 20px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
  }
  header h1 {
    font-size: 1.1rem;
    margin: 0;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .badge {
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
  }
  .badge.online { background: rgba(35, 134, 54, 0.25); color: #3fb950; border: 1px solid #238636; }
  .badge.offline { background: rgba(218, 54, 51, 0.2); color: #f85149; border: 1px solid #da3633; }
  main {
    display: grid;
    grid-template-columns: 1fr 360px;
    gap: 16px;
    padding: 16px;
    max-width: 1600px;
    margin: 0 auto;
  }
  @media (max-width: 1024px) {
    main { grid-template-columns: 1fr; }
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px;
  }
  .video-wrapper {
    position: relative;
    background: #000;
    border-radius: 8px;
    overflow: hidden;
    aspect-ratio: 16 / 9;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  canvas {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
  .hud-overlay {
    position: absolute;
    top: 12px;
    left: 12px;
    right: 12px;
    display: flex;
    justify-content: space-between;
    pointer-events: none;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 0.8rem;
    text-shadow: 0 1px 3px rgba(0,0,0,0.9);
  }
  .hud-pill {
    background: rgba(13, 17, 23, 0.75);
    border: 1px solid rgba(255,255,255,0.15);
    padding: 4px 10px;
    border-radius: 6px;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
  }
  button {
    background: #21262d;
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 0.85rem;
    cursor: pointer;
    font-weight: 500;
  }
  button:hover { background: #30363d; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.danger { background: var(--danger); border-color: var(--danger); color: #fff; }
  .kv-grid {
    display: grid;
    grid-template-columns: 120px 1fr;
    row-gap: 8px;
    font-size: 0.85rem;
    margin-bottom: 12px;
  }
  .kv-grid .label { color: var(--muted); }
  .kv-grid .val { font-family: ui-monospace, monospace; word-break: break-all; }
  input, select {
    background: #0d1117;
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 0.85rem;
    width: 100%;
  }
  .dpad {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    max-width: 220px;
    margin: 12px auto;
  }
  .dpad button {
    padding: 14px 0;
    font-weight: 700;
    user-select: none;
  }
  .dpad button.active {
    background: var(--accent);
    border-color: var(--accent);
  }
  pre.hex {
    background: #0d1117;
    border: 1px solid var(--border);
    padding: 8px;
    border-radius: 6px;
    font-size: 0.74rem;
    overflow-x: auto;
    color: #79c0ff;
    margin: 6px 0 0 0;
  }
</style>
</head>
<body>
<header>
  <h1>
    <span>Odyssey / Heliway FPV Station</span>
    <span style="font-size:0.78rem; color:var(--muted); font-weight:normal;">Fullhan FH8626V100 + Vison VS-H62 + Marvell 88W8801</span>
  </h1>
  <div id="connBadge" class="badge offline">DISCONNECTED</div>
</header>

<main>
  <section class="card">
    <div class="video-wrapper">
      <canvas id="videoCanvas" width="1280" height="720"></canvas>
      <div class="hud-overlay">
        <div class="hud-pill" id="hudLeft">IP: 172.16.10.1:8888 | Codec: Waiting...</div>
        <div class="hud-pill" id="hudRight">0.0 FPS | 0 kbps</div>
      </div>
    </div>
    <div class="toolbar">
      <button class="primary" onclick="takeSnapshot()">Save Snapshot (PNG)</button>
      <button id="btnWebmRec" onclick="toggleCanvasRecording()">Record Video (.webm)</button>
      <button id="btnRawRec" onclick="toggleRawRecording()">Record Raw Stream</button>
      <button onclick="flipCamera()" id="btnFlip">Flip 180° (ON)</button>
      <button onclick="reconnectCamera()">Re-send Vison Handshake</button>
    </div>
  </section>

  <aside style="display:flex; flex-direction:column; gap:16px;">
    <div class="card">
      <h3 style="margin:0 0 12px 0; font-size:0.95rem;">Camera Connection & Telemetry</h3>
      <div class="kv-grid">
        <div class="label">Camera IP:</div>
        <div><input id="inpIp" value="172.16.10.1" onchange="updateConfig()"></div>
        <div class="label">Video Port:</div>
        <div><input id="inpPort" type="number" value="8888" onchange="updateConfig()"></div>
        <div class="label">Auto-Detect IP:</div>
        <div><input id="chkAuto" type="checkbox" checked onchange="updateConfig()" style="width:auto;"></div>
        <div class="label">Detected Codec:</div>
        <div class="val" id="valCodec">-</div>
        <div class="label">Total Received:</div>
        <div class="val" id="valBytes">0 KB</div>
        <div class="label">Status / Error:</div>
        <div class="val" id="valError" style="color:var(--warn);">Waiting for camera Wi-Fi...</div>
      </div>
      <div style="font-size:0.78rem; color:var(--muted);">Latest Stream Header Bytes (Hex):</div>
      <pre class="hex" id="valHex">No bytes received yet</pre>
    </div>

    <div class="card">
      <h3 style="margin:0 0 8px 0; font-size:0.95rem;">Wi-Fi Drive Control (Blue UART Wire)</h3>
      <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; font-size:0.85rem;">
        <label><input id="chkDrive" type="checkbox" onchange="updateDriveConfig()" style="width:auto;"> Enable UDP Drive TX</label>
        <select id="selVariant" onchange="updateDriveConfig()" style="width:150px;">
          <option value="vison_66">Vison (0x66..0x99)</option>
          <option value="heliway_ff">Heliway (0xFF 0x08)</option>
        </select>
      </div>
      <div class="dpad">
        <div></div>
        <button id="btnW" onmousedown="setKey('w',true)" onmouseup="setKey('w',false)">W ▲</button>
        <div></div>
        <button id="btnA" onmousedown="setKey('a',true)" onmouseup="setKey('a',false)">◄ A</button>
        <button onclick="stopDrive()">STOP</button>
        <button id="btnD" onmousedown="setKey('d',true)" onmouseup="setKey('d',false)">D ►</button>
        <div></div>
        <button id="btnS" onmousedown="setKey('s',true)" onmouseup="setKey('s',false)">S ▼</button>
        <div></div>
      </div>
      <div style="font-size:0.78rem; color:var(--muted); text-align:center;" id="driveTelemetry">
        Throttle: 128 | Steering: 128
      </div>
    </div>
  </aside>
</main>

<script>
const canvas = document.getElementById('videoCanvas');
const ctx = canvas.getContext('2d');

// Draw initial placeholder
ctx.fillStyle = '#090d16';
ctx.fillRect(0, 0, canvas.width, canvas.height);
ctx.fillStyle = '#8b949e';
ctx.font = '20px sans-serif';
ctx.textAlign = 'center';
ctx.fillText('Connect PC Wi-Fi to the RC Camera to start live stream', canvas.width / 2, canvas.height / 2);

// WebCodecs H.264 Decoder setup for Fullhan FH8626V100 H.264 streams
let h264Decoder = null;
let spsNal = null;
let ppsNal = null;
let decoderConfigured = false;
let timestampCounter = 0;
let flip180 = true; // Camera reports mirror=1 on UDP 0x1A

function initH264Decoder() {
  if (!('VideoDecoder' in window)) return;
  h264Decoder = new VideoDecoder({
    output: (frame) => {
      if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
        canvas.width = frame.displayWidth || 1280;
        canvas.height = frame.displayHeight || 720;
      }
      if (flip180) {
        ctx.save();
        ctx.translate(canvas.width, canvas.height);
        ctx.rotate(Math.PI);
        ctx.drawImage(frame, 0, 0, canvas.width, canvas.height);
        ctx.restore();
      } else {
        ctx.drawImage(frame, 0, 0, canvas.width, canvas.height);
      }
      frame.close();
    },
    error: (e) => console.warn('WebCodecs H.264 decode error:', e)
  });
}
initH264Decoder();

function b64ToUint8Array(b64) {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return arr;
}

function handleH264Frame(isKey, b64Data) {
  if (!('VideoDecoder' in window)) return;
  if (!h264Decoder || h264Decoder.state === 'closed') {
    decoderConfigured = false;
    initH264Decoder();
  }
  const bytes = b64ToUint8Array(b64Data);
  if (!decoderConfigured) {
    if (!isKey) return; // Wait for 00 00 01 a1 keyframe (contains SPS + PPS + IDR)
    try {
      h264Decoder.configure({ codec: 'avc1.4d001f', optimizeForLatency: true });
      decoderConfigured = true;
    } catch (e) {
      console.warn('Configure error:', e);
      return;
    }
  }
  try {
    h264Decoder.decode(new EncodedVideoChunk({
      type: isKey ? 'key' : 'delta',
      timestamp: timestampCounter += 33333,
      data: bytes
    }));
  } catch (e) {
    console.warn('Decode chunk error:', e);
    decoderConfigured = false;
  }
}

// Subscribe to live stream events
const evtSource = new EventSource('/api/stream');
evtSource.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === 'h264_frame') {
    handleH264Frame(msg.is_key, msg.data);
  }
};

// Poll status every 1s
setInterval(async () => {
  try {
    const res = await fetch('/api/status');
    const st = await res.json();
    const badge = document.getElementById('connBadge');
    if (st.connected) {
      badge.className = 'badge online';
      badge.textContent = 'CONNECTED (' + st.camera_ip + ')';
    } else {
      badge.className = 'badge offline';
      badge.textContent = 'WAITING FOR CAMERA WI-FI';
    }
    if (document.activeElement !== document.getElementById('inpIp')) {
      document.getElementById('inpIp').value = st.camera_ip;
    }
    document.getElementById('hudLeft').textContent = `IP: ${st.camera_ip}:${st.video_port} | ${st.codec_detected}`;
    document.getElementById('hudRight').textContent = `${st.fps} FPS | ${st.bitrate_kbps} kbps`;
    document.getElementById('valCodec').textContent = st.codec_detected;
    document.getElementById('valBytes').textContent = (st.bytes_received / 1024).toFixed(1) + ' KB (' + st.frames_emitted + ' frames)';
    document.getElementById('valError').textContent = st.last_error || 'Streaming normally';
    if (st.last_hex_sample) {
      document.getElementById('valHex').textContent = st.last_hex_sample;
    }
  } catch (_) {}
}, 1000);

function takeSnapshot() {
  const a = document.createElement('a');
  a.download = `rcamera_snapshot_${Date.now()}.png`;
  a.href = canvas.toDataURL('image/png');
  a.click();
}

let mediaRecorder = null;
let recordedChunks = [];
function toggleCanvasRecording() {
  const btn = document.getElementById('btnWebmRec');
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    btn.textContent = 'Record Video (.webm)';
    btn.className = '';
  } else {
    recordedChunks = [];
    const stream = canvas.captureStream(30);
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'video/webm' });
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) recordedChunks.push(e.data); };
    mediaRecorder.onstop = () => {
      const blob = new Blob(recordedChunks, { type: 'video/webm' });
      const a = document.createElement('a');
      a.download = `rcamera_video_${Date.now()}.webm`;
      a.href = URL.createObjectURL(blob);
      a.click();
    };
    mediaRecorder.start();
    btn.textContent = 'Stop .webm Recording';
    btn.className = 'danger';
  }
}

async function toggleRawRecording() {
  const res = await fetch('/api/record', { method: 'POST' });
  const data = await res.json();
  const btn = document.getElementById('btnRawRec');
  if (data.recording) {
    btn.textContent = 'Stop Raw Stream Recording';
    btn.className = 'danger';
  } else {
    btn.textContent = 'Record Raw Stream';
    btn.className = '';
    alert('Saved raw capture to: ' + data.saved_file);
  }
}

async function updateConfig() {
  await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      camera_ip: document.getElementById('inpIp').value,
      video_port: parseInt(document.getElementById('inpPort').value, 10),
      auto_detect: document.getElementById('chkAuto').checked
    })
  });
}

async function reconnectCamera() {
  await fetch('/api/reconnect', { method: 'POST' });
}

function flipCamera() {
  flip180 = !flip180;
  document.getElementById('btnFlip').textContent = flip180 ? 'Flip 180° (ON)' : 'Flip 180° (OFF)';
}

// WASD / Arrow Driving Controls
const keys = { w: false, a: false, s: false, d: false };
function setKey(k, state) {
  keys[k] = state;
  const btn = document.getElementById('btn' + k.toUpperCase());
  if (btn) btn.className = state ? 'active' : '';
  sendDriveUpdate();
}
function stopDrive() {
  keys.w = keys.a = keys.s = keys.d = false;
  ['W','A','S','D'].forEach(k => document.getElementById('btn'+k).className = '');
  sendDriveUpdate();
}
window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  const k = e.key.toLowerCase();
  if (k === 'w' || e.key === 'ArrowUp') setKey('w', true);
  if (k === 's' || e.key === 'ArrowDown') setKey('s', true);
  if (k === 'a' || e.key === 'ArrowLeft') setKey('a', true);
  if (k === 'd' || e.key === 'ArrowRight') setKey('d', true);
  if (e.key === ' ') stopDrive();
});
window.addEventListener('keyup', (e) => {
  const k = e.key.toLowerCase();
  if (k === 'w' || e.key === 'ArrowUp') setKey('w', false);
  if (k === 's' || e.key === 'ArrowDown') setKey('s', false);
  if (k === 'a' || e.key === 'ArrowLeft') setKey('a', false);
  if (k === 'd' || e.key === 'ArrowRight') setKey('d', false);
});

async function updateDriveConfig() {
  sendDriveUpdate();
}

async function sendDriveUpdate() {
  const throttle = keys.w ? 220 : (keys.s ? 35 : 128);
  const steering = keys.d ? 220 : (keys.a ? 35 : 128);
  document.getElementById('driveTelemetry').textContent = `Throttle: ${throttle} | Steering: ${steering}`;
  await fetch('/api/control', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      throttle,
      steering,
      control_enabled: document.getElementById('chkDrive').checked,
      control_variant: document.getElementById('selVariant').value
    })
  });
}
</script>
</body>
</html>
"""


class RequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass  # Keep console clean

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            data = {
                "connected": ENGINE.connected,
                "camera_ip": ENGINE.camera_ip,
                "video_port": ENGINE.video_port,
                "control_port": ENGINE.control_port,
                "auto_detect": ENGINE.auto_detect,
                "codec_detected": ENGINE.codec_detected,
                "bytes_received": ENGINE.bytes_received,
                "frames_emitted": ENGINE.frames_emitted,
                "fps": ENGINE.fps,
                "bitrate_kbps": ENGINE.bitrate_kbps,
                "last_hex_sample": ENGINE.last_hex_sample,
                "last_error": ENGINE.last_error,
            }
            raw = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = ENGINE.subscribe()
            try:
                while True:
                    try:
                        evt = q.get(timeout=5.0)
                        payload = f"data: {json.dumps(evt)}\n\n".encode("utf-8")
                        self.wfile.write(payload)
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                ENGINE.unsubscribe(q)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            payload = {}

        if self.path == "/api/config":
            ENGINE.camera_ip = payload.get("camera_ip", ENGINE.camera_ip)
            ENGINE.video_port = int(payload.get("video_port", ENGINE.video_port))
            ENGINE.auto_detect = bool(payload.get("auto_detect", ENGINE.auto_detect))
            self._send_json({"ok": True})
        elif self.path == "/api/control":
            ENGINE.throttle = int(payload.get("throttle", 128))
            ENGINE.steering = int(payload.get("steering", 128))
            ENGINE.control_enabled = bool(payload.get("control_enabled", False))
            ENGINE.control_variant = payload.get("control_variant", "vison_66")
            self._send_json({"ok": True})
        elif self.path == "/api/record":
            res = ENGINE.toggle_raw_recording()
            self._send_json(res)
        elif self.path == "/api/reconnect":
            ENGINE.connected = False
            self._send_json({"ok": True})
        else:
            self.send_error(404)

    def _send_json(self, obj: dict[str, Any]) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        _, exc, _ = sys.exc_info()
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main() -> None:
    parser = argparse.ArgumentParser(description="Odyssey/Heliway FPV Camera Web Station")
    parser.add_argument("--ip", help="Camera IP override (disables auto-detection; default auto -> 172.16.10.1)")
    parser.add_argument("--host", default="127.0.0.1", help="Web UI bind host (use 0.0.0.0 for LAN/Pi access)")
    parser.add_argument("--port", type=int, default=8000, help="Local web UI port (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    args = parser.parse_args()

    if args.ip:
        ENGINE.camera_ip = args.ip
        ENGINE.auto_detect = False

    ENGINE.start()
    server = ThreadedHTTPServer((args.host, args.port), RequestHandler)
    display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{display_host}:{args.port}"
    print("======================================================================")
    print(f" Odyssey / Heliway Wi-Fi FPV Station running at: {url}")
    print(" Hardware: Fullhan FH8626V100 + Vison VS-H62 + Marvell 88W8801")
    print("======================================================================")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        ENGINE.running = False
        server.server_close()


if __name__ == "__main__":
    main()
