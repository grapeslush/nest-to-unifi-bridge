"""
Bridge a Google Nest Doorbell (wired) to UniFi Protect using unifi-cam-proxy.

This script requests a Nest live stream (preferring RTSP), keeps it renewed,
launches a UniFi virtual camera that pulls from the Nest stream, and optionally
polls for doorbell events.

Prerequisites
-------------
* A Google Device Access (SDM) project with camera permissions.
* An access token that has access to the Nest doorbell device.
* The device name (``enterprises/PROJECT-ID/devices/DEVICE-ID``). You can build
  this from your project ID and the doorbell device ID.
* A UniFi Protect controller (UDM Pro) reachable from where this script runs.
* A unique MAC address for the emulated camera (e.g., generate with ``python -c
  'import uuid; print(uuid.uuid4().hex[:12])'``).
* Python 3.10+.
* ``requests`` and ``unifi-cam-proxy`` installed (``pip install -r requirements.txt``).

Usage example
-------------
python nest_to_unifi_bridge.py \
    --nest-token YOUR_ACCESS_TOKEN \
    --project-id YOUR_PROJECT_ID \
    --device-id YOUR_DEVICE_ID \
    --protect-host 192.0.2.10 \
    --protect-username ubnt --protect-password ubnt \
    --camera-name "Nest Doorbell" \
    --camera-mac 01:23:45:67:89:ab

Notes
-----
* The script prefers the RTSP command. If unavailable, it attempts WebRTC and
  logs the offer/answer but leaves the media exchange to unifi-cam-proxy.
* Stream URLs expire. The script renews them ahead of expiry using the provided
  extension token or by requesting a new stream and restarting the proxy.
* Event polling is best-effort; robust production usage should integrate with
  Google Pub/Sub for real-time events.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

SDM_BASE_URL = "https://smartdevicemanagement.googleapis.com/v1"
LOG = logging.getLogger("nest_to_unifi_bridge")


def _parse_timestamp(ts: str) -> datetime:
    """Parse RFC3339 timestamps and normalize to UTC."""
    if ts.endswith("Z"):
        ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


@dataclass
class StreamInfo:
    url: str
    expires_at: datetime
    extension_token: Optional[str] = None
    protocol: str = "rtsp"

    @classmethod
    def from_rtsp_response(cls, data: Dict[str, Any]) -> "StreamInfo":
        stream = data["results"]["streamUrls"]["rtspUrl"]
        expires_at = _parse_timestamp(data["results"]["streamExtensionTokenExpiresAt"])
        token = data["results"].get("streamExtensionToken")
        return cls(url=stream, expires_at=expires_at, extension_token=token)

    @classmethod
    def from_webrtc_response(cls, data: Dict[str, Any]) -> "StreamInfo":
        # WebRTC responses return the offer/answer; the consuming proxy must handle them.
        stream = data["results"].get("answerSdp", "")
        expires_at = _parse_timestamp(data["results"]["expiresAt"])
        return cls(url=stream, expires_at=expires_at, protocol="webrtc")


class NestStreamClient:
    """Handles Nest SDM API calls for live streaming and event polling."""

    def __init__(self, access_token: str, device_name: str, session: Optional[requests.Session] = None):
        self.access_token = access_token
        self.device_name = device_name
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})
        self.current_stream: Optional[StreamInfo] = None

    def execute_command(self, command: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{SDM_BASE_URL}/{self.device_name}:executeCommand"
        payload = {"command": command, "params": params or {}}
        LOG.debug("Executing command %s", command)
        response = self.session.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def generate_rtsp_stream(self) -> StreamInfo:
        data = self.execute_command("sdm.devices.commands.CameraLiveStream.GenerateRtspStream")
        LOG.info("Generated RTSP stream")
        return StreamInfo.from_rtsp_response(data)

    def extend_rtsp_stream(self, extension_token: str) -> StreamInfo:
        data = self.execute_command(
            "sdm.devices.commands.CameraLiveStream.ExtendRtspStream",
            {"streamExtensionToken": extension_token},
        )
        LOG.info("Extended RTSP stream")
        return StreamInfo.from_rtsp_response(data)

    def generate_webrtc_stream(self) -> StreamInfo:
        # WebRTC uses offer/answer; we request an offer and expect the caller to manage SDP.
        data = self.execute_command("sdm.devices.commands.CameraLiveStream.GenerateWebRtcStream")
        LOG.info("Generated WebRTC offer; pass the SDP to your proxy")
        return StreamInfo.from_webrtc_response(data)

    def request_stream(self) -> StreamInfo:
        try:
            stream = self.generate_rtsp_stream()
        except requests.HTTPError as exc:
            LOG.warning("RTSP command unavailable (%s). Falling back to WebRTC.", exc)
            stream = self.generate_webrtc_stream()
        self.current_stream = stream
        return stream

    def ensure_stream_active(self, renew_margin: int = 120) -> StreamInfo:
        if not self.current_stream:
            return self.request_stream()

        now = datetime.now(timezone.utc)
        if (self.current_stream.expires_at - now).total_seconds() > renew_margin:
            return self.current_stream

        LOG.info("Stream close to expiry; renewing")
        try:
            if self.current_stream.extension_token:
                self.current_stream = self.extend_rtsp_stream(self.current_stream.extension_token)
            else:
                self.current_stream = self.request_stream()
        except requests.RequestException:
            LOG.exception("Failed to renew stream; requesting a new one after backoff")
            time.sleep(5)
            self.current_stream = self.request_stream()
        return self.current_stream

    def poll_events(self, interval: int, stop_event: threading.Event) -> None:
        url = f"{SDM_BASE_URL}/{self.device_name}"
        last_update: Optional[str] = None
        while not stop_event.is_set():
            try:
                response = self.session.get(url, timeout=15)
                response.raise_for_status()
                payload = response.json()
                update_time = payload.get("updateTime")
                if update_time and update_time != last_update:
                    last_update = update_time
                    events = payload.get("events", {})
                    for event_name, event_payload in events.items():
                        LOG.info("Event from Nest: %s => %s", event_name, json.dumps(event_payload))
            except requests.RequestException:
                LOG.exception("Error while polling Nest events")
            stop_event.wait(interval)


class ProtectCameraProxy:
    """Wraps unifi-cam-proxy as a subprocess to feed an RTSP/WebRTC stream into Protect."""

    def __init__(
        self,
        host: str,
        username: Optional[str],
        password: Optional[str],
        adopt_token: Optional[str],
        camera_name: str,
        mac: str,
        rtsp_username: str = "ubnt",
        rtsp_password: str = "ubnt",
        insecure: bool = False,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.adopt_token = adopt_token
        self.camera_name = camera_name
        self.mac = mac
        self.rtsp_username = rtsp_username
        self.rtsp_password = rtsp_password
        self.insecure = insecure
        self.process: Optional[subprocess.Popen] = None
        self.current_stream_url: Optional[str] = None

    def _build_command(self, stream_url: str, protocol: str) -> list[str]:
        cmd = ["unifi-cam-proxy", protocol, stream_url]
        cmd += ["--host", self.host, "--mac", self.mac, "--name", self.camera_name]
        cmd += ["--rtsp-username", self.rtsp_username, "--rtsp-password", self.rtsp_password]
        if self.username and self.password:
            cmd += ["--username", self.username, "--password", self.password]
        if self.adopt_token:
            cmd += ["--token", self.adopt_token]
        if self.insecure:
            cmd.append("--insecure")
        return cmd

    def start(self, stream_url: str, protocol: str = "rtsp") -> None:
        if self.process and self.process.poll() is None:
            LOG.info("Stopping existing proxy before restart")
            self.stop()
        if protocol != "rtsp":
            LOG.warning(
                "Starting proxy with %s. Ensure your unifi-cam-proxy build supports this mode.",
                protocol,
            )
        cmd = self._build_command(stream_url, protocol)
        LOG.info("Starting unifi-cam-proxy: %s", " ".join(cmd))
        self.process = subprocess.Popen(cmd)
        self.current_stream_url = stream_url

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            LOG.info("Terminating unifi-cam-proxy")
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                LOG.warning("Force killing unifi-cam-proxy")
                self.process.kill()
        self.process = None
        self.current_stream_url = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge Nest Doorbell to UniFi Protect")
    parser.add_argument("--nest-token", required=True, help="Google SDM access token")
    parser.add_argument("--project-id", required=True, help="Device Access project ID")
    parser.add_argument("--device-id", required=True, help="Doorbell device ID")
    parser.add_argument("--protect-host", required=True, help="Protect host/IP (UDM Pro)")
    parser.add_argument("--protect-username", help="Protect admin username")
    parser.add_argument("--protect-password", help="Protect admin password")
    parser.add_argument("--protect-token", help="Protect adoption token alternative to username/password")
    parser.add_argument("--camera-name", default="Nest Doorbell", help="Name for the virtual camera")
    parser.add_argument("--camera-mac", required=True, help="Unique MAC address to emulate")
    parser.add_argument("--rtsp-username", default="ubnt", help="RTSP username presented to Protect")
    parser.add_argument("--rtsp-password", default="ubnt", help="RTSP password presented to Protect")
    parser.add_argument("--renew-before", type=int, default=120, help="Seconds before expiry to renew")
    parser.add_argument("--check-interval", type=int, default=60, help="Loop interval to check stream health")
    parser.add_argument("--poll-events", action="store_true", help="Poll Nest for doorbell events")
    parser.add_argument("--event-interval", type=int, default=30, help="Polling interval for events")
    parser.add_argument("--insecure", action="store_true", help="Allow insecure TLS to Protect")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    device_name = f"enterprises/{args.project_id}/devices/{args.device_id}"
    nest_client = NestStreamClient(args.nest_token, device_name)
    proxy = ProtectCameraProxy(
        host=args.protect_host,
        username=args.protect_username,
        password=args.protect_password,
        adopt_token=args.protect_token,
        camera_name=args.camera_name,
        mac=args.camera_mac,
        rtsp_username=args.rtsp_username,
        rtsp_password=args.rtsp_password,
        insecure=args.insecure,
    )

    stop_event = threading.Event()
    event_thread: Optional[threading.Thread] = None
    if args.poll_events:
        event_thread = threading.Thread(
            target=nest_client.poll_events, args=(args.event_interval, stop_event), daemon=True
        )
        event_thread.start()

    try:
        stream = nest_client.request_stream()
        LOG.info("Using %s stream URL: %s (expires %s)", stream.protocol.upper(), stream.url, stream.expires_at)
        proxy.start(stream.url, stream.protocol)

        while not stop_event.is_set():
            stream = nest_client.ensure_stream_active(renew_margin=args.renew_before)
            LOG.debug("Stream expires at %s", stream.expires_at)
            # If the URL changed (e.g., after regeneration), restart the proxy with the new URL.
            if proxy.process is None or proxy.process.poll() is not None:
                LOG.warning("Proxy stopped; restarting")
                proxy.start(stream.url, stream.protocol)
            elif stream.url != proxy.current_stream_url:
                LOG.info("Stream URL changed; restarting proxy")
                proxy.start(stream.url, stream.protocol)
            stop_event.wait(args.check_interval)
    except KeyboardInterrupt:
        LOG.info("Interrupted, shutting down")
    finally:
        stop_event.set()
        if event_thread:
            event_thread.join(timeout=5)
        proxy.stop()


if __name__ == "__main__":
    main()
