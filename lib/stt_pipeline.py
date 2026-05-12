import os
import threading
import time

from lib.corrections import TERMS_LIST, CORRECTIONS, apply_corrections
from lib.translator import translate_text, voice_for_lang
from lib.audio import convert_to_pcm, pcm_chunks_realtime, pcm_stream_from_pipe
from lib.tts_engine import _ts


def _run_stt_core(audio_source, deepgram_key, stop_event, emit_fn,
                  max_stt_duration=5.0, video_start_fn=None, keyterms=None,
                  corrections=None, log_tag="STT", audio_feed_start_ref=None,
                  play_at_fn=None, endpointing_ms=500, utterance_end_ms=1000):
    """Shared Deepgram connection, audio feeding, and forced-split logic.

    Args:
        audio_source: Path to audio file (str) or file-like object (pipe from
            subprocess.stdout) producing raw S16LE 16kHz mono PCM.
        deepgram_key: Deepgram API key.
        stop_event: threading.Event to signal shutdown.
        emit_fn: callable(corrected_text, audio_start, audio_end) per utterance.
        max_stt_duration: Force-split interims longer than this.
        video_start_fn: callable() -> float|None returning current video_start
            for log timestamps. If None, uses wall clock only.
        keyterms: optional list to override TERMS_LIST.
        corrections: list of (wrong, right) tuples. Defaults to CORRECTIONS.
            Pass empty list to disable corrections.
        log_tag: prefix for log lines.
        audio_feed_start_ref: optional mutable one-element list. When provided,
            the first successful send_media() wall clock is stored in [0].
        play_at_fn: optional callable(audio_start) -> float|None used for
            debug timing logs. The actual emitted schedule still comes from
            emit_fn, so live callers can choose their own clock.

    Returns:
        int: total number of utterances emitted.
    """
    os.environ["DEEPGRAM_API_KEY"] = deepgram_key
    from deepgram import DeepgramClient
    from deepgram.listen import ListenV1Results

    is_file = isinstance(audio_source, str)
    pcm_path = convert_to_pcm(audio_source) if is_file else None
    dg_client = DeepgramClient()

    terms = keyterms if keyterms is not None else TERMS_LIST
    terms = [term.strip() for term in terms if term and term.strip()]
    if len(terms) > 100:
        print(f"[{log_tag}] Limiting Deepgram keyterms from {len(terms)} to 100")
        terms = terms[:100]
    corr_list = corrections if corrections is not None else CORRECTIONS

    def _vs():
        vs = video_start_fn() if video_start_fn else None
        return _ts(vs)

    source_label = audio_source if is_file else "live pipe"
    print(f"[{log_tag}] Streaming {source_label} through Deepgram Nova-3...")
    print(f"[{log_tag}] max_stt_duration={max_stt_duration}s\n")

    utterance_count = [0]

    connect_kwargs = {
        "model": "nova-3",
        "language": "en",
        "encoding": "linear16",
        "sample_rate": 16000,
        "punctuate": "true",
        "smart_format": "true",
        "interim_results": "true",
        "utterance_end_ms": str(utterance_end_ms),
        "endpointing": str(endpointing_ms),
        "diarize": "true",
    }
    if terms:
        connect_kwargs["keyterm"] = terms

    try:
        ws_context = dg_client.listen.v1.connect(**connect_kwargs)
        ws = ws_context.__enter__()
    except Exception as exc:
        if not terms or "400" not in str(exc):
            raise
        print(f"[{log_tag}] Deepgram rejected keyterms ({exc}); retrying without keyterms")
        connect_kwargs.pop("keyterm", None)
        ws_context = dg_client.listen.v1.connect(**connect_kwargs)
        ws = ws_context.__enter__()

    audio_thread = None
    try:

        def mark_first_chunk_sent():
            if audio_feed_start_ref is None or audio_feed_start_ref[0] is not None:
                return
            anchor = time.time()
            audio_feed_start_ref[0] = anchor
            vs_val = video_start_fn() if video_start_fn else None
            if vs_val:
                print(f"[{log_tag}] First PCM sent — {anchor - vs_val:.2f}s after video_start")
            else:
                print(f"[{log_tag}] First PCM sent")

        def feed_audio():
            try:
                if is_file:
                    for chunk, _ in pcm_chunks_realtime(pcm_path):
                        if stop_event.is_set():
                            break
                        ws.send_media(chunk)
                        mark_first_chunk_sent()
                else:
                    for chunk, _ in pcm_stream_from_pipe(audio_source, stop_event):
                        if stop_event.is_set():
                            break
                        ws.send_media(chunk)
                        mark_first_chunk_sent()
                if not stop_event.is_set():
                    ws.send_close_stream()
            except Exception as exc:
                if stop_event.is_set() or "ConnectionClosed" in type(exc).__name__:
                    print(f"[{log_tag}] Audio feed stopped — Deepgram websocket closed")
                else:
                    raise

        wall_start = time.time()
        vs_val = video_start_fn() if video_start_fn else None
        if vs_val:
            audio_feed_offset = wall_start - vs_val
            print(f"[{log_tag}] Audio feed starting — {audio_feed_offset:.2f}s after video_start")
        else:
            print(f"[{log_tag}] Audio feed starting")
        audio_thread = threading.Thread(target=feed_audio, daemon=True)
        audio_thread.start()

        MAX_STT_DURATION = max_stt_duration
        force_split_end = [0.0]
        force_split_text = [""]

        def _speaker_from_alt(alt, audio_start, audio_end):
            counts = {}
            for word in getattr(alt, "words", []) or []:
                speaker = getattr(word, "speaker", None)
                if speaker is None:
                    continue
                word_start = getattr(word, "start", None)
                word_end = getattr(word, "end", None)
                if word_start is not None and word_end is not None:
                    if word_end < audio_start or word_start > audio_end:
                        continue
                counts[speaker] = counts.get(speaker, 0) + 1
            if not counts:
                return None
            return max(counts.items(), key=lambda item: item[1])[0]

        def _word_timings_from_alt(alt, audio_start, audio_end):
            timings = []
            for word in getattr(alt, "words", []) or []:
                word_start = getattr(word, "start", None)
                word_end = getattr(word, "end", None)
                if word_start is None or word_end is None:
                    continue
                if word_end < audio_start or word_start > audio_end:
                    continue
                timings.append({
                    "word": getattr(word, "word", None) or getattr(word, "punctuated_word", ""),
                    "start": round(float(word_start), 3),
                    "end": round(float(word_end), 3),
                    "speaker": getattr(word, "speaker", None),
                    "confidence": getattr(word, "confidence", None),
                })
            return timings

        def _emit(text, audio_start, audio_end, tag="", speaker=None, word_timings=None):
            if word_timings:
                audio_start = min(w["start"] for w in word_timings)
                audio_end = max(w["end"] for w in word_timings)
            corrected = apply_corrections(text, corr_list)
            vs_now = video_start_fn() if video_start_fn else None
            play_at = play_at_fn(audio_start) if play_at_fn else ((vs_now + audio_start) if vs_now else None)
            remaining = (play_at - time.time()) if play_at else 0.0

            print(f"  [{_vs()}] [{log_tag}{tag}] audio={audio_start:.1f}-{audio_end:.1f}s "
                  f"remaining={remaining:.2f}s"
                  + (f" speaker={speaker}" if speaker is not None else "")
                  + (f" play_at_wall={play_at:.3f}" if play_at else ""))
            print(f"           \"{corrected[:70]}\"")

            emit_fn(corrected, audio_start, audio_end, speaker=speaker, word_timings=word_timings)
            utterance_count[0] += 1

        for msg in ws:
            if stop_event.is_set():
                break
            if not isinstance(msg, ListenV1Results):
                continue

            alt = msg.channel.alternatives[0]
            transcript = alt.transcript
            audio_start = msg.start if hasattr(msg, "start") and msg.start else 0
            audio_end = audio_start + (msg.duration if hasattr(msg, "duration") and msg.duration else 0)

            if not msg.is_final:
                if transcript and force_split_end[0] <= audio_start:
                    chunk_dur = audio_end - audio_start
                    if chunk_dur >= MAX_STT_DURATION:
                        print(f"  [{_vs()}] [{log_tag}] Force-splitting {chunk_dur:.1f}s interim "
                              f"at audio={audio_start:.1f}-{audio_end:.1f}s")
                        word_timings = _word_timings_from_alt(alt, audio_start, audio_end)
                        _emit(transcript, audio_start, audio_end, tag=" SPLIT",
                              speaker=_speaker_from_alt(alt, audio_start, audio_end),
                              word_timings=word_timings)
                        force_split_end[0] = audio_end
                        force_split_text[0] = transcript
                continue

            if not transcript:
                continue

            if audio_end <= force_split_end[0]:
                print(f"  [{_vs()}] [{log_tag}] Skipping final — already force-emitted "
                      f"(audio={audio_start:.1f}-{audio_end:.1f}s)")
                force_split_text[0] = ""
                continue

            if audio_start < force_split_end[0] < audio_end:
                adj_start = force_split_end[0]
                remainder = transcript
                split_text = force_split_text[0]
                if split_text:
                    if transcript.startswith(split_text):
                        remainder = transcript[len(split_text):].lstrip(" ,.")
                    else:
                        words = transcript.split()
                        split_ratio = (force_split_end[0] - audio_start) / (audio_end - audio_start)
                        split_idx = max(1, int(len(words) * split_ratio))
                        remainder = " ".join(words[split_idx:])
                force_split_text[0] = ""
                force_split_end[0] = 0.0
                if not remainder.strip():
                    print(f"  [{_vs()}] [{log_tag}] Remainder empty after strip — skipping")
                    continue
                print(f"  [{_vs()}] [{log_tag}] Partial overlap — emitting remainder "
                      f"from {adj_start:.1f}s (original {audio_start:.1f}-{audio_end:.1f}s)")
                word_timings = _word_timings_from_alt(alt, adj_start, audio_end)
                _emit(remainder, adj_start, audio_end, tag=" REMAINDER",
                      speaker=_speaker_from_alt(alt, adj_start, audio_end),
                      word_timings=word_timings)
                continue

            force_split_end[0] = 0.0
            force_split_text[0] = ""
            word_timings = _word_timings_from_alt(alt, audio_start, audio_end)
            _emit(transcript, audio_start, audio_end,
                  speaker=_speaker_from_alt(alt, audio_start, audio_end),
                  word_timings=word_timings)
    finally:
        if audio_thread and audio_thread.is_alive() and stop_event.is_set():
            audio_thread.join(timeout=1.0)
        ws_context.__exit__(None, None, None)
        if audio_thread and audio_thread.is_alive():
            audio_thread.join(timeout=1.0)

    if pcm_path:
        os.unlink(pcm_path)
    print(f"[{log_tag}] Pipeline finished — {utterance_count[0]} utterances emitted.")
    return utterance_count[0]


