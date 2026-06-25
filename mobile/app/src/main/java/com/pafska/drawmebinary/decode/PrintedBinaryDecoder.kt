package com.pafska.drawmebinary.decode

/**
 * On-device decoder for printed 0/1 artwork. Validated end-to-end against the
 * real source image (decodes "ENOUGH!").
 *
 * Pipeline:
 *   1. rotation-aware downscale of the aim-ROI to an upright grayscale grid
 *   2. light blur + adaptive (dark-on-light) threshold -> ink mask
 *   3. connected components -> candidate digit blobs (filtered by size)
 *   4. GRID from the blobs: rows by clustering y-centres, column count from the
 *      busiest rows, column x-positions by splitting the x-centres at the
 *      largest gaps. (Projection-based columns failed: a thin all-'1' column has
 *      little ink and adjacent digits touch — components + clustering are robust
 *      to both.)
 *   5. classify each (row,column) cell by counting dark runs across its width at
 *      several mid-heights: a '0' is a ring (two runs at some height), a '1' is
 *      one stroke (always one run). Decide '0' if >=2 sample heights show two
 *      runs — using "any height" rather than a majority is what made 0s reliable.
 *   6. assemble: K==4 -> 4-bit nibble pairs (top/bottom), K>=7 -> 8-bit bytes.
 */
