"""Audio and speech KATs (spec 10.3): A1 activity and onsets, A2 sync, A3 Apple CER, A4 word-start clamp,
A5 10 minutes of speech, A6 subtitle sources, A7 offline helper run (manual), A8 whisper CER against Apple.

Stages run directly on a `prepare_run` Run (INTERFACES section 8); A2 takes its visual onsets from the fixture's
truth, because the content survey belongs to another module.
"""
import json
import re
import socket
import subprocess
import time
from pathlib import Path

import numpy as np

import fixtures_audio as fa
from fixtures_core import build_copy
from katlib import FAIL, SKIP, kat, verdict
from vl import audio, speech, sync
from vl.cli import prepare_run
from vl.swiftbuild import helper_path

SEGMENT_TOLERANCE_S = 0.040
ONSET_TOLERANCE_S = 0.003
SYNC_TOLERANCE_MS = 3.0
CLAMP_TOLERANCE_S = 0.1
SUBTITLE_TOLERANCE_S = 0.001
APPLE_CER_MAX = 0.05
WHISPER_CER_MAX = 0.10
LOOP_ASR_MAX_S = 30.0
LOOP_END_TOLERANCE_S = 1.5
SILENCE_NOISE_DB = -60              # silencedetect truth uses the activity rule's silence level ...
SILENCE_MIN_S = 0.25                # ... and its merge gap
TEST_MODEL = Path("/opt/homebrew/share/whisper-cpp/for-tests-ggml-tiny.bin")
NOT_TEXT = re.compile(r"[\s.,?!·…\"'“”‘’]")
NO_NETWORK_PROFILE = "(version 1)(allow default)(deny network*)"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
HELLO, LAST = "안녕하세요", "마지막으로"


def talk_fixture(ctx):
    return ctx.fixture("audio_talk.mp4", fa.build_talk) if fa.has_voice(fa.TALK_VOICE) else None


def paragraph_fixture(ctx):
    return ctx.fixture("audio_paragraph.mp4", fa.build_paragraph) if fa.has_voice(fa.PARAGRAPH_VOICE) else None


def staged_run(ctx, video, name, *flags):
    """A Run as `analyze` builds it, with the audio stage done (speech needs its segments and wav)."""
    run = prepare_run(["analyze", str(video), "--out", str(ctx.out_dir(name)), "--mode", "both", *flags])
    run.set_mode("both", "selftest")
    run.analysis["audio"] = audio.analyze_audio(run)
    return run


def timed_speech(ctx, video, name, *flags):
    """(speech section, seconds of the speech stage alone, run); --refresh so the transcriber really runs."""
    run = staged_run(ctx, video, name, "--refresh", *flags)
    helper_path("transcribe")                    # a first-use compile must not count as transcription time
    started = time.monotonic()
    section = speech.analyze_speech(run)
    return section, time.monotonic() - started, run


def transcript_text(section):
    return " ".join(segment["text"] for segment in section["segments"])


def cer(reference, hypothesis):
    """Character error rate after dropping whitespace and punctuation (Levenshtein / reference length)."""
    ref, hyp = NOT_TEXT.sub("", reference), NOT_TEXT.sub("", hypothesis)
    previous = list(range(len(hyp) + 1))
    for i, ref_char in enumerate(ref, 1):
        current = [i]
        for j, hyp_char in enumerate(hyp, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ref_char != hyp_char)))
        previous = current
    return previous[-1] / len(ref)


def silencedetect_activity(video):
    """Independent truth: spans between ffmpeg silencedetect's silence marks (the fixtures start silent)."""
    log = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(video), "-map", "0:a",
                          "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_S}", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    spans, active_from = [], None
    for kind, value in re.findall(r"silence_(start|end): (-?[\d.]+)", log):
        if kind == "end":
            active_from = float(value)
        elif active_from is not None:
            spans.append((active_from, float(value)))
            active_from = None
    return spans


def asr_unavailable(section, run, engine):
    """SKIP reason when the engine is missing on this Mac (locale, model or binary), else None."""
    if section is not None and section["source"] == engine:
        return None
    missing = [w for w in run.warnings if "not installed" in w or "not found" in w or "no whisper model" in w]
    return f"{engine} unavailable: {missing[0]}" if missing else None


def word_start(section, text):
    return next((w[0] for seg in section["segments"] for w in seg["words"] if NOT_TEXT.sub("", w[2]) == text), None)


def raw_word_start(run, source, text):
    rows = speech.source_rows(run, source)       # cache hit: the transcriber's unclamped output
    return next((w[0] for row in rows for w in row["words"] if NOT_TEXT.sub("", w[2]) == text), None)


