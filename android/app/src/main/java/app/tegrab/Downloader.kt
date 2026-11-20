package app.tegrab

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.webkit.CookieManager
import android.widget.Toast
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLDecoder
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger

/**
 * Saves a file from the in-app server into the public Downloads folder.
 *
 * WebView cannot save files itself, so the Activity hands every download here. The request goes
 * back to the local server with the WebView's own session cookie, and the bytes are streamed into
 * MediaStore (no storage permission needed on Android 10+).
 */
class Downloader(private val ctx: Context) {

    private val pool = Executors.newFixedThreadPool(2)
    private val ui = Handler(Looper.getMainLooper())
    private val nm = ctx.getSystemService(NotificationManager::class.java)
    private val ids = AtomicInteger(100)

    init {
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Downloads", NotificationManager.IMPORTANCE_LOW))
    }

    fun enqueue(url: String, contentDisposition: String?, mime: String?) {
        val name = fileName(contentDisposition, url)
        toast("Downloading $name")
        pool.execute { run(url, name, mime) }
    }

    private fun run(url: String, name: String, mime: String?) {
        val notifId = ids.incrementAndGet()
        var uri: Uri? = null
        try {
            val conn = (URL(url).openConnection() as HttpURLConnection).apply {
                connectTimeout = 15_000
                readTimeout = 60_000
                CookieManager.getInstance().getCookie(url)?.let { setRequestProperty("Cookie", it) }
            }
            if (conn.responseCode !in 200..299) {
                val why = when (conn.responseCode) {
                    402 -> "This is a paid post — pay for it in Telegram first."
                    401 -> "You are signed out."
                    else -> "Server answered HTTP ${conn.responseCode}"
                }
                fail(notifId, name, why)
                return
            }
            val total = conn.contentLengthLong
            val type = (mime?.takeIf { it.isNotBlank() } ?: conn.contentType ?: "application/octet-stream")
                .substringBefore(';')

            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, type)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val resolver = ctx.contentResolver
            uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("could not create the file in Downloads")

            var written = 0L
            var lastTick = 0L
            conn.inputStream.use { input ->
                resolver.openOutputStream(uri)!!.use { output ->
                    val buffer = ByteArray(256 * 1024)
                    while (true) {
                        val n = input.read(buffer)
                        if (n < 0) break
                        output.write(buffer, 0, n)
                        written += n
                        val now = System.currentTimeMillis()
                        if (now - lastTick > 700) {
                            lastTick = now
                            progress(notifId, name, written, total)
                        }
                    }
                }
            }
            if (total > 0 && written != total) throw IllegalStateException("incomplete: $written of $total bytes")

            resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
            done(notifId, name)
        } catch (e: Exception) {
            uri?.let { runCatching { ctx.contentResolver.delete(it, null, null) } }
            fail(notifId, name, e.message ?: e.javaClass.simpleName)
        }
    }

    private fun progress(id: Int, name: String, done: Long, total: Long) {
        val b = android.app.Notification.Builder(ctx, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(name)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
        if (total > 0) b.setProgress(100, (done * 100 / total).toInt(), false) else b.setProgress(0, 0, true)
        nm.notify(id, b.build())
    }

    private fun done(id: Int, name: String) {
        nm.notify(
            id,
            android.app.Notification.Builder(ctx, CHANNEL)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(name)
                .setContentText("Saved to Downloads")
                .setAutoCancel(true)
                .build(),
        )
        toast("Saved to Downloads: $name")
    }

    private fun fail(id: Int, name: String, why: String) {
        nm.notify(
            id,
            android.app.Notification.Builder(ctx, CHANNEL)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle("Could not save $name")
                .setContentText(why)
                .setAutoCancel(true)
                .build(),
        )
        toast("Could not save $name: $why")
    }

    private fun toast(text: String) = ui.post { Toast.makeText(ctx, text, Toast.LENGTH_LONG).show() }

    companion object {
        private const val CHANNEL = "downloads"

        /** Prefer RFC 5987 `filename*=UTF-8''…` (what the server sends), then `filename="…"`, then the URL. */
        fun fileName(contentDisposition: String?, url: String): String {
            val cd = contentDisposition.orEmpty()
            Regex("filename\\*\\s*=\\s*([^']*)'[^']*'([^;]+)", RegexOption.IGNORE_CASE).find(cd)?.let {
                val charset = it.groupValues[1].ifBlank { "UTF-8" }
                runCatching { return clean(URLDecoder.decode(it.groupValues[2].trim(), charset)) }
            }
            Regex("filename\\s*=\\s*\"?([^\";]+)\"?", RegexOption.IGNORE_CASE).find(cd)?.let {
                return clean(it.groupValues[1])
            }
            return clean(url.substringBefore('?').substringAfterLast('/').ifBlank { "telegram_file" })
        }

        private fun clean(raw: String): String {
            val s = raw.substringAfterLast('/').substringAfterLast('\\')
                .filter { it.isLetterOrDigit() || it in " ._-()[]+,@#&'" || it.code > 127 }
                .trim()
            return s.ifBlank { "telegram_file" }
        }
    }
}
