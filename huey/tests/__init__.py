# Keep native test fixtures isolated across concurrently running workspaces.
import atexit as _atexit
import os as _os
import shutil as _shutil
import tempfile as _tempfile
TEST_TMP_DIR = _tempfile.mkdtemp(prefix='huey-native-%s-' % _os.getpid())
_atexit.register(_shutil.rmtree, TEST_TMP_DIR, ignore_errors=True)

from huey.tests.test_api import *
from huey.tests.test_asyncio import *
from huey.tests.test_bulk_enqueue import *
from huey.tests.test_consumer import *
from huey.tests.test_crontab import *
from huey.tests.test_django_tasks import *
from huey.tests.test_helpers import *
from huey.tests.test_immediate import *
from huey.tests.test_mini import *
from huey.tests.test_postgres import *
from huey.tests.test_priority import *
from huey.tests.test_registry import *
from huey.tests.test_serializer import *
from huey.tests.test_signals import *
from huey.tests.test_sql_huey import *
from huey.tests.test_stats import *
from huey.tests.test_storage import *
from huey.tests.test_utils import *
from huey.tests.test_wrappers import *
