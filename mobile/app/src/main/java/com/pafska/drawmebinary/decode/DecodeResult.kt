package com.pafska.drawmebinary.decode

/** A rectangle in normalized **upright** image coordinates (0..1). */
data class NormBox(val left: Float, val top: Float, val right: Float, val bottom: Float)

/** One decoded character positioned over the source digits it replaces. */
data class Cell(val ch: Char, val box: NormBox)

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
    val box: NormBox? = null,
    // --- on-screen debug telemetry ---
    val inkPct: Float = 0f,   // % of frame marked as ink
    val rows: Int = 0,        // detected row stripes
    val cols: Int = 0,        // detected column stripes
    val gate: Int = 0,        // adaptive brightness gate used
    val raw: String = "",     // raw per-frame decode incl. '·' (debug only)
    val cells: List<Cell> = emptyList()  // per-character boxes for in-place overlay
) {
    val hasText: Boolean get() = text.isNotBlank()

    companion object {
        val EMPTY = DecodeResult("", 0f, BitFormat.UNKNOWN, 0, null)
    }
}

enum class BitFormat { EIGHT_BIT, FOUR_BIT_NIBBLE, UNKNOWN }
