package com.pafska.drawmebinary.decode

/**
 * Fuses per-frame decodes into one stable message via voting with decay.
 *
 * Votes are keyed by each byte's **absolute vertical position in the frame**
 * (normalized y bucket), NOT by its order in the detected block. That's the key
 * to stitching partial reads: when one frame catches only the top rows and
 * another only the bottom rows, each byte still votes into the bucket for its
 * real position, so they assemble in the correct order instead of colliding.
 *
 * Each bucket keeps a decaying weighted tally; a one-off misread is outvoted by
 * repeated correct reads and then decays away (self-correcting). A bucket only
 * commits past [minVote], so a single shaky frame never displays.
 *
 * This assumes the page stays roughly fixed in the frame while scanning (hold
 * steady); if it moves a lot, old buckets simply decay and new ones build.
 */
class MessageAccumulator(
    private val decay: Float = 0.92f,
    private val minVote: Float = 2.0f,
    private val buckets: Int = 64
) {
    private val tally = HashMap<Int, HashMap<Char, Float>>()

    fun reset() = tally.clear()

    private fun bucketOf(yCenter: Float) =
        (yCenter * buckets).toInt().coerceIn(0, buckets - 1)

    /** Feed this frame's decoded cells. Pass empty to just decay (idle frame). */
    fun update(cells: List<Cell>, weight: Float = 1f) {
        val itB = tally.entries.iterator()
        while (itB.hasNext()) {
            val m = itB.next().value
            val it = m.entries.iterator()
            while (it.hasNext()) {
                val e = it.next(); val v = e.value * decay
                if (v < 0.05f) it.remove() else e.setValue(v)
            }
            if (m.isEmpty()) itB.remove()
        }
        for (c in cells) {
            if (c.ch == '·' || c.ch == ' ') continue
            val b = bucketOf((c.box.top + c.box.bottom) / 2f)
            val m = tally.getOrPut(b) { HashMap() }
            m[c.ch] = (m[c.ch] ?: 0f) + weight
        }
    }

    /** Committed char for the byte whose centre is at this normalized y. */
    fun charAtY(yCenter: Float): Char? {
        val m = tally[bucketOf(yCenter)] ?: return null
        val best = m.maxByOrNull { it.value } ?: return null
        return if (best.value >= minVote) best.key else null
    }

    /** Assembled message: committed buckets, top to bottom. */
    fun message(): String {
        val sb = StringBuilder()
        for (b in tally.keys.sorted()) {
            val best = tally[b]!!.maxByOrNull { it.value } ?: continue
            if (best.value >= minVote) sb.append(best.key)
        }
        return sb.toString()
    }
}
