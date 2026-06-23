package com.pafska.drawmebinary.decode

/**
 * Fuses per-frame decodes into one stable message via **per-position voting
 * with decay**, so partial reads add up and misreads self-correct:
 *
 *  - each character position keeps a weighted tally of the chars seen there
 *  - every frame all tallies decay a little, then the current frame adds votes
 *  - the position shows its current winner; a one-off wrong read is a single
 *    vote that gets outvoted by repeated correct reads and then decays away
 *  - unknown cells ('·') cast no vote, so a slot stays blank until truly read
 *  - a slot only commits once its winner passes [minVote], so a single shaky
 *    frame never displays
 *
 * Positions are indexed top-to-bottom from the start of the detected block, so
 * this is most reliable when the message start stays framed (an aim-ROI keeps
 * that consistent).
 */
class MessageAccumulator(
    private val decay: Float = 0.92f,
    private val minVote: Float = 2.0f
) {
    private val slots = ArrayList<HashMap<Char, Float>>()

    fun reset() = slots.clear()

    /** Feed this frame's ordered chars (top→bottom). '·'/' ' are skipped. */
    fun update(chars: List<Char>, weight: Float = 1f) {
        // decay everything first (also runs on empty frames, so stale votes fade)
        for (m in slots) {
            val it = m.entries.iterator()
            while (it.hasNext()) {
                val e = it.next()
                val v = e.value * decay
                if (v < 0.05f) it.remove() else e.setValue(v)
            }
        }
        for (i in chars.indices) {
            val ch = chars[i]
            if (ch == '·' || ch == ' ') continue
            while (slots.size <= i) slots.add(HashMap())
            val m = slots[i]
            m[ch] = (m[ch] ?: 0f) + weight
        }
    }

    /** Committed winner for a position, or null if not confident yet. */
    fun charAt(i: Int): Char? {
        if (i >= slots.size) return null
        val best = slots[i].maxByOrNull { it.value } ?: return null
        return if (best.value >= minVote) best.key else null
    }

    /** Full message; uncommitted positions appear as '·'. */
    fun message(): String {
        val sb = StringBuilder()
        for (i in slots.indices) sb.append(charAt(i) ?: '·')
        return sb.toString()
    }
}
