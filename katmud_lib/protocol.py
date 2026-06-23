"""katmud_lib.protocol - telnet filter, ANSI parser, MIP extractor,
and the network thread. Ported from pymud v6 unchanged in behavior;
only packaging and naming differ.
"""

import queue
import re
import socket
import threading

# ==========================================================================
# Telnet layer
# ==========================================================================
IAC, DONT, DO, WONT, WILL, SB, SE, GA, EOR = \
    255, 254, 253, 252, 251, 250, 240, 249, 239
OPT_ECHO, OPT_EOR = 1, 25


class TelnetFilter:
    def __init__(self):
        self.state = "data"
        self.pending_cmd = None
        self.sb_buffer = bytearray()

    def feed(self, data: bytes):
        out, responses, events = bytearray(), bytearray(), []
        for byte in data:
            if self.state == "data":
                if byte == IAC:
                    self.state = "iac"
                else:
                    out.append(byte)
            elif self.state == "iac":
                if byte == IAC:
                    out.append(IAC); self.state = "data"
                elif byte in (DO, DONT, WILL, WONT):
                    self.pending_cmd = byte; self.state = "option"
                elif byte == SB:
                    self.sb_buffer = bytearray(); self.state = "sb"
                elif byte in (GA, EOR):
                    events.append("GA"); self.state = "data"
                else:
                    self.state = "data"
            elif self.state == "option":
                cmd = self.pending_cmd
                if cmd == WILL:
                    if byte == OPT_ECHO:
                        responses += bytes([IAC, DO, OPT_ECHO])
                        events.append("ECHO_OFF")
                    elif byte == OPT_EOR:
                        responses += bytes([IAC, DO, OPT_EOR])
                    else:
                        responses += bytes([IAC, DONT, byte])
                elif cmd == WONT:
                    if byte == OPT_ECHO:
                        responses += bytes([IAC, DONT, OPT_ECHO])
                        events.append("ECHO_ON")
                    else:
                        responses += bytes([IAC, DONT, byte])
                else:
                    responses += bytes([IAC, WONT, byte])
                self.state = "data"
            elif self.state == "sb":
                if byte == IAC:
                    self.state = "sb_iac"
                else:
                    self.sb_buffer.append(byte)
            elif self.state == "sb_iac":
                if byte == SE:
                    self.state = "data"
                else:
                    self.sb_buffer.append(byte); self.state = "sb"
        return bytes(out), bytes(responses), events


