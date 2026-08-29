"""Standalone GMCP probe for 3s - a transparent telnet terminal that
accepts option 201, subscribes to every package root we know of, and
dumps each GMCP packet to screen and to logs/gmcp_<ts>.log.

Touches nothing in katmud_lib. Play normally through it; what you type
is never written to the capture file.

    python tools/gmcp_probe.py [host] [port]
"""
import datetime
import os
import re
import socket
import sys
import threading

IAC, DONT, DO, WONT, WILL, SB, SE, GA, EOR = \
    255, 254, 253, 252, 251, 250, 240, 249, 239
OPT_ECHO, OPT_EOR, OPT_GMCP = 1, 25, 201

HOST, PORT = "172.232.4.129", 3200
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Root names from docs/gmcp/gmcp.txt + docs/gmcp/mercs.txt. Roots pull
# every package under them; the server lists the real surface back in
# Core.Supported, which is the point of the probe.
ROOTS = ["Char 1", "Room 1", "Comm 1", "Guild 1", "Merc 1"]


def gmcp(payload):
    """Wrap a GMCP payload in IAC SB 201 ... IAC SE, escaping IAC."""
    body = payload.encode("utf-8").replace(bytes([IAC]), bytes([IAC, IAC]))
    return bytes([IAC, SB, OPT_GMCP]) + body + bytes([IAC, SE])


class Filter:
    """Telnet state machine: same shape as katmud_lib.protocol.TelnetFilter
    but it ACCEPTS option 201 and surfaces the subnegotiation payload."""

    def __init__(self):
        self.state = "data"
        self.cmd = None
        self.sb = bytearray()

    def feed(self, data):
        out, resp, gmcps = bytearray(), bytearray(), []
        for b in data:
            if self.state == "data":
                if b == IAC:
                    self.state = "iac"
                else:
                    out.append(b)
            elif self.state == "iac":
                if b == IAC:
                    out.append(IAC); self.state = "data"
                elif b in (DO, DONT, WILL, WONT):
                    self.cmd = b; self.state = "option"
                elif b == SB:
                    self.sb = bytearray(); self.state = "sb"
                else:
                    self.state = "data"
            elif self.state == "option":
                if self.cmd == WILL:
                    if b in (OPT_ECHO, OPT_EOR, OPT_GMCP):
                        resp += bytes([IAC, DO, b])
                    else:
                        resp += bytes([IAC, DONT, b])
                elif self.cmd == WONT:
                    resp += bytes([IAC, DONT, b])
                elif self.cmd == DO and b == OPT_GMCP:
                    resp += bytes([IAC, WILL, b])
                else:
                    resp += bytes([IAC, WONT, b])
                self.state = "data"
            elif self.state == "sb":
                if b == IAC:
                    self.state = "sb_iac"
                else:
                    self.sb.append(b)
            elif self.state == "sb_iac":
                if b == SE:
                    if self.sb and self.sb[0] == OPT_GMCP:
                        gmcps.append(
                            bytes(self.sb[1:]).decode("utf-8", "replace"))
                    self.state = "data"
                else:
                    self.sb.append(b); self.state = "sb"
        return bytes(out), bytes(resp), gmcps


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else PORT
    os.makedirs("logs", exist_ok=True)
    path = os.path.join("logs", datetime.datetime.now().strftime(
        "gmcp_%Y%m%d_%H%M%S.log"))
    log = open(path, "w", encoding="utf-8")
    print(f"[probe] {host}:{port} -> {path}")

    sock = socket.create_connection((host, port))
    filt = Filter()
    sent_hello = [False]

    def reader():
        while True:
            try:
                data = sock.recv(4096)
            except OSError:
                break
            if not data:
                break
            text, resp, gmcps = filt.feed(data)
            if resp:
                sock.sendall(resp)
                if bytes([IAC, DO, OPT_GMCP]) in resp and not sent_hello[0]:
                    sent_hello[0] = True
                    sock.sendall(gmcp(
                        'Core.Hello { "client": "katmud-probe", '
                        '"version": "0.1" }'))
                    sock.sendall(gmcp(
                        "Core.Supports.Set [" +
                        ", ".join('"%s"' % r for r in ROOTS) + "]"))
                    print("[probe] GMCP accepted; subscribed to "
                          + ", ".join(ROOTS))
                    log.write("# subscribed: %s\n" % ", ".join(ROOTS))
            if text:
                shown = text.decode("utf-8", "replace")
                sys.stdout.write(shown)
                sys.stdout.flush()
                log.write(ANSI.sub("", shown))
                log.flush()
            for g in gmcps:
                print("\nGMCP> " + g)
                log.write("GMCP " + g + "\n")
                log.flush()
        print("\n[probe] connection closed")
        try:
            log.close()
        except ValueError:
            pass
        print(f"[probe] capture saved: {path}")
        sys.stdout.flush()
        os._exit(0)

    threading.Thread(target=reader, daemon=True).start()
    try:
        for line in sys.stdin:
            sock.sendall(line.rstrip("\n").encode("utf-8") + b"\n")
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        try:
            log.close()
        except ValueError:
            pass
        print(f"\n[probe] capture saved: {path}")
        sys.stdout.flush()
        # hard-exit: the reader is a daemon thread and may be mid-write,
        # which crashes a normal interpreter shutdown.
        os._exit(0)


if __name__ == "__main__":
    main()
