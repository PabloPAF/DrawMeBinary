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
    private val adaptC = 12           // how much darker than local mean = ink (lower = catch fainter rows)
    private val minBrightGate = 70    // floor for the adaptive dark-on-light gate
    private val minInkFrac = 0.0008f
    private val maxInkFrac = 0.45f
    private val rowBandFrac = 0.18f   // rows: low threshold so faint rows aren't dropped
    private val colBandFrac = 0.22f   // cols: enough to reject texture but keep narrow all-1 columns
    private val centerOneThresh = 0.5f

    // --- scratch buffers, reused across frames (analysis is single-threaded) ---
    private var sw = 0
    private var sh = 0
    private var gray = IntArray(0)
    private var graySharp = IntArray(0)   // unblurred copy, for crisp 0/1 classification
    private var integ = LongArray(0)
    private var ink = BooleanArray(0)
    private var rowInk = IntArray(0)
    private var colInk = IntArray(0)
    private var smoothBuf = IntArray(0)
    private var blurBuf = IntArray(0)
    private var lastGate = 0          // brightness gate from the latest threshold pass

    private fun ensure(w: Int, h: Int) {
        if (w == sw && h == sh) return
        sw = w; sh = h
        gray = IntArray(w * h)
        graySharp = IntArray(w * h)
        integ = LongArray((w + 1) * (h + 1))
        ink = BooleanArray(w * h)
        rowInk = IntArray(h)
        colInk = IntArray(w)
        smoothBuf = IntArray(maxOf(w, h))
        blurBuf = IntArray(w * h)
    }

    /**
     * Light separable box blur (radius 2). This deliberately mimics a slightly
     * out-of-focus capture: it smooths away fine screen text / moiré / paper
     * texture so the bold 0/1 strokes dominate the projection. (The user noticed
     * decoding worked better when the camera was NOT perfectly focused.)
     */
    private fun blurGray(w: Int, h: Int) {
        val r = 2
        for (y in 0 until h) {
            val b = y * w
            for (x in 0 until w) {
                var s = 0; var n = 0; var k = maxOf(0, x - r); val e = minOf(w, x + r + 1)
                while (k < e) { s += gray[b + k]; n++; k++ }
                blurBuf[b + x] = s / n
            }
        }
        for (x in 0 until w) {
            for (y in 0 until h) {
                var s = 0; var n = 0; var k = maxOf(0, y - r); val e = minOf(h, y + r + 1)
                while (k < e) { s += blurBuf[k * w + x]; n++; k++ }
                gray[y * w + x] = s / n
            }
        }
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
        System.arraycopy(gray, 0, graySharp, 0, w * h)  // keep a crisp copy
        blurGray(w, h)                                   // blurred copy drives detection only
        buildIntegral(w, h)
        val frac = adaptiveThreshold(w, h)
        val inkPct = frac * 100f
        if (frac < minInkFrac || frac > maxInkFrac)
            return DecodeResult("", 0f, BitFormat.UNKNOWN, 0, null, inkPct, 0, 0, lastGate)

        // --- ROW stripes from the horizontal ink projection ---
        for (y in 0 until h) {
            var c = 0
            val base = y * w
            for (x in 0 until w) if (ink[base + x]) c++
            rowInk[y] = c
        }
        val rows = findBands(rowInk, h, maxOf(2, (h * 0.015f).toInt()), maxOf(1, (h * 0.004f).toInt()), rowBandFrac)
        if (rows.size < 2)
            return DecodeResult("", 0f, BitFormat.UNKNOWN, 0, null, inkPct, rows.size, 0, lastGate)

        val yTop = rows.first()[0]; val yBot = rows.last()[1]

        // --- COLUMN stripes from the vertical projection within the rows ---
        java.util.Arrays.fill(colInk, 0, w, 0)
        for (y in yTop until yBot) {
            val base = y * w
            for (x in 0 until w) if (ink[base + x]) colInk[x]++
        }
        val cols = findBands(colInk, w, maxOf(3, (w * 0.02f).toInt()), maxOf(1, (w * 0.004f).toInt()), colBandFrac)
        if (cols.size < 2)
            return DecodeResult("", 0f, BitFormat.UNKNOWN, 0, null, inkPct, rows.size, cols.size, lastGate)

        val xLeft = cols.first()[0]; val xRight = cols.last()[1]
        val box = NormBox(xLeft.toFloat() / w, yTop.toFloat() / h,
            xRight.toFloat() / w, yBot.toFloat() / h)
        val glyphCount = rows.size * cols.size

        // --- read the grid ---
        val c = cols.size
        val sb = StringBuilder()
        val cells = ArrayList<Cell>()
        val fw = w.toFloat(); val fh = h.toFloat()
        val fmt: BitFormat
        if (c >= 7) {                                   // 8-bit bytes per row
            fmt = BitFormat.EIGHT_BIT
            for (rb in rows) {
                val bits = StringBuilder()
                for (cb in cols) bits.append(classifyCell(rb[0], rb[1], cb[0], cb[1], w, h))
                var i = 0
                while (i + 8 <= bits.length) {
                    val ch = bitsToChar(bits.substring(i, i + 8))
                    sb.append(ch)
                    cells.add(Cell(ch, NormBox(cols[i][0] / fw, rb[0] / fh,
                        cols[i + 7][1] / fw, rb[1] / fh)))
                    i += 8
                }
            }
        } else {                                        // 4-bit nibble pairs
            fmt = BitFormat.FOUR_BIT_NIBBLE
            var i = 0
            while (i + 1 < rows.size) {
                val hi = StringBuilder(); val lo = StringBuilder()
                for (cb in cols) hi.append(classifyCell(rows[i][0], rows[i][1], cb[0], cb[1], w, h))
                for (cb in cols) lo.append(classifyCell(rows[i + 1][0], rows[i + 1][1], cb[0], cb[1], w, h))
                if (hi.length + lo.length == 8) {
                    val ch = bitsToChar(hi.toString() + lo.toString())
                    sb.append(ch)
                    // one byte spans the two rows -> letter covers both
                    cells.add(Cell(ch, NormBox(xLeft / fw, rows[i][0] / fh,
                        xRight / fw, rows[i + 1][1] / fh)))
                }
                i += 2
            }
        }

        val raw = sb.toString()
        if (raw.isBlank())
            return DecodeResult("", 0f, fmt, glyphCount, box, inkPct, rows.size, cols.size, lastGate, raw)
        val printable = raw.count { it.code in 32..126 && it != '·' }
        val conf = printable.toFloat() / raw.length
        return DecodeResult(raw.trim(), conf, fmt, glyphCount, box, inkPct,
            rows.size, cols.size, lastGate, raw, cells)
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
        // adaptive dark-on-light gate: "bright" is relative to the whole frame,
        // so it works in dim light too (a fixed value rejected dim grey pages)
        val total = integ[h * w1 + w]
        val globalMean = (total / (w.toLong() * h)).toInt()
        val gate = maxOf(minBrightGate, (globalMean * 0.85f).toInt())
        lastGate = gate
        var inkCount = 0
        for (y in 0 until h) {
            val y0 = maxOf(0, y - r); val y1 = minOf(h, y + r + 1)
            for (x in 0 until w) {
                val x0 = maxOf(0, x - r); val x1 = minOf(w, x + r + 1)
                val area = (x1 - x0) * (y1 - y0)
                val s = integ[y1 * w1 + x1] - integ[y0 * w1 + x1] -
                    integ[y1 * w1 + x0] + integ[y0 * w1 + x0]
                val mean = (s / area).toInt()
                val isInk = mean > gate && gray[y * w + x] < mean - adaptC
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
    private fun findBands(proj: IntArray, n: Int, minWidth: Int, mergeGap: Int, frac: Float): List<IntArray> {
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
        val thr = (peak * frac).toInt().coerceAtLeast(1)

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

    /**
     * '1' if the cell's centre is mostly ink (the vertical stroke), else '0'
     * (hollow centre). Reads the SHARP image with a per-cell threshold so the
     * hole of a '0' is preserved — blurring it shut was turning 0s into 1s.
     */
    private fun classifyCell(y0: Int, y1: Int, x0: Int, x1: Int, w: Int, h: Int): Char {
        // cell mean from the crisp image (dominated by the bright page)
        var sum = 0L; var n = 0
        for (yy in y0 until y1) {
            val base = yy * w
            for (xx in x0 until x1) { sum += graySharp[base + xx]; n++ }
        }
        if (n == 0) return '0'
        val darkThr = (sum / n) * 7 / 10        // dark = clearly below the cell's mean

        val cw = x1 - x0; val ch = y1 - y0
        val ix0 = (x0 + cw * 0.25f).toInt().coerceIn(0, w - 1)
        val ix1 = (x1 - cw * 0.25f).toInt().coerceIn(ix0 + 1, w)
        val iy0 = (y0 + ch * 0.2f).toInt().coerceIn(0, h - 1)
        val iy1 = (y1 - ch * 0.2f).toInt().coerceIn(iy0 + 1, h)
        var inkc = 0; var tot = 0
        for (yy in iy0 until iy1) {
            val base = yy * w
            for (xx in ix0 until ix1) { tot++; if (graySharp[base + xx] < darkThr) inkc++ }
        }
        val centerFill = if (tot > 0) inkc.toFloat() / tot else 0f
        return if (centerFill >= centerOneThresh) '1' else '0'
    }

    private fun bitsToChar(b: String): Char {
        val v = b.toInt(2)
        return if (v in 32..126) v.toChar() else '·'
    }
}