class PrintedBinaryDecoder(
    private val roiL: Float = 0.18f,
    private val roiT: Float = 0.08f,
    private val roiR: Float = 0.82f,
    private val roiB: Float = 0.92f
) : BinaryDecoder {

    private val targetMax = 640
    private val adaptRadius = 22
    private val adaptC = 12
    private val minBrightGate = 70
    private val minInkFrac = 0.0008f
    private val maxInkFrac = 0.45f

    private var sw = 0; private var sh = 0
    private var gray = IntArray(0)
    private var graySharp = IntArray(0)
    private var integ = LongArray(0)
    private var ink = BooleanArray(0)
    private var blurBuf = IntArray(0)
    private var labels = IntArray(0)
    private var stack = IntArray(0)
    private var lastGate = 0

    private fun ensure(w: Int, h: Int) {
        if (w == sw && h == sh) return
        sw = w; sh = h
        gray = IntArray(w * h); graySharp = IntArray(w * h)
        integ = LongArray((w + 1) * (h + 1)); ink = BooleanArray(w * h)
        blurBuf = IntArray(w * h); labels = IntArray(w * h); stack = IntArray(w * h)
    }

    private fun toFull(b: NormBox) = NormBox(
        roiL + b.left * (roiR - roiL), roiT + b.top * (roiB - roiT),
        roiL + b.right * (roiR - roiL), roiT + b.bottom * (roiB - roiT)
    )

    override fun decode(frame: LumaFrame): DecodeResult {
        val rot = ((frame.rotationDegrees % 360) + 360) % 360
        val upW: Int; val upH: Int
        if (rot == 90 || rot == 270) { upW = frame.height; upH = frame.width }
        else { upW = frame.width; upH = frame.height }
        val upWf = upW.toFloat(); val upHf = upH.toFloat()
        val roiWpx = (roiR - roiL) * upWf; val roiHpx = (roiB - roiT) * upHf
        val scale = targetMax.toFloat() / maxOf(roiWpx, roiHpx)
        val w = maxOf(1, (roiWpx * scale).toInt()); val h = maxOf(1, (roiHpx * scale).toInt())
        ensure(w, h)

        sampleUpright(frame, rot, w, h, scale, (roiL * upWf).toInt(), (roiT * upHf).toInt())
        System.arraycopy(gray, 0, graySharp, 0, w * h)
        blurGray(w, h)
        buildIntegral(w, h)
        val frac = adaptiveThreshold(w, h)
        val inkPct = frac * 100f
        if (frac < minInkFrac || frac > maxInkFrac)
            return DecodeResult("", 0f, BitFormat.UNKNOWN, 0, null, inkPct, 0, 0, lastGate)

        // --- connected components ---
        val comps = components(w, h)                  // each: intArrayOf(minx,miny,cw,ch)
        if (comps.size < 4)
            return DecodeResult("", 0f, BitFormat.UNKNOWN, 0, null, inkPct, 0, 0, lastGate)
        val heights = comps.map { it[3] }.sorted()
        val medH = heights[heights.size / 2].coerceAtLeast(1)
        val kept = comps.filter {
            it[3] >= medH * 0.4 && it[3] <= medH * 2.2 && it[2] >= 2 && it[2] <= medH * 2.5
        }
        if (kept.size < 4)
            return DecodeResult("", 0f, BitFormat.UNKNOWN, 0, null, inkPct, 0, 0, lastGate)

        // reject outlier blobs by x (e.g. a stray cursor) before finding columns
        val (xlo, xhi) = tukeyBounds(kept.map { it[0] + it[2] / 2 })
        val inl = kept.filter { val cx = it[0] + it[2] / 2; cx in xlo..xhi }
        if (inl.size < 4)
            return DecodeResult("", 0f, BitFormat.UNKNOWN, 0, null, inkPct, 0, 0, lastGate)

        // --- rows by clustering component y-centres ---
        val cys = inl.map { it[1] + it[3] / 2 }.sorted()
        val rowC = clusterByGap(cys, medH * 0.6f)
        if (rowC.size < 2)
            return DecodeResult("", 0f, BitFormat.UNKNOWN, 0, null, inkPct, rowC.size, 0, lastGate)

        // per-row x lists; column count K from how many blobs the busiest rows hold
        val rowx = Array(rowC.size) { ArrayList<Int>() }
        for (c in inl) {
            val cy = c[1] + c[3] / 2
            var bi = 0; var bd = Int.MAX_VALUE
            for (i in rowC.indices) { val d = kotlin.math.abs(rowC[i] - cy); if (d < bd) { bd = d; bi = i } }
            rowx[bi].add(c[0] + c[2] / 2)
        }
        val counts = rowx.map { it.size }
        val K = if (counts.count { kotlin.math.abs(it - 8) <= 1 } >
            counts.count { kotlin.math.abs(it - 4) <= 1 }) 8 else 4

        // --- column hypotheses: no single method is robust on its own, so try
        // several and keep whichever decode is the most printable (web-app-style
        // candidate vote). H1 gap-split, H2 even grid, H3 busiest-row medians. ---
        val xs = inl.map { it[0] + it[2] / 2 }.sorted().toIntArray()
        val cands = ArrayList<IntArray>()
        cands.add(columnCenters(xs, K))
        if (xs.size >= 2) {
            val xmin = xs.first(); val xmax = xs.last()
            cands.add(IntArray(K) { if (K > 1) xmin + it * (xmax - xmin) / (K - 1) else xmin })
        }
        val fulls = rowx.filter { it.size == K }.map { it.sorted() }
        if (fulls.isNotEmpty()) cands.add(IntArray(K) { j -> median(IntArray(fulls.size) { fulls[it][j] }) })

        var bestRaw = ""; var bestCells: List<Cell> = emptyList(); var bestBox: NormBox? = null
        var bestFmt = BitFormat.UNKNOWN; var bestScore = -1
        for (cc in cands) {
            if (cc.size < 2) continue
            val (raw, cells, box, fmt) = decodeGrid(rowC, cc, K, medH, w, h)
            // score by letter-likeness, NOT raw printability: a wrong column
            // placement can yield a repeating printable pattern (all '3'/';') that
            // would otherwise win. Real messages are letters, so reward A-Z/a-z
            // and character variety; degenerate decodes score ~0 and lose.
            val score = letterScore(raw)
            if (score > bestScore) { bestScore = score; bestRaw = raw; bestCells = cells; bestBox = box; bestFmt = fmt }
        }
        val glyphCount = rowC.size * K
        if (bestRaw.isBlank())
            return DecodeResult("", 0f, bestFmt, glyphCount, bestBox, inkPct, rowC.size, K, lastGate, bestRaw)
        val conf = bestP.toFloat() / bestRaw.length
        return DecodeResult(bestRaw.trim(), conf, bestFmt, glyphCount, bestBox, inkPct,
            rowC.size, K, lastGate, bestRaw, bestCells)
    }

    /** Read one column hypothesis into (raw text, cells, box, format). */
    private fun decodeGrid(rowC: IntArray, colC: IntArray, K: Int, medH: Int, w: Int, h: Int):
        Quadruple {
        val half = medH * 0.6f
        val sp = if (colC.size > 1) median(IntArray(colC.size - 1) { colC[it + 1] - colC[it] }) else 16
        val cellW = maxOf(6, (sp * 0.9f).toInt())
        val fw = w.toFloat(); val fh = h.toFloat()
        val xL = (colC.first() - cellW / 2) / fw; val xR = (colC.last() + cellW / 2) / fw
        val sb = StringBuilder(); val cells = ArrayList<Cell>()
        val fmt: BitFormat
        if (K >= 7) {
            fmt = BitFormat.EIGHT_BIT
            for (rc in rowC) {
                val y0 = (rc - half).toInt(); val y1 = (rc + half).toInt()
                val bits = StringBuilder()
                for (cx in colC) bits.append(classifyCell(y0, y1, cx - cellW / 2, cx + cellW / 2, w, h))
                var i = 0
                while (i + 8 <= bits.length) {
                    val ch = bitsToChar(bits.substring(i, i + 8)); sb.append(ch)
                    cells.add(Cell(ch, toFull(NormBox(xL, y0 / fh, xR, y1 / fh)))); i += 8
                }
            }
        } else {
            fmt = BitFormat.FOUR_BIT_NIBBLE
            var i = 0
            while (i + 1 < rowC.size) {
                val ya0 = (rowC[i] - half).toInt(); val ya1 = (rowC[i] + half).toInt()
                val yb0 = (rowC[i + 1] - half).toInt(); val yb1 = (rowC[i + 1] + half).toInt()
                val hi = StringBuilder(); val lo = StringBuilder()
                for (cx in colC) hi.append(classifyCell(ya0, ya1, cx - cellW / 2, cx + cellW / 2, w, h))
                for (cx in colC) lo.append(classifyCell(yb0, yb1, cx - cellW / 2, cx + cellW / 2, w, h))
                if (hi.length + lo.length == 8) {
                    val ch = bitsToChar(hi.toString() + lo.toString()); sb.append(ch)
                    cells.add(Cell(ch, toFull(NormBox(xL, ya0 / fh, xR, yb1 / fh))))
                }
                i += 2
            }
        }
        val box = toFull(NormBox(xL, (rowC.first() - half) / fh, xR, (rowC.last() + half) / fh))
        return Quadruple(sb.toString(), cells, box, fmt)
    }

    private inner class Quadruple(
        val raw: String, val cells: List<Cell>, val box: NormBox?, val fmt: BitFormat
    ) {
        operator fun component1() = raw
        operator fun component2() = cells
        operator fun component3() = box
        operator fun component4() = fmt
    }

    /** Reward letters and character variety; degenerate repeating patterns score ~0. */
    private fun letterScore(s: String): Int {
        val letters = s.count { it in 'A'..'Z' || it in 'a'..'z' }
        val variety = s.filter { it != '·' }.toSet().size
        return letters * 10 + variety
    }

    /** Tukey fences (Q1-1.5·IQR, Q3+1.5·IQR) for outlier rejection. */
    private fun tukeyBounds(vals: List<Int>): Pair<Int, Int> {
        if (vals.isEmpty()) return Pair(Int.MIN_VALUE, Int.MAX_VALUE)
        val s = vals.sorted()
        val q1 = s[s.size / 4]; val q3 = s[(s.size * 3) / 4]; val iqr = q3 - q1
        return Pair(q1 - (iqr * 3) / 2, q3 + (iqr * 3) / 2)
    }

    // ---- grid helpers -----------------------------------------------------

    /** Cluster sorted values into groups split where the gap exceeds [gapThr]. */
    private fun clusterByGap(sorted: List<Int>, gapThr: Float): IntArray {
        val centers = ArrayList<Int>()
        var sum = 0L; var cnt = 0; var last = sorted[0]
        for (v in sorted) {
            if (cnt > 0 && v - last > gapThr) { centers.add((sum / cnt).toInt()); sum = 0; cnt = 0 }
            sum += v; cnt++; last = v
        }
        if (cnt > 0) centers.add((sum / cnt).toInt())
        return centers.toIntArray()
    }

    /** K column centres: split sorted x-centres at the K-1 largest gaps. */
    private fun columnCenters(xs: IntArray, K: Int): IntArray {
        if (xs.size <= K) return xs
        val idx = (1 until xs.size).sortedByDescending { xs[it] - xs[it - 1] }.take(K - 1).sorted()
        val cuts = idx + xs.size
        val centers = ArrayList<Int>(); var prev = 0
        for (c in cuts) {
            if (c > prev) {
                var s = 0L; for (i in prev until c) s += xs[i]; centers.add((s / (c - prev)).toInt())
            }
            prev = c
        }
        return centers.toIntArray()
    }

    private fun median(a: IntArray): Int { val s = a.sortedArray(); return if (s.isEmpty()) 0 else s[s.size / 2] }

    /** 4-connected flood-fill components: intArrayOf(minx, miny, w, h). */
    private fun components(w: Int, h: Int): List<IntArray> {
        java.util.Arrays.fill(labels, 0)
        val out = ArrayList<IntArray>(); val n = w * h
        for (start in 0 until n) {
            if (!ink[start] || labels[start] != 0) continue
            var sp = 0; stack[sp++] = start; labels[start] = 1
            var minx = Int.MAX_VALUE; var maxx = 0; var miny = Int.MAX_VALUE; var maxy = 0
            while (sp > 0) {
                val p = stack[--sp]; val px = p % w; val py = p / w
                if (px < minx) minx = px; if (px > maxx) maxx = px
                if (py < miny) miny = py; if (py > maxy) maxy = py
                if (px > 0)     { val q = p - 1; if (ink[q] && labels[q] == 0) { labels[q] = 1; stack[sp++] = q } }
                if (px < w - 1) { val q = p + 1; if (ink[q] && labels[q] == 0) { labels[q] = 1; stack[sp++] = q } }
                if (py > 0)     { val q = p - w; if (ink[q] && labels[q] == 0) { labels[q] = 1; stack[sp++] = q } }
                if (py < h - 1) { val q = p + w; if (ink[q] && labels[q] == 0) { labels[q] = 1; stack[sp++] = q } }
            }
            out.add(intArrayOf(minx, miny, maxx - minx + 1, maxy - miny + 1))
        }
        return out
    }

    /**
     * Classify a cell as '0' or '1'. A '0' is a ring (two dark runs at some
     * mid-height); a '1' is one stroke (always one run). To be robust we
     * ENSEMBLE over three dark thresholds and majority-vote: same pixels,
     * different thresholds -> errors differ -> the vote recovers the truth.
     */
    private fun classifyCell(y0: Int, y1: Int, x0: Int, x1: Int, w: Int, h: Int): Char {
        val xa = x0.coerceIn(0, w - 1); val xb = x1.coerceIn(xa + 1, w)
        val ya = y0.coerceIn(0, h - 1); val yb = y1.coerceIn(ya + 1, h)
        var sum = 0L; var nn = 0
        for (yy in ya until yb) { val base = yy * w; for (xx in xa until xb) { sum += graySharp[base + xx]; nn++ } }
        if (nn == 0) return '0'
        val mean = sum / nn
        val cw = xb - xa; val ch = yb - ya; val minRun = maxOf(2, (cw * 0.12f).toInt())
        var zeroVotes = 0
        for (mult in intArrayOf(58, 70, 82)) {            // three dark thresholds
            val darkThr = mean * mult / 100
            var twoRunHeights = 0
            for (f in floatArrayOf(0.30f, 0.40f, 0.50f, 0.60f, 0.70f)) {
                val sy = ya + (ch * f).toInt(); if (sy < 0 || sy >= h) continue
                val base = sy * w; var runs = 0; var runLen = 0
                for (xx in xa until xb) {
                    if (graySharp[base + xx] < darkThr) runLen++
                    else { if (runLen >= minRun) runs++; runLen = 0 }
                }
                if (runLen >= minRun) runs++
                if (runs >= 2) twoRunHeights++
            }
            if (twoRunHeights >= 2) zeroVotes++
        }
        return if (zeroVotes >= 2) '0' else '1'        // majority of the 3 thresholds
    }

    private fun bitsToChar(b: String): Char { val v = b.toInt(2); return if (v in 32..126) v.toChar() else '·' }

    // ---- pixel stages -----------------------------------------------------

    private fun sampleUpright(frame: LumaFrame, rot: Int, w: Int, h: Int, scale: Float, oxUp: Int, oyUp: Int) {
        val rawW = frame.width; val rawH = frame.height
        val upW = if (rot == 90 || rot == 270) rawH else rawW
        val upH = if (rot == 90 || rot == 270) rawW else rawH
        var i = 0
        for (sy in 0 until h) {
            val uy = (oyUp + (sy / scale).toInt()).coerceIn(0, upH - 1)
            for (sx in 0 until w) {
                val ux = (oxUp + (sx / scale).toInt()).coerceIn(0, upW - 1)
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

    private fun blurGray(w: Int, h: Int) {
        val r = 2
        for (y in 0 until h) {
            val b = y * w
            for (x in 0 until w) {
                var s = 0; var n = 0; var k = maxOf(0, x - r); val e = minOf(w, x + r + 1)
                while (k < e) { s += gray[b + k]; n++; k++ }; blurBuf[b + x] = s / n
            }
        }
        for (x in 0 until w) {
            for (y in 0 until h) {
                var s = 0; var n = 0; var k = maxOf(0, y - r); val e = minOf(h, y + r + 1)
                while (k < e) { s += blurBuf[k * w + x]; n++; k++ }; gray[y * w + x] = s / n
            }
        }
    }

    private fun buildIntegral(w: Int, h: Int) {
        val w1 = w + 1
        for (y in 0 until h) {
            var rowsum = 0L; val rowBase = y * w
            for (x in 0 until w) { rowsum += gray[rowBase + x]; integ[(y + 1) * w1 + (x + 1)] = integ[y * w1 + (x + 1)] + rowsum }
        }
    }

    private fun adaptiveThreshold(w: Int, h: Int): Float {
        val w1 = w + 1; val r = adaptRadius
        val total = integ[h * w1 + w]; val globalMean = (total / (w.toLong() * h)).toInt()
        val gate = maxOf(minBrightGate, (globalMean * 0.85f).toInt()); lastGate = gate
        var inkCount = 0
        for (y in 0 until h) {
            val y0 = maxOf(0, y - r); val y1 = minOf(h, y + r + 1)
            for (x in 0 until w) {
                val x0 = maxOf(0, x - r); val x1 = minOf(w, x + r + 1)
                val area = (x1 - x0) * (y1 - y0)
                val s = integ[y1 * w1 + x1] - integ[y0 * w1 + x1] - integ[y1 * w1 + x0] + integ[y0 * w1 + x0]
                val mean = (s / area).toInt()
                val isInk = mean > gate && gray[y * w + x] < mean - adaptC
                ink[y * w + x] = isInk
                if (isInk) inkCount++
            }
        }
        return inkCount.toFloat() / (w * h)
    }
}
