package com.pafska.drawmebinary.ui

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import com.pafska.drawmebinary.decode.Cell
import com.pafska.drawmebinary.decode.NormBox

/**
 * Draws the scan reticle and, when decoded, the letters **in place** over the
 * 0/1 digits (each byte's character covers the digits it was read from).
 * Coordinates use FIT_CENTER math, so set the PreviewView scaleType to
 * FIT_CENTER for the overlay to line up.
 */
class OverlayView @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null, defStyle: Int = 0
) : View(context, attrs, defStyle) {

    private var box: NormBox? = null
    private var cells: List<Cell> = emptyList()
    private var srcAspect = 0.75f
    private var locked = false

    private val boxPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeWidth = 6f
    }
    private val guidePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeWidth = 4f
        color = Color.parseColor("#665B9DFF")
    }
    private val coverPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL; color = Color.parseColor("#F2101622")  // cover the digits
    }
    private val letterPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE; textAlign = Paint.Align.CENTER
        isFakeBoldText = true
    }

    fun setResult(b: NormBox?, cells: List<Cell>, aspect: Float, isLocked: Boolean) {
        box = b
        this.cells = cells
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

        fun mapX(nx: Float) = ox + nx * fw
        fun mapY(ny: Float) = oy + ny * fh

        val b = box
        if (b != null) {
            val pad = 10f
            canvas.drawRoundRect(
                RectF(mapX(b.left) - pad, mapY(b.top) - pad, mapX(b.right) + pad, mapY(b.bottom) + pad),
                16f, 16f, boxPaint
            )
        } else {
            val gh = fh * 0.18f
            canvas.drawRoundRect(
                RectF(ox + fw * 0.08f, oy + fh / 2 - gh / 2, ox + fw * 0.92f, oy + fh / 2 + gh / 2),
                14f, 14f, guidePaint
            )
        }

        // substitute each decoded letter over its source digits
        for (cell in cells) {
            if (cell.ch == '·' || cell.ch == ' ') continue
            val l = mapX(cell.box.left); val t = mapY(cell.box.top)
            val r = mapX(cell.box.right); val bot = mapY(cell.box.bottom)
            canvas.drawRoundRect(RectF(l, t, r, bot), 8f, 8f, coverPaint)
            val cellH = bot - t
            letterPaint.textSize = cellH * 0.7f
            val cx = (l + r) / 2f
            // vertically centre the glyph
            val fm = letterPaint.fontMetrics
            val cy = (t + bot) / 2f - (fm.ascent + fm.descent) / 2f
            canvas.drawText(cell.ch.toString(), cx, cy, letterPaint)
        }
    }
}
