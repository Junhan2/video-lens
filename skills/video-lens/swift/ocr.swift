// video-lens OCR helper: macOS Vision VNRecognizeTextRequest, accurate level (the fast level has no Korean).
//
//   ocr [--langs ko-KR,en-US]   reads image paths from stdin, one per line; prints one JSON line per image:
//                               {"file", "w", "h", "lines": [{"text", "conf", "box": [x, y, w, h]}]}
//                               box is normalized to the image, top-left origin. An unreadable image or a failed
//                               request prints {"file", "error"} instead. Each line is flushed as soon as it is done.
//   ocr --list                  prints the languages the accurate level supports, as a JSON array
import Foundation
import ImageIO
import Vision

@main
struct OCR {
    static func main() {
        let args = Array(CommandLine.arguments.dropFirst())
        if args.first == "--list" {
            printJSON(supportedLanguages(), file: "")
            return
        }
        var langs = ["ko-KR", "en-US"]
        if let flag = args.firstIndex(of: "--langs"), flag + 1 < args.count {
            langs = args[flag + 1].split(separator: ",").map(String.init)
        }
        while let path = readLine() {
            if path.isEmpty { continue }
            printJSON(recognize(path: path, langs: langs), file: path)
        }
    }

    static func supportedLanguages() -> [String] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        return (try? request.supportedRecognitionLanguages()) ?? []
    }

    static func recognize(path: String, langs: [String]) -> [String: Any] {
        let url = URL(fileURLWithPath: path) as CFURL
        guard let source = CGImageSourceCreateWithURL(url, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            return ["file": path, "error": "unreadable image"]
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = langs
        request.usesLanguageCorrection = true
        do {
            try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        } catch {
            return ["file": path, "error": "vision: \(error.localizedDescription)"]
        }
        var lines: [[String: Any]] = []
        for observation in request.results ?? [] {
            guard let best = observation.topCandidates(1).first else { continue }
            let box = observation.boundingBox      // normalized, bottom-left origin
            guard [box.minX, box.maxY, box.width, box.height].allSatisfy({ $0.isFinite }), best.confidence.isFinite else { continue }
            lines.append(["text": best.string, "conf": rounded(Double(best.confidence), 3),
                          "box": [box.minX, 1 - box.maxY, box.width, box.height].map { rounded(Double($0), 4) }])
        }
        return ["file": path, "w": image.width, "h": image.height, "lines": lines]
    }

    static func rounded(_ value: Double, _ decimals: Int) -> Double {
        let scale = pow(10.0, Double(decimals))
        return (value * scale).rounded() / scale
    }

    /// Every request gets exactly one line: the caller reads one answer per image and would wait forever otherwise.
    static func printJSON(_ object: Any, file: String) {
        if let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) {
            print(text)
        } else {
            let escaped = file.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
            print("{\"error\":\"result not encodable as JSON\",\"file\":\"\(escaped)\"}")
        }
        fflush(stdout)      // the caller streams results; a pipe would otherwise buffer them
    }
}
