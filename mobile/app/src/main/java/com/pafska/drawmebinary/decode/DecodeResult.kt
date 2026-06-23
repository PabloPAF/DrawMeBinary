package com.pafska.drawmebinary.decode

/** A rectangle in normalized **upright** image coordinates (0..1). */
data class NormBox(val left: Float, val top: Float, val right: Float, val bottom: Float)

/**
 * Outcome of decoding one frame.
 *
 * @param text       best-effort decoded message (empty if nothing readable)
 * @param confidence 0..1 quality score (fraction of printable characters)
 * @param bitFormat  which layout produced the text
 * @param glyphCount number of 0/1 glyphs kept this frame (for diagnostics)
 * @param box        bounds of the detected binary block (normalized, upright)
 */
data class DecodeResult(
    val text: String,
    val confidence: Float,
    val bitFormat: BitFormat,
    val glyphCount: Int,
    val box: NormBox? = null
) {
    val hasText: Boolean get() = text.isNotBlank()

    companion object {
        val EMPTY = DecodeResult("", 0f, BitFormat.UNKNOWN, 0, null)
    }
}

enum class BitFormat { EIGHT_BIT, FOUR_BIT_NIBBLE, UNKNOWN }