def clamp_failures(section, run, source, video):
    """HELLO and LAST clamped to the voiced onsets that silencedetect finds, within ±0.1 s."""
    truth = silencedetect_activity(video)
    expected = {HELLO: truth[0][0], LAST: next(start for start, _ in truth if start > fa.CUTS_S[1])}
    failures, notes = [], []
    for text, onset in expected.items():
        start = word_start(section, text)
        if start is None:
            failures.append(f"{source}: word {text} not recognised")
            continue
        raw = raw_word_start(run, source, text)
        notes.append(f"{text} {raw:.2f}->{start:.2f} (truth {onset:.3f})")
        if abs(start - onset) > CLAMP_TOLERANCE_S:
            failures.append(f"{source}: {text} starts {start:.3f}, voiced onset {onset:.3f}")
    if not section["word_start_clamped"]:
        failures.append(f"{source}: word_start_clamped is false")
    return failures, ", ".join(notes)


@kat("A1", "audio", quick=True)
def a1_activity_and_onsets(ctx):
    video = talk_fixture(ctx)
    if video is None:
        return SKIP, f"voice {fa.TALK_VOICE} not installed"
    section = staged_run(ctx, video, "A1").analysis["audio"]
    truth = silencedetect_activity(video)
    measured = [(s["start_s"], s["end_s"]) for s in section["segments"]]
    failures = []
    worst = 0.0
    if len(measured) != len(truth):
        failures.append(f"{len(measured)} segments, silencedetect finds {len(truth)}: {measured} vs {truth}")
    else:
        worst = max(abs(a - b) for m, t in zip(measured, truth) for a, b in zip(m, t))
        if worst > SEGMENT_TOLERANCE_S:
            failures.append(f"segment edge off by {worst * 1000:.0f} ms (> 40)")
    onset_times = np.array([onset["t"] for onset in section["onsets"]])
    beeps = [flash + fa.AUDIO_LATE_S for flash in fa.FLASHES_S]
    errors_ms = [float(onset_times[np.argmin(np.abs(onset_times - b))] - b) * 1000 for b in beeps] if onset_times.size else []
    if len(errors_ms) != len(beeps) or max(abs(e) for e in errors_ms) > ONSET_TOLERANCE_S * 1000:
        failures.append(f"beep onset errors {[round(e, 1) for e in errors_ms]} ms (> ±3)")
    beep_kinds = {s["kind"] for s in section["segments"] if any(s["start_s"] <= b + 0.05 < s["end_s"] for b in beeps)}
    if beep_kinds != {"blip"}:
        failures.append(f"100 ms beeps labelled {sorted(beep_kinds)}, expected blip")
    return verdict(failures, f"{len(measured)} segments, max edge error {worst * 1000:.1f} ms vs silencedetect; "
                             f"beep onsets {', '.join(f'{e:+.1f}' for e in errors_ms)} ms; beeps are blips")


@kat("A2", "sync", quick=True)
def a2_sync(ctx):
    video = talk_fixture(ctx)
    if video is None:
        return SKIP, f"voice {fa.TALK_VOICE} not installed"
    run = staged_run(ctx, video, "A2")
    run.analysis["speech"] = speech.analyze_speech(run)       # labels speech segments; their onsets are excluded
    run.visual_onsets = ([(t, "cut") for t in fa.CUTS_S] + [(fa.DISSOLVE_S[0], "transition")] +
                         [(t, kind) for t in fa.FLASHES_S for kind in ("transient", "flash")])
    result = sync.analyze_sync(run)
    truth_ms = fa.AUDIO_LATE_S * 1000
    if result["status"] != "ok":
        return FAIL, f"sync {result}"
    failures = []
    if result["pairs"] < 3:
        failures.append(f"{result['pairs']} pairs")
    if abs(result["offset_ms_median"] - truth_ms) > SYNC_TOLERANCE_MS:
        failures.append(f"median offset {result['offset_ms_median']} ms, truth +{truth_ms:.0f}")
    if abs(result["xcorr_lag_ms"] - truth_ms) > SYNC_TOLERANCE_MS:
        failures.append(f"xcorr lag {result['xcorr_lag_ms']} ms, truth +{truth_ms:.0f}")
    end_to_end = ctx.vl("analyze", video, "--out", ctx.out_dir("A2-e2e"), "--json")
    e2e = json.loads(end_to_end.stdout)["sync"] if end_to_end.returncode == 0 else None
    if e2e is None or e2e["status"] != "ok" or e2e["pairs"] < 3 or abs(e2e["offset_ms_median"] - truth_ms) > SYNC_TOLERANCE_MS:
        failures.append(f"analyze end to end: sync {e2e} (exit {end_to_end.returncode})")
    e2e_detail = f"; analyze end to end {e2e['offset_ms_median']:+.1f} ms, n={e2e['pairs']}" if e2e else ""
    return verdict(failures, f"median {result['offset_ms_median']:+.1f} ms (truth +{truth_ms:.0f}), n={result['pairs']}, "
                             f"IQR {result['offset_ms_iqr']}, xcorr {result['xcorr_lag_ms']:+d} ms (fixture onsets)"
                             + e2e_detail)


