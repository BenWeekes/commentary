import os
from dataclasses import dataclass, field

import yaml


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

    # Shared
    video_delay: float = 7.0
    events_offset: int = 0
    max_stt_duration: float = 5.0
    languages: list[str] = field(default_factory=lambda: ["es", "pt", "fr", "tr", "de"])

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
    translation_model: str = "gpt-4o-mini"
    matches: list[MatchConfig] = field(default_factory=list)
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
            source_channel=m.get("source_channel", ""),
            video_uid=m.get("video_uid", 73),
            atmosphere_uid=m.get("atmosphere_uid", 74),
            commentary_uid=m.get("commentary_uid", 75),
            video_delay=m.get("video_delay", 7.0),
            events_offset=m.get("events_offset", 0),
            max_stt_duration=m.get("max_stt_duration", 5.0),
            languages=m.get("languages", ["es", "pt", "fr", "tr", "de"]),
            display_name=m.get("display_name", ""),
            enabled=m.get("enabled", True),
            auto_manage=m.get("auto_manage", False),
            kickoff_utc=m.get("kickoff_utc", ""),
        ))

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
        translation_model=raw.get("translation_model", "gpt-4o-mini"),
        matches=matches,
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
            if not m.source_channel:
                errors.append(f"{prefix}: source_channel required for live mode")
        if not m.languages:
            errors.append(f"{prefix}: no languages configured")

    if errors:
        raise ValueError("Config validation failed:\n  " + "\n  ".join(errors))
