package app.tegrab

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.JavascriptInterface
import android.webkit.JsResult
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.app.Activity
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/** Shows either the one-time "enter your API keys" screen or the TeleGrab web UI. */
class MainActivity : Activity() {

    private lateinit var root: FrameLayout
    private var web: WebView? = null
    private lateinit var downloader: Downloader

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        downloader = Downloader(applicationContext)
        root = FrameLayout(this).apply { setBackgroundColor(COLOR_BG) }
        setContentView(root)
        // Keep content out from under the status bar, nav bar and keyboard.
        root.setOnApplyWindowInsetsListener { v, insets ->
            val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars() or android.view.WindowInsets.Type.ime())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        route()
    }

    private fun route() {
        if (Config.hasCredentials(this)) showWeb() else showSetup(firstRun = true)
    }

    // ------------------------------------------------------------------ setup screen

    private fun showSetup(firstRun: Boolean) {
        web?.visibility = View.GONE
        val dp = resources.displayMetrics.density
        fun px(v: Int) = (v * dp).toInt()

        fun label(text: String, size: Float = 14f, color: Int = COLOR_MUTED) =
            TextView(this).apply { this.text = text; textSize = size; setTextColor(color) }

        fun field(hint: String, type: Int) = EditText(this).apply {
            this.hint = hint
            inputType = type
            setTextColor(Color.WHITE)
            setHintTextColor(0xFF777777.toInt())
            setBackgroundColor(COLOR_SURFACE)
            setPadding(px(14), px(12), px(14), px(12))
            isSingleLine = true
        }

        val id = field("api_id  (digits)", InputType.TYPE_CLASS_NUMBER).apply { setText(Config.apiId(this@MainActivity)) }
        val hash = field("api_hash  (32 characters)", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD)
            .apply { setText(Config.apiHash(this@MainActivity)) }
        val error = label("", 13f, 0xFFFF6B6B.toInt())

        val save = Button(this).apply {
            text = if (firstRun) "Continue" else "Save"
            isAllCaps = false
            setTextColor(Color.WHITE)
            setBackgroundColor(COLOR_ACCENT)
            setOnClickListener {
                if (!Config.isValid(id.text.toString(), hash.text.toString())) {
                    error.text = "api_id is digits only, and api_hash is the 32-character code from my.telegram.org."
                    return@setOnClickListener
                }
                val changed = Config.apiId(this@MainActivity) != id.text.toString().trim() ||
                    Config.apiHash(this@MainActivity) != hash.text.toString().trim()
                Config.save(this@MainActivity, id.text.toString(), hash.text.toString())
                if (changed && !firstRun) {
                    // The server read the keys at start-up; the simplest correct move is a clean restart.
                    restartApp()
                } else {
                    showWeb()
                }
            }
        }
        val getKeys = Button(this).apply {
            text = "Get my api_id / api_hash"
            isAllCaps = false
            setTextColor(COLOR_ACCENT)
            setBackgroundColor(Color.TRANSPARENT)
            setOnClickListener { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("https://my.telegram.org/apps"))) }
        }

        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(px(24), px(40), px(24), px(24))
            addView(label("TeleGrab", 28f, Color.WHITE))
            addView(label("Browse and download your Telegram media.", 15f).apply { setPadding(0, px(6), 0, px(24)) })
            addView(label("One-time setup", 17f, Color.WHITE))
            addView(
                label(
                    "Telegram needs an api_id and api_hash that belong to you. Open my.telegram.org on this phone, " +
                        "sign in, choose “API development tools”, create an app, then paste the two values here. " +
                        "They stay on this device.",
                    14f,
                ).apply { setPadding(0, px(8), 0, px(18)) },
            )
            addView(id, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = px(12) })
            addView(hash, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = px(8) })
            addView(error, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = px(8) })
            addView(save, LinearLayout.LayoutParams(-1, px(50)))
            addView(getKeys, LinearLayout.LayoutParams(-1, px(50)).apply { topMargin = px(8) })
        }
        val scroll = ScrollView(this).apply { tag = SETUP_TAG; isFillViewport = true; addView(column) }

        root.findViewWithTag<View>(SETUP_TAG)?.let { root.removeView(it) }
        root.addView(scroll, FrameLayout.LayoutParams(-1, -1))
    }

    private fun restartApp() {
        val intent = packageManager.getLaunchIntentForPackage(packageName)!!
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        stopService(Intent(this, ServerService::class.java))
        startActivity(intent)
        // The server runs inside this process; ending it guarantees the new keys are read.
        android.os.Process.killProcess(android.os.Process.myPid())
    }

    // ------------------------------------------------------------------ web UI

    @SuppressLint("SetJavaScriptEnabled")
    private fun showWeb() {
        root.findViewWithTag<View>(SETUP_TAG)?.let { root.removeView(it) }
        web?.let { it.visibility = View.VISIBLE; return }

        ServerService.start(this)

        val splash = TextView(this).apply {
            text = "Starting TeleGrab…"
            setTextColor(COLOR_MUTED)
            gravity = Gravity.CENTER
            textSize = 16f
        }
        root.addView(splash, FrameLayout.LayoutParams(-1, -1))

        val view = WebView(this).apply {
            setBackgroundColor(COLOR_BG)
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                mediaPlaybackRequiresUserGesture = false
                allowFileAccess = false
                allowContentAccess = false
                mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
                cacheMode = WebSettings.LOAD_DEFAULT
                setSupportZoom(false)
            }
            overScrollMode = View.OVER_SCROLL_NEVER
            addJavascriptInterface(Bridge(), "TeleGrabAndroid")

            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(v: WebView, request: WebResourceRequest): Boolean {
                    val url = request.url
                    // Anything that is not the local server (e.g. a tg:// login link) opens outside the app.
                    if (url.scheme == "http" && url.host == "127.0.0.1") return false
                    runCatching { startActivity(Intent(Intent.ACTION_VIEW, url)) }
                    return true
                }
            }
            webChromeClient = object : WebChromeClient() {
                // Without this, window.confirm() silently returns false (so "Log out" would do nothing).
                override fun onJsConfirm(v: WebView, url: String, message: String, result: JsResult): Boolean {
                    AlertDialog.Builder(this@MainActivity)
                        .setMessage(message)
                        .setPositiveButton("OK") { _, _ -> result.confirm() }
                        .setNegativeButton("Cancel") { _, _ -> result.cancel() }
                        .setOnCancelListener { result.cancel() }
                        .show()
                    return true
                }

                override fun onJsAlert(v: WebView, url: String, message: String, result: JsResult): Boolean {
                    AlertDialog.Builder(this@MainActivity)
                        .setMessage(message)
                        .setPositiveButton("OK") { _, _ -> result.confirm() }
                        .setOnCancelListener { result.confirm() }
                        .show()
                    return true
                }
            }
            setDownloadListener { url, _, contentDisposition, mime, _ ->
                downloader.enqueue(url, contentDisposition, mime)
            }
        }
        web = view
        root.addView(view, 0, FrameLayout.LayoutParams(-1, -1))
        view.visibility = View.INVISIBLE

        // Wait until the Python server answers, then load the UI.
        thread(name = "wait-for-server") {
            val deadline = System.currentTimeMillis() + 60_000
            var up = false
            while (System.currentTimeMillis() < deadline) {
                up = runCatching {
                    (URL("${Config.BASE_URL}/api/status").openConnection() as HttpURLConnection).run {
                        connectTimeout = 800; readTimeout = 3000
                        responseCode in 200..499
                    }
                }.getOrDefault(false)
                if (up) break
                Thread.sleep(400)
            }
            runOnUiThread {
                if (up) {
                    root.removeView(splash)
                    view.visibility = View.VISIBLE
                    view.loadUrl(Config.BASE_URL)
                } else {
                    splash.text = "The server did not start.\nClose the app and open it again."
                }
            }
        }
    }

    /** Called from the web page (see app.js) to reach native features. */
    inner class Bridge {
        @JavascriptInterface
        fun openApiKeys() = runOnUiThread { showSetup(firstRun = false) }
    }

    // ------------------------------------------------------------------ back button

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (root.findViewWithTag<View>(SETUP_TAG) != null && web != null) {
            // Leaving the keys screen without saving returns to the web UI.
            root.findViewWithTag<View>(SETUP_TAG)?.let { root.removeView(it) }
            web?.visibility = View.VISIBLE
            return
        }
        val view = web
        if (view == null || view.visibility != View.VISIBLE) {
            @Suppress("DEPRECATION") super.onBackPressed()
            return
        }
        // Let the page close the viewer / selection / chat first; leave the app only if it did nothing.
        view.evaluateJavascript("(window.__tgBack ? window.__tgBack() : false)") { handled ->
            if (handled != "true") moveTaskToBack(true)
        }
    }

    override fun onDestroy() {
        web?.let { (it.parent as? ViewGroup)?.removeView(it); it.destroy() }
        super.onDestroy()
    }

    companion object {
        private const val SETUP_TAG = "setup"
        private const val COLOR_BG = 0xFF181818.toInt()
        private const val COLOR_SURFACE = 0xFF212121.toInt()
        private const val COLOR_ACCENT = 0xFF8774E1.toInt()
        private const val COLOR_MUTED = 0xFFAAAAAA.toInt()
    }
}
