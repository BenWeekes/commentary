import os
from dataclasses import dataclass, field

import yaml


@dataclass
class LiveSourceConfig:
    type: str = "agora"
    channel: str = ""
    video_uid: int = 73
    atmosphere_uid: int = 74
    commentary_uid: int = 75
    url: str = ""
    ingest_channel: str = ""
    publish_uid: int = 73
    retry_seconds: float = 5.0
    original_channel: str = ""
    original_buffer_seconds: float = 1.0
    audio_stream_index: int = -1
    atmosphere_audio_stream_index: int = -1
    demo_media_file: str = ""
    demo_atmosphere_file: str = ""
    demo_srt_port: int = 10080


@dataclass
class MatchConfig:
    match_id: str
    mode: str = "demo"  # "demo" or "live"
    sport_event_id: str = ""

    # Demo mode fields (required when mode=demo)
    audio: str = ""
    video_h264: str = ""
    events: str = ""
    atmosphere: str | None = None

    # Live mode fields (required when mode=live)
    source_channel: str = ""
    video_uid: int = 73
    atmosphere_uid: int = 74
    commentary_uid: int = 75
    source: LiveSourceConfig | None = None

    # Shared
    video_delay: float = 7.0
    events_offset: int = 0
    max_stt_duration: float = 6.5
    languages: list[str] = field(default_factory=lambda: ["es", "pt", "fr", "tr", "de"])
    prestart_seconds: float = 30.0

    # Management
    display_name: str = ""
    enabled: bool = True
    auto_manage: bool = False
    kickoff_utc: str = ""


@dataclass
class ServerConfig:
    agora_app_id: str
    agora_app_cert: str
    deepgram_api_key: str
    openai_api_key: str
    elevenlabs_api_key: str
    sportradar_api_key: str
    control_port: int = 8080
    translation_model: str = "gpt-5.4"
    matches: list[MatchConfig] = field(default_factory=list)
    # Cloud Recording
    cloud_recording: dict | None = None
    agora_customer_key: str = ""
    agora_customer_secret: str = ""
    # Auth
    ops_auth_enabled: bool = False
    ops_username: str = "ops"
    ops_password: str = ""
    ops_session_secret: str = ""
    ops_session_ttl_hours: int = 12


def _resolve_path(path: str, base_dir: str) -> str:
    """Resolve a path relative to the config file's directory."""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path))


def _parse_live_source(raw_match: dict, base_dir: str = "") -> LiveSourceConfig | None:
    """Parse nested `source:` or synthesize an Agora source from legacy fields."""
    source_raw = raw_match.get("source")
    if source_raw:
        source_type = source_raw.get("type", "agora")
        demo_media_file = source_raw.get("demo_media_file", "")
        demo_atmosphere_file = source_raw.get("demo_atmosphere_file", "")
        if demo_media_file and base_dir:
            demo_media_file = _resolve_path(demo_media_file, base_dir)
        if demo_atmosphere_file and base_dir:
            demo_atmosphere_file = _resolve_path(demo_atmosphere_file, base_dir)

        return LiveSourceConfig(
            type=source_type,
            channel=source_raw.get("channel", ""),
            video_uid=source_raw.get("video_uid", 73),
            atmosphere_uid=source_raw.get("atmosphere_uid", 74),
            commentary_uid=source_raw.get("commentary_uid", 75),
            url=source_raw.get("url", ""),
            ingest_channel=source_raw.get("ingest_channel", ""),
            publish_uid=source_raw.get("publish_uid", 73),
            retry_seconds=source_raw.get("retry_seconds", 5.0),
            original_channel=source_raw.get("original_channel", f"{raw_match.get('match_id', '')}-original"),
            original_buffer_seconds=source_raw.get("original_buffer_seconds", 1.0),
            audio_stream_index=source_raw.get("audio_stream_index", -1),
            atmosphere_audio_stream_index=source_raw.get("atmosphere_audio_stream_index", -1),
            demo_media_file=demo_media_file,
            demo_atmosphere_file=demo_atmosphere_file,
            demo_srt_port=int(source_raw.get("demo_srt_port", 10080)),
        )

    if any(k in raw_match for k in ("source_channel", "video_uid", "atmosphere_uid", "commentary_uid")):
        return LiveSourceConfig(
            type="agora",
            channel=raw_match.get("source_channel", ""),
            video_uid=raw_match.get("video_uid", 73),
            atmosphere_uid=raw_match.get("atmosphere_uid", 74),
            commentary_uid=raw_match.get("commentary_uid", 75),
        )

    return None


def get_live_source(match_cfg: MatchConfig) -> LiveSourceConfig | None:
    """Return the effective live source config for a match."""
    if match_cfg.mode != "live":
        return None
    if match_cfg.source:
        return match_cfg.source
    if match_cfg.source_channel:
        return LiveSourceConfig(
            type="agora",
            channel=match_cfg.source_channel,
            video_uid=match_cfg.video_uid,
            atmosphere_uid=match_cfg.atmosphere_uid,
            commentary_uid=match_cfg.commentary_uid,
        )
    return None


