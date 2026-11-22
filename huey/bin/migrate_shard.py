#!/usr/bin/env python
"""Optional command-line entry for SQLite shard migration.

Example:
    python -m huey.bin.migrate_shard \\
        --source examples.shard_migration.huey_settings:source_huey \\
        --destination examples.shard_migration.huey_settings:dest_huey \\
        --selector examples.shard_migration.huey_settings:order_group_selector \\
        --migration-id orders-group-A-to-shard2 \\
        --batch-size 5

Producer compatibility: messages enqueued by Huey >=2.0.0,<4.0.0 use the same
``huey.registry.Message`` wire format this tool decodes (see
``huey.contrib.migrate.SUPPORTED_PRODUCER_SPEC``). Both Huey instances must
share Serializer settings (compression / secret / salt).
"""
import importlib
import logging
import optparse
import sys

from huey.contrib.migrate import SUPPORTED_PRODUCER_SPEC
from huey.contrib.migrate import ShardMigrator


def err(s):
    sys.stderr.write('\033[91m%s\033[0m\n' % s)


def get_options():
    parser = optparse.OptionParser(usage=__doc__)
    parser.add_option('--source', dest='source',
                      help='Import path of the source Huey instance '
                           '(module:attr or module containing `huey`).')
    parser.add_option('--destination', dest='destination',
                      help='Import path of the destination Huey instance.')
    parser.add_option('--selector', dest='selector',
                      help='Import path of a fn(Message)->bool used to pick '
                           'the migrating business group.')
    parser.add_option('--destination-serializer', dest='dst_serializer',
                      default=None,
                      help='Optional import path of the destination Huey '
                           'serializer (defaults to the source serializer).')
    parser.add_option('--key-selector', dest='key_selector', default=None,
                      help='Optional fn(kind, queue, key)->bool for '
                           'KV/counter rows.')
    parser.add_option('--migration-id', dest='migration_id', default='default',
                      help='Stable id identifying this logical migration; '
                           'resume state is keyed by it.')
    parser.add_option('--batch-size', dest='batch_size', default=200,
                      type='int', help='Rows per source transaction.')
    parser.add_option('--max-batches', dest='max_batches', default=None,
                      type='int', help='Stop after N batches per kind '
                                       '(useful for canary/partial moves).')
    parser.add_option('--kinds', dest='kinds', default='queue,schedule,kv,'
                                                       'counter',
                      help='Comma-separated subset of '
                           'queue,schedule,kv,counter.')
    parser.add_option('--dry-run', dest='dry_run', action='store_true',
                      default=False, help='Decode/select/count only.')
    parser.add_option('-v', '--verbose', dest='verbose', action='store_true',
                      default=False)
    options, args = parser.parse_args()
    if not (options.source and options.destination and options.selector):
        parser.error('--source, --destination and --selector are required.')
    return options


def resolve(path):
    """Resolve ``module:attr`` (preferred) or ``module.attr`` style paths."""
    if ':' in path:
        module_name, attr = path.split(':', 1)
    else:
        module_name, attr = path.rsplit('.', 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def main(argv=None):
    options = get_options()
    logging.basicConfig(
        level=logging.INFO if options.verbose else logging.WARNING,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    source_huey = resolve(options.source)
    dest_huey = resolve(options.destination)
    selector = resolve(options.selector)
    key_selector = (resolve(options.key_selector)
                    if options.key_selector else None)
    dst_serializer = (resolve(options.dst_serializer)
                      if options.dst_serializer else None)

    print('Huey shard migration (supported producer versions: %s)' %
          SUPPORTED_PRODUCER_SPEC)
    migrator = ShardMigrator(
        source_huey.storage, dest_huey.storage, selector,
        migration_id=options.migration_id,
        serializer=source_huey.serializer,
        destination_serializer=dst_serializer,
        batch_size=options.batch_size,
        kinds=tuple(k.strip() for k in options.kinds.split(',') if k.strip()),
        key_selector=key_selector)

    stats = migrator.migrate(max_batches=options.max_batches,
                             dry_run=options.dry_run)
    for progress in migrator.progress():
        print('  progress %-8s last_row=%-6d migrated=%d (updated %s)' %
              (progress.kind, progress.last_row_id, progress.migrated,
               progress.updated_at))
    print('  scanned=%d selected=%d migrated=%d skipped=%d conflicts=%d '
          'deserialization_errors=%d batches=%d dry_run=%s' %
          (stats.scanned, stats.selected, stats.migrated, stats.skipped,
           stats.conflicts, stats.deserialization_errors, stats.batches,
           options.dry_run))
    source_huey.storage.close()
    dest_huey.storage.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
