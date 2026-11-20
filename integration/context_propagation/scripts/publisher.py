"""
发布进程：把两个租户的任务、失败重试任务与嵌套调用任务写入 SQLite 队列。

通过环境变量接收数据库路径等参数，不依赖工作目录，可在 `python -I` 下运行。
"""
import os
import sys

_REPO_ROOT = os.environ['HUEY_REPO_ROOT']
_SCRIPT_DIR = os.environ.get('HUEY_SCRIPTS_DIR') or \
        os.path.dirname(os.path.abspath(__file__))
for path in (_REPO_ROOT, _SCRIPT_DIR):
    if path and path not in sys.path:
        sys.path.insert(0, path)

from huey import PublishContext  # noqa: E402
from huey import publish_context  # noqa: E402

from integration_tasks import huey  # noqa: E402
from integration_tasks import echo_tenant  # noqa: E402
from integration_tasks import flaky_task  # noqa: E402
from integration_tasks import nested_parent  # noqa: E402
from integration_tasks import cleanup_probe  # noqa: E402


def main():
    attempt_path = os.environ['HUEY_CTX_ATTEMPT']
    marker_path = os.environ['HUEY_CTX_MARKER']

    tenant_a = PublishContext('tenant-a', correlation_id='req-A')
    tenant_b = PublishContext('tenant-b', correlation_id='req-B')

    # 1. 两个租户交错入队。
    echo_tenant('a1', task_context=tenant_a)
    echo_tenant('b1', task_context=tenant_b)
    echo_tenant('a2', task_context=tenant_a)
    echo_tenant('b2', task_context=tenant_b)

    # 2. 失败后重试的任务，上下文必须贯穿所有尝试。
    flaky_task(attempt_path, task_context=PublishContext(
        'tenant-retry', correlation_id='req-retry'))

    # 3. 嵌套调用：父任务动态发布子任务（继承）。
    nested_parent(False, task_context=PublishContext(
        'tenant-nested', correlation_id='req-nested'))

    # 3b. 嵌套调用中的显式覆盖，只作用于子任务分支。
    nested_parent(True, task_context=PublishContext(
        'tenant-nested2', correlation_id='req-nested2'))

    # 4. 环境上下文（publish_context 块）自动附加。
    with publish_context(PublishContext('tenant-ambient',
                                        correlation_id='req-ambient')):
        cleanup_probe(marker_path)

    print('published %d tasks' % len(huey))


if __name__ == '__main__':
    main()
