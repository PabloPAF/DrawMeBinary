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

    // temporal hold: keep the last confident decode on screen for a moment
    private var lastText = ""
    private var lastBox: NormBox? = null
    private var lastTs = 0L
    private val holdMs = 1500L
    private val minConfidence = 0.5f

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

            val now = SystemClock.elapsedRealtime()
            val fresh = r.hasText && r.confidence >= minConfidence
            if (fresh) {
                lastText = r.text; lastBox = r.box; lastTs = now
            }
            val holding = now - lastTs < holdMs && lastText.isNotEmpty()

            when {
                fresh -> {
                    binding.decodedText.text = r.text
                    binding.overlay.setResult(r.box, stats.srcAspect, isLocked = true)
                }
                holding -> {
                    binding.decodedText.text = lastText
                    binding.overlay.setResult(r.box ?: lastBox, stats.srcAspect, isLocked = false)
                }
                else -> {
                    binding.decodedText.text = getString(R.string.scan_hint)
                    binding.overlay.setResult(r.box, stats.srcAspect, isLocked = false)
                }
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
