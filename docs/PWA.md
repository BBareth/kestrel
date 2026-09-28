# Progressive Web App (iPhone / iPad / desktop)

Kestrel is an installable PWA: web app manifest (`standalone`, start URL `/dashboard`, theme
`#0a0d11`), 192/512 px + maskable icons, a 180 px Apple touch icon, iOS splash screens for current
iPhones, safe-area aware layout (notch / home indicator), bottom tab bar on phones, and a service worker
that caches the app shell so the app opens offline (showing "Live connection lost"); API data is never
cached — prices, positions and orders are always live.

## Why HTTPS with a local CA

Service workers and Web Push only work in a secure context. On the LAN, Caddy issues certificates from
its **own internal CA** for `192.168.1.178`. The iPhone must trust that CA once.

## Install on iPhone (iOS 16.4 or newer)

The iPhone must be on the home Wi-Fi (or connected to the WireGuard VPN).

1. **Download the CA:** in Safari open `http://192.168.1.178:8089/kestrel-ca.crt` → *Allow* →
   "Profile Downloaded".
2. **Install it:** Settings → *Profile Downloaded* (top of Settings) → Install → enter passcode → Install.
3. **Trust it:** Settings → General → About → *Certificate Trust Settings* → switch on
   **Caddy Local Authority – 2026 ECC Root** → Continue.
4. Open **https://192.168.1.178:8443** in Safari (no warning should appear).
5. Tap **Share** (square with arrow) → **Add to Home Screen** → name "Kestrel" → Add.
6. Open **Kestrel from the Home Screen icon** (it launches full-screen with its splash screen), sign in.
7. Enable notifications: **More → Notifications → Enable notifications** → Allow → *Send test notification*.

To stay signed in, the session lasts 14 days (3 days idle). Sensitive actions ask for your password
again; the kill switch never does.

### Updating the app

The service worker updates itself: after a deploy, close and reopen the app (swipe it away) once.

### Uninstall

Long-press the icon → Remove App. Remove the CA via Settings → General → VPN & Device Management → the
Caddy profile → Remove Profile.

## Alternatives to the local CA (more secure, need a decision)

Trusting a private root CA means the phone accepts any certificate that CA signs (see SECURITY.md).
Options with a publicly trusted certificate — all keep the app off the public internet:

1. **Real hostname + Let's Encrypt DNS-01**: a DNS record such as `kestrel.barex.ch → 192.168.1.178`
   (private IP) and a Caddy build with the Cloudflare DNS module and a scoped DNS-edit token. No CA
   profile on the phone.
2. **Tailscale** with MagicDNS/HTTPS certificates (`*.ts.net`).
3. **Cloudflare Tunnel + Access** (like openGym) — makes it reachable from anywhere but publishes a
   trading control panel on the internet; not recommended, and it would need the Swiss legal pages.

## Desktop

Chrome/Edge/Safari on the LAN: open the URL (trust the CA in the OS keychain or accept the warning),
optionally *Install app* from the address bar.
