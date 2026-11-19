"""
Task definitions shared by the reliable-mode tests and their helper process.

Tasks must be registered under the same module path in every process so that
messages serialized on one side can be deserialized on the other.
"""
import os
import time


def configure(huey, log_path):
    @huey.task()
    def archive(name, seconds=0):
        with open(log_path, 'a') as fh:
            fh.write('start:%s:%s:%.3f\n' % (name, os.getpid(), time.time()))
        if seconds:
            time.sleep(seconds)
        else:
            # Yield briefly so a supervising process can observe the start.
            time.sleep(0.001)
        with open(log_path, 'a') as fh:
            fh.write('done:%s:%s:%.3f\n' % (name, os.getpid(), time.time()))
        return name

    @huey.task(retries=1)
    def flaky(name):
        with open(log_path, 'a') as fh:
            fh.write('flaky:%s:%s\n' % (name, os.getpid()))
        raise ValueError('transient failure')

    return archive, flaky
