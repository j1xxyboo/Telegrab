import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// ---------------------------------------------------------------------------
// Where the server code comes from
//
// The Python server lives one level up (../app) and is shared with the desktop
// version. It is copied into build/ before every build so there is exactly one
// copy of the code, and Chaquopy packages it alongside the Android entry point.
// ---------------------------------------------------------------------------
val serverSource = rootProject.file("../app")
val generatedPython = layout.buildDirectory.dir("generated/tegrab-python")

val syncServer = tasks.register<Sync>("syncServer") {
    from(serverSource) {
        into("app")
        exclude("**/__pycache__/**", "**/*.pyc")
    }
    into(generatedPython)
}

tasks.named("preBuild") { dependsOn(syncServer) }

// -PabiList=arm64-v8a  → phone-only build (smaller). Default includes x86_64 for emulators.
val abis = (project.findProperty("abiList") as String? ?: "arm64-v8a,x86_64").split(",").map { it.trim() }

android {
    namespace = "app.tegrab"
    compileSdk = 35

    defaultConfig {
        applicationId = "app.tegrab"
        minSdk = 29          // MediaStore.Downloads: saves to the public Downloads folder, no storage permission
        targetSdk = 35
        versionCode = (project.findProperty("versionCode") as String? ?: "1").toInt()
        versionName = project.findProperty("versionName") as String? ?: "1.0.0"
        ndk { abiFilters += abis }
    }

    signingConfigs {
        create("release") {
            val props = rootProject.file("keystore.properties")
            if (props.isFile) {
                val p = Properties().apply { props.inputStream().use { load(it) } }
                storeFile = rootProject.file(p.getProperty("storeFile"))
                storePassword = p.getProperty("storePassword")
                keyAlias = p.getProperty("keyAlias")
                keyPassword = p.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            isShrinkResources = false
            val releaseSigning = signingConfigs.getByName("release")
            // Without a keystore the release build falls back to the debug key so it still installs.
            signingConfig = if (releaseSigning.storeFile != null) releaseSigning else signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    packaging {
        jniLibs { useLegacyPackaging = false }
    }
}

chaquopy {
    defaultConfig {
        version = "3.13"
        // Chaquopy needs a host Python of the same minor version. Override with
        // -PbuildPython=/path/to/python3.13 (CI uses the runner's).
        val host = project.findProperty("buildPython") as String?
        if (host != null) buildPython(host) else buildPython("python3.13")

        pip {
            // pyaes (a Telethon dependency) ships only as an sdist, which Chaquopy 17 refuses to
            // install; android/wheels holds a wheel built from that exact sdist.
            options("--find-links", rootProject.file("wheels").absolutePath)
            install("-r", rootProject.file("requirements-android.txt").absolutePath)
        }
    }
    sourceSets {
        getByName("main") {
            srcDir(generatedPython)
        }
    }
}

// Chaquopy collects the generated sources, so they must exist first.
afterEvaluate {
    tasks.matching { it.name.startsWith("merge") && it.name.endsWith("PythonSources") || it.name.contains("PythonRequirements") }
        .configureEach { dependsOn(syncServer) }
}
