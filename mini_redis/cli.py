"""Usage: python3 -m mini_redis.cli SET greeting hello EX 60"""
import argparse
from .client import Client
from .protocol import ProtocolError, RedisError


def main():
    parser = argparse.ArgumentParser(description='Mini Redis command-line client')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=6379)
    parser.add_argument('command', nargs='+')
    args = parser.parse_args()
    try:
        value = Client(args.host, args.port).execute(*args.command)
    except (OSError, RedisError, ProtocolError) as error:
        parser.exit(1, f'Error: {error}\n')
    if isinstance(value, bytes):
        print(value.decode('utf-8', errors='backslashreplace'))
    else:
        print('(nil)' if value is None else value)


if __name__ == '__main__':
    main()
