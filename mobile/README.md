# DrawMeBinary — Mobile (Android)

The mobile companion to the DrawMeBinary web app: point the phone camera at
binary (0/1) artwork and decode the hidden message **live, on-device, offline**
as you pan across it. Separate project from the web app, but its security logs
land in the **same SIEM** (see [Logging](#logging)).

> **Status — Milestone 1: live camera preview.** The camera pipeline, per-frame
> analysis loop, decoder interface, and SIEM logging are wired end to end. The
> decoder itself is a **stub** (no decoding yet) — that's Milestone 2.

## Tech

- **Kotlin**, min SDK 24, target/compile SDK 34
- **CameraX** (`camera-core/camera2/lifecycle/view`) for preview + `ImageAnalysis`
- Decoding will use **OpenCV (Android)** / a small **TFLite** glyph classifier;
  see [`docs/DECODER_PORT.md`](docs/DECODER_PORT.md)
- No network permission — fully offline

## Build & run

This project ships source only; the Gradle **wrapper jar** is generated on first
open (it isn't committed).

**Android Studio (recommended)**
1. *File ▸ Open* → select this `mobile/` folder.
2. Let it sync; it generates the Gradle wrapper and downloads dependencies.
3. Run on a **physical device** (camera needed; emulators have no real camera).

**Command line**
```bash
cd mobile
gradle wrapper --gradle-version 8.7   # one-time: creates ./gradlew + wrapper jar
./gradlew assembleDebug                # build the APK
./gradlew installDebug                 # install on a connected device
```

On launch the app asks for camera permission, then shows the live preview with
a status line (FPS · resolution · per-frame ms · contrast · rough glyph count)
and a scan reticle. Decoded text will appear in the bottom bar once Milestone 2
lands.

## Architecture

```
MainActivity            permission + UI; receives FrameStats on the main thread
 └─ CameraController     binds Preview + ImageAnalysis (latest-frame, own thread)
     └─ FrameAnalyzer    Y-plane → LumaFrame, FPS/EMA, calls the decoder
         └─ BinaryDecoder    interface
             └─ StubBinaryDecoder   M1 placeholder (contrast probe only)
SecLog                  ECS JSON logging → on-device file buffer
```

The decoder is deliberately decoupled from Android types (`LumaFrame` is a plain
grayscale buffer) so the ported logic stays testable on the JVM and the real
decoder drops in behind `BinaryDecoder` without touching camera/UI code.

## Logging

`SecLog` emits the **same ECS schema** as the web service
(`drawmebinary/seclog.py`), one JSON object per line, so both projects feed one
SIEM index — only `service.name` differs (`drawmebinary-mobile`). It never logs
the decoded message, the photo, or a file path: only a SHA-256 of decoded text
plus metadata. Camera scans have no client IP, so `source.ip` is omitted. Events
buffer to a local file (`filesDir/logs/security.jsonl`); shipping to the
collector when online is Milestone 3. See the web repo's `SECURITY_LOGGING.md`,
section *"Two projects, one SIEM"*.

## Roadmap

- **M1 (done):** camera preview + frame loop + decoder interface + logging stub
- **M2:** on-device decoder for clean printed **8-bit** binary (OpenCV glyph
  extraction → 0/1 classification → ASCII/UTF-8), live overlay + temporal voting
  across frames for stability
- **M3:** 4-bit nibble-pair layout; SIEM log shipping; confidence-gated capture
- **M4:** robustness (hand-painted glyphs, skew, lighting), iOS via shared core
