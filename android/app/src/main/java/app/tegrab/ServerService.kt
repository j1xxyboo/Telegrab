package app.tegrab

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

/**
 * Runs the Python server. It is a foreground service so Android keeps the process (and any
 * download in flight) alive while the app is in the background.
 */
class ServerService : Service() {

    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        val channel = NotificationChannel(CHANNEL, "TeleGrab server", NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notification: Notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("TeleGrab is running")
            .setContentText("Keeps your downloads going in the background")
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        startForeground(NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)

        startServer()
        return START_STICKY
    }

    private fun startServer() {
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        val files: File = filesDir
        val sessions = File(files, "sessions").apply { mkdirs() }
        val cache = File(cacheDir, "thumbs").apply { mkdirs() }
        val logFile = File(files, "logs/tegrab.log").apply { parentFile?.mkdirs() }
        try {
            Python.getInstance().getModule("tegrab_android").callAttr(
                "start", Config.PORT, sessions.absolutePath, cache.absolutePath, logFile.absolutePath,
                Config.apiId(this).toIntOrNull() ?: 0, Config.apiHash(this),
            )
        } catch (e: Exception) {
            Log.e(TAG, "could not start the Python server", e)
        }
    }

    /** Android 15 caps dataSync foreground services at ~6 h; stop cleanly instead of crashing. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        wakeLock?.takeIf { it.isHeld }?.release()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val TAG = "TeleGrab"
        private const val CHANNEL = "server"
        private const val NOTIF_ID = 1

        fun start(ctx: Context) {
            ctx.startForegroundService(Intent(ctx, ServerService::class.java))
        }
    }
}
