"""A binary-safe client. One connection per call makes instances thread safe."""
import socket
from .protocol import encode, read_response


class Client:
    def __init__(self, host='127.0.0.1', port=6379, timeout=3):
        self.host, self.port, self.timeout = host, port, timeout

    def execute(self, *args):
        args = [arg if isinstance(arg, bytes) else str(arg).encode() for arg in args]
        with socket.create_connection((self.host, self.port), self.timeout) as connection:
            connection.sendall(encode(args))
            with connection.makefile('rb') as stream:
                return read_response(stream)
