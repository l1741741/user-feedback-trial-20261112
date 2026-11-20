"""Standalone entry-point for the local completion-event receiver.

Run as::

    python -m huey.completion_receiver app.module.huey_instance [--once]

Implemented as a separate module so ``python -m huey.completion`` does not
trigger the package-already-imported warning from ``huey/__init__`` importing
``huey.completion`` for its constants.
"""
import sys

from huey.completion import receiver_main


if __name__ == '__main__':
    sys.exit(receiver_main())
