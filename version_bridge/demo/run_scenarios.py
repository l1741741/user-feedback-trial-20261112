"""端到端场景驱动：以实际操作系统子进程演示两个代码版本的写入与取件。

三个场景（每个场景使用独立的临时 SQLite 队列与事件日志）：

1. ``upgrade`` 升级：v1 消费者与 v2 写入并行。v1 消费者取走可处理的
   v1 投递，遇到 v2 专属投递原样保留并继续取后续 v1；切换到 v2 消费者
   后，保留项被读出，旧版字段经迁移钩子补齐。
2. ``rollback`` 回退：v2 写入已产生 v2 积压，随后 v1 写入又追加 v1
   消息（模拟新版下线、旧版恢复投递）。v1 消费者只清空 v1 部分，v2
   积压始终保留；v2 消费者再上线时两类消息都能处理。
3. ``default_format`` 原有默认消息格式：全程只有 v1 代码（stock Huey
   生产 + 兼容入口消费），并直接检查库中字节与 stock Huey 原生
   dequeue() 的互通性。

用法::

    python -m version_bridge.demo.run_scenarios            # 运行全部场景
    python -m version_bridge.demo.run_scenarios upgrade    # 只跑升级场景
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

from huey.api import SqliteHuey
from huey.storage import SqliteStorage

from version_bridge.envelope import ENVELOPE_MAGIC
from version_bridge.envelope import is_envelope


def _run(module, db_path, log_path, *extra):
    """以独立子进程运行一个 demo 入口（两个代码版本的进程边界）。"""
    cmd = [sys.executable, '-I', '-m', module,
           '--db', db_path, '--log', log_path, *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            'subprocess %s failed (exit %d)\nstdout:\n%s\nstderr:\n%s'
            % (module, proc.returncode, proc.stdout, proc.stderr))
    return proc.stdout.strip()


def read_events(log_path):
    with open(log_path, encoding='utf-8') as fh:
        return [json.loads(line) for line in fh if line.strip()]


def events_of(events, actor, event_name):
    return [e for e in events if e.get('actor') == actor
            and e.get('event') == event_name]


def normalize_backlog(backlog):
    """JSON 会把字典整数键转成字符串，断言前归一化回整数键。"""
    return {int(version): count for version, count in backlog.items()}


def new_workdir():
    return tempfile.mkdtemp(prefix='vbridge-scenario-')


def scenario_upgrade(workdir):
    print('\n=== 场景 1：升级（v1 消费者保留 v2，切换后 v2 读取保留项）===')
    db_path = os.path.join(workdir, 'upgrade.db')
    log_path = os.path.join(workdir, 'upgrade.log')

    # 1) 旧版先投递一批 v1（原始默认格式）。
    print(_run('version_bridge.demo.v1_producer', db_path, log_path,
               '--order', 'o-1', '--order', 'o-2'))

    # 2) v1 消费者与 v2 写入并行：先启动 v1 消费者（轮询等待），
    #    再让 v2 写入进程投递新版专属消息。
    consumer = subprocess.Popen([
        sys.executable, '-I', '-m',
        'version_bridge.demo.v1_consumer',
        '--db', db_path, '--log', log_path,
        '--max-wait', '6', '--interval', '0.05'])
    # v1 消费者启动后，v2 写入进程投递两条 v2（队列顺序为
    # o-1, o-2, o-3, o-4；v1 消费者会先取走 o-1/o-2，再遇到 v2 保留）。
    print(_run('version_bridge.demo.v2_producer', db_path, log_path,
               '--order', 'o-3', '--channel', 'web',
               '--order', 'o-4', '--channel', 'mobile'))
    # 再追加一条 v1，证明旧消费者跳过 v2 后仍能继续取后续可处理项。
    print(_run('version_bridge.demo.v1_producer', db_path, log_path,
               '--order', 'o-5'))
    consumer.wait(timeout=30)
    assert consumer.returncode == 0, 'v1 消费者异常退出'

    events = read_events(log_path)
    v1_processed = events_of(events, 'v1-consumer', 'processed')
    retained = events_of(events, 'v1-consumer', 'retained-incompatible')
    assert [e['order_id'] for e in v1_processed] == ['o-1', 'o-2', 'o-5'], \
        'v1 消费者应恰好取走全部 v1 投递（含跳过 v2 后的 o-5）'
    assert retained and sum(e['skipped'] for e in retained) >= 2, \
        'v1 消费者应记录对 v2 专属投递的保留'

    # 3) 直接核对库中剩余字节：两条 v2 信封消息仍在，且带信封魔数。
    storage = SqliteStorage('orders', filename=db_path)
    try:
        leftover = [bytes(row) for row in storage.enqueued_items()]
    finally:
        storage.close()
    assert len(leftover) == 2, 'v1 处理后应恰好保留两条 v2 消息'
    assert all(is_envelope(row) and row[:8] == ENVELOPE_MAGIC
               for row in leftover), '保留项必须是 v2 信封格式'

    # 4) 切换到 v2 消费者：读取保留项，v1 风格字段经迁移补齐。
    print(_run('version_bridge.demo.v2_consumer', db_path, log_path,
               '--max-wait', '2'))
    events = read_events(log_path)
    v2_processed = events_of(events, 'v2-consumer', 'processed')
    migrated = events_of(events, 'v2-consumer', 'migrated')
    assert sorted(e['order_id'] for e in v2_processed) == ['o-3', 'o-4'], \
        'v2 消费者应读取 v1 消费者保留的两条 v2 消息'
    assert {e['channel'] for e in v2_processed} == {'web', 'mobile'}, \
        'v2 消息应保留新版专属 channel 字段'
    assert not migrated, '纯 v2 保留项不需要迁移钩子'

    final = events_of(events, 'v2-consumer', 'exit')[-1]
    assert normalize_backlog(final['backlog']) == {}, '切换后队列应被清空'
    print('  ✓ v1 取走 o-1/o-2/o-5，保留 v2 的 o-3/o-4；v2 切换后清空队列')


def scenario_rollback(workdir):
    print('\n=== 场景 2：回退（新版下线后旧版恢复投递，v2 积压保持保留）===')
    db_path = os.path.join(workdir, 'rollback.db')
    log_path = os.path.join(workdir, 'rollback.log')

    # 1) 新版运行期间产生 v2 积压。
    print(_run('version_bridge.demo.v2_producer', db_path, log_path,
               '--order', 'n-1', '--channel', 'web',
               '--order', 'n-2', '--channel', 'app'))

    # 2) 回退：旧版恢复投递 v1，旧消费者并行处理，v2 消息不得被取走。
    consumer = subprocess.Popen([
        sys.executable, '-I', '-m',
        'version_bridge.demo.v1_consumer',
        '--db', db_path, '--log', log_path,
        '--max-wait', '6', '--interval', '0.05'])
    print(_run('version_bridge.demo.v1_producer', db_path, log_path,
               '--order', 'n-3', '--order', 'n-4'))
    consumer.wait(timeout=30)
    assert consumer.returncode == 0

    events = read_events(log_path)
    v1_processed = events_of(events, 'v1-consumer', 'processed')
    assert sorted(e['order_id'] for e in v1_processed) == ['n-3', 'n-4'], \
        '回退后旧消费者只应处理旧版投递'
    exit_event = events_of(events, 'v1-consumer', 'exit')[-1]
    assert normalize_backlog(exit_event['backlog']) == {2: 2}, \
        'v2 积压必须原样保留，实际：%r' % exit_event['backlog']

    # 3) 新版重新上线：保留的 v2 与可能的残余 v1 都能处理。
    print(_run('version_bridge.demo.v2_consumer', db_path, log_path,
               '--max-wait', '2'))
    events = read_events(log_path)
    v2_processed = events_of(events, 'v2-consumer', 'processed')
    assert sorted(e['order_id'] for e in v2_processed) == ['n-1', 'n-2']
    assert {e['channel'] for e in v2_processed} == {'app', 'web'}
    print('  ✓ 回退期间 v2 的 n-1/n-2 保留，旧版 n-3/n-4 正常处理；'
          '新版再上线后清空保留项')


def scenario_default_format(workdir):
    print('\n=== 场景 3：原有默认消息格式（stock Huey 生产，互通性校验）===')
    db_path = os.path.join(workdir, 'default.db')
    log_path = os.path.join(workdir, 'default.log')

    # 1) v1 写入进程使用未改造的 stock Huey 投递。
    print(_run('version_bridge.demo.v1_producer', db_path, log_path,
               '--order', 'd-1', '--order', 'd-2'))

    # 2) 库中字节不得带任何信封魔数——与未接入本包的 Huey 完全一致。
    stock = SqliteHuey('orders', filename=db_path, results=False, utc=False)
    raw_items = [bytes(row) for row in stock.storage.enqueued_items()]
    assert len(raw_items) == 2
    assert all(not is_envelope(row) for row in raw_items), \
        'v1 投递必须是 Huey 原始默认字节，不得带信封'

    # 3) stock Huey 原生 dequeue() 能直接取走（证明默认格式未被破坏）。
    #    反序列化需要任务已注册，这里注册同一个共享任务类。
    from version_bridge.demo.v1_tasks import bind_v1_tasks
    bind_v1_tasks(stock)
    stock_task = stock.dequeue()
    assert stock_task is not None and stock_task.args == ('d-1',)

    # 4) 兼容入口也能处理剩余的原始格式消息，迁移钩子按 v1→v2 补齐字段。
    print(_run('version_bridge.demo.v2_consumer', db_path, log_path,
               '--max-wait', '2'))
    events = read_events(log_path)
    migrated = events_of(events, 'v2-consumer', 'migrated')
    processed = events_of(events, 'v2-consumer', 'processed')
    assert len(migrated) == 1 and migrated[0]['order_id'] == 'd-2', \
        'v2 读取 v1 默认格式消息时应触发迁移钩子'
    assert processed[0]['channel'] == 'legacy-default', \
        '迁移应补齐新版业务字段默认值'
    assert processed[0]['message_version'] == 1
    print('  ✓ v1 投递为裸 Huey 默认字节；stock dequeue 与兼容入口均可读取，'
          '迁移钩子补齐 channel')


SCENARIOS = {
    'upgrade': scenario_upgrade,
    'rollback': scenario_rollback,
    'default_format': scenario_default_format,
}


def main(argv=None):
    names = argv if argv else list(SCENARIOS)
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        raise SystemExit('未知场景: %s，可选: %s'
                         % (', '.join(unknown), ', '.join(SCENARIOS)))
    workdirs = []
    try:
        for name in names:
            workdir = new_workdir()
            workdirs.append(workdir)
            SCENARIOS[name](workdir)
    finally:
        for workdir in workdirs:
            shutil.rmtree(workdir, ignore_errors=True)
    print('\n全部场景通过：%s' % ', '.join(names))


if __name__ == '__main__':
    main()
