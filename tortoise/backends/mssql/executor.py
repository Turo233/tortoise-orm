from pypika import Parameter, functions
from pypika.enums import SqlTypes
from pypika.terms import Criterion
from typing import Sequence
from tortoise import Model
import re
from tortoise.backends.base.executor import BaseExecutor
from tortoise.fields import BigIntField, IntField, SmallIntField
from tortoise.filters import (
    Like,
    Term,
    ValueWrapper,
    contains,
    ends_with,
    format_quotes,
    insensitive_contains,
    insensitive_ends_with,
    insensitive_exact,
    insensitive_starts_with,
    starts_with,
)


class StrWrapper(ValueWrapper):  # type: ignore
    """
    Naive str wrapper that doesn't use the monkey-patched pypika ValueWraper for MySQL
    """

    def get_value_sql(self, **kwargs):
        quote_char = kwargs.get("secondary_quote_char") or ""
        value = self.value.replace(quote_char, quote_char * 2)
        return format_quotes(value, quote_char)


def escape_like(val: str) -> str:
    return val.replace("\\", "\\\\\\\\").replace("%", "\\%").replace("_", "\\_")


def mssql_contains(field: Term, value: str) -> Criterion:
    return Like(
        functions.Cast(field, SqlTypes.CHAR), StrWrapper(f"%{escape_like(value)}%"), escape=""
    )


def mssql_starts_with(field: Term, value: str) -> Criterion:
    return Like(
        functions.Cast(field, SqlTypes.CHAR), StrWrapper(f"{escape_like(value)}%"), escape=""
    )


def mssql_ends_with(field: Term, value: str) -> Criterion:
    return Like(
        functions.Cast(field, SqlTypes.CHAR), StrWrapper(f"%{escape_like(value)}"), escape=""
    )


def mssql_insensitive_exact(field: Term, value: str) -> Criterion:
    return functions.Upper(functions.Cast(field, SqlTypes.CHAR)).eq(functions.Upper(str(value)))


def mssql_insensitive_contains(field: Term, value: str) -> Criterion:
    return Like(
        functions.Upper(functions.Cast(field, SqlTypes.CHAR)),
        functions.Upper(StrWrapper(f"%{escape_like(value)}%")),
        escape="",
    )


def mssql_insensitive_starts_with(field: Term, value: str) -> Criterion:
    return Like(
        functions.Upper(functions.Cast(field, SqlTypes.CHAR)),
        functions.Upper(StrWrapper(f"{escape_like(value)}%")),
        escape="",
    )


def mssql_insensitive_ends_with(field: Term, value: str) -> Criterion:
    return Like(
        functions.Upper(functions.Cast(field, SqlTypes.CHAR)),
        functions.Upper(StrWrapper(f"%{escape_like(value)}")),
        escape="",
    )


class MSSQLExecutor(BaseExecutor):
    FILTER_FUNC_OVERRIDE = {
        contains: mssql_contains,
        starts_with: mssql_starts_with,
        ends_with: mssql_ends_with,
        insensitive_exact: mssql_insensitive_exact,
        insensitive_contains: mssql_insensitive_contains,
        insensitive_starts_with: mssql_insensitive_starts_with,
        insensitive_ends_with: mssql_insensitive_ends_with,
    }
    EXPLAIN_PREFIX = "EXPLAIN FORMAT=JSON"

    def parameter(self, pos: int) -> Parameter:
        return Parameter("?")

    async def _process_insert_result(self, instance: Model, results: int) -> None:
        pk_field_object = self.model._meta.pk
        if (
            isinstance(pk_field_object, (SmallIntField, IntField, BigIntField))
            and pk_field_object.generated
        ):
            instance.pk = results

    def _prepare_insert_statement(self, columns: Sequence[str], has_generated: bool = True) -> str:
        # Insert should implement returning new id to saved object
        # Each db has it's own methods for it, so each implementation should
        # go to descendant executors
        query = str(
            self.db.query_class.into(self.model._meta.basetable)
            .columns(*columns)
            .insert(*[self.parameter(i) for i in range(len(columns))])
        )
        pk_field_object = self.model._meta.pk
        if pk_field_object.generated == True:
            query = re.sub(r'VALUES', "output inserted.%s VALUES" % pk_field_object.model_field_name, query)
        return query