@kat("A3", "speech")
def a3_apple_cer(ctx):
    video = paragraph_fixture(ctx)
    if video is None:
        return SKIP, f"voice {fa.PARAGRAPH_VOICE} not installed"
    section, seconds, run = timed_speech(ctx, video, "A3", "--asr", "apple")
    unavailable = asr_unavailable(section, run, speech.APPLE)
    if unavailable:
        return SKIP, unavailable
    if section is None or section["source"] != speech.APPLE:
        return FAIL, f"no Apple transcript: {run.warnings}"
    rate = cer(fa.PARAGRAPH, transcript_text(section))
    failures = [f"CER {rate:.1%} > 5 %"] if rate > APPLE_CER_MAX else []
    return verdict(failures, f"Apple CER {rate:.1%} on {run.range.duration_s:.1f} s, speech stage {seconds:.2f} s")


@kat("A4", "speech", quick=True)
def a4_word_start_clamp(ctx):
    video = talk_fixture(ctx)
    if video is None:
        return SKIP, f"voice {fa.TALK_VOICE} not installed"
    run = staged_run(ctx, video, "A4", "--asr", "apple")
    section = speech.analyze_speech(run)
    unavailable = asr_unavailable(section, run, speech.APPLE)
    if unavailable:
        return SKIP, unavailable
    if section is None or section["source"] != speech.APPLE:
        return FAIL, f"no Apple transcript: {run.warnings}"
    failures, notes = clamp_failures(section, run, speech.APPLE, video)
    return verdict(failures, f"Apple word starts raw->clamped: {notes}")


@kat("A5", "speech")
def a5_ten_minutes(ctx):
    if not fa.has_voice(fa.PARAGRAPH_VOICE):
        return SKIP, f"voice {fa.PARAGRAPH_VOICE} not installed"
    video = ctx.fixture("audio_loop10m.mp4", fa.build_loop)
    section, seconds, run = timed_speech(ctx, video, "A5", "--asr", "apple")
    unavailable = asr_unavailable(section, run, speech.APPLE)
    if unavailable:
        return SKIP, unavailable
    if section is None or not section["segments"]:
        return FAIL, f"no transcript: {run.warnings}"
    last_end = section["segments"][-1]["end_s"]
    audio_end = run.probe["duration_s"]
    failures = []
    if seconds >= LOOP_ASR_MAX_S:
        failures.append(f"ASR took {seconds:.1f} s (>= 30)")
    if abs(audio_end - last_end) > LOOP_END_TOLERANCE_S:
        failures.append(f"last segment ends {last_end:.2f} s, audio ends {audio_end:.2f} s")
    return verdict(failures, f"{audio_end / 60:.1f} min in {seconds:.1f} s, {len(section['segments'])} segments, "
                             f"last ends {audio_end - last_end:.2f} s before the audio end, "
                             f"CER {cer(fa.PARAGRAPH * fa.LOOP_REPEATS, transcript_text(section)):.1%}")


def subtitle_failures(label, section, expected_source):
    if section is None or section["source"] != expected_source:
        return [f"{label}: source {section and section['source']}, expected {expected_source}"]
    failures = []
    texts = tuple(segment["text"] for segment in section["segments"])
    if texts != fa.REFERENCE_TEXTS:
        failures.append(f"{label}: texts {texts}")
    pairs = zip(section["segments"], fa.REFERENCE_CUES)
    worst = max((max(abs(seg["start_s"] - cue[0]), abs(seg["end_s"] - cue[1])) for seg, cue in pairs), default=0.0)
    if worst > SUBTITLE_TOLERANCE_S + 1e-9:
        failures.append(f"{label}: cue times off by {worst * 1000:.1f} ms")
    if section["word_start_clamped"]:
        failures.append(f"{label}: subtitle times were clamped")
    return failures


@kat("A6", "speech", quick=True)
def a6_subtitle_sources(ctx):
    talk = talk_fixture(ctx)
    if talk is None:
        return SKIP, f"voice {fa.TALK_VOICE} not installed"
    embedded = ctx.fixture("audio_talk_mov_text.mp4", fa.build_talk_with_subtitles(talk))
    ctx.fixture("audio_talk_sidecar.srt", fa.build_text(fa.reference_srt()))
    ctx.fixture("audio_talk_webvtt.vtt", fa.build_text(fa.reference_vtt()))
    cases = (("mov_text", embedded, speech.SUBTITLE_STREAM),
             ("sidecar .srt", ctx.fixture("audio_talk_sidecar.mp4", build_copy(talk)), speech.SIDECAR),
             ("sidecar .vtt", ctx.fixture("audio_talk_webvtt.mp4", build_copy(talk)), speech.SIDECAR))
    failures = []
    for label, video, expected in cases:
        run = staged_run(ctx, video, f"A6-{label.replace(' ', '-').replace('.', '')}")
        failures += subtitle_failures(label, speech.analyze_speech(run), expected)
    return verdict(failures, f"mov_text, sidecar .srt and .vtt: {len(fa.REFERENCE_CUES)} cues each, text identical "
                             "(markup stripped, lines joined), times within 1 ms, not clamped")


