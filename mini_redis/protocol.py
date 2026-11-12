"""Bounded RESP2 framing. Buffered streams handle fragmented TCP packets."""
MAX_BULK = 1024 * 1024
MAX_ARGS = 64
MAX_REQUEST = 2 * MAX_BULK


class ProtocolError(ValueError):
    pass


class RedisError(Exception):
    pass


class Simple(str):
    pass


def line(stream):
    data = stream.readline(128)
    if not data.endswith(b'\r\n'):
        raise ProtocolError('invalid or incomplete line')
    return data[:-2]


def number(raw):
    if not raw or len(raw) > 20 or not raw.isdigit():
        raise ProtocolError('invalid length')
    return int(raw)


def read_request(stream):
    prefix = stream.read(1)
    if not prefix:
        return None
    if prefix != b'*':
        raise ProtocolError('expected array of bulk strings')
    count = number(line(stream))
    if not 1 <= count <= MAX_ARGS:
        raise ProtocolError('invalid argument count')
    args, total = [], 0
    for _ in range(count):
        if stream.read(1) != b'$':
            raise ProtocolError('expected bulk string')
        size = number(line(stream))
        total += size
        if size > MAX_BULK or total > MAX_REQUEST:
            raise ProtocolError('request too large')
        data = stream.read(size)
        if len(data) != size or stream.read(2) != b'\r\n':
            raise ProtocolError('incomplete bulk string')
        args.append(data)
    return args


def encode(value):
    if value is None:
        return b'$-1\r\n'
    if isinstance(value, Simple):
        return b'+' + value.encode() + b'\r\n'
    if isinstance(value, Exception):
        message = str(value).replace('\r', ' ').replace('\n', ' ')
        return b'-ERR ' + message.encode() + b'\r\n'
    if isinstance(value, int):
        return b':' + str(value).encode() + b'\r\n'
    if isinstance(value, bytes):
        return b'$' + str(len(value)).encode() + b'\r\n' + value + b'\r\n'
    if isinstance(value, (list, tuple)):
        return b'*' + str(len(value)).encode() + b'\r\n' + b''.join(encode(v) for v in value)
    raise TypeError('unsupported RESP value')


def read_response(stream):
    prefix = stream.read(1)
    if not prefix:
        raise ConnectionError('server closed connection')
    raw = line(stream)
    if prefix == b'+':
        return raw.decode()
    if prefix == b'-':
        raise RedisError(raw.decode(errors='replace'))
    if prefix == b':':
        return int(raw)
    if prefix == b'$':
        if raw == b'-1':
            return None
        size = number(raw)
        if size > MAX_BULK:
            raise ProtocolError('response too large')
        data = stream.read(size)
        if len(data) != size or stream.read(2) != b'\r\n':
            raise ProtocolError('incomplete response')
        return data
    raise ProtocolError('unsupported response type')
