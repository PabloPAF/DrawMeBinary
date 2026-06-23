package com.pafska.drawmebinary.decode

/**
 * On-device decoder for clean **printed** 0/1 artwork, using a projection-based
 * grid read (robust to merged/split digits, unlike per-glyph segmentation):
 *
 *   1. rotation-aware downscale of the luma plane to an upright grayscale grid
 *   2. adaptive (local-mean) threshold, gated to dark-on-light, -> ink mask
 *   3. horizontal ink projection -> ROW stripes; vertical projection (within
 *      those rows) -> COLUMN stripes. This recovers the grid directly from ink
 *      density, so it doesn't matter if two digits touch or one breaks up.
 *   4. classify each grid cell as '1' (stroke through the centre) or '0'
 *      (hollow centre) by central ink fill.
 *   5. assemble: ~8 columns -> 8-bit bytes per row; ~4 columns -> 4-bit nibble
 *      pairs (top row = high nibble, bottom = low) - the web app's two layouts.
 *
 * Returns the detected block's bounding box (normalized, upright) so the UI can
 * snap the reticle to it. Constants are tuned for printed pages; see
 * docs/DECODER_PORT.md.
 */
class PrintedBinaryDecoder : BinaryDecoder {

    // --- tuning knobs ---
    private val targetMax = 640       // longest upright edge after downscale
    private val adaptRadius = 22      // local-mean window radius (px, downscaled)
    private val adaptC = 16           // how much darker than local mean = ink (higher = ignore faint texture)
    private val brightGate = 110      // surroundings must be this bright (dark-on-light)
    private val minInkFrac = 0.0008f
    private val maxInkFrac = 0.45f
    private val bandFrac = 0.30f      // band threshold as fraction of projection peak
    private val centerOneThresh = 0.5f

    // --- scratch buffers, reused across frames (analysis is single-threaded) ---
    private var sw = 0
    private var sh = 0
    private var gray = IntArray(0)
    private var integ = LongArray(0)
    private var ink = BooleanArray(0)
    private var rowInk = IntArray(0)
    private var colInk = IntArray(0)
    private var smoothBuf = IntArray(0)

    private fun ensure(w: Int, h: Int) {
        if (w == sw && h == sh) return
        sw = w; sh = h
        gray = IntArray(w * h)
        integ = LongArray((w + 1) * (h + 1))
        ink = BooleanArray(w * h)
        rowInk = IntArray(h)
        colInk = IntArray(w)
        smoothBuf = IntArray(maxOf(w, h))
    }

    override fun decode(frame: LumaFrame): DecodeResult {
        val rot = ((frame.rotationDegrees % 360) + 360) % 360
        val upW: Int; val upH: Int
        if (rot == 90 || rot == 270) { upW = frame.height; upH = frame.width }
        else { upW = frame.width; upH = frame.height }

        val scale = targetMax.toFloat() / maxOf(upW, upH)
        val w = maxOf(1, (upW * scale).toInt())
        val h = maxOf(1, (upH * scale).toInt())
        ensure(w, h)

        sampleUpright(frame, rot, w, h, scale)
        buildIntegral(w, h)
        val frac = adaptiveThreshold(w, h)
        if (frac < minInkFrac || frac > maxInkFrac) return DecodeResult.EMPTY

        // --- ROW stripes from the horizontal ink projection ---
        for (y in 0 until h) {
            var c = 0
            val base = y * w
            for (x in 0 until w) if (ink[base + x]) c++
            rowInk[y] = c
        }
        val rows = findBands(rowInk, h, maxOf(3, (h * 0.02f).toInt()), maxOf(1, (h * 0.004f).toInt()))
        if (rows.size < 2) return DecodeResult.EMPTY

        val yTop = rows.first()[0]; val yBot = rows.last()[1]

        // --- COLUMN stripes from the vertical projection within the rows ---
        java.util.Arrays.fill(colInk, 0, w, 0)
        for (y in yTop until yBot) {
            val base = y * w
            for (x in 0 until w) if (ink[base + x]) colInk[x]++
        }
        val cols = findBands(colInk, w, maxOf(3, (w * 0.02f).toInt()), maxOf(1, (w * 0.004f).toInt()))
        if (cols.size < 2) return DecodeResult.EMPTY

        val xLeft = cols.first()[0]; val xRight = cols.last()[1]
        val box = NormBox(xLeft.toFloat() / w, yTop.toFloat() / h,
            xRight.toFloat() / w, yBot.toFloat() / h)
        val glyphCount = rows.size * cols.size

        // --- read the grid ---
        val c = cols.size
        val sb = StringBuilder()
        val fmt: BitFormat
        if (c >= 7) {                                   // 8-bit bytes per row
            fmt = BitFormat.EIGHT_BIT
            for (rb in rows) {
                val bits = StringBuilder()
                for (cb in cols) bits.append(classifyCell(rb[0], rb[1], cb[0], cb[1], w, h))
                var i = 0
                while (i + 8 <= bits.length) { sb.append(bitsToChar(bits.substring(i, i + 8))); i += 8 }
            }
        } else {                                        // 4-bit nibble pairs
            fmt = BitFormat.FOUR_BIT_NIBBLE
            var i = 0
            while (i + 1 < rows.size) {
                val hi = StringBuilder(); val lo = StringBuilder()
                for (cb in cols) hi.append(classifyCell(rows[i][0], rows[i][1], cb[0], cb[1], w, h))
                for (cb in cols) lo.append(classifyCell(rows[i + 1][0], rows[i + 1][1], cb[0], cb[1], w, h))
                if (hi.length + lo.length == 8) sb.append(bitsToChar(hi.toString() + lo.toString()))
                i += 2
            }
        }

        val text = sb.toString()
        if (text.isBlank()) return DecodeResult("", 0f, fmt, glyphCount, box)
        val printable = text.count { it.code in 32..126 && it != '·' }
        val conf = printable.toFloat() / text.length
        return DecodeResult(text.trim(), conf, fmt, glyphCount, box)
    }

