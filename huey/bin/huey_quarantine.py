#!/usr/bin/env python
"""Inspect, replay or discard quarantined Huey messages.

Usage:

    python -m huey.bin.huey_quarantine path.to.huey_instance [command] [id]
"""
import sys

from huey.quarantine import main


if __name__ == '__main__':
    sys.exit(main())
