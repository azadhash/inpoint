// On-device transcription through Apple's Speech framework (macOS 26+).
//
//   apple-transcribe <audio file> [language code]
//
// Prints JSON on stdout: {"locale": "en_US", "segments": [{start, end, text, words: [{word, start, end}]}]}
// Streams "[start --> end] text" progress lines on stderr as segments finalise.
// Exit 3 = language not supported by Apple's engine (caller should fall back).
import AVFoundation
import Foundation
import Speech

struct Word: Codable { let word: String; let start: Double; let end: Double }
struct Segment: Codable { let start: Double; let end: Double; let text: String; let words: [Word] }
struct Output: Codable { let locale: String; let segments: [Segment] }

func fail(_ msg: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(code)
}

func pickLocale(_ code: String?) async -> Locale? {
    let want = (code ?? "en").lowercased().replacingOccurrences(of: "_", with: "-")
    let lang = want.split(separator: "-").first.map(String.init) ?? want
    let supported = await SpeechTranscriber.supportedLocales
    let installed = await SpeechTranscriber.installedLocales
    func matches(_ l: Locale) -> Bool {
        l.identifier.lowercased().replacingOccurrences(of: "_", with: "-") == want
            || l.language.languageCode?.identifier.lowercased() == lang
    }
    let exact = { (l: Locale) in l.identifier.lowercased().replacingOccurrences(of: "_", with: "-") == want }
    let current = Locale.current
    if matches(current), supported.contains(where: { $0.identifier == current.identifier }) { return current }
    return installed.first(where: exact) ?? supported.first(where: exact)
        ?? installed.first(where: matches) ?? supported.first(where: matches)
}

@main struct Main {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count >= 2 else { fail("usage: apple-transcribe <audio> [language]", code: 2) }
        let url = URL(fileURLWithPath: args[1])
        guard let locale = await pickLocale(args.count > 2 ? args[2] : nil) else {
            fail("language not supported by Apple speech", code: 3)
        }
        do {
            let transcriber = SpeechTranscriber(
                locale: locale, transcriptionOptions: [], reportingOptions: [],
                attributeOptions: [.audioTimeRange])
            if let req = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                FileHandle.standardError.write("downloading Apple speech assets for \(locale.identifier)\n".data(using: .utf8)!)
                try await req.downloadAndInstall()
            }
            let analyzer = SpeechAnalyzer(modules: [transcriber])
            let file = try AVAudioFile(forReading: url)

            let collector = Task { () -> [Segment] in
                var out: [Segment] = []
                for try await result in transcriber.results where result.isFinal {
                    let text = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
                    if text.isEmpty { continue }
                    var words: [Word] = []
                    for run in result.text.runs {
                        guard let r = run.audioTimeRange else { continue }
                        let w = String(result.text[run.range].characters).trimmingCharacters(in: .whitespacesAndNewlines)
                        if w.isEmpty { continue }
                        words.append(Word(word: w, start: r.start.seconds, end: r.end.seconds))
                    }
                    let start = words.first?.start ?? result.range.start.seconds
                    let end = words.last?.end ?? result.range.end.seconds
                    out.append(Segment(start: start, end: end, text: text, words: words))
                    FileHandle.standardError.write(String(format: "[%.3f --> %.3f] %@\n", start, end, text).data(using: .utf8)!)
                }
                return out
            }
            if let last = try await analyzer.analyzeSequence(from: file) {
                try await analyzer.finalizeAndFinish(through: last)
            } else {
                await analyzer.cancelAndFinishNow()
            }
            let segments = try await collector.value
            let data = try JSONEncoder().encode(Output(locale: locale.identifier, segments: segments))
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write("\n".data(using: .utf8)!)
        } catch {
            fail("apple speech failed: \(error)")
        }
    }
}