def run_stt_pipeline(audio_path, tts, deepgram_key, lang, oai_client,
                     last_stt_time, stop_event, lang_file=None,
                     video_delay=3.0, max_stt_duration=6.5,
                     get_current_lang=None):
    """
    Stream audio through Deepgram → Corrections → Translate → ElevenLabs TTS.
    Uses play_at scheduling: each utterance plays at video_start + audio_start.
    Video is already delayed by video_delay (Go publisher), so video_start
    includes the delay and play_at needs no extra offset.

    get_current_lang: optional callable(lang_file, default_lang) -> lang_code
                      for demo file-based language switching.
    """

    def _resolve_lang():
        if get_current_lang and lang_file:
            return get_current_lang(lang_file, lang)
        return lang

    def emit_fn(corrected, audio_start, audio_end, speaker=None, word_timings=None):
        play_at = tts.video_start + audio_start

        def make_stt_translate_fn():
            def translate(t):
                cur_lang = _resolve_lang()
                vid = voice_for_lang(cur_lang)
                if cur_lang == "en":
                    return (t, vid)
                return (translate_text(oai_client, t, cur_lang), vid)
            return translate

        tts.speak(corrected, play_at=play_at, translate_fn=make_stt_translate_fn())
        last_stt_time[0] = time.time()

    print(f"[STT] Pipeline: STT → Correct → Translate({lang}) → ElevenLabs TTS → Agora")
    print(f"[STT] Video delay: {video_delay}s (pipeline budget)")

    _run_stt_core(
        audio_source=audio_path,
        deepgram_key=deepgram_key,
        stop_event=stop_event,
        emit_fn=emit_fn,
        max_stt_duration=max_stt_duration,
        video_start_fn=lambda: tts.video_start,
    )


