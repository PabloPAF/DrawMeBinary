package com.pafska.drawmebinary.decode

/**
 * Contract for an on-device binary decoder. Milestone 1 ships [StubBinaryDecoder];
 * the real implementation (OpenCV glyph extraction -> classification -> ASCII/
 * UTF-8 assembly, porting drawmebinary/decoding.py) drops in behind this same
 * interface without touching the camera or UI layers.
 *
 * Implementations must be cheap enough to run on the camera analysis thread at
 * preview frame rates, and must never block on I/O or the network.
 */
interface BinaryDecoder {
    /** Decode one grayscale frame. Returns [DecodeResult.EMPTY] if unreadable. */
    fun decode(frame: LumaFrame): DecodeResult

    /** Release any native/scratch resources. */
    fun close() {}
}
