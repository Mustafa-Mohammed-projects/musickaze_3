[app]
title = KazeMusic
package.name = kazemusic
package.domain = org.kaze

source.dir = .
# NOTE: buildozer does not support comments at the end of a value line,
# so every comment lives on its own line.
# ttf is required: the bundled font is what makes Arabic text readable.
source.include_exts = py,png,jpg,kv,atlas,json,ttf
source.exclude_dirs = tests,bin,venv,.git,.github,.buildozer,music,__pycache__

version = 1.1.0

# kivymd was removed (it was never imported and only made the build heavier).
# "android" is provided automatically by python-for-android.
requirements = python3,kivy==2.3.0,pyjnius

# The playback service: runs in its own process, owns the MediaPlayer and the
# notification.  Format  Name:script.py  -> Java class <package>.ServiceMusic
# (the name "Music" is also used in audio_backend.py).
services = Music:service.py

orientation = portrait
fullscreen = 0

icon.filename = %(source.dir)s/icon.png
presplash.filename = %(source.dir)s/presplash.png
android.presplash_color = #0E0E13

# Android 13+ uses READ_MEDIA_AUDIO, older versions use READ_EXTERNAL_STORAGE.
# WAKE_LOCK keeps the music playing when the screen turns off,
# FOREGROUND_SERVICE lets the playback service keep running in the background.
android.permissions = READ_MEDIA_AUDIO,READ_EXTERNAL_STORAGE,POST_NOTIFICATIONS,WAKE_LOCK,FOREGROUND_SERVICE

android.api = 33
android.minapi = 24
android.ndk = 25b
android.enable_androidx = True
android.archs = arm64-v8a
android.accept_sdk_license = True

android.release_artifact = apk
# Signing: the workflow passes the keystore through P4A_RELEASE_* environment
# variables, so the android.keystore* fields must stay unset here.

p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
