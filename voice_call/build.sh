#!/bin/bash
# 重新编译语音通话助手（改了 source.swift 才需要跑）
set -e
cd "$(dirname "$0")"

APP="VoiceCall.app"
mkdir -p "$APP/Contents/MacOS"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.assistant.voicecall</string>
  <key>CFBundleName</key><string>VoiceCall</string>
  <key>CFBundleExecutable</key><string>VoiceCall</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>语音通话需要麦克风，把你说的话转成文字。</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>语音通话需要语音识别，把你说的话转成文字。</string>
</dict>
</plist>
PLIST

swiftc -O source.swift -o "$APP/Contents/MacOS/VoiceCall"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "✅ 已编译 $APP"
