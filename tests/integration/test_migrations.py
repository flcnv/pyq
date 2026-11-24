from alembic import command
from sqlalchemy import URL

from tests.integration.conftest import alembic_config


def test_migrations_match_the_orm_models(database_url: URL) -> None:
    """Fails if a model changed without a migration (`alembic check`)."""
    command.check(alembic_config(database_url))


def test_migrations_downgrade_and_upgrade_cleanly(database_url: URL) -> None:
    config = alembic_config(database_url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")
