package com.pafska.drawmebinary

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.pafska.drawmebinary.camera.CameraController
import com.pafska.drawmebinary.camera.FrameAnalyzer
import com.pafska.drawmebinary.databinding.ActivityMainBinding
import com.pafska.drawmebinary.decode.StubBinaryDecoder
import com.pafska.drawmebinary.log.SecLog
import java.util.Locale

/**
 * Milestone 1: request camera permission, show the live preview, and run the
 * per-frame analysis loop end to end (decoder stubbed). The status bar shows
 * FPS + frame size; the bottom bar will show decoded text once the real
 * decoder lands.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var camera: CameraController? = null

    private val requestCamera =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) startCamera()
            else binding.decodedText.text = getString(R.string.perm_denied)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        SecLog.init(applicationContext)
        SecLog.newCorrelation()
        SecLog.event(
            action = "app.started",
            category = "process",
            message = "mobile scanner starting"
        )

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
            decoder = StubBinaryDecoder(),
            onFrame = ::onFrame
        ).also { it.start() }
    }

    /**
     * Called per analyzed frame on the CameraX analysis thread (a background
     * thread), so we MUST hop to the main thread before touching any views.
     */
    private fun onFrame(stats: FrameAnalyzer.FrameStats) {
        if (isFinishing || isDestroyed) return
        runOnUiThread {
            binding.statusText.text = String.format(
                Locale.US,
                "%.0f fps · %d×%d · %d ms · contrast %.2f · glyphs~%d",
                stats.fps, stats.frameWidth, stats.frameHeight,
                stats.analysisMs, stats.result.confidence, stats.result.glyphCount
            )
            // Decoder is stubbed: keep the scan hint until real text arrives.
            if (stats.result.hasText) binding.decodedText.text = stats.result.text
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        camera?.stop()
        SecLog.event(action = "app.stopped", category = "process", message = "scanner stopped")
        SecLog.flush()
    }
}
