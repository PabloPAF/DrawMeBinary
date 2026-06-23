package com.pafska.drawmebinary.decode

import kotlin.math.abs

/**
 * First real on-device decoder (Milestone 2) for clean **printed** 0/1 artwork.
 *
 * Pure Kotlin (no OpenCV yet), tuned to run per-frame:
 *   1. rotation-aware downscale of the luma plane to an upright grayscale grid
 *   2. adaptive (local-mean) threshold -> ink mask  (ignores large uniform
 *      dark areas like a desk, unlike a global threshold)
 *   3. connected-component labelling -> candidate glyph boxes
 *   4. size/aspect filtering, then 0/1 classification by central ink fill
 *      ('1' has the stroke through its centre; '0' is hollow there)
 *   5. group glyphs into lines; decode either as 8-bit-per-line bytes or, when
 *      lines are ~4 glyphs, as 4-bit nibble pairs (top = high, bottom = low) -
 *      the same two layouts the web app produces.
 *
 * It also returns the bounding box of the detected block (normalized, upright)
 * so the UI can snap the reticle to it. This is a first pass: thresholds and
 * the 0/1 heuristic will need tuning on real photos; see docs/DECODER_PORT.md.
 */
class PrintedBinaryDecoder : BinaryDecoder {

    // --- tuning knobs ---
    private val targetMax = 480       // longest upright edge after downscale
    private val adaptRadius = 18      // local-mean window radius (px, downscaled)
    private val adaptC = 8            // how much darker than local mean = ink
    private val minInkFrac = 0.001f
    private val maxInkFrac = 0.35f    // above this the threshold clearly failed
    private val centerOneThresh = 0.5f

    // --- scratch buffers, reused across frames (analysis is single-threaded) ---
    private var sw = 0
    private var sh = 0
    private var gray = IntArray(0)
    private var integ = LongArray(0)
    private var ink = BooleanArray(0)
    private var labels = IntArray(0)
    private var stack = IntArray(0)

    private data class Glyph(val x: Int, val y: Int, val w: Int, val h: Int, val count: Int)
    private data class GlyphBit(val g: Glyph, val bit: Char)

    private fun ensure(w: Int, h: Int) {
        if (w == sw && h == sh) return
        sw = w; sh = h
        gray = IntArray(w * h)
        integ = LongArray((w + 1) * (h + 1))
        ink = BooleanArray(w * h)
        labels = IntArray(w * h)
        stack = IntArray(w * h)
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

        val glyphs = components(w, h)
        if (glyphs.size < 4) return DecodeResult.EMPTY

        val heights = glyphs.map { it.h }.sorted()
        val medH = heights[heights.size / 2].coerceAtLeast(1)
        val kept = glyphs.filter { g ->
            g.h >= medH * 0.5 && g.h <= medH * 2.0 &&
                g.w >= 2 && g.h >= 4 &&
                (g.w.toFloat() / g.h) in 0.12f..1.4f &&
                g.count >= g.w * g.h * 0.12f
        }
        if (kept.size < 4) return DecodeResult.EMPTY

        // classify + group into lines (upright space: lines run horizontally)
        val bits = kept.map { GlyphBit(it, classify(it, w, h)) }
            .sortedBy { it.g.y + it.g.h / 2 }
        val lines = ArrayList<MutableList<GlyphBit>>()
        for (gb in bits) {
            val cy = gb.g.y + gb.g.h / 2
            val line = lines.lastOrNull()
            if (line != null) {
                val lcy = line.map { it.g.y + it.g.h / 2.0 }.average()
                if (abs(cy - lcy) < medH * 0.6) { line.add(gb); continue }
            }
            lines.add(mutableListOf(gb))
        }
        for (line in lines) line.sortBy { it.g.x }
        lines.sortBy { l -> l.minOf { it.g.y } }

        val perLine = lines.map { it.size }.sorted()
        val medLen = perLine[perLine.size / 2]
        val text = if (medLen >= 7) decode8bit(lines) else decode4bit(lines)
        if (text.isBlank()) return DecodeResult.EMPTY

        val printable = text.count { it.code in 32..126 }
        val conf = printable.toFloat() / text.length

        val minX = kept.minOf { it.x }; val minY = kept.minOf { it.y }
        val maxX = kept.maxOf { it.x + it.w }; val maxY = kept.maxOf { it.y + it.h }
        val box = NormBox(
            minX.toFloat() / w, minY.toFloat() / h,
            maxX.toFloat() / w, maxY.toFloat() / h
        )
        val fmt = if (medLen >= 7) BitFormat.EIGHT_BIT else BitFormat.FOUR_BIT_NIBBLE
        return DecodeResult(text.trim(), conf, fmt, kept.size, box)
    }

