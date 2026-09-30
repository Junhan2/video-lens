"""Speech transcript (spec 7.4): subtitle stream > sidecar .srt/.vtt > Apple SpeechTranscriber > whisper.cpp > none.

ASR word starts are clamped to the voiced onset found by the audio stage (only ASR word ends are trusted).
A source that fails is a warning and the next source is tried; speech never ends the run.
"""
import bisect
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from . import audio, decode
from .cache import parse_jsonl, write_atomic
from .context import clock, content_time
from .errors import VlError
from .swiftbuild import helper_path

SUBTITLE_STREAM = "subtitle-stream"
SIDECAR = "sidecar"
APPLE = "apple-speechtranscriber"
WHISPER = "whisper.cpp"
NO_SOURCE = "none"
SUBTITLE_SOURCES = (SUBTITLE_STREAM, SIDECAR)
CLAMP_WORD_STARTS = True            # spec 11.1 gate A4
EXIT_LOCALE_MISSING = 4             # transcribe.swift: the locale's model is not installed on this Mac
WHISPER_CLI = "whisper-cli"
WHISPER_THREADS = 8
TEST_MODEL_MARK = "for-tests"
DTW_PRESETS = ("tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en",
               "large.v1", "large.v2", "large.v3", "large.v3.turbo")
CLAMP_END_MARGIN_S = 0.02
SPEECH_COVERAGE_MIN = 0.5           # an activity segment half covered by words is speech, and a whisper
                                    # segment must have half its word time over activity to be kept
SRT_CUE_MAX_S = 7.0
TXT_NAME = "transcript.txt"
SRT_NAME = "transcript.srt"
SRT_TIMING = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")
MARKUP = re.compile(r"<[^>]*>|\{\\[^}]*\}")
SENTENCE_END = re.compile(r"[.?!。？！…]$")
INSTALLED_LOCALES = re.compile(r"installed: (\[.*\])")


def analyze_speech(run):
    """The analysis.json `speech` section; None when there is nothing to transcribe (no audio, no subtitles)."""
    plan = source_plan(run)
    if not plan:
        if run.params["speech"]["asr"] != "auto":
            run.warn(f"no transcript: --asr {run.params['speech']['asr']} needs an audio stream")
        return None
    for source in plan:
        rows = source_rows(run, source)
        if rows is not None:
            return build_section(run, source, rows)
    run.warn("no transcript: no speech source worked (reasons in the warnings above)")
    return {"source": NO_SOURCE, "locale": run.params["speech"]["lang"], "word_start_clamped": False, "chars": 0,
            "segments": [], "sentences": [], "files": None}


def source_plan(run):
    """Sources to try in order: --asr apple|whisper pins one engine; auto prefers the subtitles already there."""
    asr = run.params["speech"]["asr"]
    has_audio = run.analysis["audio"] is not None
    if asr != "auto":
        return [APPLE if asr == "apple" else WHISPER] if has_audio else []
    plan = [SUBTITLE_STREAM] if run.probe["subtitle_streams"] else []
    plan += [SIDECAR] if run.probe["sidecar_subtitles"] else []
    if has_audio:
        plan.append(APPLE)
        if run.params["speech"]["whisper_model"]:
            plan.append(WHISPER)
    return plan


def source_rows(run, source):
    """Raw rows `{start_s, end_s, text, words: [[s, e, w]]}` on the timeline, cached per source; None on failure.

    A failure is cached too, as one `{"failed": [warnings]}` row: otherwise a source that always fails (bitmap
    subtitles, a missing locale) would rerun on every analyze and record `speech` as a cache miss. --refresh retries.
    """
    entry = run.cache.entry("speech", source_key(run, source), "jsonl")
    if entry.is_hit:
        rows = entry.read_jsonl()
        if rows and "failed" in rows[0]:
            for message in rows[0]["failed"]:
                run.warn(message)
            return None
        return rows
    warned_before = len(run.warnings)
    rows = TRANSCRIBERS[source](run)
    entry.save_jsonl(rows if rows is not None else [{"failed": run.warnings[warned_before:]}])
    return rows


