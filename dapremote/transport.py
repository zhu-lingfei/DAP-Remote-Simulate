"""TCP and optional pySerial adapters with bounded read operations."""
import socket


class SocketTransport:
    supports_baudrate = False
    baudrate = None
    def __init__(self, host="127.0.0.1", port=8765, connect_timeout=3.0):
        self.socket = socket.create_connection((host, port), connect_timeout)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def read(self, max_bytes, timeout):
        self.socket.settimeout(max(0.001, timeout))
        try:
            data = self.socket.recv(max_bytes)
        except socket.timeout:
            return b""
        if not data:
            raise ConnectionError("peer disconnected")
        return data

    def write(self, data):
        self.socket.settimeout(3.0)
        self.socket.sendall(data)

    def close(self):
        self.socket.close()


class SerialTransport:
    supports_baudrate = True
    def __init__(self, port, baudrate=115200, rtscts=False):
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError('Install serial support: pip install ".[serial]"') from exc
        self.serial = serial.Serial(port, baudrate, timeout=0.1,
                                    write_timeout=3.0, rtscts=rtscts, xonxoff=False)

    def read(self, max_bytes, timeout):
        self.serial.timeout = max(0.001, timeout)
        return self.serial.read(min(max_bytes, max(1, self.serial.in_waiting)))

    def write(self, data):
        view = memoryview(data)
        while view:
            count = self.serial.write(view)
            if not count:
                raise ConnectionError("serial write made no progress")
            view = view[count:]

    @property
    def baudrate(self):
        return self.serial.baudrate

    def set_baudrate(self, baudrate):
        """Set only this local serial port. No peer commands or module AT commands."""
        if type(baudrate) is not int or not 1 <= baudrate <= 4000000:
            raise ValueError("baudrate must be an integer in 1..4000000")
        self.serial.baudrate = baudrate

    def reset_input(self):
        self.serial.reset_input_buffer()

    def close(self):
        self.serial.close()


def connect(endpoint="tcp://127.0.0.1:8765", baudrate=115200):
    if endpoint.startswith("tcp://"):
        from urllib.parse import urlsplit
        url = urlsplit(endpoint)
        if not url.hostname or not url.port:
            raise ValueError("TCP endpoint requires hostname and port")
        return SocketTransport(url.hostname, url.port)
    return SerialTransport(endpoint, baudrate)