    // ---- stages -----------------------------------------------------------

    /** Downscale + rotate the Y plane into [gray] (upright orientation). */
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

    /** Adaptive threshold via integral image; returns ink fraction. */
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
                val isInk = gray[y * w + x] < mean - adaptC
                ink[y * w + x] = isInk
                if (isInk) inkCount++
            }
        }
        return inkCount.toFloat() / (w * h)
    }

    /** 4-connected flood-fill labelling; returns candidate glyph boxes. */
    private fun components(w: Int, h: Int): List<Glyph> {
        java.util.Arrays.fill(labels, 0)
        val out = ArrayList<Glyph>()
        val n = w * h
        for (start in 0 until n) {
            if (!ink[start] || labels[start] != 0) continue
            var sp = 0
            stack[sp++] = start; labels[start] = 1
            var minx = Int.MAX_VALUE; var maxx = 0; var miny = Int.MAX_VALUE; var maxy = 0; var cnt = 0
            while (sp > 0) {
                val p = stack[--sp]
                val px = p % w; val py = p / w
                if (px < minx) minx = px; if (px > maxx) maxx = px
                if (py < miny) miny = py; if (py > maxy) maxy = py
                cnt++
                if (px > 0)     { val q = p - 1; if (ink[q] && labels[q] == 0) { labels[q] = 1; stack[sp++] = q } }
                if (px < w - 1) { val q = p + 1; if (ink[q] && labels[q] == 0) { labels[q] = 1; stack[sp++] = q } }
                if (py > 0)     { val q = p - w; if (ink[q] && labels[q] == 0) { labels[q] = 1; stack[sp++] = q } }
                if (py < h - 1) { val q = p + w; if (ink[q] && labels[q] == 0) { labels[q] = 1; stack[sp++] = q } }
            }
            out.add(Glyph(minx, miny, maxx - minx + 1, maxy - miny + 1, cnt))
        }
        return out
    }

    /** '1' if the glyph's centre is mostly ink (stroke), else '0' (hollow). */
    private fun classify(g: Glyph, w: Int, h: Int): Char {
        val cw = maxOf(1, (g.w * 0.5).toInt())
        val ch = maxOf(1, (g.h * 0.6).toInt())
        val cx0 = g.x + (g.w - cw) / 2
        val cy0 = g.y + (g.h - ch) / 2
        var inkc = 0; var tot = 0
        for (yy in cy0 until cy0 + ch) {
            if (yy < 0 || yy >= h) continue
            for (xx in cx0 until cx0 + cw) {
                if (xx < 0 || xx >= w) continue
                tot++
                if (ink[yy * w + xx]) inkc++
            }
        }
        val centerFill = if (tot > 0) inkc.toFloat() / tot else 0f
        return if (centerFill >= centerOneThresh) '1' else '0'
    }

    private fun decode8bit(lines: List<List<GlyphBit>>): String {
        val sb = StringBuilder()
        for (line in lines) {
            val s = line.joinToString("") { it.bit.toString() }
            var i = 0
            while (i + 8 <= s.length) { sb.append(bitsToChar(s.substring(i, i + 8))); i += 8 }
        }
        return sb.toString()
    }

    /** Nibble pairs: consecutive lines (top = high nibble, bottom = low). */
    private fun decode4bit(lines: List<List<GlyphBit>>): String {
        val sb = StringBuilder()
        var i = 0
        while (i + 1 < lines.size) {
            val hi = lines[i].joinToString("") { it.bit.toString() }
            val lo = lines[i + 1].joinToString("") { it.bit.toString() }
            if (hi.length == 4 && lo.length == 4) sb.append(bitsToChar(hi + lo))
            i += 2
        }
        return sb.toString()
    }

    private fun bitsToChar(b: String): Char {
        val v = b.toInt(2)
        return if (v in 32..126) v.toChar() else '·' // '·' for non-printable
    }
}
