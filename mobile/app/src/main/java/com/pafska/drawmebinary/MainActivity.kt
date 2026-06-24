package com.pafska.drawmebinary

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.SystemClock
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import com.pafska.drawmebinary.camera.CameraController
import com.pafska.drawmebinary.camera.FrameAnalyzer
import com.pafska.drawmebinary.databinding.ActivityMainBinding
import com.pafska.drawmebinary.decode.Cell
import com.pafska.drawmebinary.decode.NormBox
import com.pafska.drawmebinary.decode.PrintedBinaryDecoder
import com.pafska.drawmebinary.log.SecLog
import java.util.Locale

/**
 * Milestone 2: live camera preview + on-device decoding. The decoder reads the
 * 0/1 artwork each frame; the overlay snaps to the detected block and the
 * decoded letters are shown at the bottom, held briefly for stability so the
 * text doesn't flicker between frames.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var camera: CameraController? = null

    // scan mode: LIVE shows the running best; tap starts a CAPTURE burst that
    // collects reads for ~1.2s and locks the plurality result (FROZEN).
    private enum class Mode { LIVE, CAPTURING, FROZEN }
    private var mode = Mode.LIVE

    // raw per-frame reads (incl. '·') collected within the current display window
    private val windowRaws = ArrayList<String>()
    // analysis runs every frame, but the shown text only refreshes this often,
    // holding the window's per-position vote so the readout stays calm
    private val displayMs = 800L
    private var lastDisplayTs = 0L
    private var emptyWindows = 0

    private val captureRaws = ArrayList<String>()  // raws collected during a capture burst
    private var captureStart = 0L
    private val captureMs = 1500L

    private var goodCells: List<Cell> = emptyList() // cells from the last good frame
    private var goodBox: NormBox? = null

    // aim box (fraction of the upright frame): line the digits up inside it
    private val roiL = 0.18f; private val roiT = 0.08f
    private val roiR = 0.82f; private val roiB = 0.92f

    private val requestCamera =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) startCamera()
            else binding.decodedText.text = getString(R.string.perm_denied)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        // FIT_CENTER so the overlay box lines up with the analyzed frame
        binding.previewView.scaleType = PreviewView.ScaleType.FIT_CENTER
        binding.overlay.setRoi(roiL, roiT, roiR, roiB)

        // tap: LIVE -> start a capture burst; FROZEN -> back to live
        binding.overlay.setOnClickListener {
            when (mode) {
                Mode.LIVE -> {
                    mode = Mode.CAPTURING; captureRaws.clear()
                    captureStart = SystemClock.elapsedRealtime()
                    toast("Capturing… hold steady")
                }
                Mode.FROZEN -> { mode = Mode.LIVE; windowRaws.clear(); toast("Live") }
                Mode.CAPTURING -> {}
            }
        }

        SecLog.init(applicationContext)
        SecLog.newCorrelation()
        SecLog.event("app.started", "process", message = "mobile scanner starting")

        if (hasCameraPermission()) startCamera()
        else requestCamera.launch(Manifest.permission.CAMERA)
    }

    private fun hasCameraPermission() =
        ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED

    private fun startCamera() {
        camera = CameraController(
            context = this,
            lifecycleOwner = this,
            previewView = binding.previewView,
            decoder = PrintedBinaryDecoder(roiL, roiT, roiR, roiB),
            onFrame = ::onFrame
        ).also { it.start() }
    }

    /**
     * Called per analyzed frame on the CameraX analysis (background) thread, so
     * we hop to the main thread before touching any views.
     */
    private fun onFrame(stats: FrameAnalyzer.FrameStats) {
        if (isFinishing || isDestroyed) return
        runOnUiThread {
            val r = stats.result
            binding.statusText.text = String.format(
                Locale.US,
                "%.0f fps · %d×%d · %d ms · %s · glyphs %d · conf %.2f",
                stats.fps, stats.frameWidth, stats.frameHeight, stats.analysisMs,
                r.bitFormat.name.lowercase(), r.glyphCount, r.confidence
            )
            val rawShown = if (r.raw.length > 28) r.raw.substring(0, 28) + "…" else r.raw
            binding.debugText.text = String.format(
                Locale.US,
                "ink %.2f%% · rows %d · cols %d · gate %d · raw \"%s\"",
                r.inkPct, r.rows, r.cols, r.gate, rawShown
            )
            if (mode == Mode.FROZEN) return@runOnUiThread   // keep the frozen result

            // collect any structurally-valid frame (don't gate on confidence:
            // per-position voting recovers the right byte from partial reads)
            val validGrid = (r.cols == 4 || r.cols == 8) && r.rows >= 4 && r.raw.isNotBlank()
            if (validGrid) { goodCells = r.cells; goodBox = r.box }
            val aspect = stats.srcAspect
            val now = SystemClock.elapsedRealtime()

            when (mode) {
                Mode.LIVE -> {
                    if (validGrid) windowRaws.add(r.raw)        // analyse every frame
                    if (now - lastDisplayTs >= displayMs) {     // refresh shown text on interval
                        val voted = votePerPosition(windowRaws)
                        val msg = voted.trimEnd('·')
                        if (msg.isNotBlank()) {
                            binding.decodedText.text = msg
                            binding.overlay.setResult(goodBox, labelCells(voted), aspect, true)
                            emptyWindows = 0
                        } else if (++emptyWindows >= 3) {
                            binding.decodedText.text = getString(R.string.scan_hint)
                            binding.overlay.setResult(goodBox, emptyList(), aspect, false)
                        }
                        windowRaws.clear(); lastDisplayTs = now
                    }
                }
                Mode.CAPTURING -> {
                    if (validGrid) captureRaws.add(r.raw)
                    binding.decodedText.text = "capturing… (${captureRaws.size})"
                    binding.overlay.setResult(r.box ?: goodBox, r.cells.ifEmpty { goodCells }, aspect, false)
                    if (now - captureStart > captureMs) {
                        val voted = votePerPosition(captureRaws)
                        val result = voted.trimEnd('·')
                        mode = Mode.FROZEN
                        binding.decodedText.text =
                            if (result.isNotBlank()) result else "Nothing captured — tap to retry"
                        binding.overlay.setResult(goodBox, labelCells(voted), aspect, result.isNotBlank())
                    }
                }
                Mode.FROZEN -> {}
            }
        }
    }

    /**
     * Per-byte majority vote across frames. Different frames misread different
     * cells, so voting each position recovers the correct byte even when no
     * single frame is fully right. Only frames of the modal length are voted
     * (so partial/short reads don't shift the alignment); '·' casts no vote.
     */
    private fun votePerPosition(raws: List<String>): String {
        if (raws.isEmpty()) return ""
        val len = raws.groupingBy { it.length }.eachCount().maxByOrNull { it.value }?.key ?: return ""
        val same = raws.filter { it.length == len }
        val sb = StringBuilder()
        for (i in 0 until len) {
            val counts = HashMap<Char, Int>()
            for (s in same) { val c = s[i]; if (c != '·' && c != ' ') counts[c] = (counts[c] ?: 0) + 1 }
            sb.append(counts.maxByOrNull { it.value }?.key ?: '·')
        }
        return sb.toString()
    }

    /** Map a voted string onto the last good cells' boxes (for the overlay). */
    private fun labelCells(voted: String): List<Cell> =
        goodCells.mapIndexed { i, c -> Cell(if (i < voted.length) voted[i] else '·', c.box) }

    private fun toast(msg: String) =
        android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_SHORT).show()

    override fun onDestroy() {
        super.onDestroy()
        camera?.stop()
        SecLog.event("app.stopped", "process", message = "scanner stopped")
        SecLog.flush()
    }
}
