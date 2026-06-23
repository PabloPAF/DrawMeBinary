package com.pafska.drawmebinary.decode

/**
 * A single grayscale frame, decoupled from CameraX/Android types so the
 * decoder can be unit-tested on the JVM and ported logic stays platform-free.
 *
 * [luma] is the Y (luminance) plane of the camera image: one byte per pixel,
 * row-major, with [rowStride] bytes per row (>= [width] due to alignment
 * padding). Values are unsigned 0..255 stored in a signed Byte.
 */
data class LumaFrame(
    val luma: ByteArray,
    val width: Int,
    val height: Int,
    val rowStride: Int,
    /** Clockwise rotation (degrees) needed to view the frame upright. */
    val rotationDegrees: Int
) {
    /** Unsigned luminance at (x, y); 0 (black) .. 255 (white). */
    fun pixel(x: Int, y: Int): Int = luma[y * rowStride + x].toInt() and 0xFF

    // data class arrays: identity-based equals/hashCode are fine here; we never
    // use LumaFrame as a map key, but override to satisfy lint expectations.
    override fun equals(other: Any?): Boolean = this === other
    override fun hashCode(): Int = System.identityHashCode(this)
}
