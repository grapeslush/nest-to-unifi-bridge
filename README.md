# Nest to UniFi Protect Bridge

Python helper script that requests a Google Nest Doorbell live stream and feeds it into UniFi Protect (UDM Pro) using [unifi-cam-proxy](https://github.com/unifi-camera/unifi-cam-proxy). The script renews expiring Nest RTSP streams, restarts the proxy when needed, and can optionally poll for doorbell events.

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Create a Device Access project and obtain:
   - An OAuth access token with SDM scope (generated via the Google Auth Platform)
   - The project ID
   - The doorbell device ID (combine as `enterprises/<PROJECT_ID>/devices/<DEVICE_ID>`)
3. Ensure your UDM Pro/Protect controller is reachable and note either admin credentials or an adoption token.
4. Pick a unique MAC address for the virtual camera (e.g., `python -c "import uuid;print(':'.join([uuid.uuid4().hex[i:i+2] for i in range(0,12,2)]))"`).

## Obtaining Google Device Access credentials
These steps walk through every Google requirement for a new user. You only need to complete them once.

1. **Join the Device Access program**
   - Sign in with the Google account linked to your Nest hardware.
   - Go to [Google Device Access](https://developers.google.com/nest/device-access) and click **Get Started**.
   - Accept the terms and pay the one-time $5 registration fee. Without this, API calls will be rejected.

2. **Create a Device Access project**
   - In the Device Access console, click **Create project**. Give it a name (e.g., `Nest Bridge`).
   - After creation, copy the numeric **Project ID**. The script uses this in the format `enterprises/<PROJECT_ID>/…`.

3. **Create OAuth credentials in the Google Cloud console**
   - Open the [Google Cloud console](https://console.cloud.google.com/) and create a project (or reuse an existing one).
   - Configure the OAuth consent screen (External is fine for personal use) and publish the app.
   - Go to **APIs & Services → Credentials** and create an **OAuth client ID** with **Desktop** as the application type.
   - Download the JSON that contains `client_id` and `client_secret`.

4. **Register the OAuth client with Device Access and run the consent flow**
   - In the Device Access console, go to **Projects → OAuth** and upload the JSON from the Google Cloud console.
   - Start an OAuth flow using the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground):
     1. Click the gear icon and check **Use your own OAuth credentials**, then paste the `client_id` and `client_secret`.
     2. In Step 1, add the scope `https://www.googleapis.com/auth/sdm.service` and click **Authorize APIs**.
     3. Complete the Google sign-in with the account that owns the Nest device and approve the request.
     4. In Step 2, click **Exchange authorization code for tokens** to receive an access token and refresh token.
   - The access token (bearer token) is the value you pass to `--nest-token`. Tokens expire after an hour; use the refresh
     token to obtain new access tokens as needed.

5. **Find your device ID**
   - In the Device Access console, open **Devices**. You should see your Nest Doorbell listed. The **Name** column shows the
     full resource string `enterprises/<PROJECT_ID>/devices/<DEVICE_ID>`.
   - If you prefer an API call, use the access token to list devices:
     ```bash
     curl -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
       "https://smartdevicemanagement.googleapis.com/v1/enterprises/YOUR_PROJECT_ID/devices"
     ```
     The response includes `name` fields containing the device ID. Provide just the `<DEVICE_ID>` portion to the script.

6. **Link your Google account (if prompted)**
   - If the Devices list is empty, click **Link** in the console to connect your Google account to the project. Approve access
     for the Nest device, then refresh the Devices page.

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
- `--check-interval` – how often to verify the stream/proxy is alive.
- `--log-level DEBUG` – verbose logging.

### Behavior
- Prefers RTSP (`GenerateRtspStream`); falls back to WebRTC if necessary.
- WebRTC fallback invokes the `webrtc` subcommand in `unifi-cam-proxy` (requires a build that supports it) and logs the SDP answer.
- Automatically extends RTSP streams (or requests a fresh one) before expiry.
- Restarts `unifi-cam-proxy` if the process exits or the stream URL changes.
- Event polling is best-effort; for production alerts, integrate with Google Pub/Sub.

## Troubleshooting
- Ensure the access token is valid and has the `sdm.service` scope.
- The adoption token must match the Protect controller you target.
- If Protect rejects the camera, try a different MAC address and name.
