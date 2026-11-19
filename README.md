# Ferry

A small self-hosted web app for moving files between your machines. It runs in Docker on a NAS. You open it in a browser on your computer or phone and get two panes side by side, each pointed at a connection: the NAS itself, a Mac or Linux box over SFTP, a Windows share over SMB, an FTP server, or Dropbox. Pick files on one side, copy or move them to the other.

Built for reaching home over Tailscale, including from a phone.

https://github.com/user-attachments/assets/e3d3baad-05de-4881-8d77-faa0614a532b

The video is a screen recording of a demo copy. Photos by @lovepubliclands.

## What it does

- Browse, rename, make folders, upload from the device you're on, download files or whole folders as a zip
- Copy and move between any two connections, with a queue you can pause, resume, cancel and retry
- Copies start right away while folders are still being counted
- Rerunning a transfer only fills in what's missing: a file already there with the same size and date is skipped
- Files are written under a hidden name and renamed when complete, so a dropped connection never leaves half a file behind
- When a machine drops off (a laptop going to sleep) the transfer waits and carries on when it's back. Big files continue where they stopped
- Transfers survive a restart of Ferry
- Keeps modified dates and file permissions
- A move removes each original only after its copy is confirmed
- Photo grid with thumbnails, including HEIC and camera RAW
- iPhone Live Photos (a HEIC plus its MOV) show and travel as one item
- Plays video and audio. Subtitles next to a video (`.srt`, `.vtt`, `.ass`) load automatically, with a timing nudge
- Opens ZIP, RAR (including old solid, encrypted and split ones), 7z and tar archives and extracts them to either side
- Phone layout, plus Share to Ferry from the Android share menu once installed
- Deleting moves things to a trash folder you choose per connection

## Run it

```
git clone https://github.com/GhostInTheBus/ferry.git
cd ferry
cp .env.example .env      # set FERRY_PASSWORD, PUID, PGID
docker compose up -d --build
```

Ferry listens on port 8420. By default it binds to `127.0.0.1` only, so nothing on your home network can reach it. Put Tailscale in front of it:

```
tailscale serve --bg --https=8420 http://127.0.0.1:8420
```

and open `https://<your-nas>.<your-tailnet>.ts.net:8420`. Over https the phone can install it as an app.

The folder in `FERRY_FILES` (default `/volume1`) shows up as the first connection.

## Adding a Mac

1. System Settings, General, Sharing, turn on Remote Login. Click the info button and tick "Allow full disk access for remote users", otherwise Documents, Desktop and Downloads are blocked.
2. In Ferry open Connections, add "Mac or Linux (SFTP)" and copy the line shown under Ferry's key into `~/.ssh/authorized_keys` on the Mac. The `restrict,command=...` part means that key can only transfer files, it can't open a shell. You can also add `from="<nas tailscale ip>",` at the front so it only works from the NAS.
3. Host is the Mac's Tailscale address, start folder blank for the home folder, trash `/.Trash` so deletes land in the Mac's Trash.

## Synology and Tailscale

Synology's Tailscale package runs in userspace mode, so programs on the NAS, Docker containers included, can't open normal connections to other tailnet machines. Ferry gets around this by mounting the package's `tailscale` binary and socket and connecting through `tailscale nc`. It does this on its own for `100.x` and `.ts.net` addresses (Route: Automatic in the connection form). The two volume lines in `docker-compose.yml` set this up. Remove them if your NAS isn't a Synology or doesn't run Tailscale.

## Deleting

Nothing is gone for good unless you delete it from inside the trash. Each connection has a trash setting:

- `@synology` moves things into the shared folder's `#recycle`, or a `delete_me` folder if that share has no recycle bin
- a path like `/.Trash` moves things there
- blank deletes for good (Dropbox keeps its own deleted files for 30 days)

## Dropbox

Make an app once at dropbox.com/developers/apps: Scoped access, Full Dropbox, with the files.metadata.read, files.content.read and files.content.write permissions. Paste its App key into Ferry, sign in, and paste back the code Dropbox shows you. No app secret is stored.

## NFS

Mount the NFS export on the NAS (Synology: File Station, Tools, Mount Remote Folder) and it shows up under the NAS connection like any other folder.

## Security

- Every request needs a signed in session. The password is stored as a scrypt hash, repeated wrong passwords lock out for a few minutes, and changing it signs out every other device.
- Saved connection passwords and tokens are encrypted on disk.
- Requests that change anything need a custom header, so other websites can't trigger them through your browser.
- Ferry makes its own SSH key the first time it needs one, and it never leaves the NAS.
- Don't expose it to the open internet. Keep it behind Tailscale, a VPN, or a reverse proxy with https.

## Tests

The tests run Ferry against local SFTP and FTP servers (and Samba if you have it):

```
pip install -r requirements.txt httpx
python3 tests/make_fixtures.py /tmp/ferrytest
FERRY_DATA=/tmp/ferrytest/data FERRY_LOCAL_ROOT=/tmp/ferrytest/nas FERRY_PASSWORD=testpass123 \
  uvicorn ferry.main:app --port 8420 &
python3 tests/servers.py /tmp/ferrytest &
python3 tests/e2e.py /tmp/ferrytest
python3 tests/e2e_v02.py /tmp/ferrytest
```

## Known gaps

- Video plays only in formats the browser handles. No transcoding.
- Subtitles inside MKV files aren't read, only separate subtitle files.
- Archives that aren't on the NAS download in full before you can browse them.

## License

MIT. Fonts: Barlow by Jeremy Tribby, SIL Open Font License (`web/fonts/OFL.txt`).
