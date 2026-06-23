package com.pafska.drawmebinary.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import com.pafska.drawmebinary.decode.NormBox

/**
 * Draws the scan reticle on top of the camera preview. When the decoder
 * reports a detected block it snaps a box around it; otherwise it shows a
 * default centred guide. Coordinates are mapped using FIT_CENTER math, so set
 * the PreviewView's scaleType to FIT_CENTER for the box to line up.
 */
class OverlayView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyle: Int = 0
) : View(context, attrs, defStyle) {

    private var box: NormBox? = null
    private var srcAspect = 0.75f      // upright width / height of the analysis frame
    private var locked = false

    private val boxPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 6f
        color = Color.parseColor("#5B9DFF")
    }
    private val guidePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 4f
        color = Color.parseColor("#665B9DFF")
    }

    /** @param aspect upright width/height of the analysis frame (0 = keep last) */
    fun setResult(b: NormBox?, aspect: Float, isLocked: Boolean) {
        box = b
        if (aspect > 0f) srcAspect = aspect
        locked = isLocked
        boxPaint.color = if (isLocked) Color.parseColor("#5BD6A0") else Color.parseColor("#5B9DFF")
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val vw = width.toFloat(); val vh = height.toFloat()
        if (vw <= 0 || vh <= 0) return

        // FIT_CENTER rect for the source aspect inside this view
        val viewAspect = vw / vh
        val fw: Float; val fh: Float
        if (srcAspect > viewAspect) { fw = vw; fh = vw / srcAspect }
        else { fh = vh; fw = vh * srcAspect }
        val ox = (vw - fw) / 2f; val oy = (vh - fh) / 2f

        val b = box
        if (b != null) {
            val pad = 10f
            val r = RectF(
                ox + b.left * fw - pad, oy + b.top * fh - pad,
                ox + b.right * fw + pad, oy + b.bottom * fh + pad
            )
            canvas.drawRoundRect(r, 16f, 16f, boxPaint)
        } else {
            // default centred guide band
            val gh = fh * 0.18f
            val r = RectF(ox + fw * 0.08f, oy + fh / 2 - gh / 2,
                ox + fw * 0.92f, oy + fh / 2 + gh / 2)
            canvas.drawRoundRect(r, 14f, 14f, guidePaint)
        }
    }
}
