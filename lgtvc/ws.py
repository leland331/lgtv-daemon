"""Minimal blocking WebSocket client (text frames only), stdlib only."""
import base64
import os
import socket
import ssl
import struct
from urllib.parse import urlparse


class WSError(Exception):
    pass


class WebSocket:
    def __init__(self, url, timeout=5.0):
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE   # TVs use self-signed certificates
            sock = ctx.wrap_socket(sock, server_hostname=u.hostname)
        self.sock = sock
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n") % (path, u.hostname, port, key)
        sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise WSError("connection closed during handshake")
            buf += chunk
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise WSError("handshake failed: %r" % head[:80])

    def settimeout(self, t):
        self.sock.settimeout(t)

    def send(self, text):
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0xFE, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0xFF, n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:
                raise WSError("closed by peer")
            if op == 0x9:   # ping -> pong
                self.sock.sendall(struct.pack("!BB", 0x8A, 0x80 | len(data)) + b"\0\0\0\0" + data)
                continue
            if op in (0x1, 0x2, 0x0):
                msg += data
                if b1 & 0x80:
                    return msg.decode(errors="replace")

    def close(self):
        try:
            self.sock.sendall(b"\x88\x80\0\0\0\0")
        except OSError:
            pass
        self.sock.close()
