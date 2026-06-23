# Porting the decoder to on-device Kotlin (Milestone 2+)

The web app decodes with a Python/OpenCV pipeline
(`drawmebinary/extraction.py` + `decoding.py`). On mobile we re-implement the
same stages in Kotlin (with OpenCV-Android for the pixel work) behind the
`BinaryDecoder` interface, tuned for **real-time, per-frame, offline** use.

## Pipeline mapping

| Stage | Python (web) | Android plan |
|-------|--------------|--------------|
| Acquire | file/PDF → BGR image | CameraX `ImageAnalysis` → `LumaFrame` (Y plane, grayscale already) |
| Binarize | adaptive threshold | OpenCV `adaptiveThreshold` (or Otsu) on the reticle ROI only |
| Glyph detect | contours → boxes | `findContours` + size/aspect filtering; restrict to the scan band |
| Classify 0/1 | shape + optional Keras verifier | fast geometric test first (hole/solidity), small **TFLite** CNN as fallback |
| Group lines | `group_lines` (neighbour chaining) | port directly; pure geometry on glyph boxes |
| Tokenize | `tokens_in_line` (gap split) | port directly |
| Assemble | 8-bit / 4-bit nibble pairs, UTF-8 | port `_cells_to_units`, `_bits_to_text` (start 8-bit only) |
| Score | `LanguageValidator.quality` | lightweight printable/letter ratio first; bundled wordlist later |

## Real-time strategy (differs from the web app)

- **ROI, not full frame.** Only analyze the reticle band; the user aligns one
  line of binary there. Keeps per-frame cost bounded.
- **Temporal voting.** A single frame is noisy. Maintain a short ring buffer of
  recent per-frame decodes and emit the majority/highest-confidence string, so
  the overlay is stable as the user pans (this replaces the web app's
  multi-candidate vote, which it does within one image).
- **Frame budget.** Target < ~30 ms analysis at 720p. Prefer the geometric
  classifier; only fall back to TFLite for ambiguous glyphs. Downscale the ROI
  before contouring.
- **No error-repair DP per frame.** The web app's `_repair_decode` is too heavy
  for every frame — run it only on a captured still when the user taps to lock.

## Threading & memory

- Analysis runs on CameraX's single analysis executor (`STRATEGY_KEEP_ONLY_LATEST`).
- Reuse OpenCV `Mat`s across frames; never allocate per pixel.
- `LumaFrame` already hands over the Y plane, so no YUV→RGB conversion is needed
  for grayscale CV — a meaningful saving versus decoding full color.

## OpenCV integration options

1. **OpenCV Android SDK** (`org.opencv:opencv:4.x` AAR) — full `imgproc`,
   straightforward port of the Python calls.
2. **Hand-rolled Kotlin** for the few ops we need (threshold, connected
   components) — smaller binary, no native dep, but more code.

Recommendation: start with the OpenCV AAR for parity, profile, then trim.

## What stays identical to the web app

- The **bit→text** semantics (8-bit ASCII/UTF-8; 4-bit nibble pairs:
  top = high nibble, bottom = low) must match exactly, so a message encoded by
  the web "Draw & Encode" tool decodes the same on mobile.
- The **logging schema** (`SecLog`) — same ECS fields/actions as the web app.
