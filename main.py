"""Presto now-playing display for a Mozaiklabs Tune server.

Shows album art + title/artist while a Tune zone is playing, an analog clock
while idle, and recovers on its own from WiFi / router outages (e.g. a nightly
reboot).

secrets.py needs:
    TUNE_HOST       e.g. "192.168.1.18"            (required)
    TUNE_PORT       default 8888
    TUNE_ZONE       zone id or name; default = first playing zone
    TUNE_API_KEY    only if authentication is enabled on the server
    TIMEZONE_OFFSET standard-time offset in hours (Pacific: -8)
plus whatever Presto.connect() already uses for WiFi.
"""
import binascii
import gc
import json
import os
import select
import socket
import sys

import jpegdec
import machine
import network
import ntptime
import pngdec
import secrets
import urequests as requests
import utime
from picovector import ANTIALIAS_FAST, PicoVector, Polygon, Transform
from presto import Presto

# ---------------------------------------------------------------- config ---
TUNE_HOST = secrets.TUNE_HOST
TUNE_PORT = getattr(secrets, "TUNE_PORT", 8888)
TUNE_ZONE = getattr(secrets, "TUNE_ZONE", None)
TUNE_API_KEY = getattr(secrets, "TUNE_API_KEY", None)

# secrets.TIMEZONE_OFFSET is the STANDARD-time offset in hours (Pacific: -8).
# With USE_US_DST the script adds an hour during US daylight saving time.
TZ_OFFSET = secrets.TIMEZONE_OFFSET * 3600
USE_US_DST = True   # set False outside the US/Canada

HTTP_TIMEOUT = 15          # seconds, art download
API_TIMEOUT = 4            # seconds, Tune requests (a LAN server answers in <1s)
POLL_PLAYING = 1.0         # seconds between polls while playing
POLL_IDLE = 1              # seconds between polls while idle
IDLE_AFTER_POLLS = 3       # not playing for this many polls -> show the clock
ACTIVE_STATES = ("playing",)   # add "paused" to keep the art up while paused
FAIL_LIMIT = 5             # consecutive failed polls before recovery kicks in
RECONNECT_EVERY = 30       # seconds between WiFi reconnect attempts
RESET_AFTER_WIFI_DOWN = 600  # hard reset if WiFi is down this long (seconds)
NTP_RESYNC_EVERY = 6 * 3600  # seconds

# Push updates: Tune sends playback events over a WebSocket, so the Presto only
# asks the server for status when something changed instead of every second.
WS_ENABLED = True          # False = plain polling (POLL_PLAYING / POLL_IDLE)
WS_PATH = "/ws"
# Some servers want a subscribe message after connecting. If the log says the
# socket is silent, put the JSON text here (or in secrets.TUNE_WS_SUBSCRIBE).
WS_SUBSCRIBE = getattr(secrets, "TUNE_WS_SUBSCRIBE", None)
# Only these kinds of playback events make us re-read the zone. Anything else
# (position ticks, audio levels, ...) can arrive many times a second and is ignored.
WS_TRIGGERS = (b"started", b"track_changed", b"stopped", b"paused", b"resumed",
               b"state", b"finished", b"ended", b"seek")
WS_MIN_GAP_MS = 1000       # never fetch status more often than this because of events
WS_SAFETY_POLL = 30        # seconds between status checks while events flow (safety net)
WS_RETRY = 30              # seconds between WebSocket reconnect attempts
WS_PING_MS = 15000         # send a ping this often
WS_DEAD_MS = 45000         # nothing received for this long -> reconnect
WS_SILENT_AFTER = 20       # playing this long with no frames at all -> fall back to polling
WS_MAX_FRAME = 8192        # bigger frames are skipped, not buffered

ART_SIZE = 480             # art fills the whole screen
ART_QUERY = ""             # appended to Tune artwork URLs, e.g. "?size=480" if
                           # your server version supports resizing (unverified)
ART_MAX_BYTES = 900000     # refuse to download art bigger than this
TITLE_XY = (30, 445)       # text sits over the art
ARTIST_XY = (30, 465)
LIGHT_BG_LUMA = 140        # art brighter than this (0-255) -> dark text

AUTO_RESTART_ON_CRASH = True   # reboot instead of leaving a frozen screen
LOG_FILE = "log.txt"          # read it via Thonny after a stall
LOG_MAX_BYTES = 20000