# ==========================================================================
# ANSI layer
# ==========================================================================
ANSI_COLORS = {
    30: "#3a3a3a", 31: "#cc4444", 32: "#44aa44", 33: "#ccaa44",
    34: "#5577cc", 35: "#aa55aa", 36: "#44aaaa", 37: "#cccccc",
    90: "#777777", 91: "#ff6b6b", 92: "#6bff6b", 93: "#ffd76b",
    94: "#7b9bff", 95: "#ff7bff", 96: "#6bffff", 97: "#ffffff",
}
ANSI_BG = {
    40: "#000000", 41: "#552222", 42: "#225522", 43: "#555522",
    44: "#222255", 45: "#552255", 46: "#225555", 47: "#aaaaaa",
}
DEFAULT_FG = "#cccccc"
ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m|\x1b\[[0-9;]*[A-Za-ln-z]")


class AnsiParser:
    def __init__(self):
        self.reset()

    def reset(self):
        self.fg = self.bg = None
        self.bold = self.underline = self.italic = False

    def _apply(self, params):
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                self.reset()
            elif p == 1:
                self.bold = True
            elif p == 3:
                self.italic = True
            elif p == 4:
                self.underline = True
            elif p == 22:
                self.bold = False
            elif p == 23:
                self.italic = False
            elif p == 24:
                self.underline = False
            elif 30 <= p <= 37 or 90 <= p <= 97:
                self.fg = p
            elif p == 39:
                self.fg = None
            elif 40 <= p <= 47:
                self.bg = p
            elif p == 49:
                self.bg = None
            elif p in (38, 48):
                if i + 1 < len(params) and params[i + 1] == 5:
                    i += 2
                elif i + 1 < len(params) and params[i + 1] == 2:
                    i += 4
            i += 1

    def current_tag(self):
        if self.fg is None and self.bg is None and not self.bold \
           and not self.underline and not self.italic:
            return None
        fg = self.fg
        if self.bold and fg is not None and 30 <= fg <= 37:
            fg += 60
        parts = ["s", f"f{fg}" if fg is not None else "f",
                 f"b{self.bg}" if self.bg is not None else "b"]
        if self.bold:
            parts.append("B")
        if self.underline:
            parts.append("U")
        if self.italic:
            parts.append("I")
        return "_".join(parts)

    def parse(self, text):
        spans, clean_parts, pos = [], [], 0
        for m in ANSI_RE.finditer(text):
            if m.start() > pos:
                chunk = text[pos:m.start()]
                spans.append((chunk, self.current_tag()))
                clean_parts.append(chunk)
            if m.group(1) is not None:
                params = [int(x) for x in m.group(1).split(";") if x] or [0]
                self._apply(params)
            pos = m.end()
        if pos < len(text):
            chunk = text[pos:]
            spans.append((chunk, self.current_tag()))
            clean_parts.append(chunk)
        return spans, "".join(clean_parts)


def tag_to_style(tag):
    opts = {}
    for part in tag.split("_")[1:]:
        if part.startswith("f") and len(part) > 1:
            opts["foreground"] = ANSI_COLORS.get(int(part[1:]), DEFAULT_FG)
        elif part.startswith("b") and len(part) > 1:
            opts["background"] = ANSI_BG.get(int(part[1:]))
        elif part == "B":
            opts["bold"] = True
        elif part == "U":
            opts["underline"] = True
    return opts


# ==========================================================================
# MIP layer
# ==========================================================================
class MipExtractor:
    """Pulls #K% packets out of the decoded text stream, before line
    splitting, so packets that arrive mid-line or unterminated never
    pollute the display or the trigger pipeline.

    feed(text) -> (clean_text, [(tag, data), ...])
    Holds partial packets across feeds. Wrong-pin packets are dropped.
    """
    HEADER = "#K%"
    MIN_PACKET = 14          # header(3) + pin(5) + len(3) + tag(3)

    def __init__(self, pin):
        self.pin = f"{pin:05d}"
        self.buffer = ""
        self._swallow_nl = False

    def _consume_leading_nl(self):
        """A MIP packet sits on its own line, so the newline that
        terminated it must go too - otherwise extraction leaves a blank
        line in the main output (one per heartbeat once guild feeds are
        on). Drop a single leading CR/LF/CRLF from the buffer. If the
        newline hasn't arrived yet (packet ended this chunk), defer to the
        next feed; if the next char isn't a newline, the packet wasn't
        line-terminated, so swallow nothing."""
        b = self.buffer
        if not b:
            self._swallow_nl = True
            return
        if b[0] == "\n":
            self.buffer = b[1:]
        elif b[0] == "\r":
            self.buffer = b[2:] if b[1:2] == "\n" else b[1:]
        self._swallow_nl = False

    def feed(self, text):
        self.buffer += text
        if self._swallow_nl:
            self._consume_leading_nl()
        out = []
        packets = []
        while self.buffer:
            idx = self.buffer.find(self.HEADER)
            if idx == -1:
                keep = 0
                for n in (2, 1):
                    if self.buffer.endswith(self.HEADER[:n]):
                        keep = n
                        break
                if keep:
                    out.append(self.buffer[:-keep])
                    self.buffer = self.buffer[-keep:]
                else:
                    out.append(self.buffer)
                    self.buffer = ""
                break
            out.append(self.buffer[:idx])
            rest = self.buffer[idx:]
            if len(rest) < self.MIN_PACKET:
                self.buffer = rest
                break
            pin = rest[3:8]
            length_s = rest[8:11]
            if not (pin.isdigit() and length_s.isdigit()):
                out.append(self.HEADER)
                self.buffer = rest[3:]
                continue
            length = int(length_s)
            total = 11 + length
            if len(rest) < total:
                self.buffer = rest
                break
            tag = rest[11:14]
            data = rest[14:total]
            self.buffer = rest[total:]
            if pin == self.pin:
                packets.append((tag, data.rstrip("\r\n")))
            self._consume_leading_nl()
        return "".join(out), packets


COMPOSITE_NUMERIC = {
    "A": "hp", "B": "hpmax", "C": "sp", "D": "spmax",
    "E": "gp1", "F": "gp1max", "G": "gp2", "H": "gp2max",
    "L": "enemycond", "N": "round",
}
COMPOSITE_TEXT = {"I": "hpbar1", "J": "hpbar2", "K": "enemy"}

MIP_COLOR_RE = re.compile(r"<\w([^>]*)>")


def strip_mip_colors(s):
    """'Mode:[<rFocused>] <v-940>' -> 'Mode:[Focused] -940'"""
    return MIP_COLOR_RE.sub(r"\1", s)


def parse_composite(data):
    """FFF payload: LETTER~value~LETTER~value~...
    PARTIAL updates - only changed fields appear - so this returns
    ONLY the fields present (caller must merge, not replace).
    Defensive parsing posture (spec 5.1): all fields optional."""
    tokens = data.split("~")
    fields = {}
    i = 0
    while i < len(tokens):
        key = tokens[i]
        if len(key) == 1 and key.isalpha() and key.isupper():
            fields[key] = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 2
        else:
            i += 1
    out = {}
    for letter, name in COMPOSITE_NUMERIC.items():
        if letter in fields:
            try:
                out[name] = int(fields[letter])
            except ValueError:
                # Some pools are sent as decimals (e.g. 3s changeling Stamina
                # in the E field, "98.99") - int() drops them, so fall back to
                # float rather than losing the field entirely.
                try:
                    out[name] = float(fields[letter])
                except ValueError:
                    pass
    for letter, name in COMPOSITE_TEXT.items():
        if letter in fields:
            out[name] = fields[letter]
    return out


# ==========================================================================
# Network thread
# ==========================================================================
class MudConnection(threading.Thread):
    def __init__(self, host, port, out_queue, mip_pin):
        super().__init__(daemon=True)
        self.host, self.port = host, port
        self.out_queue = out_queue
        self.sock = None
        self.alive = False
        self.telnet = TelnetFilter()
        self.ansi = AnsiParser()
        self.mip = MipExtractor(mip_pin)
        self._linebuf = ""

    def run(self):
        try:
            self.out_queue.put(("status",
                                f"Connecting to {self.host}:{self.port}..."))
            self.sock = socket.create_connection((self.host, self.port),
                                                 timeout=15)
            self.sock.settimeout(None)
            self.alive = True
            self.out_queue.put(("event", "CONNECTED"))
        except OSError as e:
            self.out_queue.put(("status", f"Connection failed: {e}"))
            self.out_queue.put(("event", "DISCONNECTED"))
            return
        while self.alive:
            try:
                data = self.sock.recv(4096)
            except OSError:
                break
            if not data:
                break
            clean_bytes, responses, events = self.telnet.feed(data)
            if responses:
                try:
                    self.sock.sendall(responses)
                except OSError:
                    break
            text = clean_bytes.decode("utf-8", "replace")
            text, packets = self.mip.feed(text)
            for tag, pdata in packets:
                self.out_queue.put(("mip", tag, pdata))
            self._process(text, prompt=("GA" in events))
            for ev in events:
                if ev in ("ECHO_OFF", "ECHO_ON"):
                    self.out_queue.put(("event", ev))
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass
        self.out_queue.put(("event", "DISCONNECTED"))

    def _process(self, text, prompt=False):
        self._linebuf += text
        while "\n" in self._linebuf:
            raw, self._linebuf = self._linebuf.split("\n", 1)
            line = raw.rstrip("\r")
            spans, clean = self.ansi.parse(line)
            self.out_queue.put(("line", spans, clean))
        if prompt and self._linebuf:
            spans, clean = self.ansi.parse(self._linebuf)
            self._linebuf = ""
            self.out_queue.put(("prompt", spans, clean))

    def send(self, text):
        if self.alive and self.sock:
            try:
                self.sock.sendall(text.encode("utf-8", "replace") + b"\r\n")
                return True
            except OSError:
                return False
        return False

    def stop(self):
        self.alive = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
