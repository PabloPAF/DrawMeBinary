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

    private val recent = ArrayDeque<String>()      // recent high-conf live reads
    private val recentMax = 12
    private val goodConf = 0.9f

    private val captureReads = ArrayList<String>() // reads collected during a burst
    private var captureStart = 0L
    private val captureMs = 1200L

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
                    mode = Mode.CAPTURING; captureReads.clear()
                    captureStart = SystemClock.elapsedRealtime()
                    toast("Capturing… hold steady")
                }
                Mode.FROZEN -> { mode = Mode.LIVE; recent.clear(); toast("Live") }
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

            val good = (r.cols == 4 || r.cols == 8) && r.confidence >= goodConf && r.text.isNotBlank()
            if (good) { goodCells = r.cells; goodBox = r.box }
            val aspect = stats.srcAspect

            when (mode) {
                Mode.LIVE -> {
                    if (good) { recent.addLast(r.text); while (recent.size > recentMax) recent.removeFirst() }
                    val msg = plurality(recent)
                    binding.decodedText.text = if (msg.isNotBlank()) msg else getString(R.string.scan_hint)
                    binding.overlay.setResult(r.box ?: goodBox,
                        r.cells.ifEmpty { goodCells }, aspect, msg.isNotBlank())
                }
                Mode.CAPTURING -> {
                    if (good) captureReads.add(r.text)
                    binding.decodedText.text = "capturing… (${captureReads.size})"
                    binding.overlay.setResult(r.box ?: goodBox, r.cells.ifEmpty { goodCells }, aspect, false)
                    if (SystemClock.elapsedRealtime() - captureStart > captureMs) {
                        val result = plurality(captureReads)
                        mode = Mode.FROZEN
                        binding.decodedText.text =
                            if (result.isNotBlank()) result else "Nothing captured — tap to retry"
                        // label the captured cells with the locked result
                        val cells = if (result.length == goodCells.size)
                            goodCells.mapIndexed { i, c -> Cell(result[i], c.box) } else goodCells
                        binding.overlay.setResult(goodBox, cells, aspect, result.isNotBlank())
                    }
                }
                Mode.FROZEN -> {}
            }
        }
    }

    /** Most frequent non-empty string; ties broken toward the longer one. */
    private fun plurality(xs: Collection<String>): String {
        val counts = xs.filter { it.isNotBlank() }.groupingBy { it }.eachCount()
        return counts.maxByOrNull { it.value * 1000 + it.key.length }?.key ?: ""
    }

    private fun toast(msg: String) =
        android.widget.Toast.makeText(this, msg, android.widget.Toast.LENGTH_SHORT).show()

    override fun onDestroy() {
        super.onDestroy()
        camera?.stop()
        SecLog.event("app.stopped", "process", message = "scanner stopped")
        SecLog.flush()
    }
}
