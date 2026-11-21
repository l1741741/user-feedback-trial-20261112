"""Shared settings for the task-rename migration demo."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = (os.environ.get('HUEY_ALIAS_DB') or
           os.path.join(BASE_DIR, 'alias_demo.db'))
STATE_DIR = os.environ.get('HUEY_ALIAS_STATE') or os.path.join(
    BASE_DIR, 'alias_demo_state')

QUEUE_NAME = 'reports'

# The old publisher's task lives in tasks_old.py, so its canonical queue name
# is "tasks_old.generate_report".
OLD_NAME = 'tasks_old.generate_report'
# The renamed task lives in tasks_new.py.
NEW_NAME = 'tasks_new.generate_report'

RETRIES = 2
RETRY_DELAY = 1
