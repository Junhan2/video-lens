// video-lens selftest fixture renderer: draws Korean text with AppKit (cv2's Hershey font has no Hangul).
//
//   render OUT.png WIDTH HEIGHT [options] "SIZE|TEXT" ["SIZE|TEXT" ...]
//     --bg R,G,B       solid background (default 255,255,255)
//     --stripes        8 px hue stripes instead of a solid background (busy subtitle background)
//     --fg R,G,B       text colour (default 255,255,255)
//     --font NAME      PostScript font name (default AppleSDGothicNeo-Bold)
//     --stroke W       outline width in points, drawn in black around the fill
//     --x X            left edge of every line (default 80)
//     --y0 Y           the first line's box bottom sits Y px below the top (default 140); each next line goes
//                      down by 1.8 x the size of the line above
//     --center         centre each line horizontally instead of using --x
//     --bottom Y       single-line layout: the line's box bottom sits Y px above the image bottom
import AppKit

@main
struct Render {
    static func main() {
        var args = Array(CommandLine.arguments.dropFirst())
        guard args.count >= 3, let width = Int(args[1]), let height = Int(args[2]) else {
            FileHandle.standardError.write("usage: render OUT.png WIDTH HEIGHT [options] \"SIZE|TEXT\"...\n".data(using: .utf8)!)
            exit(2)
        }
        let out = args[0]
        args.removeFirst(3)
        let options = Options(parsing: &args)
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height, bitsPerSample: 8,
                                   samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                   bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        drawBackground(options, width: width, height: height)
        drawLines(args, options, width: width, height: height)
        NSGraphicsContext.restoreGraphicsState()
        do {
            try rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
        } catch {
            FileHandle.standardError.write("render: cannot write \(out): \(error)\n".data(using: .utf8)!)
            exit(1)
        }
    }

    static func drawBackground(_ options: Options, width: Int, height: Int) {
        if options.isStriped {
            for x in stride(from: 0, to: width, by: 8) {
                NSColor(calibratedHue: CGFloat(x) / CGFloat(width), saturation: 0.5, brightness: 0.6, alpha: 1).setFill()
                NSRect(x: x, y: 0, width: 8, height: height).fill()
            }
            return
        }
        options.background.setFill()
        NSRect(x: 0, y: 0, width: width, height: height).fill()
    }

    static func drawLines(_ specs: [String], _ options: Options, width: Int, height: Int) {
        var y = CGFloat(options.bottom ?? (height - options.y0))
        for spec in specs {
            let parts = spec.split(separator: "|", maxSplits: 1).map(String.init)
            guard parts.count == 2, let size = Double(parts[0]), let font = NSFont(name: options.font, size: CGFloat(size)) else {
                FileHandle.standardError.write("render: bad line or font: \(spec)\n".data(using: .utf8)!)
                exit(2)
            }
            var attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: options.foreground]
            if options.stroke > 0 {
                attributes[.strokeColor] = NSColor.black
                attributes[.strokeWidth] = -options.stroke     // negative: fill and stroke
            }
            let text = NSAttributedString(string: parts[1], attributes: attributes)
            let x = options.isCentered ? (CGFloat(width) - text.size().width) / 2 : CGFloat(options.x)
            text.draw(at: NSPoint(x: x, y: y))
            y -= CGFloat(size) * 1.8
        }
    }
}

struct Options {
    var background = NSColor.white
    var foreground = NSColor.white
    var font = "AppleSDGothicNeo-Bold"
    var stroke = 0.0
    var x = 80
    var y0 = 140
    var bottom: Int? = nil
    var isCentered = false
    var isStriped = false

    init(parsing args: inout [String]) {
        var rest: [String] = []
        var index = 0
        while index < args.count {
            let flag = args[index]
            let value = index + 1 < args.count ? args[index + 1] : ""
            switch flag {
            case "--bg": background = Options.color(value); index += 2
            case "--fg": foreground = Options.color(value); index += 2
            case "--font": font = value; index += 2
            case "--stroke": stroke = Double(value) ?? 0; index += 2
            case "--x": x = Int(value) ?? x; index += 2
            case "--y0": y0 = Int(value) ?? y0; index += 2
            case "--bottom": bottom = Int(value); index += 2
            case "--center": isCentered = true; index += 1
            case "--stripes": isStriped = true; index += 1
            default: rest.append(flag); index += 1
            }
        }
        args = rest
    }

    static func color(_ text: String) -> NSColor {
        let parts = text.split(separator: ",").compactMap { Double($0) }.map { CGFloat($0) / 255 }
        guard parts.count == 3 else { return .white }
        return NSColor(deviceRed: parts[0], green: parts[1], blue: parts[2], alpha: 1)
    }
}
