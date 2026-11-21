"""真实子进程集成测试：两个代码版本的进程分别写入/取件。

NATIVE-RUNTIME.md 指出多进程行为必须用实际子进程验证，不能由同进程
夹具代替。本模块每个用例都在独立临时目录中启动 demo 入口进程，
覆盖升级、回退、原有默认格式三条路径。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from huey.storage import SqliteStorage

from version_bridge.envelope import is_envelope


def run_module(module, db_path, log_path, *extra):
    cmd = [sys.executable, '-I', '-m', module,
           '--db', db_path, '--log', log_path, *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            '%s exited %d\nstdout:\n%s\nstderr:\n%s'
            % (module, proc.returncode, proc.stdout, proc.stderr))


def start_consumer(module, db_path, log_path, max_wait='6'):
    return subprocess.Popen([
        sys.executable, '-I', '-m', module,
        '--db', db_path, '--log', log_path,
        '--max-wait', max_wait, '--interval', '0.05'])


def read_events(log_path):
    with open(log_path, encoding='utf-8') as fh:
        return [json.loads(line) for line in fh if line.strip()]


def events_of(events, actor, name):
    return [e for e in events if e.get('actor') == actor
            and e.get('event') == name]


def normalize_backlog(backlog):
    """JSON 会把字典整数键转成字符串，断言前归一化回整数键。"""
    return {int(version): count for version, count in backlog.items()}


class SubprocessScenarioTestCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='vbridge-subproc-')
        self.db_path = os.path.join(self.workdir, 'queue.db')
        self.log_path = os.path.join(self.workdir, 'events.log')

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_upgrade_v1_retains_v2_then_v2_drains(self):
        run_module('version_bridge.demo.v1_producer', self.db_path,
                   self.log_path, '--order', 'o-1', '--order', 'o-2')

        consumer = start_consumer(
            'version_bridge.demo.v1_consumer', self.db_path, self.log_path)
        run_module('version_bridge.demo.v2_producer', self.db_path,
                   self.log_path,
                   '--order', 'o-3', '--channel', 'web',
                   '--order', 'o-4', '--channel', 'mobile')
        run_module('version_bridge.demo.v1_producer', self.db_path,
                   self.log_path, '--order', 'o-5')
        consumer.wait(timeout=30)
        self.assertEqual(consumer.returncode, 0)

        events = read_events(self.log_path)
        processed = events_of(events, 'v1-consumer', 'processed')
        self.assertEqual([e['order_id'] for e in processed],
                         ['o-1', 'o-2', 'o-5'])
        self.assertTrue(
            all(e['message_version'] == 1 for e in processed))
        retained = events_of(events, 'v1-consumer',
                             'retained-incompatible')
        self.assertGreaterEqual(sum(e['skipped'] for e in retained), 2)

        storage = SqliteStorage('orders', filename=self.db_path)
        try:
            leftover = [bytes(row) for row in storage.enqueued_items()]
        finally:
            storage.close()
        self.assertEqual(len(leftover), 2)
        self.assertTrue(all(is_envelope(row) for row in leftover))

        run_module('version_bridge.demo.v2_consumer', self.db_path,
                   self.log_path, '--max-wait', '2')
        events = read_events(self.log_path)
        v2_processed = events_of(events, 'v2-consumer', 'processed')
        self.assertEqual(sorted(e['order_id'] for e in v2_processed),
                         ['o-3', 'o-4'])
        self.assertEqual(
            sorted(e['channel'] for e in v2_processed),
            ['mobile', 'web'])
        backlog = events_of(events, 'v2-consumer', 'exit')[-1]['backlog']
        self.assertEqual(normalize_backlog(backlog), {})

    def test_rollback_v2_backlog_survives_v1_consumer(self):
        run_module('version_bridge.demo.v2_producer', self.db_path,
                   self.log_path,
                   '--order', 'n-1', '--channel', 'web',
                   '--order', 'n-2', '--channel', 'app')

        consumer = start_consumer(
            'version_bridge.demo.v1_consumer', self.db_path, self.log_path)
        run_module('version_bridge.demo.v1_producer', self.db_path,
                   self.log_path, '--order', 'n-3', '--order', 'n-4')
        consumer.wait(timeout=30)
        self.assertEqual(consumer.returncode, 0)

        events = read_events(self.log_path)
        v1_processed = events_of(events, 'v1-consumer', 'processed')
        self.assertEqual(sorted(e['order_id'] for e in v1_processed),
                         ['n-3', 'n-4'])
        backlog = events_of(events, 'v1-consumer', 'exit')[-1]['backlog']
        self.assertEqual(normalize_backlog(backlog), {2: 2})

        run_module('version_bridge.demo.v2_consumer', self.db_path,
                   self.log_path, '--max-wait', '2')
        events = read_events(self.log_path)
        v2_processed = events_of(events, 'v2-consumer', 'processed')
        self.assertEqual(sorted(e['order_id'] for e in v2_processed),
                         ['n-1', 'n-2'])

    def test_default_format_roundtrip_and_migration(self):
        run_module('version_bridge.demo.v1_producer', self.db_path,
                   self.log_path, '--order', 'd-1', '--order', 'd-2')

        storage = SqliteStorage('orders', filename=self.db_path)
        try:
            raw_items = [bytes(row) for row in storage.enqueued_items()]
        finally:
            storage.close()
        self.assertEqual(len(raw_items), 2)
        self.assertTrue(all(not is_envelope(row) for row in raw_items))

        run_module('version_bridge.demo.v2_consumer', self.db_path,
                   self.log_path, '--max-wait', '2')
        events = read_events(self.log_path)
        migrated = events_of(events, 'v2-consumer', 'migrated')
        # stock 原生 dequeue 未在本用例使用，两条 v1 都经迁移钩子处理。
        self.assertEqual(sorted(e['order_id'] for e in migrated),
                         ['d-1', 'd-2'])
        processed = events_of(events, 'v2-consumer', 'processed')
        self.assertTrue(all(e['channel'] == 'legacy-default'
                            for e in processed))
        self.assertTrue(all(e['message_version'] == 1 for e in processed))


if __name__ == '__main__':
    unittest.main()
