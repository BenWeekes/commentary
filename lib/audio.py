import subprocess
import tempfile
import time
import wave


def load_atmosphere(wav_path):
    """Load a 16kHz mono wav file as raw PCM bytes."""
    with wave.open(wav_path, 'rb') as wf:
        assert wf.getsampwidth() == 2 and wf.getnchannels() == 1 and wf.getframerate() == 16000, \
            f"Atmosphere must be 16kHz 16-bit mono, got {wf.getframerate()}Hz {wf.getsampwidth()}B {wf.getnchannels()}ch"
        pcm = wf.readframes(wf.getnframes())
    print(f"[ATMOS] Loaded {len(pcm)/32000:.1f}s of atmosphere from {wav_path}")
    return pcm


def convert_to_pcm(audio_path):
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    pcm_path = tmp.name
    tmp.close()
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path,
         "-ar", "16000", "-ac", "1", "-f", "wav", pcm_path],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed converting {audio_path}: {result.stderr.decode(errors='replace').strip()}"
        )
    return pcm_path


def pcm_stream_from_pipe(pipe, stop_event, chunk_ms=10):
    """Read live PCM from a subprocess stdout pipe.

    Yields (chunk_bytes, audio_offset_seconds) tuples.
    No pacing needed — the pipe naturally produces data at real-time rate
    because the Go subscriber receives audio in real time from Agora.

    Accumulates partial reads into a buffer and only yields complete chunks
    to avoid injecting silence via zero-padding.

    Args:
        pipe: file-like object (subprocess.stdout) producing raw S16LE 16kHz mono PCM.
        stop_event: threading.Event to signal shutdown.
        chunk_ms: chunk duration in ms (default 10).
    """
    bytes_per_chunk = int(16000 * 2 * 1 * chunk_ms / 1000)  # 320 for 10ms
    offset_s = 0.0
    increment = chunk_ms / 1000.0
    buf = b''

    while not stop_event.is_set():
        data = pipe.read(bytes_per_chunk - len(buf))
        if not data:
            break
        buf += data
        while len(buf) >= bytes_per_chunk:
            yield (buf[:bytes_per_chunk], offset_s)
            buf = buf[bytes_per_chunk:]
            offset_s += increment


def pcm_chunks_realtime(wav_path, chunk_ms=100):
    bytes_per_sec = 32000
    chunk_bytes = int(bytes_per_sec * chunk_ms / 1000)
    chunk_duration = chunk_ms / 1000.0
    # Use wave.open to read PCM data (handles variable-size WAV headers)
    wf = wave.open(wav_path, "rb")
    pcm_data = wf.readframes(wf.getnframes())
    wf.close()
    offset_bytes = 0
    audio_offset = 0.0
    t_start = time.monotonic()
    while offset_bytes < len(pcm_data):
        data = pcm_data[offset_bytes:offset_bytes + chunk_bytes]
        if not data:
            break
        yield data, audio_offset
        offset_bytes += chunk_bytes
        audio_offset += chunk_duration
        target = t_start + audio_offset
        sleep_for = target - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