def get_live_source_channel(match_cfg: MatchConfig) -> str:
    """Return the viewer-facing source channel for live original audio/video."""
    source = get_live_source(match_cfg)
    if not source:
        return ""
    if source.type == "srt":
        return source.ingest_channel
    if source.type in ("srt_direct", "demo_srt_direct"):
        return source.original_channel
    return source.channel


def load_config(yaml_path: str) -> ServerConfig:
    """Parse YAML config, merge with env vars, validate file paths."""
    with open(yaml_path) as f:
        raw = yaml.safe_load(f)

    base_dir = os.path.dirname(os.path.abspath(yaml_path))

    agora_app_id = raw.get("agora_app_id") or os.environ.get("AGORA_APP_ID", "")
    agora_app_cert = raw.get("agora_app_cert") or os.environ.get("AGORA_APP_CERT", "")
    deepgram_key = os.environ.get("DEEPGRAM_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY", "")
    sportradar_key = os.environ.get("SPORTRADAR_API_KEY", "")

    matches = []
    for m in raw.get("matches", []):
        mode = m.get("mode", "demo")
        live_source = _parse_live_source(m, base_dir)

        # Resolve file paths only for demo mode
        audio = ""
        video_h264 = ""
        events = ""
        atmosphere = None
        if mode == "demo":
            audio = _resolve_path(m["audio"], base_dir)
            video_h264 = _resolve_path(m["video_h264"], base_dir)
            events = _resolve_path(m["events"], base_dir)
            if m.get("atmosphere"):
                atmosphere = _resolve_path(m["atmosphere"], base_dir)

        matches.append(MatchConfig(
            match_id=m["match_id"],
            mode=mode,
            sport_event_id=m.get("sport_event_id", ""),
            audio=audio,
            video_h264=video_h264,
            events=events,
            atmosphere=atmosphere,
            source_channel=live_source.channel if live_source and live_source.type == "agora" else (
                live_source.ingest_channel if live_source and live_source.type == "srt"
                else live_source.original_channel if live_source and live_source.type in ("srt_direct", "demo_srt_direct")
                else m.get("source_channel", "")
            ),
            video_uid=live_source.video_uid if live_source and live_source.type == "agora" else (
                live_source.publish_uid if live_source and live_source.type == "srt"
                else live_source.publish_uid if live_source and live_source.type in ("srt_direct", "demo_srt_direct")
                else m.get("video_uid", 73)
            ),
            atmosphere_uid=live_source.atmosphere_uid if live_source and live_source.type == "agora" else 0,
            commentary_uid=live_source.commentary_uid if live_source and live_source.type == "agora" else (
                live_source.publish_uid if live_source and live_source.type == "srt"
                else live_source.publish_uid if live_source and live_source.type in ("srt_direct", "demo_srt_direct")
                else m.get("commentary_uid", 75)
            ),
            source=live_source,
            video_delay=m.get("video_delay", 7.0),
            events_offset=m.get("events_offset", 0),
            max_stt_duration=m.get("max_stt_duration", 6.5),
            languages=m.get("languages", ["es", "pt", "fr", "tr", "de"]),
            prestart_seconds=m.get("prestart_seconds", 30.0),
            display_name=m.get("display_name", ""),
            enabled=m.get("enabled", True),
            auto_manage=m.get("auto_manage", False),
            kickoff_utc=m.get("kickoff_utc", ""),
        ))

    # Cloud Recording config — S3 keys come from env, rest from YAML
    cloud_recording_raw = raw.get("cloud_recording")
    if cloud_recording_raw:
        s3_access = os.environ.get("S3_ACCESS_KEY", "")
        s3_secret = os.environ.get("S3_SECRET_KEY", "")
        if s3_access:
            cloud_recording_raw["accessKey"] = s3_access
        if s3_secret:
            cloud_recording_raw["secretKey"] = s3_secret
    agora_customer_key = os.environ.get("AGORA_CUSTOMER_KEY", "")
    agora_customer_secret = os.environ.get("AGORA_CUSTOMER_SECRET", "")

    # Auth config — env vars take precedence
    ops_password = os.environ.get("OPS_PASSWORD", raw.get("ops_password", ""))
    ops_session_secret = os.environ.get("OPS_SESSION_SECRET", raw.get("ops_session_secret", ""))
    ops_auth_enabled = raw.get("ops_auth_enabled", False)
    # Auto-enable auth if password is set via env
    if ops_password and not raw.get("ops_auth_enabled"):
        ops_auth_enabled = True

    return ServerConfig(
        agora_app_id=agora_app_id,
        agora_app_cert=agora_app_cert,
        deepgram_api_key=deepgram_key,
        openai_api_key=openai_key,
        elevenlabs_api_key=elevenlabs_key,
        sportradar_api_key=sportradar_key,
        control_port=raw.get("control_port", 8080),
        translation_model=raw.get("translation_model", "gpt-5.4"),
        matches=matches,
        cloud_recording=cloud_recording_raw,
        agora_customer_key=agora_customer_key,
        agora_customer_secret=agora_customer_secret,
        ops_auth_enabled=ops_auth_enabled,
        ops_password=ops_password,
        ops_username=os.environ.get("OPS_USERNAME", raw.get("ops_username", "ops")),
        ops_session_secret=ops_session_secret,
        ops_session_ttl_hours=int(raw.get("ops_session_ttl_hours", 12)),
    )


