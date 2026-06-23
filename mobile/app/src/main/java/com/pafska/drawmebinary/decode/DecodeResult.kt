package com.pafska.drawmebinary.decode

/**
 * Outcome of decoding one frame.
 *
 * @param text       best-effort decoded message (empty if nothing readable)
 * @param confidence 0..1 quality score (mirrors the Python pipeline's quality)
 * @param bitFormat  which layout produced the text
 * @param glyphCount number of 0/1 glyphs detected this frame (for diagnostics)
 */
data class DecodeResult(
    val text: String,
    val confidence: Float,
    val bitFormat: BitFormat,
    val glyphCount: Int
) {
    val hasText: Boolean get() = text.isNotBlank()

    companion object {
        val EMPTY = DecodeResult("", 0f, BitFormat.UNKNOWN, 0)
    }
}

enum class BitFormat { EIGHT_BIT, FOUR_BIT_NIBBLE, UNKNOWN }
