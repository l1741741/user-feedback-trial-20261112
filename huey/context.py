"""
显式的发布上下文（publish context）。

发布上下文把请求关联 ID（correlation id）与租户标识（tenant）等 *受控* 元数据
附加在任务消息上，随序列化一起进入队列（SQLite / Redis / ...），在消费者进程里
原样还原，不依赖发布进程中的任何全局变量。

设计要点：

* :class:`PublishContext` 是不可变的值对象，只接受字符串等受控类型与有界大小，
  非法输入在构造时即抛出 :class:`ContextValidationError`，因此任务还没有进入队列
  就会被拒绝（fail fast，发布前拒绝）。
* :data:`current_context` 基于 :mod:`contextvars`，按线程 / 协程隔离。任务执行
  前绑定该任务自带的上下文，执行结束（包括业务抛错）后通过 token 恢复原值，同一
  工作线程连续执行不同租户的任务不会残留。
* :class:`ContextLogFilter` 把上下文字段安全地注入日志记录，格式化阶段的任何
  异常都被吞掉，绝不会阻断任务收尾，也不会污染其它记录。

注意：本模块与 ``@huey.task(context=True)`` 的 ``context`` 参数完全无关。后者的
语义是“把 Task 实例本身作为 ``task`` 关键字参数传给任务函数”，保持原样不变。
"""
import contextvars
import logging
import re
import uuid


__all__ = (
    'ContextValidationError',
    'PublishContext',
    'current_context',
    'get_current_context',
    'publish_context',
    'ContextLogFilter',
    'NO_CONTEXT',
)


# 有界的结构与大小限制，防止把队列消息变成不受控的大对象。
MAX_CORRELATION_ID_LENGTH = 128
MAX_TENANT_LENGTH = 64
MAX_FIELDS = 16
MAX_FIELD_KEY_LENGTH = 64
MAX_FIELD_VALUE_LENGTH = 256
MAX_SERIALIZED_SIZE = 4096

# 绝不允许进入日志上下文的字段名（小写匹配）。
SENSITIVE_KEY_PARTS = (
    'password', 'passwd', 'secret', 'token', 'authorization', 'auth',
    'api_key', 'apikey', 'cookie', 'credential', 'private_key',
)

# 形如 "Authorization: Bearer ..."、"Bearer xxx"、"Basic xxx" 的原始凭证。
_AUTH_HEADER_RE = re.compile(
    r'^\s*(authorization\s*:|bearer|basic|digest)\s+', re.IGNORECASE)

NO_CONTEXT = '-'


class ContextValidationError(ValueError):
    """发布上下文不满足受控结构 / 大小约束。"""


def _validate_string(value, label, max_length, allow_empty=False):
    if not isinstance(value, str):
        raise ContextValidationError(
            '%s must be a string, got %r' % (label, type(value).__name__))
    if not allow_empty and not value:
        raise ContextValidationError('%s must not be empty' % label)
    if len(value) > max_length:
        raise ContextValidationError(
            '%s is too long (max %d characters, got %d)' % (
                label, max_length, len(value)))
    return value


def _is_sensitive_key(key):
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _validate_fields(fields):
    if fields is None:
        return {}
    if not isinstance(fields, dict):
        raise ContextValidationError(
            'context fields must be a dict, got %r' % type(fields).__name__)
    if len(fields) > MAX_FIELDS:
        raise ContextValidationError(
            'too many context fields (max %d, got %d)' % (
                MAX_FIELDS, len(fields)))

    validated = {}
    for key, value in fields.items():
        if not isinstance(key, str):
            raise ContextValidationError(
                'context field keys must be strings, got %r' %
                type(key).__name__)
        if not key:
            raise ContextValidationError('context field keys must not be empty')
        if len(key) > MAX_FIELD_KEY_LENGTH:
            raise ContextValidationError(
                'context field key %r is too long (max %d characters)' % (
                    key, MAX_FIELD_KEY_LENGTH))
        if _is_sensitive_key(key):
            raise ContextValidationError(
                'context field %r looks sensitive and is not allowed in the '
                'logging context' % key)
        if isinstance(value, bool):
            # bool 是 int 的子类，先单独处理，统一转成字符串。
            value = 'true' if value else 'false'
        elif isinstance(value, (int, float)):
            value = str(value)
        elif not isinstance(value, str):
            raise ContextValidationError(
                'context field %r must contain a string, number or boolean, '
                'got %r (arbitrary objects are not allowed)' % (
                    key, type(value).__name__))
        if len(value) > MAX_FIELD_VALUE_LENGTH:
            raise ContextValidationError(
                'context field %r value is too long (max %d characters)' % (
                    key, MAX_FIELD_VALUE_LENGTH))
        if _AUTH_HEADER_RE.match(value):
            raise ContextValidationError(
                'context field %r contains a raw Authorization header and is '
                'not allowed in the logging context' % key)
        validated[key] = value
    return validated


