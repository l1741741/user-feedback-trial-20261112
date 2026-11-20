"""
真实多进程集成测试共用的任务定义。

发布进程与消费者进程都导入本模块，因此任务注册表在两侧一致。任务通过 SQLite
队列传递，上下文随消息序列化跨进程传播，不依赖任何进程内全局变量。
"""
import logging
import os
import sys

# 允许以 `python -I` 运行时通过 PYTHONPATH 之外的方式找到 huey 包。
_REPO_ROOT = os.environ.get('HUEY_REPO_ROOT')
if _REPO_ROOT and _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from huey import SqliteHuey  # noqa: E402
from huey import PublishContext  # noqa: E402
from huey import get_current_context  # noqa: E402


DB_PATH = os.environ['HUEY_CTX_DB']
RESULT_PATH = os.environ['HUEY_CTX_RESULT']

huey = SqliteHuey('context-integration', filename=DB_PATH, utc=False)

logger = logging.getLogger('integration.tasks')


def _record(line):
    with open(RESULT_PATH, 'a', encoding='utf-8') as fh:
        fh.write(line + '\n')


@huey.task()
def echo_tenant(tag):
    """记录当前上下文；验证两个租户交错执行时不串号。"""
    ctx = get_current_context()
    assert ctx is not None, 'context missing inside task %s' % tag
    _record('echo %s tenant=%s cid=%s' % (tag, ctx.tenant, ctx.correlation_id))
    logger.info('echo %s', tag)
    return '%s/%s' % (ctx.tenant, ctx.correlation_id)


@huey.task(retries=2)
def flaky_task(attempt_path):
    """前两次抛错触发重试，第三次成功；重试必须保留同一关联 ID。"""
    ctx = get_current_context()
    assert ctx is not None
    _record('attempt tenant=%s cid=%s' % (ctx.tenant, ctx.correlation_id))
    if os.path.exists(attempt_path):
        with open(attempt_path) as fh:
            count = int(fh.read().strip() or '0')
    else:
        count = 0
    count += 1
    with open(attempt_path, 'w') as fh:
        fh.write(str(count))
    if count < 3:
        raise RuntimeError('flaky failure #%d' % count)
    _record('flaky-success tenant=%s cid=%s' % (ctx.tenant, ctx.correlation_id))
    return 'ok'


@huey.task()
def nested_parent(child_should_override=False):
    """嵌套 immediate 风格的动态子任务：同步链路 + 动态入队子任务。"""
    ctx = get_current_context()
    assert ctx is not None
    _record('parent tenant=%s cid=%s' % (ctx.tenant, ctx.correlation_id))

    # 动态发布的子任务应继承父任务上下文。
    if child_should_override:
        dynamic_child(task_context=PublishContext(
            'override-tenant', correlation_id='cid-override'))
    else:
        dynamic_child()
    return ctx.tenant


@huey.task()
def dynamic_child():
    ctx = get_current_context()
    assert ctx is not None
    _record('child tenant=%s cid=%s' % (ctx.tenant, ctx.correlation_id))
    return ctx.tenant


@huey.task()
def cleanup_probe(marker_path):
    """执行结束后由消费者进程内的后续检查验证上下文已恢复。"""
    ctx = get_current_context()
    _record('probe tenant=%s cid=%s' % (ctx.tenant, ctx.correlation_id))
    with open(marker_path, 'w') as fh:
        fh.write('%s|%s' % (ctx.tenant, ctx.correlation_id))
    return True
