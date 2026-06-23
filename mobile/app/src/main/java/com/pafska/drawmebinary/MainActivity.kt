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
import com.pafska.drawmebinary.decode.MessageAccumulator
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

    // fuse partial reads across frames; misreads self-correct via voting
    private val acc = MessageAccumulator()
    private var lastCells: List<Cell> = emptyList()
    private var lastBox: NormBox? = null
    private var lastCellsTs = 0L
    private val cellsHoldMs = 700L   // keep the last letters on screen briefly

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
            decoder = PrintedBinaryDecoder(),
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

            val now = SystemClock.elapsedRealtime()
            // accumulate this frame's reads (empty frame still decays old votes)
            acc.update(r.cells.map { it.ch })
            if (r.cells.isNotEmpty()) { lastCells = r.cells; lastBox = r.box; lastCellsTs = now }

            val msg = acc.message().trimEnd('·')
            binding.decodedText.text = if (msg.isNotBlank()) msg else getString(R.string.scan_hint)

            // overlay: place the VOTED (stable) char over each current cell, so a
            // transient misread shows the accumulated winner, not the bad guess
            val fresh = now - lastCellsTs < cellsHoldMs
            val box = if (fresh) lastBox else r.box ?: lastBox
            val cells = if (fresh)
                lastCells.mapIndexed { i, c -> Cell(acc.charAt(i) ?: c.ch, c.box) }
            else emptyList()
            binding.overlay.setResult(box, cells, stats.srcAspect, isLocked = msg.isNotBlank())
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        camera?.stop()
        SecLog.event("app.stopped", "process", message = "scanner stopped")
        SecLog.flush()
    }
}