def log(msg, exc=None):
    """Print and append to log.txt (errors only: keeps flash wear low)."""
    print(msg)
    if exc is not None:
        sys.print_exception(exc)
    try:
        try:
            if os.stat(LOG_FILE)[6] > LOG_MAX_BYTES:
                os.remove(LOG_FILE)
        except OSError:
            pass
        with open(LOG_FILE, "a") as f:
            f.write("[up {}s] {}\n".format(utime.ticks_ms() // 1000, msg))
            if exc is not None:
                sys.print_exception(exc, f)
    except Exception:
        pass


ZONES_PATH = "/api/v1/zones"
ARTWORK_PATH = "/api/v1/library/artwork/"

# --------------------------------------------------------------- display ---
presto = Presto(ambient_light=False, full_res=True)
presto.set_backlight(0.2)
display = presto.display
WIDTH, HEIGHT = display.get_bounds()

BLACK = display.create_pen(0, 0, 0)
WHITE = display.create_pen(255, 255, 255)

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

vector = PicoVector(display)
vector.set_antialiasing(ANTIALIAS_FAST)
vector.set_font("Roboto-Medium.af", 14)
vector.set_font_letter_spacing(100)
vector.set_font_word_spacing(100)
vector.set_transform(Transform())

jpd = jpegdec.JPEG(display)
pnd = pngdec.PNG(display)

# The bundled font has no accented glyphs, so fold them to plain ASCII.
_SRC = "áàäâãåéèëêíìïîóòöôõúùüûñçÁÀÄÂÃÅÉÈËÊÍÌÏÎÓÒÖÔÕÚÙÜÛÑÇ"
_DST = "aaaaaaeeeeiiiiooooouuuuncAAAAAAEEEEIIIIOOOOOUUUUNC"
CHAR_MAP = dict(zip(_SRC, _DST))


def to_ascii(text):
    return "".join(CHAR_MAP.get(c, c) for c in text)


def clear():
    display.set_pen(BLACK)
    display.clear()


# ------------------------------------------------------------------ wifi ---
wlan = network.WLAN(network.STA_IF)
time_synced = False
last_ntp = 0
last_ntp_try = -60000


def sync_time():
    """Set the RTC from NTP (UTC). Returns True on success."""
    global time_synced, last_ntp, last_ntp_try
    last_ntp_try = utime.ticks_ms()
    for _ in range(5):
        try:
            ntptime.settime()
            time_synced = True
            last_ntp = utime.time()
            return True
        except Exception as e:
            print("NTP error:", e)
            utime.sleep(2)
    return False


def connect_wifi(force=False):
    """Connect (or reconnect) to WiFi. Returns True if connected."""
    if wlan.isconnected() and not force:
        if not time_synced:  # e.g. WiFi survived a soft reset: still need NTP
            sync_time()
        return True
    print("Connecting to WiFi")
    try:
        if force:
            wlan.disconnect()
            utime.sleep(1)
        presto.connect()
    except Exception as e:
        print("WiFi error:", e)
    ok = wlan.isconnected()
    if ok and not time_synced:
        sync_time()
    return ok


# ------------------------------------------------------------------ http ---
def http_get(host, port, path, timeout, max_bytes):
    """Plain-HTTP GET. Returns (status, body bytes).

    Tune serves plain HTTP only (no TLS). The request is HTTP/1.0 so the
    server never answers with chunked encoding: we just read until it closes.
    """
    addr = socket.getaddrinfo(host, port)[0][-1]
    s = socket.socket()
    s.settimeout(timeout)
    buf = bytearray()
    try:
        s.connect(addr)
        req = "GET {} HTTP/1.0\r\nHost: {}:{}\r\nAccept: */*\r\n".format(path, host, port)
        if TUNE_API_KEY and host == TUNE_HOST:
            req += "Authorization: ApiKey {}\r\n".format(TUNE_API_KEY)
        s.send((req + "Connection: close\r\n\r\n").encode())
        while True:
            chunk = s.recv(2048)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_bytes:
                raise OSError("response too large")
    finally:
        s.close()

    i = buf.find(b"\r\n\r\n")
    if i < 0 or len(buf) < 12:
        raise OSError("bad response")
    status = int(bytes(buf[9:12]).decode())
    body = bytes(memoryview(buf)[i + 4:])
    return status, body


def tune_get_json(path):
    t0 = utime.ticks_ms()
    status, body = http_get(TUNE_HOST, TUNE_PORT, path, API_TIMEOUT, 65536)
    if status != 200:
        raise OSError("HTTP {} for {}".format(status, path))
    result = json.loads(body.decode("utf-8"))
    print("GET {} (took {}ms)".format(path[-14:], utime.ticks_diff(utime.ticks_ms(), t0)))
    return result


class WebSocket:
    """Tiny RFC 6455 client: plain ws://, text frames in, ping/pong handled."""

    def __init__(self):
        self.sock = None
        self.poller = None
        self.buf = b""
        self.msg = b""
        self.skip = 0
        self.frames = 0          # text messages received on this connection
        self.connected_at = 0
        self.last_rx = 0
        self.last_ping = 0

    def connect(self, host, port, path, api_key=None):
        self.close()
        addr = socket.getaddrinfo(host, port)[0][-1]
        s = socket.socket()
        try:
            s.settimeout(API_TIMEOUT)
            s.connect(addr)
            key = binascii.b2a_base64(os.urandom(16)).strip().decode()
            req = ("GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: {}\r\n"
                   "Sec-WebSocket-Version: 13\r\n").format(path, host, port, key)
            if api_key:
                req += "Authorization: ApiKey {}\r\n".format(api_key)
            s.send((req + "\r\n").encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = s.recv(512)
                if not chunk:
                    raise OSError("closed during handshake")
                data += chunk
                if len(data) > 4096:
                    raise OSError("handshake too long")
            head, _, rest = data.partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n", 1)[0]
            if b" 101" not in status_line:
                raise OSError("handshake refused: " + status_line[:40].decode())
        except BaseException:
            s.close()
            raise
        self.sock = s
        self.poller = select.poll()
        self.poller.register(s, select.POLLIN)
        self.buf = rest          # frames may already follow the handshake
        self.msg = b""
        self.skip = 0
        self.frames = 0
        now = utime.ticks_ms()
        self.connected_at = self.last_rx = self.last_ping = now

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self.poller = None

    def _send(self, opcode, payload=b""):
        n = len(payload)
        mask = os.urandom(4)     # client frames must be masked
        if n < 126:
            hdr = bytes([0x80 | opcode, 0x80 | n])
        else:
            hdr = bytes([0x80 | opcode, 0x80 | 126, n >> 8, n & 255])
        body = bytes(payload[i] ^ mask[i & 3] for i in range(n))
        self.sock.send(hdr + mask + body)

    def send_text(self, text):
        self._send(0x1, text.encode())

    def poll(self, timeout_ms):
        """Wait up to timeout_ms for data. Returns a list of complete text
        messages (bytes). Raises OSError when the connection is gone."""
        if self.sock is None:
            raise OSError("websocket closed")
        now = utime.ticks_ms()
        if utime.ticks_diff(now, self.last_rx) > WS_DEAD_MS:
            raise OSError("websocket silent")
        if utime.ticks_diff(now, self.last_ping) > WS_PING_MS:
            self.last_ping = now
            self._send(0x9)
        if self.poller.poll(timeout_ms):
            data = self.sock.recv(1024)
            if not data:
                raise OSError("websocket closed by server")
            self.buf += data
            self.last_rx = utime.ticks_ms()
        return self._parse()

    def _parse(self):
        out = []
        while True:
            if self.skip:                      # discarding an oversized frame
                n = min(self.skip, len(self.buf))
                self.buf = self.buf[n:]
                self.skip -= n
                if self.skip:
                    break
            b = self.buf
            if len(b) < 2:
                break
            opcode = b[0] & 0x0F
            fin = b[0] & 0x80
            masked = b[1] & 0x80
            n = b[1] & 0x7F
            hdr = 2
            if n == 126:
                if len(b) < 4:
                    break
                n = (b[2] << 8) | b[3]
                hdr = 4
            elif n == 127:
                if len(b) < 10:
                    break
                n = int.from_bytes(b[2:10], "big")
                hdr = 10
            if masked:                         # servers should not mask; cope anyway
                hdr += 4
            if len(b) < hdr:
                break
            if n > WS_MAX_FRAME:
                self.buf = b[hdr:]
                self.skip = n
                self.msg = b""
                continue
            if len(b) < hdr + n:
                break
            payload = b[hdr:hdr + n]
            if masked:
                m = b[hdr - 4:hdr]
                payload = bytes(payload[i] ^ m[i & 3] for i in range(n))
            self.buf = b[hdr + n:]
            if opcode == 0x8:
                raise OSError("websocket closed by server")
            if opcode == 0x9:
                self._send(0xA, payload)
            elif opcode in (0x1, 0x0):         # text / continuation
                self.msg += payload
                if len(self.msg) > WS_MAX_FRAME:
                    self.msg = b""
                elif fin:
                    out.append(self.msg)
                    self.msg = b""
                    self.frames += 1
        return out


ZONE_ID = None   # resolved once (first successful poll), then reused


def resolve_zone():
    """Look up the zone to follow (GET /zones) and remember its id.

    Runs once at startup (and again only if the zone later disappears).
    TUNE_ZONE (id or name) picks it; without it we take the first zone that is
    playing right now, else the first zone in the list.
    """
    global ZONE_ID
    data = tune_get_json(ZONES_PATH)
    zones = data
    if isinstance(data, dict):
        zones = data.get("zones") or data.get("items") or [data]
    if not zones:
        raise OSError("Tune reports no zones")
    chosen = None
    if TUNE_ZONE is not None:
        want = str(TUNE_ZONE).lower()
        for z in zones:
            if str(z.get("id")) == want or str(z.get("name", "")).lower() == want:
                chosen = z
                break
        if chosen is None:
            raise OSError("zone {!r} not found; zones: {}".format(
                TUNE_ZONE, [(z.get("id"), z.get("name")) for z in zones]))
    else:
        for z in zones:
            if z.get("state") in ACTIVE_STATES:
                chosen = z
                break
        if chosen is None:
            chosen = zones[0]
    ZONE_ID = chosen.get("id")
    log("following zone {!r} (id {})".format(chosen.get("name"), ZONE_ID))


def zone_status():
    """Current state of the followed zone: {state, current_track, ...}."""
    global ZONE_ID
    if ZONE_ID is None:
        resolve_zone()
    try:
        return tune_get_json("{}/{}".format(ZONES_PATH, ZONE_ID))
    except OSError as e:
        if "HTTP 404" in str(e):  # zone was removed/renumbered: look it up again
            ZONE_ID = None
        raise


# ------------------------------------------------------------- album art ---
def is_lan(host):
    if host == TUNE_HOST:
        return True
    for prefix in ("192.168.", "10.", "127.", "169.254."):
        if host.startswith(prefix):
            return True
    if host.startswith("172."):
        try:
            return 16 <= int(host.split(".")[1]) <= 31
        except ValueError:
            return False
    return False


def split_url(url):
    """http://host[:port]/path -> (host, port, path)."""
    hostport, _, path = url[7:].partition("/")
    host, _, port = hostport.partition(":")
    return host, int(port) if port else 80, "/" + path


def resolve_art(cover):
    """Turn Tune's current_track.cover_path into a URL we can download.

    cover_path is either a content hash (local library art, served by Tune),
    a path on the Tune server, or a full URL (streaming services, LAN URLs).
    """
    if cover.startswith("http"):
        host = cover.split("/")[2].split(":")[0]
        if is_lan(host):
            return cover
        # fit=cover crops non-square art so it fills the screen exactly
        # output=jpg keeps the download small (PNG covers can be huge)
        return "https://wsrv.nl/?url={}&w={}&h={}&fit=cover&output=jpg&q=85".format(
            cover, ART_SIZE, ART_SIZE)
    if cover.startswith("/"):
        return "http://{}:{}{}".format(TUNE_HOST, TUNE_PORT, cover)
    return "http://{}:{}{}{}{}".format(TUNE_HOST, TUNE_PORT, ARTWORK_PATH, cover, ART_QUERY)


def fetch_art(url):
    """Download album art. Returns the raw image bytes, or None on failure."""
    gc.collect()
    print("Fetching", url)
    t0 = utime.ticks_ms()
    response = None
    try:
        if url.startswith("http://"):  # Tune / LAN: plain HTTP
            host, port, path = split_url(url)
            status, data = http_get(host, port, path, HTTP_TIMEOUT, ART_MAX_BYTES)
            if status != 200:
                print("Art HTTP status:", status)
                return None
        else:  # external art via the wsrv.nl resizer
            response = requests.get(url, timeout=HTTP_TIMEOUT)
            if response.status_code != 200:
                print("Art HTTP status:", response.status_code)
                return None
            data = response.content
        print("art {} bytes in {}ms".format(
            len(data), utime.ticks_diff(utime.ticks_ms(), t0)))
        return data
    except Exception as e:
        print("Art download error:", e)
    finally:
        if response:
            response.close()
    return None


def jpeg_info(data):
    """(width, height, progressive) read from the JPEG header, or None."""
    n = len(data)
    i = 2
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xC0, 0xC1, 0xC2):
            return (data[i + 7] << 8) | data[i + 8], (data[i + 5] << 8) | data[i + 6], m == 0xC2
        if m == 0xD9:
            break
        if m == 0xFF:
            i += 1
        elif m == 0xD8 or m == 0x01 or 0xD0 <= m <= 0xD7:
            i += 2
        else:
            i += 2 + ((data[i + 2] << 8) | data[i + 3])
    return None


SCALES = ((1, jpegdec.JPEG_SCALE_FULL), (2, jpegdec.JPEG_SCALE_HALF),
          (4, jpegdec.JPEG_SCALE_QUARTER), (8, jpegdec.JPEG_SCALE_EIGHTH))


def pick_scale(w, h):
    """jpegdec only scales by 1/2, 1/4, 1/8: pick the one nearest the screen."""
    big = max(w, h)
    return min(SCALES, key=lambda s: abs(big // s[0] - ART_SIZE))


def paint_art(data):
    """Clear the screen and draw the (cached) art centred. True on success."""
    clear()
    if not data:
        return False
    try:
        if data[:4] == b"\x89PNG":
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            pnd.open_RAM(memoryview(data))
            pnd.decode((WIDTH - w) // 2, (HEIGHT - h) // 2)
        else:
            info = jpeg_info(data)
            if info is None:
                raise ValueError("not a JPEG")
            w, h, progressive = info
            if progressive:
                raise ValueError("progressive JPEG not supported by jpegdec")
            div, scale = pick_scale(w, h)
            jpd.open_RAM(memoryview(data))
            jpd.decode((WIDTH - w // div) // 2, (HEIGHT - h // div) // 2, scale)
        return True
    except Exception as e:
        print("Art decode error:", e)
        return False
    finally:
        gc.collect()


# ------------------------------------------------- text over the artwork ---
# To pick a readable text colour we measure how bright the art is behind the
# text by reading the framebuffer directly (RGB565). Which byte order the
# buffer uses is detected once at startup by drawing a pure red pixel.
STRIDE = WIDTH * 2


def _init_framebuffer():
    try:
        fb = memoryview(display)
        display.set_pen(display.create_pen(255, 0, 0))
        display.pixel(0, 0)
        if fb[0] == 0xF8 and fb[1] == 0x00:
            return fb, True
        if fb[0] == 0x00 and fb[1] == 0xF8:
            return fb, False
        print("Unrecognised framebuffer format")
    except Exception as e:
        print("No framebuffer access:", e)
    return None, False


FB, FB_BIG_ENDIAN = _init_framebuffer()
print("Framebuffer luminance sampling:", "on" if FB else "off (white text)")


def region_luma(x, y, w, h):
    """Average brightness (0-255) of a screen region, or None if unavailable."""
    if FB is None:
        return None
    total = count = 0
    for yy in range(max(0, y), min(HEIGHT, y + h), 3):
        row = yy * STRIDE
        for xx in range(max(0, x), min(WIDTH, x + w), 3):
            i = row + xx * 2
            if FB_BIG_ENDIAN:
                v = (FB[i] << 8) | FB[i + 1]
            else:
                v = (FB[i + 1] << 8) | FB[i]
            # 5/6/5-bit channels with approximate Rec.601 weights
            total += (((v >> 11) & 31) * 616 + ((v >> 5) & 63) * 600 + (v & 31) * 232) >> 8
            count += 1
    return total // count if count else None


def text_width(text):
    """Width of `text` at the current font size (call set_font_size first)."""
    try:
        return int(vector.measure_text(text)[2])
    except Exception:
        return WIDTH - 2 * TITLE_XY[0]


def pick_colors(text, x, y, size):
    """Return (text pen, outline pen) that contrast with the art behind it."""
    w = min(text_width(text), WIDTH - x)
    luma = region_luma(x - 4, y - size, w + 8, size + size // 3)
    if luma is not None and luma >= LIGHT_BG_LUMA:
        return BLACK, WHITE
    return WHITE, BLACK


# ---------------------------------------------------------------- screens ---
OUTLINE = ((-2, 0), (2, 0), (0, -2), (0, 2), (-2, -2), (2, 2), (-2, 2), (2, -2))


def draw_track_text(title, artist):
    """Draw title + artist over the art in a colour that contrasts with it."""
    lines = ((to_ascii(title), TITLE_XY, 30), (to_ascii(artist), ARTIST_XY, 20))

    # Measure both lines first: drawing one would pollute the other's sample.
    plan = []
    for text, (x, y), size in lines:
        vector.set_font_size(size)
        fg, bg = pick_colors(text, x, y, size)
        plan.append((text, x, y, size, fg, bg))

    for text, x, y, size, fg, bg in plan:
        vector.set_font_size(size)
        # A thin outline in the opposite colour keeps text legible even when
        # the art behind it is busy or half light, half dark.
        display.set_pen(bg)
        for dx, dy in OUTLINE:
            vector.text(text, x + dx, y + dy)
        display.set_pen(fg)
        vector.text(text, x, y)
    presto.update()


def _nth_sunday(year, month, nth):
    """Day of month of the nth Sunday (nth = 1 or 2)."""
    weekday_of_1st = utime.localtime(utime.mktime((year, month, 1, 0, 0, 0, 0, 0)))[6]
    return 1 + (6 - weekday_of_1st) % 7 + 7 * (nth - 1)


def us_dst_active(utc):
    """True during US DST: 2nd Sunday of March 2:00 -> 1st Sunday of Nov 2:00 (local)."""
    year = utime.localtime(utc)[0]
    start = utime.mktime((year, 3, _nth_sunday(year, 3, 2), 2, 0, 0, 0, 0)) - TZ_OFFSET
    # DST ends at 2:00 daylight time, which is one hour earlier in UTC terms
    end = utime.mktime((year, 11, _nth_sunday(year, 11, 1), 2, 0, 0, 0, 0)) - TZ_OFFSET - 3600
    return start <= utc < end


def local_time():
    utc = utime.time()
    offset = TZ_OFFSET + (3600 if USE_US_DST and us_dst_active(utc) else 0)
    return utime.localtime(utc + offset)


# Analog clock face. Angles are degrees clockwise from 12 o'clock.
CLOCK_CX, CLOCK_CY, CLOCK_R = WIDTH // 2, HEIGHT // 2, 228
DATE_FONT = 46                # date text size (two lines: day, then date)
SECOND_HAND = True            # False = redraw once a minute instead of every second
RED = display.create_pen(255, 70, 60)
GREY = display.create_pen(150, 150, 150)


def _bar(pen, width, top, length, angle):
    """A filled bar, `length` long, starting `top` px above the centre and
    rotated about the centre by `angle`. Used for ticks and hands."""
    t = Transform()
    t.rotate(angle, (CLOCK_CX, CLOCK_CY))
    vector.set_transform(t)
    display.set_pen(pen)
    p = Polygon()
    p.rectangle(CLOCK_CX - width / 2, CLOCK_CY - top, width, length)
    vector.draw(p)


def draw_clock(tm):
    t0 = utime.ticks_ms()
    clear()
    hour, minute, second = tm[3] % 12, tm[4], tm[5]

    # Tick marks: 60 small, 12 big
    for i in range(60):
        if i % 5 == 0:
            _bar(WHITE, 7, CLOCK_R, 30, i * 6)
        else:
            _bar(GREY, 2, CLOCK_R, 12, i * 6)

    # Date, inside the lower half of the face
    vector.set_transform(Transform())
    display.set_pen(WHITE)
    vector.set_font_size(DATE_FONT)
    for line, base in ((DAYS[tm[6]], CLOCK_CY + 108),
                       ("{:02d} {}".format(tm[2], MONTHS[tm[1] - 1]), CLOCK_CY + 108 + DATE_FONT + 8)):
        vector.text(line, CLOCK_CX - text_width(line) // 2, base)

    # Hands (hour, minute, then the thin red second hand on top)
    _bar(WHITE, 14, 120, 120 + 20, hour * 30 + minute * 0.5)
    _bar(WHITE, 9, 185, 185 + 25, minute * 6 + second * 0.1)
    if SECOND_HAND:
        _bar(RED, 3, 205, 205 + 40, second * 6)

    # Centre cap
    vector.set_transform(Transform())
    display.set_pen(RED if SECOND_HAND else WHITE)
    cap = Polygon()
    cap.circle(CLOCK_CX, CLOCK_CY, 11)
    vector.draw(cap)

    vector.set_transform(Transform())
    presto.update()
    print("clock drawn in {}ms".format(utime.ticks_diff(utime.ticks_ms(), t0)))


# ------------------------------------------------------------ main logic ---
class NowPlaying:
    def __init__(self):
        self.reset_track()
        self.stalled = 0
        self.idle = True
        self.fails = 0
        self.clock_minute = None
        self.last_reconnect = 0
        self.wifi_down_since = None
        self.logged_first_poll = False
        self.logged_first_draw = False
        # push events
        self.next_fetch = utime.ticks_ms()   # when to ask the server again
        self.ws = None
        self.ws_retry_at = utime.ticks_ms()
        self.ws_silent = False
        self.ws_frames_logged = 0
        self.ws_down_logged = False
        self.ws_seen = []                    # distinct event names logged so far
        self.last_fetch = utime.ticks_ms()

    def reset_track(self):
        self.track_key = None
        self.art_cover = ""
        self.art_data = None  # raw image bytes, kept so we can repaint

    def go_idle(self):
        if not self.idle:
            self.idle = True
            self.clock_minute = None  # force a redraw
        self.reset_track()
        self.stalled = 0

    def refresh_track(self, track, key):
        """Download art for a new track and redraw."""
        cover = track.get("cover_path") or ""
        if cover and cover != self.art_cover:
            self.art_data = None  # free the old image before downloading
            self.art_data = fetch_art(resolve_art(cover))
            self.art_cover = cover if self.art_data else ""
        elif not cover:
            self.art_data = None
            self.art_cover = ""

        # Repaint the art on every track change: that also erases the
        # previous track's text, which now lives on top of the art.
        paint_art(self.art_data)
        draw_track_text(track.get("title") or "", track.get("artist_name") or "")
        if not self.logged_first_draw:
            self.logged_first_draw = True
            log("startup: first track drawn")
        self.track_key = key  # only remember it once everything worked

    # -- push events -----------------------------------------------------
    def ws_live(self):
        """True while WebSocket events are trusted to tell us about changes."""
        return self.ws is not None and not self.ws_silent

    def drop_ws(self):
        if self.ws is not None:
            self.ws.close()
        self.ws = None
        self.ws_retry_at = utime.ticks_add(utime.ticks_ms(), WS_RETRY * 1000)

    def service_ws(self):
        """Wait ~200ms for events. Returns True if the server should be asked
        for fresh status (an event arrived, or the socket was just (re)made)."""
        if not WS_ENABLED:
            utime.sleep_ms(200)
            return False
        now = utime.ticks_ms()
        if self.ws is None:
            if utime.ticks_diff(now, self.ws_retry_at) >= 0:
                self.ws_retry_at = utime.ticks_add(now, WS_RETRY * 1000)
                ws = WebSocket()
                try:
                    ws.connect(TUNE_HOST, TUNE_PORT, WS_PATH, TUNE_API_KEY)
                    if WS_SUBSCRIBE:
                        ws.send_text(WS_SUBSCRIBE)
                except Exception as e:
                    ws.close()
                    if not self.ws_down_logged:
                        self.ws_down_logged = True
                        log("websocket connect failed, polling instead: {!r}".format(e))
                    return False
                self.ws = ws
                self.ws_silent = False
                if self.ws_down_logged or self.ws_frames_logged == 0:
                    log("websocket connected")
                self.ws_down_logged = False
                return True          # resync after (re)connecting
            utime.sleep_ms(200)
            return False

        try:
            msgs = self.ws.poll(200)
        except Exception as e:
            log("websocket lost: {!r}".format(e))
            self.drop_ws()
            return True
        hit = None
        for m in msgs:
            i = m.find(b"playback.")
            if i < 0:
                continue
            j = m.find(b'"', i)
            name = m[i:j] if j > i else m[i:i + 40]
            if any(t in name for t in WS_TRIGGERS):
                hit = name
            elif name not in self.ws_seen and len(self.ws_seen) < 12:
                # log each ignored event type once, so a missing trigger is easy to spot
                self.ws_seen.append(name)
                log("websocket: ignoring event {!r} (e.g. {!r})".format(name, m[:120]))
        if hit is not None:
            gap_end = utime.ticks_add(self.last_fetch, WS_MIN_GAP_MS)
            if utime.ticks_diff(now, gap_end) >= 0:
                print("fetch: event", hit)
                self.ws_silent = False
                return True
            # too soon after the last fetch: do it when the gap is over
            if utime.ticks_diff(self.next_fetch, gap_end) > 0:
                self.next_fetch = gap_end
        if msgs:
            self.ws_silent = False
        elif (not self.idle and self.ws.frames == 0 and not self.ws_silent and
                utime.ticks_diff(now, self.ws.connected_at) > WS_SILENT_AFTER * 1000):
            self.ws_silent = True
            log("websocket connected but no events while playing: polling "
                "instead (the server may need TUNE_WS_SUBSCRIBE)")
        return False

    def poll(self):
        self.last_fetch = utime.ticks_ms()
        gc.collect()  # keep the heap tidy before the next download
        zone = zone_status()
        if self.fails >= FAIL_LIMIT:
            log("polling recovered after {} failures".format(self.fails))
        self.fails = 0
        self.wifi_down_since = None

        zone = zone or {}
        track = zone.get("current_track") or {}
        if not self.logged_first_poll:
            self.logged_first_poll = True
            # one-off look at the real field names, in case a server version differs
            log("startup: first poll ok, zone {!r} state {!r} track {}".format(
                zone.get("name"), zone.get("state"), str(track)[:300]))

        live = self.ws_live()
        active = zone.get("state") in ACTIVE_STATES and track
        if active:
            self.stalled = 0
            self.idle = False
            key = (track.get("title"), track.get("artist_name"), track.get("cover_path"))
            if key != self.track_key:
                self.refresh_track(track, key)
        else:
            self.stalled += 1
            # events are prompt, so one confirming re-check is enough
            if self.stalled > (1 if live else IDLE_AFTER_POLLS):
                self.go_idle()

        if live:
            # events drive updates; re-check soon only to confirm "not playing"
            delay = 2000 if (not active and not self.idle) else WS_SAFETY_POLL * 1000
        else:
            delay = (POLL_IDLE if self.idle else POLL_PLAYING) * 1000
        self.next_fetch = utime.ticks_add(utime.ticks_ms(), int(delay))

    def recover_network(self):
        """Called while polls keep failing: reconnect WiFi, maybe hard reset."""
        now = utime.time()
        if wlan.isconnected():
            # Associated but the Tune server is unreachable (or the link is
            # stale after the router restarted). Bounce WiFi occasionally.
            if now - self.last_reconnect >= RECONNECT_EVERY:
                self.last_reconnect = now
                self.drop_ws()
                connect_wifi(force=True)
            return

        self.drop_ws()
        if self.wifi_down_since is None:
            self.wifi_down_since = now
        if now - self.last_reconnect >= RECONNECT_EVERY:
            self.last_reconnect = now
            if connect_wifi():
                self.wifi_down_since = None
                return
        if now - self.wifi_down_since > RESET_AFTER_WIFI_DOWN:
            log("WiFi down too long, resetting")
            machine.reset()

    def tick_clock(self):
        tm = local_time()
        key = (tm[4], tm[5]) if SECOND_HAND else tm[4]
        if key != self.clock_minute:
            self.clock_minute = key
            draw_clock(tm)


def monitor_playback():
    np = NowPlaying()
    gc.collect()
    print("Free:", gc.mem_free())

    while True:
        event = False
        if wlan.isconnected():
            try:
                event = np.service_ws()   # also paces the loop (~200ms)
            except Exception as e:
                log("websocket error", e)
                np.drop_ws()
                utime.sleep_ms(200)
        else:
            np.drop_ws()
            utime.sleep_ms(200)

        if event or utime.ticks_diff(utime.ticks_ms(), np.next_fetch) >= 0:
            try:
                np.poll()
            except Exception as e:
                np.fails += 1
                np.next_fetch = utime.ticks_add(utime.ticks_ms(), 1000)
                # log the first failure of a streak (with traceback) and the
                # moment recovery starts, not every retry
                if np.fails == 1 or np.fails == FAIL_LIMIT:
                    log("poll error #{}: {!r}".format(np.fails, e), e)
                if np.fails >= FAIL_LIMIT:
                    np.go_idle()
                    try:
                        np.recover_network()
                    except Exception as e2:
                        log("recover_network error", e2)

        try:
            if wlan.isconnected():
                since = utime.ticks_diff(utime.ticks_ms(), last_ntp_try)
                if not time_synced and since > 60000:
                    sync_time()  # boot-time sync failed: keep retrying
                elif time_synced and utime.time() - last_ntp > NTP_RESYNC_EVERY:
                    sync_time()
            if np.idle:
                np.tick_clock()
        except Exception as e:
            log("clock/ntp error", e)


def main():
    clear()
    presto.update()
    while not connect_wifi():
        utime.sleep(5)
    log("startup: wifi up, ntp synced={}".format(time_synced))
    monitor_playback()


def run():
    log("boot: reset cause {}, free mem {}".format(machine.reset_cause(), gc.mem_free()))
    try:
        main()
        log("main() returned without an error (unexpected)")
    except KeyboardInterrupt:
        raise  # Ctrl-C in Thonny stops the script as usual
    except BaseException as e:
        log("FATAL: {!r}".format(e), e)
    if AUTO_RESTART_ON_CRASH:
        utime.sleep(5)
        machine.reset()


run()


