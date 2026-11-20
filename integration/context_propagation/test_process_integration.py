"""
真实发布 / 消费进程的集成测试。

与单元测试不同，这里通过 subprocess 启动两个独立的 Python 进程：
  * publisher.py  —— 发布进程，把任务写入 SQLite 队列；
  * consumer_runner.py —— 消费者进程，运行真实 Consumer 消费任务。

两个进程只通过 SQLite 文件与结果文件通信，证明发布上下文随消息序列化跨进程
传播，而不是依赖发布进程内的全局变量。

脚本本身放在独立的 scripts/ 目录，数据放在独立的 data/ 目录，子进程使用
``python -I`` 启动并通过绝对路径 / 环境变量传参。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, 'scripts')
REPO_ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))


class TestContextPropagationProcesses(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix='huey-ctx-integration-')
        self.db_path = os.path.join(self.data_dir, 'queue.db')
        self.result_path = os.path.join(self.data_dir, 'results.log')
        self.log_path = os.path.join(self.data_dir, 'consumer.log')
        self.attempt_path = os.path.join(self.data_dir, 'attempts.txt')
        self.marker_path = os.path.join(self.data_dir, 'marker.txt')
        self.env = dict(os.environ)
        self.env.update({
            'HUEY_REPO_ROOT': REPO_ROOT,
            'HUEY_SCRIPTS_DIR': SCRIPTS_DIR,
            'HUEY_CTX_DB': self.db_path,
            'HUEY_CTX_RESULT': self.result_path,
            'HUEY_CTX_LOG': self.log_path,
            'HUEY_CTX_ATTEMPT': self.attempt_path,
            'HUEY_CTX_MARKER': self.marker_path,
        })

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _run(self, script_name):
        # -I: isolated mode, no cwd / PYTHONPATH site-package surprises.
        proc = subprocess.run(
            [sys.executable, '-I', os.path.join(SCRIPTS_DIR, script_name)],
            env=self.env,
            cwd=self.data_dir,
            capture_output=True,
            text=True,
            timeout=120)
        if proc.returncode != 0:
            self.fail('subprocess %s failed (rc=%s)\nSTDOUT:\n%s\nSTDERR:\n%s'
                      % (script_name, proc.returncode,
                         proc.stdout, proc.stderr))
        return proc

    def test_publish_and_consume_across_processes(self):
        self._run_scenario('thread')

    def test_publish_and_consume_with_process_workers(self):
        # Forked worker processes: verifies cleanup across real OS-process
        # boundaries, not just threads.
        self._run_scenario('process')

    def _run_scenario(self, worker_type):
        self.env['HUEY_CTX_WORKER'] = worker_type

        # 1. Publisher process writes everything into SQLite.
        pub = self._run('publisher.py')
        self.assertIn('published', pub.stdout)

        # 2. Consumer process drains the queue in a separate process.
        self._run('consumer_runner.py')

        with open(self.result_path, encoding='utf-8') as fh:
            lines = [line.strip() for line in fh if line.strip()]

        # --- two tenants interleaved, each keeping its own ids ---
        # Two worker threads consume concurrently, so completion order is not
        # guaranteed: compare as a set rather than relying on FIFO.
        echo_lines = set(l for l in lines if l.startswith('echo '))
        self.assertEqual(echo_lines, {
            'echo a1 tenant=tenant-a cid=req-A',
            'echo b1 tenant=tenant-b cid=req-B',
            'echo a2 tenant=tenant-a cid=req-A',
            'echo b2 tenant=tenant-b cid=req-B',
        })

        # --- retry keeps the same correlation id across all attempts ---
        attempt_lines = [l for l in lines if l.startswith('attempt ')]
        self.assertEqual(len(attempt_lines), 3)
        for line in attempt_lines:
            self.assertIn('tenant=tenant-retry cid=req-retry', line)
        self.assertIn('flaky-success tenant=tenant-retry cid=req-retry',
                      lines)

        # --- nested dynamic child inherits the parent context ---
        self.assertIn('parent tenant=tenant-nested cid=req-nested', lines)
        self.assertIn('child tenant=tenant-nested cid=req-nested', lines)

        # --- explicit override affects only the child branch ---
        self.assertIn('parent tenant=tenant-nested2 cid=req-nested2', lines)
        self.assertIn('child tenant=override-tenant cid=cid-override', lines)

        # --- ambient publish_context() was attached and serialized ---
        probe_lines = [l for l in lines if l.startswith('probe ')]
        self.assertEqual(len(probe_lines), 1)
        self.assertIn('tenant=tenant-ambient cid=req-ambient', probe_lines[0])
        with open(self.marker_path) as fh:
            self.assertEqual(fh.read(), 'tenant-ambient|req-ambient')

        # --- no context residue on the consumer side after the run ---
        self.assertIn('residue-check main-thread clean', lines)
        self.assertIn('consumer-stopped', lines)
        residue = [l for l in lines if 'RESIDUE' in l]
        self.assertEqual(residue, [])

        # --- log lines carry the correct correlation id, and later tasks
        # never inherit a previous tenant's id ---
        with open(self.log_path, encoding='utf-8') as fh:
            log_lines = [l.strip() for l in fh if l.strip()]
        # Every formatted line resolved the filter placeholders (no KeyError,
        # no literal "%(tenant)s" left behind).
        self.assertTrue(log_lines)
        for line in log_lines:
            self.assertNotIn('%(tenant)s', line)
            self.assertNotIn('%(correlation_id)s', line)

        def log_lines_for(cid):
            return [l for l in log_lines if 'cid=%s' % cid in l]

        # The retry task's three attempts all share req-retry.
        retry_executing = [l for l in log_lines_for('req-retry')
                           if 'Executing' in l]
        self.assertGreaterEqual(len(retry_executing), 3)
        # req-A's lines never show req-B's id and vice-versa.
        self.assertEqual(
            [l for l in log_lines_for('req-A') if 'cid=req-B' in l], [])
        self.assertEqual(
            [l for l in log_lines_for('req-B') if 'cid=req-A' in l], [])
        # The explicit child override logs with cid-override, not the parent's.
        self.assertTrue(log_lines_for('cid-override'))
        self.assertEqual(
            [l for l in log_lines_for('cid-override')
             if 'cid=req-nested2' in l], [])

        # The SQLite queue must be fully drained.
        # (Re-open read-only from this process to verify.)
        sys.path.insert(0, REPO_ROOT)
        try:
            from huey import SqliteHuey
            verifier = SqliteHuey('context-integration',
                                  filename=self.db_path, utc=False)
            self.assertEqual(verifier.pending_count(), 0)
            self.assertEqual(verifier.scheduled_count(), 0)
        finally:
            sys.path.remove(REPO_ROOT)


if __name__ == '__main__':
    unittest.main()
