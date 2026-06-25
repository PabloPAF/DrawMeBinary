package com.pafska.drawmebinary.camera

import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import com.pafska.drawmebinary.decode.BinaryDecoder
import com.pafska.drawmebinary.decode.DecodeResult
import com.pafska.drawmebinary.decode.LumaFrame

/**
 * Per-frame analyzer. Converts each CameraX frame's Y plane into a [LumaFrame],
 * runs the [decoder], measures throughput, and reports both via [onFrame].
 *
 * CameraX delivers frames here on a single background executor and will drop
 * frames while we're busy (STRATEGY_KEEP_ONLY_LATEST), so the preview stays
 * smooth even if a frame takes a while. We always close the ImageProxy.
 */
class FrameAnalyzer(
    private val decoder: BinaryDecoder,
    private val onFrame: (FrameStats) -> Unit
) : ImageAnalysis.Analyzer {

    data class FrameStats(
        val fps: Float,
        val analysisMs: Long,
        val frameWidth: Int,
        val frameHeight: Int,
        /** upright width/height of the frame (accounts for sensor rotation). */
        val srcAspect: Float,
        val result: DecodeResult,
        /** the frame's grayscale data, retained so it can be saved for debugging */
        val frame: LumaFrame
    )

    private var lastTimestampNs = 0L
    private var emaFps = 0f

    override fun analyze(image: ImageProxy) {
        val t0 = System.nanoTime()
        try {
            val frame = toLumaFrame(image)
            val result = decoder.decode(frame)

            // exponential moving average of instantaneous FPS
            val now = System.nanoTime()
            if (lastTimestampNs != 0L) {
                val instFps = 1_000_000_000f / (now - lastTimestampNs)
                emaFps = if (emaFps == 0f) instFps else emaFps * 0.8f + instFps * 0.2f
            }
            lastTimestampNs = now

            val analysisMs = (System.nanoTime() - t0) / 1_000_000
            val rot = ((frame.rotationDegrees % 360) + 360) % 360
            val srcAspect = if (rot == 90 || rot == 270)
                frame.height.toFloat() / frame.width
            else
                frame.width.toFloat() / frame.height
            onFrame(
                FrameStats(
                    fps = emaFps,
                    analysisMs = analysisMs,
                    frameWidth = frame.width,
                    frameHeight = frame.height,
                    srcAspect = srcAspect,
                    result = result,
                    frame = frame
                )
            )
        } finally {
            image.close() // MUST close or the pipeline stalls
        }
    }

    /** Copy the Y (luma) plane out of the YUV_420_888 frame. */
    private fun toLumaFrame(image: ImageProxy): LumaFrame {
        val plane = image.planes[0]                 // plane 0 = luminance
        val buffer = plane.buffer
        val bytes = ByteArray(buffer.remaining())
        buffer.get(bytes)
        return LumaFrame(
            luma = bytes,
            width = image.width,
            height = image.height,
            rowStride = plane.rowStride,
            rotationDegrees = image.imageInfo.rotationDegrees
        )
    }
}