    // ---- stages -----------------------------------------------------------

    private fun sampleUpright(frame: LumaFrame, rot: Int, w: Int, h: Int, scale: Float) {
        val rawW = frame.width; val rawH = frame.height
        val upW = if (rot == 90 || rot == 270) rawH else rawW
        val upH = if (rot == 90 || rot == 270) rawW else rawH
        var i = 0
        for (sy in 0 until h) {
            val uy = (sy / scale).toInt().coerceIn(0, upH - 1)
            for (sx in 0 until w) {
                val ux = (sx / scale).toInt().coerceIn(0, upW - 1)
                val rx: Int; val ry: Int
                when (rot) {
                    90 -> { rx = uy; ry = rawH - 1 - ux }
                    180 -> { rx = rawW - 1 - ux; ry = rawH - 1 - uy }
                    270 -> { rx = rawW - 1 - uy; ry = ux }
                    else -> { rx = ux; ry = uy }
                }
                gray[i++] = frame.pixel(rx.coerceIn(0, rawW - 1), ry.coerceIn(0, rawH - 1))
            }
        }
    }

    private fun buildIntegral(w: Int, h: Int) {
        val w1 = w + 1
        for (y in 0 until h) {
            var rowsum = 0L
            val rowBase = y * w
            for (x in 0 until w) {
                rowsum += gray[rowBase + x]
                integ[(y + 1) * w1 + (x + 1)] = integ[y * w1 + (x + 1)] + rowsum
            }
        }
    }

    private fun adaptiveThreshold(w: Int, h: Int): Float {
        val w1 = w + 1
        val r = adaptRadius
        var inkCount = 0
        for (y in 0 until h) {
            val y0 = maxOf(0, y - r); val y1 = minOf(h, y + r + 1)
            for (x in 0 until w) {
                val x0 = maxOf(0, x - r); val x1 = minOf(w, x + r + 1)
                val area = (x1 - x0) * (y1 - y0)
                val s = integ[y1 * w1 + x1] - integ[y0 * w1 + x1] -
                    integ[y1 * w1 + x0] + integ[y0 * w1 + x0]
                val mean = (s / area).toInt()
                val isInk = mean > brightGate && gray[y * w + x] < mean - adaptC
                ink[y * w + x] = isInk
                if (isInk) inkCount++
            }
        }
        return inkCount.toFloat() / (w * h)
    }

    /**
     * Turn a 1-D projection into stripes (bands). The projection is smoothed to
     * suppress single-pixel texture noise; runs above a fraction of the peak
     * become bands; bands separated by <= mergeGap are joined; bands narrower
     * than minWidth (i.e. speckle, not a real row/column) are dropped.
     */
    private fun findBands(proj: IntArray, n: Int, minWidth: Int, mergeGap: Int): List<IntArray> {
        val sm = smoothBuf
        val r = 1
        for (i in 0 until n) {
            var s = 0; var cnt = 0; var k = maxOf(0, i - r); val e = minOf(n, i + r + 1)
            while (k < e) { s += proj[k]; cnt++; k++ }
            sm[i] = s / cnt
        }
        var peak = 0
        for (i in 0 until n) if (sm[i] > peak) peak = sm[i]
        if (peak == 0) return emptyList()
        val thr = (peak * bandFrac).toInt().coerceAtLeast(1)

        val raw = ArrayList<IntArray>()
        var st = -1
        for (i in 0 until n) {
            val on = sm[i] > thr
            if (on && st < 0) st = i
            if (!on && st >= 0) { raw.add(intArrayOf(st, i)); st = -1 }
        }
        if (st >= 0) raw.add(intArrayOf(st, n))

        val merged = ArrayList<IntArray>()
        for (b in raw) {
            val last = merged.lastOrNull()
            if (last != null && b[0] - last[1] <= mergeGap) last[1] = b[1]
            else merged.add(b)
        }
        return merged.filter { it[1] - it[0] >= minWidth }
    }

    /** '1' if the cell's centre is mostly ink (stroke), else '0' (hollow). */
    private fun classifyCell(y0: Int, y1: Int, x0: Int, x1: Int, w: Int, h: Int): Char {
        val cw = x1 - x0; val ch = y1 - y0
        val ix0 = (x0 + cw * 0.25f).toInt().coerceIn(0, w - 1)
        val ix1 = (x1 - cw * 0.25f).toInt().coerceIn(ix0 + 1, w)
        val iy0 = (y0 + ch * 0.2f).toInt().coerceIn(0, h - 1)
        val iy1 = (y1 - ch * 0.2f).toInt().coerceIn(iy0 + 1, h)
        var inkc = 0; var tot = 0
        for (yy in iy0 until iy1) {
            val base = yy * w
            for (xx in ix0 until ix1) { tot++; if (ink[base + xx]) inkc++ }
        }
        val centerFill = if (tot > 0) inkc.toFloat() / tot else 0f
        return if (centerFill >= centerOneThresh) '1' else '0'
    }

    private fun bitsToChar(b: String): Char {
        val v = b.toInt(2)
        return if (v in 32..126) v.toChar() else '·'
    }
}