class PublishContext(object):
    """
    随任务消息一起发布的不可变上下文。

    :param str tenant: 租户标识，必填、非空、有界。
    :param str correlation_id: 请求关联 ID。省略时自动生成一个 UUID，便于在日志
        中把整条调用链串起来。
    :param dict fields: 可选的、扁平的 ``str -> str|int|float|bool`` 附加字段，
        条目数与单值长度均有上限，敏感键名（password / authorization / token ...）
        与原始 Authorization 头一律拒绝。
    :raises ContextValidationError: 任何参数不满足受控约束时抛出，调用方应在发布
        任务之前处理，任务不会被放入队列。
    """
    __slots__ = ('correlation_id', 'tenant', '_fields', '_serialized_size')

    def __init__(self, tenant, correlation_id=None, fields=None):
        tenant = _validate_string(
            tenant, 'tenant', MAX_TENANT_LENGTH, allow_empty=False)
        if correlation_id is None:
            correlation_id = uuid.uuid4().hex
        correlation_id = _validate_string(
            correlation_id, 'correlation_id', MAX_CORRELATION_ID_LENGTH,
            allow_empty=False)
        fields = _validate_fields(fields)

        # 最后一道总量防线：避免字段名很多时累计体积失控。
        wire = (correlation_id, tenant, fields)
        size = len(repr(wire))
        if size > MAX_SERIALIZED_SIZE:
            raise ContextValidationError(
                'serialized context is too large (max %d, got %d)' % (
                    MAX_SERIALIZED_SIZE, size))

        object.__setattr__(self, 'tenant', tenant)
        object.__setattr__(self, 'correlation_id', correlation_id)
        object.__setattr__(self, '_fields', fields)
        object.__setattr__(self, '_serialized_size', size)

    @property
    def fields(self):
        # 返回副本，调用方无法修改不可变对象内部状态。
        return dict(self._fields)

    def to_dict(self):
        """供日志 / 展示使用的安全字典（只含受控字符串）。"""
        data = {'correlation_id': self.correlation_id, 'tenant': self.tenant}
        data.update(self._fields)
        return data

    def to_wire(self):
        """转成可被默认 pickle 序列化的普通元组结构。"""
        return (self.correlation_id, self.tenant, dict(self._fields))

    @classmethod
    def from_wire(cls, data):
        """
        从消息中的数据还原上下文。

        消费端收到的是经过序列化的数据，不能假定它仍然可信：这里重新执行一遍
        完整校验，损坏 / 越界的消息会在任务执行前抛出
        :class:`ContextValidationError`。
        """
        if isinstance(data, PublishContext):
            return data
        if not isinstance(data, tuple) or len(data) != 3:
            raise ContextValidationError(
                'invalid serialized publish context: %r' % (data,))
        correlation_id, tenant, fields = data
        return cls(tenant=tenant,
                   correlation_id=correlation_id,
                   fields=fields)

    def __setattr__(self, name, value):
        raise AttributeError('PublishContext is immutable')

    def __repr__(self):
        return 'PublishContext(tenant=%r, correlation_id=%r, fields=%r)' % (
            self.tenant, self.correlation_id, self._fields)

    def __eq__(self, rhs):
        if not isinstance(rhs, PublishContext):
            return NotImplemented
        return (self.tenant == rhs.tenant and
                self.correlation_id == rhs.correlation_id and
                self._fields == rhs._fields)

    def __ne__(self, rhs):
        result = self.__eq__(rhs)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.tenant, self.correlation_id,
                     tuple(sorted(self._fields.items()))))

    def __reduce__(self):
        # Pickling goes through __init__, so an unpickled context is rebuilt
        # and re-validated instead of restoring raw slot state.
        return (PublishContext,
                (self.tenant, self.correlation_id, dict(self._fields)))


# 按线程 / 协程隔离的当前上下文。任务执行期间绑定为任务自带的上下文，
# 发布进程中也可以通过 publish_context() 临时设置“环境上下文”。
current_context = contextvars.ContextVar('huey_publish_context', default=None)


def get_current_context():
    """返回当前正在执行（或发布）的 :class:`PublishContext`，没有则为 ``None``。"""
    return current_context.get()


class publish_context(object):
    """
    临时设置当前发布上下文的上下文管理器::

        with publish_context(PublishContext(tenant='acme', correlation_id='r1')):
            order_task(order_id)  # 自动携带该上下文

    使用 ContextVar token 恢复，支持嵌套；退出时精确还原进入前的值（包括
    ``None``），不会把外层上下文残留给后续代码。也可作为装饰器使用。
    """
    def __init__(self, context):
        if not isinstance(context, PublishContext):
            raise ContextValidationError(
                'publish_context() expects a PublishContext instance, got %r' %
                type(context).__name__)
        self.context = context
        self._token = None

    def __enter__(self):
        self._token = current_context.set(self.context)
        return self.context

    def __exit__(self, exc_type, exc_val, exc_tb):
        current_context.reset(self._token)
        return False

    def __call__(self, func):
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)
        wrapper.__name__ = getattr(func, '__name__', 'wrapper')
        wrapper.__doc__ = getattr(func, '__doc__', None)
        return wrapper


class ContextLogFilter(logging.Filter):
    """
    把当前 :class:`PublishContext` 注入每条日志记录。

    注入的属性：``correlation_id``、``tenant`` 以及上下文中的附加字段，无上下文
    时使用 :data:`NO_CONTEXT` 占位。可在格式化字符串中使用，例如::

        %(asctime)s %(levelname)s [tenant=%(tenant)s cid=%(correlation_id)s] %(message)s

    整个过滤器对异常免疫：日志格式化或注入过程中的任何错误都不会向外传播，保证
    日志问题永远不能阻断任务收尾，也不会让后续任务的记录贴上错误的关联 ID。
    """
    def filter(self, record):
        try:
            context = get_current_context()
            if context is None:
                record.correlation_id = NO_CONTEXT
                record.tenant = NO_CONTEXT
                return True
            record.correlation_id = context.correlation_id
            record.tenant = context.tenant
            for key, value in context.fields.items():
                # 不覆盖 LogRecord 内置属性，避免破坏日志框架自身的行为。
                if not hasattr(record, key):
                    setattr(record, key, value)
        except Exception:
            # 日志绝不能影响任务执行。
            try:
                record.correlation_id = getattr(
                    record, 'correlation_id', NO_CONTEXT)
                record.tenant = getattr(record, 'tenant', NO_CONTEXT)
            except Exception:
                pass
        return True
