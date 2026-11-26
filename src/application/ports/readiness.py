from collections.abc import Awaitable, Callable

# Returns when the database answers; raises `StorageUnavailableError` when it doesn't.
type ReadinessCheck = Callable[[], Awaitable[None]]
