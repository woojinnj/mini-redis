from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mini_redis.client import Client


class LifecycleTests(unittest.TestCase):
    def test_save_survives_crash_and_shutdown_saves_latest(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'snapshot.json'
            processes = []

            def start():
                process = subprocess.Popen(
                    [sys.executable, '-m', 'mini_redis.server', '--port', '0', '--snapshot', str(path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(process)
                line = process.stdout.readline()
                self.assertIn('listening on', line)
                return process, Client(port=int(line.rsplit(':', 1)[1]))

            try:
                process, client = start()
                client.execute('SET', 'saved', 'before-crash')
                self.assertEqual(client.execute('SAVE'), 'OK')
                client.execute('SET', 'unsaved', 'may-be-lost')
                process.kill()
                process.wait(timeout=5)
                process, client = start()
                self.assertEqual(client.execute('GET', 'saved'), b'before-crash')
                self.assertIsNone(client.execute('GET', 'unsaved'))
                client.execute('SET', 'saved', 'after-restart')
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 0)
                process, client = start()
                self.assertEqual(client.execute('GET', 'saved'), b'after-restart')
                process.terminate()
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)