def run_stt_pipeline_multi(audio_path, on_utterance, deepgram_key, stop_event,
                           video_start_ref, video_delay=7.0,
                           max_stt_duration=6.5, keyterms=None,
                           corrections=None):
    """Run STT pipeline with a multi-language callback.

    Args:
        audio_path: Path to commentary audio file.
        on_utterance: callable(corrected_text, audio_start, audio_end, play_at)
            called per utterance. The match worker fans out to all languages.
        deepgram_key: Deepgram API key.
        stop_event: threading.Event to signal shutdown.
        video_start_ref: list[float] — mutable ref, [0] is current video_start.
        video_delay: video delay in seconds (for logging).
        max_stt_duration: force-split threshold.
        keyterms: optional list to override TERMS_LIST.
        corrections: list of (wrong, right) tuples. Pass [] to disable.

    Returns:
        int: total number of utterances emitted.
    """

    def emit_fn(corrected, audio_start, audio_end, speaker=None, word_timings=None):
        vs = video_start_ref[0] if video_start_ref[0] else 0.0
        play_at = vs + audio_start
        on_utterance(corrected, audio_start, audio_end, play_at,
                     speaker=speaker, word_timings=word_timings)

    print(f"[STT] Pipeline: STT → Correct → multi-lang fan-out")
    print(f"[STT] Video delay: {video_delay}s (pipeline budget)")

    return _run_stt_core(
        audio_source=audio_path,
        deepgram_key=deepgram_key,
        stop_event=stop_event,
        emit_fn=emit_fn,
        max_stt_duration=max_stt_duration,
        video_start_fn=lambda: video_start_ref[0],
        keyterms=keyterms,
        corrections=corrections,
    )