def validate_config(cfg: ServerConfig, dry_run=False):
    """Validate config, checking that required keys and files exist.

    Raises ValueError with details on failure.
    """
    errors = []

    if not cfg.agora_app_id:
        errors.append("AGORA_APP_ID not set (env or config)")
    if not cfg.agora_app_cert:
        errors.append("AGORA_APP_CERT not set (env or config)")
    if not cfg.deepgram_api_key:
        errors.append("DEEPGRAM_API_KEY not set")
    if not cfg.openai_api_key:
        errors.append("OPENAI_API_KEY not set")
    if not cfg.elevenlabs_api_key:
        errors.append("ELEVENLABS_API_KEY not set")

    if cfg.cloud_recording:
        if not cfg.agora_customer_key:
            errors.append("cloud_recording configured but AGORA_CUSTOMER_KEY not set")
        if not cfg.agora_customer_secret:
            errors.append("cloud_recording configured but AGORA_CUSTOMER_SECRET not set")
        for field in ("vendor", "region", "bucket", "accessKey", "secretKey"):
            if field not in cfg.cloud_recording:
                errors.append(f"cloud_recording missing required field: {field}")

    if cfg.ops_auth_enabled:
        if not cfg.ops_password:
            errors.append("ops_auth_enabled is true but OPS_PASSWORD is not set")
        if not cfg.ops_session_secret:
            errors.append("ops_auth_enabled is true but OPS_SESSION_SECRET is not set")

    if not cfg.matches:
        errors.append("No matches configured")

    for m in cfg.matches:
        prefix = f"match '{m.match_id}'"
        if not m.enabled:
            continue  # skip validation for disabled matches
        if m.mode not in ("demo", "live"):
            errors.append(f"{prefix}: invalid mode '{m.mode}' (must be 'demo' or 'live')")
        elif m.mode == "demo":
            if not os.path.isfile(m.audio):
                errors.append(f"{prefix}: audio file not found: {m.audio}")
            if not os.path.isfile(m.video_h264):
                errors.append(f"{prefix}: video_h264 file not found: {m.video_h264}")
            if not os.path.isfile(m.events):
                errors.append(f"{prefix}: events file not found: {m.events}")
            if m.atmosphere and not os.path.isfile(m.atmosphere):
                errors.append(f"{prefix}: atmosphere file not found: {m.atmosphere}")
        elif m.mode == "live":
            source = get_live_source(m)
            if not source:
                errors.append(f"{prefix}: source or legacy live fields required for live mode")
            elif source.type == "agora":
                if not source.channel:
                    errors.append(f"{prefix}: source.channel required for live agora source")
            elif source.type == "srt":
                if not source.url:
                    errors.append(f"{prefix}: source.url required for live srt source")
                if not source.ingest_channel:
                    errors.append(f"{prefix}: source.ingest_channel required for live srt source")
                if not source.publish_uid:
                    errors.append(f"{prefix}: source.publish_uid required for live srt source")
            elif source.type in ("srt_direct", "demo_srt_direct"):
                if source.type == "demo_srt_direct":
                    if not source.demo_media_file:
                        errors.append(f"{prefix}: source.demo_media_file required for live demo_srt_direct source")
                    elif not os.path.isfile(source.demo_media_file):
                        errors.append(f"{prefix}: source.demo_media_file not found: {source.demo_media_file}")
                    if not source.demo_atmosphere_file:
                        errors.append(f"{prefix}: source.demo_atmosphere_file required for live demo_srt_direct source")
                    elif not os.path.isfile(source.demo_atmosphere_file):
                        errors.append(f"{prefix}: source.demo_atmosphere_file not found: {source.demo_atmosphere_file}")
                    if source.demo_srt_port <= 0:
                        errors.append(f"{prefix}: source.demo_srt_port must be > 0")
                elif not source.url:
                    errors.append(f"{prefix}: source.url required for live srt_direct source")
                if not source.original_channel:
                    errors.append(f"{prefix}: source.original_channel required for live {source.type} source")
                if not source.publish_uid:
                    errors.append(f"{prefix}: source.publish_uid required for live {source.type} source")
                if source.original_buffer_seconds < 0:
                    errors.append(f"{prefix}: source.original_buffer_seconds must be >= 0")
                if source.original_buffer_seconds >= m.video_delay:
                    errors.append(f"{prefix}: source.original_buffer_seconds must be less than video_delay")
            else:
                errors.append(f"{prefix}: invalid live source type '{source.type}'")
        if not m.languages:
            errors.append(f"{prefix}: no languages configured")

    if errors:
        raise ValueError("Config validation failed:\n  " + "\n  ".join(errors))
