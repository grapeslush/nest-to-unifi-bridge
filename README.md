# Nest to UniFi Protect Bridge

Python helper script that requests a Google Nest Doorbell live stream and feeds it into UniFi Protect (UDM Pro) using [unifi-cam-proxy](https://github.com/unifi-camera/unifi-cam-proxy). The script renews expiring Nest RTSP streams, restarts the proxy when needed, and can optionally poll for doorbell events.

## What this bridge does
- Requests a Nest live stream via the [Smart Device Management API](https://developers.google.com/nest/device-access/api/controls).
- Prefers RTSP (`GenerateRtspStream`) and falls back to WebRTC when RTSP is unavailable.
- Keeps stream URLs valid by extending the RTSP token or regenerating the stream.
- Restarts `unifi-cam-proxy` automatically when the stream URL or process changes.
- Optionally polls the SDM API for events (motion/doorbell) for basic observability.

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Create a Device Access project and obtain:
   - An OAuth access token with the `sdm.service` scope (Google Auth Platform or OAuth 2.0 Playground).
   - The Device Access project ID.
   - The doorbell device ID (combine as `enterprises/<PROJECT_ID>/devices/<DEVICE_ID>`).
3. Ensure your UDM Pro/Protect controller is reachable and note either admin credentials or an adoption token.
4. Pick a unique MAC address for the virtual camera (e.g., `python -c "import uuid;print(':'.join([uuid.uuid4().hex[i:i+2] for i in range(0,12,2)]))"`).

## Obtain Google Device Access credentials (current flow)
These steps align with the latest Google Device Access and OAuth requirements.

1. **Join the Device Access program**
   - Sign in with the Google account linked to your Nest hardware.
   - Go to [Google Device Access](https://developers.google.com/nest/device-access) and click **Get Started**.
   - Accept the terms and pay the one-time $5 registration fee. Without this, API calls will be rejected.

2. **Create a Device Access project**
   - In the Device Access console, select **Create project** and assign a name (e.g., `Nest Bridge`).
   - After creation, copy the numeric **Project ID**. The script references devices as `enterprises/<PROJECT_ID>/devices/<DEVICE_ID>`.

3. **Create OAuth credentials in Google Cloud**
   - Open the [Google Cloud console](https://console.cloud.google.com/), choose (or create) a project, and configure the OAuth consent screen. For personal use, **External** is acceptable; publish the app after validation.
   - Navigate to **APIs & Services → Credentials** and create an **OAuth client ID** with **Desktop app** as the application type.
   - Download the JSON containing `client_id` and `client_secret` for the client you just created.

4. **Register the OAuth client with Device Access and run the consent flow**
   - In the Device Access console, open **Projects → OAuth** and upload the OAuth client JSON from the Google Cloud console.
   - Start an OAuth flow using the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground) or another OAuth client that supports Google Auth Platform:
     1. Click the gear icon, enable **Use your own OAuth credentials**, then paste `client_id` and `client_secret`.
     2. In Step 1, add the scope `https://www.googleapis.com/auth/sdm.service` and click **Authorize APIs**.
     3. Complete the Google sign-in with the account that owns the Nest device and approve the consent screen.
     4. In Step 2, click **Exchange authorization code for tokens** to receive an **access token** and **refresh token**.
   - Supply the access token as `--nest-token`. Tokens expire after an hour; use the refresh token to obtain new access tokens when needed.

5. **Find your device ID**
   - In the Device Access console, open **Devices**. The **Name** column shows the full resource string `enterprises/<PROJECT_ID>/devices/<DEVICE_ID>`.
   - Alternatively, list devices with the SDM API:
     ```bash
     curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
       "https://smartdevicemanagement.googleapis.com/v1/enterprises/YOUR_PROJECT_ID/devices"
     ```
     Use only the `<DEVICE_ID>` portion when invoking the script.

6. **Link your Google account (if prompted)**
   - If the Devices list is empty, click **Link** in the Device Access console to connect your Google account. Approve access for the Nest device, then refresh the Devices page.

## Usage
Request a stream and register the virtual camera:
```bash
python nest_to_unifi_bridge.py \
  --nest-token YOUR_ACCESS_TOKEN \
  --project-id YOUR_PROJECT_ID \
  --device-id YOUR_DEVICE_ID \
  --protect-host 192.0.2.10 \
  --protect-username ubnt --protect-password ubnt \
  --camera-name "Nest Doorbell" \
  --camera-mac 01:23:45:67:89:ab
```

### Helpful flags
- `--protect-token` – adoption token instead of username/password.
- `--renew-before` – seconds before expiration to refresh the Nest stream (default 120).
- `--poll-events` – poll the SDM API for motion/doorbell events and log them.
- `--event-interval` – polling cadence for events (default 30s).
- `--check-interval` – how often to verify the stream/proxy is alive.
- `--rtsp-username` / `--rtsp-password` – credentials presented to Protect (defaults `ubnt`/`ubnt`).
- `--insecure` – allow self-signed TLS when connecting to Protect.
- `--log-level DEBUG` – verbose logging.

### Behavior
- Prefers RTSP (`GenerateRtspStream`); falls back to WebRTC if necessary (requires a `unifi-cam-proxy` build with WebRTC support).
- Automatically extends RTSP streams (or requests a fresh one) before expiry.
- Restarts `unifi-cam-proxy` if the process exits or the stream URL changes.
- Event polling is best-effort; for production alerts, integrate with Google Pub/Sub.

## Troubleshooting
- Ensure the access token is valid and includes the `sdm.service` scope; regenerate it with the refresh token if it is expired.
- The adoption token or credentials must match the Protect controller you target.
- If Protect rejects the camera, try a different MAC address and camera name.
- For WebRTC fallback, confirm your `unifi-cam-proxy` build includes the `webrtc` subcommand.
