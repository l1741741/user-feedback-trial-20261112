WORKER_THREAD = 'thread'
WORKER_GREENLET = 'greenlet'
WORKER_PROCESS = 'process'
WORKER_TYPES = (WORKER_THREAD, WORKER_GREENLET, WORKER_PROCESS)

# Error-callback (on_error) trigger policies:
# * always - run the error callback on every surfaced failure, including
#   attempts that will be retried.
# * final - run the error callback only once the task will not be retried.
ERROR_POLICY_ALWAYS = 'always'
ERROR_POLICY_FINAL = 'final'
ERROR_POLICIES = (ERROR_POLICY_ALWAYS, ERROR_POLICY_FINAL)


class EmptyData(object):
    pass