def run_stt_pipeline_live(audio_pipe, on_utterance, deepgram_key, stop_event,
                          video_start_ref, video_delay=7.0,
                          max_stt_duration=6.5, keyterms=None,
                          corrections=None, source_media_start_ref=None):
    """Run STT pipeline from a live audio pipe (subprocess stdout).

    Same as run_stt_pipeline_multi but takes a pipe instead of a file path.

    Args:
        audio_pipe: file-like object (subprocess.stdout) producing raw PCM.
        on_utterance: callable(corrected_text, audio_start, audio_end, play_at)
        deepgram_key: Deepgram API key.
        stop_event: threading.Event to signal shutdown.
        video_start_ref: list[float] — mutable ref, [0] is current video_start.
        video_delay: video delay in seconds (for logging).
        max_stt_duration: force-split threshold.
        keyterms: optional list to override TERMS_LIST.
        corrections: list of (wrong, right) tuples. Pass [] to disable.

    Returns:
        int: total number of utterances emitted.
    """

    audio_feed_start_wall = [None]

    def schedule_anchor():
        if source_media_start_ref and source_media_start_ref[0] is not None:
            return source_media_start_ref[0]
        return audio_feed_start_wall[0]

    def live_play_at(audio_start):
        anchor = schedule_anchor()
        if anchor is not None:
            return anchor + audio_start + video_delay
        vs = video_start_ref[0] if video_start_ref[0] else 0.0
        return vs + audio_start if vs else None

    def intended_skew_ms(audio_start, play_at):
        anchor = schedule_anchor()
        if anchor is None or play_at is None:
            return None
        intended = anchor + audio_start + video_delay
        return round((play_at - intended) * 1000)

    def emit_fn(corrected, audio_start, audio_end, speaker=None, word_timings=None):
        play_at = live_play_at(audio_start)
        on_utterance(
            corrected, audio_start, audio_end, play_at,
            intended_skew_ms=intended_skew_ms(audio_start, play_at),
            speaker=speaker,
            provider="deepgram",
            schedule_anchor_wall=schedule_anchor(),
            occurred_at=(schedule_anchor() + audio_start) if schedule_anchor() is not None else None,
            occurred_end_at=(schedule_anchor() + audio_end) if schedule_anchor() is not None else None,
            word_timings=word_timings,
        )

    print(f"[STT-LIVE] Pipeline: live pipe → STT → Correct → multi-lang fan-out")
    print(f"[STT-LIVE] Video delay: {video_delay}s (pipeline budget)")

    return _run_stt_core(
        audio_source=audio_pipe,
        deepgram_key=deepgram_key,
        stop_event=stop_event,
        emit_fn=emit_fn,
        max_stt_duration=max_stt_duration,
        video_start_fn=lambda: video_start_ref[0],
        keyterms=keyterms,
        corrections=corrections,
        log_tag="STT-LIVE",
        audio_feed_start_ref=audio_feed_start_wall,
        play_at_fn=live_play_at,
    )
