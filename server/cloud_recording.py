"""Agora Cloud Recording REST API wrapper.

Provides acquire → start → query → stop lifecycle for mix-mode audio+video
recording of per-language output channels. Each channel gets its own
recording session saved to S3 as HLS segments.

Uses urllib.request (same pattern as sr_data.py) — no extra dependencies.
"""

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

API_BASE = "https://api.sd-rtn.com"


@dataclass
class RecordingSession:
    channel: str
    uid: str
    resource_id: str
    sid: str
    mode: str = "mix"


def _basic_auth_header(customer_key: str, customer_secret: str) -> str:
    """Build HTTP Basic Authorization header value."""
    credentials = f"{customer_key}:{customer_secret}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


def _api_request(method: str, path: str, customer_key: str,
                 customer_secret: str, body: dict | None = None) -> dict:
    """Make a request to the Agora Cloud Recording REST API.

    Returns parsed JSON response.  Raises on HTTP or network errors.
    """
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json",
        "Authorization": _basic_auth_header(customer_key, customer_secret),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        raise urllib.error.HTTPError(
            e.url, e.code, f"{e.reason}: {error_body}", e.headers, None
        ) from None


def _acquire(app_id: str, customer_key: str, customer_secret: str,
             channel: str, uid: str) -> str:
    """Acquire a cloud recording resource ID.

    Returns the resourceId string needed for start/stop/query.
    """
    path = f"/v1/apps/{app_id}/cloud_recording/acquire"
    body = {
        "cname": channel,
        "uid": uid,
        "clientRequest": {
            "scene": 0,
            "resourceExpiredHour": 72,
        },
    }
    resp = _api_request("POST", path, customer_key, customer_secret, body)
    return resp["resourceId"]


def start_channel_recording(
    app_id: str,
    app_cert: str,
    customer_key: str,
    customer_secret: str,
    channel: str,
    recording_uid: int,
    storage_config: dict,
) -> RecordingSession:
    """Acquire resource + start mix-mode audio+video recording for one channel.

    Uses mix mode so all UIDs in the channel are recorded into a single
    mixed HLS output. Mix mode supports #allstream# subscriptions to
    capture all publishers automatically.

    Args:
        app_id: Agora App ID.
        app_cert: Agora App Certificate (for token generation).
        customer_key: Agora REST API customer key.
        customer_secret: Agora REST API customer secret.
        channel: Channel name to record.
        recording_uid: UID for the recording bot (800000+ range).
        storage_config: S3 storage config dict with vendor, region, bucket,
            accessKey, secretKey, fileNamePrefix.

    Returns:
        RecordingSession with resource_id and sid for later stop/query.
    """
    from server.token_api import generate_viewer_token

    uid_str = str(recording_uid)

    # Step 1: Acquire resource
    resource_id = _acquire(app_id, customer_key, customer_secret, channel, uid_str)

    # Step 2: Generate token for recording UID on this channel
    token = generate_viewer_token(app_id, app_cert, channel, recording_uid)

    # Step 3: Start recording (mix mode — single mixed AV output per channel)
    path = (f"/v1/apps/{app_id}/cloud_recording/resourceid/{resource_id}"
            f"/mode/mix/start")
    body = {
        "cname": channel,
        "uid": uid_str,
        "clientRequest": {
            "token": token,
            "recordingConfig": {
                "streamTypes": 2,  # audio + video
                "channelType": 1,  # live broadcast
                "subscribeAudioUids": ["#allstream#"],
                "subscribeVideoUids": ["#allstream#"],
                "maxIdleTime": 120,
                # Mix mode defaults to 360x640 portrait at 500kbps — override
                # to landscape HD so the recording matches the source frame.
                "transcodingConfig": {
                    "width": 1280,
                    "height": 720,
                    "fps": 25,
                    "bitrate": 4000,         # kbps
                    "mixedVideoLayout": 1,   # 1 = best-fit (single publisher fills canvas)
                    "backgroundColor": "#000000",
                },
            },
            "recordingFileConfig": {
                "avFileType": ["hls"],
            },
            "storageConfig": storage_config,
        },
    }
    resp = _api_request("POST", path, customer_key, customer_secret, body)
    sid = resp["sid"]

    return RecordingSession(
        channel=channel,
        uid=uid_str,
        resource_id=resource_id,
        sid=sid,
    )


def stop_channel_recording(
    app_id: str,
    customer_key: str,
    customer_secret: str,
    session: RecordingSession,
) -> dict:
    """Stop an active recording session.

    Returns server response with upload status.
    """
    path = (f"/v1/apps/{app_id}/cloud_recording/resourceid/{session.resource_id}"
            f"/sid/{session.sid}/mode/{session.mode}/stop")
    body = {
        "cname": session.channel,
        "uid": session.uid,
        "clientRequest": {
            "async_stop": False,
        },
    }
    return _api_request("POST", path, customer_key, customer_secret, body)


def query_channel_recording(
    app_id: str,
    customer_key: str,
    customer_secret: str,
    session: RecordingSession,
) -> dict:
    """Query recording status.

    Returns server response with recording status details.
    """
    path = (f"/v1/apps/{app_id}/cloud_recording/resourceid/{session.resource_id}"
            f"/sid/{session.sid}/mode/{session.mode}/query")
    return _api_request("GET", path, customer_key, customer_secret)
