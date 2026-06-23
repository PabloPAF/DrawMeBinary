package com.pafska.drawmebinary.decode

/**
 * Placeholder decoder for Milestone 1 (camera preview only).
 *
 * It does NOT decode. It only does the cheapest possible touch of the frame -
 * a coarse "is there high-contrast, glyph-like structure in view?" estimate -
 * so the UI can show that frames are flowing and the analysis thread is wired
 * end to end. Replace with the real OpenCV-backed decoder in Milestone 2
 * (see docs/DECODER_PORT.md).
 */
class StubBinaryDecoder : BinaryDecoder {

    override fun decode(frame: LumaFrame): DecodeResult {
        // Sample a sparse grid and estimate contrast as a liveness signal only.
        val stepX = (frame.width / 32).coerceAtLeast(1)
        val stepY = (frame.height / 32).coerceAtLeast(1)
        var min = 255
        var max = 0
        var darkRuns = 0
        var x = 0
        while (x < frame.width) {
            var y = 0
            var prevDark = false
            while (y < frame.height) {
                val v = frame.pixel(x, y)
                if (v < min) min = v
                if (v > max) max = v
                val dark = v < 110
                if (dark && !prevDark) darkRuns++
                prevDark = dark
                y += stepY
            }
            x += stepX
        }
        val contrast = (max - min) / 255f
        // No decoding yet: report empty text, but expose the contrast as a
        // confidence-like hint so the overlay can nudge the user to get closer.
        return DecodeResult(
            text = "",
            confidence = contrast.coerceIn(0f, 1f),
            bitFormat = BitFormat.UNKNOWN,
            glyphCount = darkRuns
        )
    }
}
