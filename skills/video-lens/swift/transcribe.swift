import Foundation
import Speech
import AVFoundation
import CoreMedia
// usage: transcribe AUDIO_FILE [locale=ko-KR]  -> JSON lines per final segment {"start","end","text","words":[[start,end,text]]}
@main struct Transcribe {
  static func main() async {
    let args = CommandLine.arguments
    guard args.count > 1 else { fputs("usage: transcribe FILE [locale]\n", stderr); exit(2) }
    let locale = Locale(identifier: args.count > 2 ? args[2] : "ko-KR")
    guard #available(macOS 26.0, *) else { fputs("needs macOS 26\n", stderr); exit(3) }
    let installed = await SpeechTranscriber.installedLocales.map { $0.identifier(.bcp47) }
    guard installed.contains(locale.identifier(.bcp47)) else {
      fputs("locale \(locale.identifier(.bcp47)) not installed on device; installed: \(installed)\n", stderr); exit(4)
    }
    do {
      let transcriber = SpeechTranscriber(locale: locale, preset: .timeIndexedTranscriptionWithAlternatives)
      let file = try AVAudioFile(forReading: URL(fileURLWithPath: args[1]))
      let collect = Task { () -> Int in
        var n = 0
        for try await r in transcriber.results {
          var words: [[Any]] = []
          for run in r.text.runs {
            if let tr = run.audioTimeRange {
              let w = String(r.text[run.range].characters)
              words.append([ (tr.start.seconds * 1000).rounded() / 1000, (tr.end.seconds * 1000).rounded() / 1000, w ])
            }
          }
          let obj: [String: Any] = ["start": (r.range.start.seconds * 1000).rounded() / 1000,
                                    "end": (r.range.end.seconds * 1000).rounded() / 1000,
                                    "text": String(r.text.characters), "words": words]
          let d = try JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
          print(String(data: d, encoding: .utf8)!); n += 1
        }
        return n
      }
      let analyzer = SpeechAnalyzer(modules: [transcriber])
      if let last = try await analyzer.analyzeSequence(from: file) {
        try await analyzer.finalizeAndFinish(through: last)
      } else { await analyzer.cancelAndFinishNow() }
      let n = try await collect.value
      fputs("segments: \(n)\n", stderr)
    } catch { fputs("error: \(error)\n", stderr); exit(1) }
  }
}
