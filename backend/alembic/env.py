from logging.config import fileConfig

from alembic import context
from geoalchemy2 import Geometry

import app.models
from app.database.base import Base
from app.database.session import engine

config = context.config


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and reflected and name == "spatial_ref_sys":
        return False
    return True


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, Geometry):
        autogen_context.imports.add("import geoalchemy2.types")
    return False


if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=engine.url.render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
            render_item=render_item,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
