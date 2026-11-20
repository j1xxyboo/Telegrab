package app.tegrab

import android.content.Context

/** Where the in-app server listens and where the user's Telegram API keys are kept. */
object Config {
    /** Fixed on purpose: the WebView's session cookie is per-origin, so a new port would log you out. */
    const val PORT = 18765
    const val BASE_URL = "http://127.0.0.1:$PORT"

    private const val PREFS = "tegrab"
    private const val KEY_ID = "api_id"
    private const val KEY_HASH = "api_hash"

    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun apiId(ctx: Context): String = prefs(ctx).getString(KEY_ID, "") ?: ""
    fun apiHash(ctx: Context): String = prefs(ctx).getString(KEY_HASH, "") ?: ""

    fun hasCredentials(ctx: Context): Boolean = isValid(apiId(ctx), apiHash(ctx))

    fun save(ctx: Context, id: String, hash: String) {
        prefs(ctx).edit().putString(KEY_ID, id.trim()).putString(KEY_HASH, hash.trim()).apply()
    }

    /** api_id is digits only; api_hash is the 32-character hex string from my.telegram.org. */
    fun isValid(id: String, hash: String): Boolean =
        id.trim().matches(Regex("\\d{3,12}")) && hash.trim().matches(Regex("[0-9a-fA-F]{32}"))
}