def is_port_reachable(port, sandboxed):
    prefix = [SANDBOX_EXEC, "-p", NO_NETWORK_PROFILE] if sandboxed else []
    return subprocess.run([*prefix, "/usr/bin/nc", "-z", "-w", "2", "127.0.0.1", str(port)],
                          capture_output=True).returncode == 0


@kat("A7", "privacy", manual=True)
def a7_offline_transcribe(ctx):
    """Transcribe under sandbox-exec with all network denied (the XPC speech service is outside the sandbox,
    so a Wi-Fi-off run by the user is still needed). A loopback listener is the positive control."""
    video = talk_fixture(ctx)
    if video is None:
        return SKIP, f"voice {fa.TALK_VOICE} not installed"
    wav = audio.asr_wav(staged_run(ctx, video, "A7"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        control = (is_port_reachable(port, sandboxed=False), is_port_reachable(port, sandboxed=True))
    if control != (True, False):
        return FAIL, f"sandbox control: loopback reachable unsandboxed={control[0]}, sandboxed={control[1]}"
    result = subprocess.run([SANDBOX_EXEC, "-p", NO_NETWORK_PROFILE, str(helper_path("transcribe")), str(wav), "ko-KR"],
                            capture_output=True, text=True)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    failures = [f"exit {result.returncode}: {result.stderr.strip()[-200:]}"] if result.returncode != 0 or not lines else []
    return verdict(failures, f"transcribe gave {len(lines)} segments with network denied (control: loopback blocked "
                             "only inside the sandbox); OCR helper not covered here; Wi-Fi-off run still manual")


@kat("A8", "speech")
def a8_whisper(ctx):
    video = paragraph_fixture(ctx)
    if video is None:
        return SKIP, f"voice {fa.PARAGRAPH_VOICE} not installed"
    whisper_section, whisper_s, run = timed_speech(ctx, video, "A8-whisper", "--asr", "whisper")
    unavailable = asr_unavailable(whisper_section, run, speech.WHISPER)
    if unavailable:
        return SKIP, unavailable
    if whisper_section is None or whisper_section["source"] != speech.WHISPER:
        return FAIL, f"no whisper transcript: {run.warnings}"
    whisper_cer = cer(fa.PARAGRAPH, transcript_text(whisper_section))
    failures = [f"whisper CER {whisper_cer:.1%} > 10 %"] if whisper_cer > WHISPER_CER_MAX else []
    apple_section, apple_s, apple_run = timed_speech(ctx, video, "A8-apple", "--asr", "apple")
    apple = (f"Apple CER {cer(fa.PARAGRAPH, transcript_text(apple_section)):.1%} in {apple_s:.2f} s"
             if apple_section and apple_section["source"] == speech.APPLE else "Apple unavailable")
    talk = talk_fixture(ctx)
    clamp_note = "clamp not checked (no talk voice)"
    if talk is not None:
        talk_run = staged_run(ctx, talk, "A8-talk", "--asr", "whisper")
        clamp_errors, clamp_note = clamp_failures(speech.analyze_speech(talk_run), talk_run, speech.WHISPER, talk)
        failures += clamp_errors
    silent_run = staged_run(ctx, ctx.fixture("audio_non_speech.mp4", fa.build_non_speech), "A8-non-speech",
                            "--asr", "whisper")
    invented = speech.analyze_speech(silent_run)["segments"]
    failures += [f"whisper text over non-speech: {[s['text'] for s in invented]}"] if invented else []
    refusal = "test model absent"
    if TEST_MODEL.is_file():
        test_run = staged_run(ctx, video, "A8-test-model", "--asr", "whisper", "--whisper-model", str(TEST_MODEL))
        refused = speech.analyze_speech(test_run)
        is_refused = refused["source"] == speech.NO_SOURCE and any("refused" in w for w in test_run.warnings)
        failures += [] if is_refused else [f"for-tests model not refused: {refused['source']}"]
        refusal = "for-tests model refused"
    return verdict(failures, f"whisper CER {whisper_cer:.1%} in {whisper_s:.2f} s vs {apple} "
                             f"({run.range.duration_s:.1f} s clip); whisper word starts raw->clamped: {clamp_note}; "
                             f"no text over beeps only; {refusal}")
