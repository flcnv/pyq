from dataclasses import dataclass, field

import pytest
from pydantic import SecretStr

from src.adapters.parsers.llm import LlmInvoiceParser
from src.adapters.parsers.unconfigured_llm import UnconfiguredLlmParser
from src.bootstrap import build_context
from src.config import Settings
from tests.unit.logs import app_logs

API_KEY = "sk-ant-test-secret"
POSTGRES_CREDENTIAL = "pg-test-secret"
REDIS_CREDENTIAL = "redis-test-secret"
WITH_KEY = Settings(
    anthropic_api_key=SecretStr(API_KEY),
    postgres_password=SecretStr(POSTGRES_CREDENTIAL),
    redis_password=SecretStr(REDIS_CREDENTIAL),
)


class BuildError(Exception):
    pass


class CloseError(Exception):
    pass


@dataclass
class Resources:
    """Stands in for the real clients: records which were created and closed, in order."""

    created: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    fail_to_create: str | None = None
    fail_to_close: set[str] = field(default_factory=set)

    def create(self, name: str) -> FakeClient:
        if name == self.fail_to_create:
            raise BuildError(f"creating {name} failed")
        self.created.append(name)
        return FakeClient(name, self)


class FakeClient:
    def __init__(self, name: str, resources: Resources) -> None:
        self._name = name
        self._resources = resources

    async def close(self) -> None:
        self._resources.closed.append(self._name)
        if self._name in self._resources.fail_to_close:
            raise CloseError(f"closing {self._name} failed")

    # The engine's, Redis' and Anthropic's close methods have different names.
    dispose = aclose = close


@pytest.fixture
def resources(monkeypatch: pytest.MonkeyPatch) -> Resources:
    resources = Resources()
    monkeypatch.setattr("src.bootstrap.create_engine", lambda url: resources.create("engine"))
    monkeypatch.setattr("src.bootstrap.create_session_factory", lambda engine: object())
    monkeypatch.setattr(
        "src.bootstrap.create_redis_client", lambda **kwargs: resources.create("redis")
    )
    monkeypatch.setattr(
        "src.bootstrap.AsyncAnthropic", lambda **kwargs: resources.create("anthropic")
    )
    return resources


async def test_closes_resources_in_reverse_order_of_creation(resources: Resources) -> None:
    context = await build_context(WITH_KEY)

    assert resources.created == ["engine", "redis", "anthropic"]
    assert resources.closed == []
    await context.aclose()
    assert resources.closed == ["anthropic", "redis", "engine"]


async def test_closing_twice_closes_once(resources: Resources) -> None:
    context = await build_context(WITH_KEY)

    await context.aclose()
    await context.aclose()

    assert resources.closed == ["anthropic", "redis", "engine"]


async def test_a_failure_while_building_closes_what_was_created(resources: Resources) -> None:
    resources.fail_to_create = "anthropic"

    with pytest.raises(BuildError):
        await build_context(WITH_KEY)

    assert resources.closed == ["redis", "engine"]


async def test_a_failing_cleanup_doesnt_hide_the_build_error(resources: Resources) -> None:
    resources.fail_to_create = "anthropic"
    resources.fail_to_close = {"redis"}

    with pytest.raises(BuildError):
        await build_context(WITH_KEY)

    assert resources.closed == ["redis", "engine"]


async def test_a_failing_cleanup_doesnt_stop_the_others(resources: Resources) -> None:
    resources.fail_to_close = {"redis"}
    context = await build_context(WITH_KEY)

    with app_logs() as logs:
        await context.aclose()

    assert resources.closed == ["anthropic", "redis", "engine"]
    [warning] = [line for line in logs.json() if line["level"] == "WARNING"]
    assert warning["resource"] == "redis"
    assert warning["error_type"] == "CloseError"
    assert "closing redis failed" not in str(warning)  # the type only, never the message


@pytest.mark.parametrize("api_key", [None, SecretStr("")], ids=["unset", "empty"])
async def test_without_an_api_key_there_is_no_anthropic_client(
    resources: Resources, api_key: SecretStr | None
) -> None:
    context = await build_context(Settings(anthropic_api_key=api_key))

    assert resources.created == ["engine", "redis"]
    assert isinstance(context.llm_parser, UnconfiguredLlmParser)
    await context.aclose()
    assert resources.closed == ["redis", "engine"]


async def test_with_an_api_key_the_llm_parser_is_claude(resources: Resources) -> None:
    context = await build_context(WITH_KEY)

    assert isinstance(context.llm_parser, LlmInvoiceParser)
    await context.aclose()


async def test_lifecycle_logs_carry_no_secrets(resources: Resources) -> None:
    with app_logs(level="DEBUG") as logs:
        context = await build_context(WITH_KEY)
        await context.aclose()

    [ready] = [line for line in logs.json() if line["message"] == "app context ready"]
    assert ready["llm_fallback_configured"] is True
    assert ready["extraction_fingerprint"]
    assert any(line["message"] == "app context closed" for line in logs.json())
    for secret in (API_KEY, POSTGRES_CREDENTIAL, REDIS_CREDENTIAL, "postgresql"):
        assert all(secret not in line for line in logs.lines)