def source_key(run, source):
    speech = run.params["speech"]
    if source == SUBTITLE_STREAM:
        return {"source": source, "stream": run.probe["subtitle_streams"][0]["index"]}
    if source == SIDECAR:
        path = Path(run.probe["sidecar_subtitles"][0])
        stat = path.stat()
        return {"source": source, "path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    key = {"source": source, "lang": speech["lang"], "audio": audio.audio_params(run)}
    if source == WHISPER:
        model = Path(speech["whisper_model"]) if speech["whisper_model"] else None
        key["model"] = [str(model), model.stat().st_size] if model and model.is_file() else None
    return key


def subtitle_stream_rows(run):
    stream = run.probe["subtitle_streams"][0]
    try:
        text = decode.run_ffmpeg(["-i", str(run.input_path), "-map", f"0:{stream['index']}", "-f", "srt", "-"],
                                 "subtitle extraction")
    except VlError as error:
        run.warn(f"subtitle stream {stream['index']} ({stream['codec']}) is not readable as text ({error.what}); "
                 "trying the next speech source")
        return None
    return parse_srt(text.decode("utf-8", "replace"))


def sidecar_rows(run):
    """-copyts keeps the file's own cue times: a sidecar's 0 is the start of playback, i.e. timeline 0."""
    path = run.probe["sidecar_subtitles"][0]
    try:
        text = decode.run_ffmpeg(["-copyts", "-i", path, "-f", "srt", "-"], "sidecar subtitle read")
    except VlError as error:
        run.warn(f"sidecar subtitles {Path(path).name} unreadable ({error.what}); trying the next speech source")
        return None
    return parse_srt(text.decode("utf-8", "replace"))


def parse_srt(text):
    """SRT cues as rows; markup (<i>, {\\an8}) is dropped and the lines of a cue are joined with a space."""
    rows = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip().split("\n")
        timing = next((i for i, line in enumerate(lines) if SRT_TIMING.search(line)), None)
        if timing is None:
            continue
        numbers = [int(n) for n in SRT_TIMING.search(lines[timing]).groups()]
        cue_text = " ".join(MARKUP.sub("", line).strip() for line in lines[timing + 1:]).strip()
        if cue_text:
            rows.append({"start_s": srt_seconds(*numbers[:4]), "end_s": srt_seconds(*numbers[4:]),
                         "text": re.sub(r"\s+", " ", cue_text), "words": []})
    return sorted(rows, key=lambda row: row["start_s"])


def srt_seconds(hours, minutes, seconds, millis):
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def apple_rows(run):
    """transcribe.swift (SpeechTranscriber, on device) on the range's 16 kHz wav; one JSON line per segment."""
    locale = run.params["speech"]["lang"]
    wav = audio.asr_wav(run)
    if wav is None:
        return None
    try:
        helper = helper_path("transcribe")
    except VlError as error:
        run.warn(f"Apple speech helper unavailable ({error.what}); trying the next speech source")
        return None
    run.progress("speech", 0, 1)
    result = subprocess.run([str(helper), str(wav), locale], capture_output=True, text=True)
    run.progress("speech", 1, 1)
    if result.returncode == EXIT_LOCALE_MISSING:
        run.warn(f"ASR locale {locale} not installed (installed: {installed_locales(result.stderr)})")
        return None
    if result.returncode != 0:
        run.warn(f"Apple speech transcription failed ({last_line(result.stderr, result.returncode)})")
        return None
    return [shifted_row(row, run.range.start_s) for row in parse_jsonl(result.stdout)]


def installed_locales(stderr):
    match = INSTALLED_LOCALES.search(stderr)
    try:
        return ", ".join(json.loads(match.group(1))) if match else "unknown"
    except json.JSONDecodeError:
        return match.group(1)


def last_line(stderr, code):
    lines = stderr.strip().splitlines()
    return lines[-1] if lines else f"exit {code}"


def shifted_row(row, offset_s):
    """A helper row (seconds from the wav start) moved onto the timeline; word text keeps its leading space."""
    return {"start_s": row["start"] + offset_s, "end_s": row["end"] + offset_s, "text": row["text"],
            "words": [[s + offset_s, e + offset_s, w] for s, e, w in row["words"]]}


def whisper_rows(run):
    """whisper-cli with a real ggml model; word times come from DTW alignment when the model has a preset."""
    model = run.params["speech"]["whisper_model"]
    if not model:
        run.warn("no whisper model: pass --whisper-model PATH or set VIDEO_LENS_WHISPER_MODEL")
        return None
    if TEST_MODEL_MARK in Path(model).name:
        run.warn(f"whisper model {Path(model).name} is a test model with no real transcript; refused. "
                 "Pass a real ggml model with --whisper-model")
        return None
    if not Path(model).is_file():
        run.warn(f"whisper model not found: {model}")
        return None
    cli = shutil.which(WHISPER_CLI)
    if cli is None:
        run.warn("whisper-cli not found; install whisper-cpp or use --asr apple")
        return None
    wav = audio.asr_wav(run)
    if wav is None:
        return None
    language = run.params["speech"]["lang"].split("-")[0].lower()
    preset = dtw_preset(model)
    alignment = ["-nfa", "-dtw", preset] if preset else []     # whisper.cpp runs DTW only without flash attention
    with tempfile.TemporaryDirectory(dir=run.cache.dir, prefix=".whisper-") as folder:
        prefix = Path(folder) / "asr"
        run.progress("speech", 0, 1)
        result = subprocess.run([cli, "-m", model, "-l", language, "-t", str(WHISPER_THREADS), *alignment, "-oj", "-ojf",
                                 "-of", str(prefix), "-np", "-f", str(wav)], capture_output=True, text=True)
        run.progress("speech", 1, 1)
        report = prefix.with_suffix(".json")
        if result.returncode != 0 or not report.is_file():
            run.warn(f"whisper.cpp transcription failed ({last_line(result.stderr, result.returncode)})")
            return None
        document = json.loads(report.read_bytes().decode("utf-8", "replace"))
    return [whisper_row(item, run.range.start_s) for item in document.get("transcription", [])]


def dtw_preset(model):
    """whisper.cpp's alignment-head preset for a standard model file name (ggml-large-v3-turbo-q5_0.bin ->
    large.v3.turbo); None for other names."""
    name = re.sub(r"-q\d.*$", "", re.sub(r"^ggml-", "", Path(model).stem))
    preset = name.replace("-", ".")
    return preset if preset in DTW_PRESETS else None


def whisper_row(item, offset_s):
    start_s = item["offsets"]["from"] / 1000 + offset_s
    return {"start_s": start_s, "end_s": item["offsets"]["to"] / 1000 + offset_s, "text": item["text"],
            "words": dtw_words(item.get("tokens", []), start_s, offset_s)}


def dtw_words(tokens, segment_start_s, offset_s):
    """Words timed by DTW (t_dtw, 10 ms units). A token with a leading space and letters starts a word; a word
    ends at the DTW time of its last token with letters (punctuation gets stray DTW times) and starts where the
    previous word ended, the first at the segment start. whisper's own token offsets collapse to the segment
    start or run seconds early, so without DTW there are no word times at all."""
    words = []
    for token in tokens:
        text = token.get("text", "")
        if not text or text.startswith("[_"):
            continue
        is_spoken = has_letters(text)
        if words and not (text.startswith(" ") and is_spoken):
            words[-1][2] += text
        elif is_spoken:
            words.append([None, None, text])
        if is_spoken and words and token.get("t_dtw", -1) >= 0:
            words[-1][1] = token["t_dtw"] / 100 + offset_s
    if any(end is None for _, end, _ in words):
        return []
    start = segment_start_s
    for word in words:
        word[0], word[1] = start, max(word[1], start)
        start = word[1]
    return words


TRANSCRIBERS = {SUBTITLE_STREAM: subtitle_stream_rows, SIDECAR: sidecar_rows, APPLE: apple_rows, WHISPER: whisper_rows}


def build_section(run, source, rows):
    if source in SUBTITLE_SOURCES:
        rows = [row for row in rows if row["end_s"] > run.range.start_s and row["start_s"] < run.range.end_s]
    activity = run.analysis["audio"]["segments"] if run.analysis["audio"] else []
    voiced_starts = sorted(segment["start_s"] for segment in activity)
    has_words = any(row["words"] for row in rows)
    is_clamped = CLAMP_WORD_STARTS and source not in SUBTITLE_SOURCES and bool(voiced_starts) and has_words
    segments = [clamped_row(row, voiced_starts if is_clamped else []) for row in rows if has_letters(row["text"])]
    if source == WHISPER:
        segments = audible_segments(run, segments, activity)
    label_activity(activity, speech_spans(segments))
    sentences = write_transcripts(run, segments)
    return {
        "source": source,
        "locale": source_locale(run, source),
        "word_start_clamped": is_clamped,
        "chars": sum(len(segment["text"]) for segment in segments),
        "segments": [output_segment(segment) for segment in segments],
        "sentences": [[content_time(start), content_time(end), text] for start, end, text in sentences],
        "files": {"txt": TXT_NAME, "srt": SRT_NAME},
    }


def source_locale(run, source):
    """The ASR locale; a subtitle stream's language tag; nothing for a sidecar file."""
    if source == SUBTITLE_STREAM:
        return run.probe["subtitle_streams"][0]["lang"]
    return None if source == SIDECAR else run.params["speech"]["lang"]


def clamped_row(row, voiced_starts):
    """Spec 7.4: word start = max(start, start of the LAST activity segment starting <= word end - 20 ms).
    A segment spans its words: Apple's segment range runs on through trailing silence (to the file end on
    the last one), while word ends are the trusted times."""
    words = [[clamped_start(s, e, voiced_starts), e, w] for s, e, w in row["words"]]
    if not words:
        return {"start_s": row["start_s"], "end_s": row["end_s"], "text": row["text"].strip(), "words": []}
    return {"start_s": words[0][0], "end_s": max(e for _, e, _ in words), "text": row["text"].strip(), "words": words}


def clamped_start(start, end, voiced_starts):
    index = bisect.bisect_right(voiced_starts, end - CLAMP_END_MARGIN_S) - 1
    return max(start, voiced_starts[index]) if index >= 0 else start


def has_letters(text):
    """ASR output without a letter or digit (Apple gives "." over beeps) is not a transcript line."""
    return any(char.isalnum() for char in text)


def word_spans(segment):
    return [(w[0], w[1]) for w in segment["words"]] or [(segment["start_s"], segment["end_s"])]


def merged_spans(spans):
    """Sorted, non-overlapping (starts, ends) arrays."""
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return np.array([m[0] for m in merged]), np.array([m[1] for m in merged])


def covered_seconds(spans, starts, ends):
    """Seconds of each query interval [starts[i], ends[i]) that the merged spans cover."""
    span_starts, span_ends = spans
    if span_starts.size == 0:
        return np.zeros(len(starts))
    covered_before = np.concatenate([[0.0], np.cumsum(span_ends - span_starts)])

    def covered_until(t):
        after = np.searchsorted(span_starts, t, side="right")
        last = np.maximum(after - 1, 0)
        inside = np.minimum(t, span_ends[last]) - span_starts[last]
        return np.where(after > 0, covered_before[last] + inside, 0.0)

    return covered_until(np.asarray(ends, float)) - covered_until(np.asarray(starts, float))


def speech_spans(segments):
    """Merged spans of the words, or of whole segments when a source has no word times."""
    return merged_spans([span for segment in segments for span in word_spans(segment)])


def audible_segments(run, segments, activity):
    """whisper writes text over non-speech (measured: "자막 제공 및 ... 광고를 포함하고 있습니다." over sparse beeps,
    "한글자막 by 한효정" over pink noise). A segment stays only when at least half of its word time lies in
    activity segments; the dropped text is named in a warning, so real speech masked by noise is not lost silently."""
    activity_spans = merged_spans([(a["start_s"], a["end_s"]) for a in activity])
    kept, dropped = [], []
    for segment in segments:
        spans = np.array(word_spans(segment), float)
        duration = float((spans[:, 1] - spans[:, 0]).sum())
        audible = float(covered_seconds(activity_spans, spans[:, 0], spans[:, 1]).sum())
        is_audible = audible >= SPEECH_COVERAGE_MIN * duration if duration > 0 else audible > 0
        (kept if is_audible else dropped).append(segment)
    if dropped:
        quoted = "; ".join(f"{clock(d['start_s'])} \"{d['text'][:40]}\"" for d in dropped[:3])
        run.warn(f"whisper.cpp: dropped {len(dropped)} segment(s) with under half of their words over audible "
                 f"activity (likely hallucinated): {quoted}")
    return kept


def label_activity(activity, spans):
    """An activity segment at least half covered by speech becomes `speech`; others keep sound or blip."""
    if not activity:
        return
    covered = covered_seconds(spans, [a["start_s"] for a in activity], [a["end_s"] for a in activity])
    for segment, seconds in zip(activity, covered):
        if seconds >= SPEECH_COVERAGE_MIN * (segment["end_s"] - segment["start_s"]):
            segment["kind"] = "speech"


def output_segment(segment):
    return {"start_s": content_time(segment["start_s"]), "end_s": content_time(segment["end_s"]), "text": segment["text"],
            "words": [[content_time(s), content_time(e), w.strip()] for s, e, w in segment["words"] if w.strip()]}


def write_transcripts(run, segments):
    """transcript.txt: `[mm:ss.s] sentence` lines; transcript.srt: cues of at most 7 s cut at word boundaries.
    Returns the sentences, (start, end, text) per transcript.txt line."""
    sentences = word_groups(segments)
    lines = [f"[{clock(start)}] {text}" for start, _, text in sentences]
    write_atomic(run.out_path(TXT_NAME), ("\n".join(lines) + "\n" if lines else "").encode())
    cues = word_groups(segments, SRT_CUE_MAX_S)
    blocks = [f"{n}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n" for n, (start, end, text) in enumerate(cues, 1)]
    write_atomic(run.out_path(SRT_NAME), "\n".join(blocks).encode())
    return sentences


def word_groups(segments, max_span_s=None):
    """(start, end, text) per sentence, or per cue of at most max_span_s cut at word boundaries. Sentences run
    across ASR segment breaks (Apple cuts mid-sentence); a segment without word times stays whole."""
    groups, current = [], []

    def close():
        text = "".join(word[2] for word in current).strip()    # ASR words carry their own leading spaces
        if text:
            groups.append((current[0][0], current[-1][1], text))
        current.clear()

    for segment in segments:
        if not segment["words"]:
            close()
            groups.append((segment["start_s"], segment["end_s"], segment["text"]))
            continue
        for word in segment["words"]:
            if max_span_s and current and word[1] - current[0][0] > max_span_s:
                close()
            current.append(word)
            if SENTENCE_END.search(word[2].strip()):
                close()
    close()
    return groups


def srt_time(t):
    millis = round(max(0.0, t) * 1000)
    hours, rest = divmod(millis, 3600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
