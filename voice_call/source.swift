import Foundation
import AVFoundation

// ============================================================
// 语音通话的「耳朵」：录一段，检测到你说完就停，存成 wav。
// 识别交给 Python（faster-whisper），这里只管录。
//
//   open -W -n -a VoiceCall.app --args --wav /tmp/a.wav --max 12
//
// 必须以 .app 方式启动（open -a）才拿得到麦克风权限；
// 直接执行 Contents/MacOS/VoiceCall 会被 TCC 拒绝。
// ============================================================

func argValue(_ name: String, _ fallback: String) -> String {
    let a = CommandLine.arguments
    if let i = a.firstIndex(of: name), i + 1 < a.count { return a[i + 1] }
    return fallback
}

let wavPath = argValue("--wav", "/tmp/voice_call.wav")
let maxSec = max(2.0, min(30.0, Double(argValue("--max", "12")) ?? 12))
let silenceSec = max(0.4, min(3.0, Double(argValue("--silence", "1.0")) ?? 1.0))
let noSpeechSec = 8.0

func finish(_ status: String, _ code: Int32) -> Never {
    FileHandle.standardError.write((status + "\n").data(using: .utf8)!)
    try? status.write(toFile: wavPath + ".status", atomically: true, encoding: .utf8)
    exit(code)
}

let engine = AVAudioEngine()
let input = engine.inputNode
let format = input.outputFormat(forBus: 0)
guard format.sampleRate > 0 else { finish("no-input", 4) }

let url = URL(fileURLWithPath: wavPath)
try? FileManager.default.removeItem(at: url)

// 硬看门狗：不管卡在引擎、权限还是哪一步，最多 max+4 秒必须退出。
// 麦克风被一个卡死的进程一直占着，比录不到声音严重得多。
DispatchQueue.global(qos: .background).async {
    Thread.sleep(forTimeInterval: maxSec + 4)
    exit(9)
}

let settings: [String: Any] = [
    AVFormatIDKey: kAudioFormatLinearPCM,
    AVSampleRateKey: format.sampleRate,
    AVNumberOfChannelsKey: Int(format.channelCount),
    AVLinearPCMBitDepthKey: 32,
    AVLinearPCMIsFloatKey: true,
    AVLinearPCMIsNonInterleaved: true,
]
guard let file = try? AVAudioFile(forWriting: url, settings: settings,
                                  commonFormat: .pcmFormatFloat32,
                                  interleaved: false) else {
    finish("cannot-write", 5)
}

let lock = NSLock()
var level: Float = 0
var maxLevel: Float = 0

input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
    try? file.write(from: buffer)
    guard let data = buffer.floatChannelData?[0] else { return }
    let n = Int(buffer.frameLength)
    var sum: Float = 0
    for i in 0..<n { sum += data[i] * data[i] }
    let rms = sqrt(sum / Float(max(n, 1)))
    lock.lock()
    level = rms
    maxLevel = max(maxLevel, rms)
    lock.unlock()
}

do {
    engine.prepare()
    try engine.start()
} catch {
    finish("cannot-start", 6)
}

let started = Date()

// 0.3 秒环境噪声，定个门槛
var noise: Float = 0
for _ in 0..<3 {
    Thread.sleep(forTimeInterval: 0.1)
    lock.lock(); noise = max(noise, level); lock.unlock()
}
let threshold = max(0.006, noise * 2.2)

var speechAt: Date? = nil
var lastLoud = started

while true {
    Thread.sleep(forTimeInterval: 0.1)
    lock.lock(); let rms = level; lock.unlock()
    let now = Date()

    if rms > threshold {
        if speechAt == nil { speechAt = now }
        lastLoud = now
    }

    let total = now.timeIntervalSince(started)
    if let s = speechAt {
        let spoke = now.timeIntervalSince(s)
        if spoke > 0.5 && now.timeIntervalSince(lastLoud) > silenceSec { break }
        if total > maxSec { break }
    } else if total > noSpeechSec {
        break
    }
}

engine.stop()
input.removeTap(onBus: 0)

// 给文件一点时间落盘
Thread.sleep(forTimeInterval: 0.25)

let seconds = Date().timeIntervalSince(started)
if speechAt == nil {
    // 没人说话就别留下文件，免得 Whisper 对着静音瞎编
    try? FileManager.default.removeItem(at: url)
    finish("empty peak=\(maxLevel) dur=\(seconds)", 1)
}
finish("ok peak=\(maxLevel) dur=\(seconds)", 0)
