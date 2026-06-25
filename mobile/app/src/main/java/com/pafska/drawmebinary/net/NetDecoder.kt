package com.pafska.drawmebinary.net

import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * Cloud fallback for hard layouts (horizontal/2D/colour) the on-device decoder
 * can't handle. POSTs a PNG to the existing web decoder's /decode endpoint and
 * returns the decoded text. Plain HttpURLConnection — no extra dependencies.
 *
 * Only called on an explicit capture when the on-device result is weak, never
 * on the live path, so it adds no weight to live scanning.
 */
object NetDecoder {

    /** @return decoded text, or null on any failure (offline, timeout, error). */
    fun decode(endpoint: String, pngBytes: ByteArray): String? {
        val boundary = "----dmb${System.currentTimeMillis()}"
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(endpoint).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                doOutput = true
                connectTimeout = 4000
                readTimeout = 9000
                setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
            }
            val pre = ("--$boundary\r\n" +
                "Content-Disposition: form-data; name=\"image\"; filename=\"frame.png\"\r\n" +
                "Content-Type: image/png\r\n\r\n").toByteArray()
            val post = "\r\n--$boundary--\r\n".toByteArray()
            conn.outputStream.use { os ->
                os.write(pre); os.write(pngBytes); os.write(post); os.flush()
            }
            val ok = conn.responseCode in 200..299
            val body = (if (ok) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.readText() ?: return null
            if (!ok) return null
            val text = JSONObject(body).optString("text", "")
            if (text.isBlank()) null else text
        } catch (e: Exception) {
            null
        } finally {
            conn?.disconnect()
        }
    }

    fun pngBytes(bmp: android.graphics.Bitmap): ByteArray {
        val bos = ByteArrayOutputStream()
        bmp.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, bos)
        return bos.toByteArray()
    }
}
