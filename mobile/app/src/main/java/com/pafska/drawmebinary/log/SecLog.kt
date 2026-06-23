package com.pafska.drawmebinary.log

import android.content.Context
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.UUID
import java.util.concurrent.Executors

/**
 * On-device security logging for the DrawMeBinary mobile project.
 *
 * Emits the SAME ECS (Elastic Common Schema) JSON, one object per line, as the
 * web service (drawmebinary/seclog.py) so both projects land in one SIEM index;
 * only `service.name` differs (here: "drawmebinary-mobile"). See the web repo's
 * SECURITY_LOGGING.md, section "Two projects, one SIEM".
 *
 * Consistency rules carried over from the web app:
 *  - Never log the decoded message, the photo, or a file path. Only a SHA-256
 *    hash of decoded text plus metadata.
 *  - A camera scan has no client IP, so `source.ip` is omitted.
 *  - Offline-first: events are appended to a local rotating file now; a future
 *    flush() ships buffered lines to the collector when connectivity returns.
 *
 * This is a Milestone-1 stub: file sink + schema only. The network shipper and
 * a device/install id under labels.* are TODO.
 */
object SecLog {

    private const val ECS_VERSION = "8.11.0"
    private const val SERVICE_NAME = "drawmebinary-mobile"
    private const val SERVICE_VERSION = "0.1.0"
    private const val MAX_FILE_BYTES = 2L * 1024 * 1024

    private val io = Executors.newSingleThreadExecutor()
    private val iso = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("UTC") }

    private lateinit var logFile: File
    private var environment: String = "development"
    @Volatile private var correlationId: String? = null

    fun init(context: Context, environment: String = "development") {
        this.environment = environment
        val dir = File(context.filesDir, "logs").apply { mkdirs() }
        logFile = File(dir, "security.jsonl")
    }

    /** Start a fresh correlation id (one per scan session) and return it. */
    fun newCorrelation(): String = UUID.randomUUID().toString().replace("-", "")
        .also { correlationId = it }

    fun hashText(text: String): String = "sha256:" + sha256(text.toByteArray(Charsets.UTF_8))

    /**
     * Emit one ECS event. [fields] are dotted ECS keys already mapped by the
     * caller (e.g. "labels.streams" to 3), matching the web app's field set.
     */
    fun event(
        action: String,
        category: String,
        outcome: String = "success",
        level: String = "info",
        type: String = "info",
        message: String? = null,
        fields: Map<String, Any?> = emptyMap()
    ) {
        if (!::logFile.isInitialized) return
        val rec = JSONObject()
        rec.put("@timestamp", iso.format(Date()))
        rec.putNested("ecs.version", ECS_VERSION)
        rec.putNested("log.level", level)
        if (message != null) rec.put("message", message)
        rec.put("event", JSONObject()
            .put("kind", "event")
            .put("category", JSONArray().put(category))
            .put("type", JSONArray().put(type))
            .put("action", action)
            .put("outcome", outcome))
        rec.putNested("service.name", SERVICE_NAME)
        rec.putNested("service.version", SERVICE_VERSION)
        rec.putNested("service.environment", environment)
        rec.putNested("host.name", "${Build.MANUFACTURER}-${Build.MODEL}")
        rec.putNested("device.model.identifier", Build.MODEL)
        correlationId?.let { rec.putNested("trace.id", it) }
        for ((k, v) in fields) if (v != null) rec.putNested(k, v)

        val line = rec.toString()
        io.execute { appendLine(line) }
    }

    /** TODO(Milestone 3): ship buffered lines to the SIEM collector when online. */
    fun flush() { /* no-op for now: file is the on-device buffer */ }

    private fun appendLine(line: String) {
        try {
            if (logFile.length() > MAX_FILE_BYTES) {
                File(logFile.parentFile, "security.jsonl.1").also { it.delete() }
                logFile.renameTo(File(logFile.parentFile, "security.jsonl.1"))
            }
            logFile.appendText(line + "\n")
        } catch (_: Exception) {
            // logging must never crash the app
        }
    }

    private fun sha256(data: ByteArray): String {
        val d = MessageDigest.getInstance("SHA-256").digest(data)
        return d.joinToString("") { "%02x".format(it) }
    }

    /** Assign a nested ECS field from a dotted key, e.g. "service.name". */
    private fun JSONObject.putNested(dotted: String, value: Any) {
        val parts = dotted.split(".")
        var node = this
        for (i in 0 until parts.size - 1) {
            node = node.optJSONObject(parts[i]) ?: JSONObject().also { node.put(parts[i], it) }
        }
        node.put(parts.last(), value)
    }
}
