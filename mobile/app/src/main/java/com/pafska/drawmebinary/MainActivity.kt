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

    // latch a confident read so a brief good frame persists on screen (the page
    // is static, so a high-confidence decode is trustworthy to hold)
    private val latchConf = 0.9f
    private val holdMs = 2500L
    private var latchText = ""
    private var latchCells: List<com.pafska.drawmebinary.decode.Cell> = emptyList()
    private var latchBox: NormBox? = null
    private var latchTs = 0L
    private var lastBox: NormBox? = null

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
            // latch a confident read; prefer a longer one, else refresh after hold
            if (r.confidence >= latchConf && r.text.length >= 2 &&
                (r.text.length >= latchText.length || now - latchTs > holdMs)) {
                latchText = r.text; latchCells = r.cells; latchBox = r.box; latchTs = now
            }
            val holding = now - latchTs < holdMs && latchText.isNotEmpty()

            lastBox = r.box ?: lastBox
            if (holding) {
                binding.decodedText.text = latchText
                binding.overlay.setResult(latchBox ?: lastBox, latchCells, stats.srcAspect, true)
            } else {
                latchText = ""
                binding.decodedText.text = getString(R.string.scan_hint)
                binding.overlay.setResult(r.box ?: lastBox, emptyList(), stats.srcAspect, false)
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        camera?.stop()
        SecLog.event("app.stopped", "process", message = "scanner stopped")
        SecLog.flush()
    }
}
