import asyncio
from functools import wraps
from typing import Any, Callable, List, Optional, SupportsInt, Tuple, TypeVar, Union

import re
import urllib.parse
import aioodbc
import pyodbc
from pypika import MSSQLQuery

from tortoise.backends.base.client import (
    BaseDBAsyncClient,
    BaseTransactionWrapper,
    Capabilities,
    ConnectionWrapper,
    NestedTransactionPooledContext,
    PoolConnectionWrapper,
    TransactionContext,
    TransactionContextPooled,
)
from tortoise.backends.mssql.executor import MSSQLExecutor
from tortoise.backends.mssql.schema_generator import MSSQLSchemaGenerator

from tortoise.exceptions import (
    DBConnectionError,
    IntegrityError,
    OperationalError,
    TransactionManagementError,
)

import os
from concurrent.futures import ThreadPoolExecutor

FuncType = Callable[..., Any]
F = TypeVar("F", bound=FuncType)


def translate_exceptions(func: F) -> F:
    @wraps(func)
    async def translate_exceptions_(self, *args):
        try:
            return await func(self, *args)
        except (
                pyodbc.OperationalError,
                pyodbc.ProgrammingError,
                pyodbc.DataError,
                pyodbc.InternalError,
                pyodbc.NotSupportedError,
        ) as exc:
            raise OperationalError(exc)
        except  pyodbc.IntegrityError as exc:
            raise IntegrityError(exc)

    return translate_exceptions_  # type: ignore


_g_thread_executor = ThreadPoolExecutor(os.cpu_count() * 5)


class MSSqlClient(BaseDBAsyncClient):
    query_class = MSSQLQuery
    executor_class = MSSQLExecutor
    schema_generator = MSSQLSchemaGenerator
    capabilities = Capabilities("mssql", requires_limit=True, inline_comment=True)

    def __init__(
            self,
            *,
            user: str,
            password: str,
            database: str,
            host: str,
            port: SupportsInt,
            **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        self.user = user
        self.password = password
        self.database = database
        self.host = host
        self.port = int(port)  # make sure port is int type
        self.extra = kwargs.copy()
        self.storage_engine = self.extra.pop("storage_engine", "")
        self.extra.pop("connection_name", None)
        self.extra.pop("fetch_inserted", None)
        self.extra.pop("db", None)
        self.extra.pop("autocommit", None)
        self.extra.setdefault("sql_mode", "STRICT_TRANS_TABLES")
        self.charset = self.extra.pop("charset", "utf8mb4")
        self.pool_minsize = int(self.extra.pop("minsize", 1))
        self.pool_maxsize = int(self.extra.pop("maxsize", 5))
        self.pool_recycle = int(self.extra.pop("pool_recycle", 5))
        self.timeout = int(self.extra.pop("timeout", 5))

        dsn = self.extra.pop("dsn", "")
        if dsn:
            self.dsn = urllib.parse.unquote(dsn)
        else:
            self.dsn = "DSN=MYMSSQL;DATABASE={DB_DATABASE};UID={DB_USER};PWD={DB_PASSWORD}".format(
                DB_DATABASE=database, DB_USER=user, DB_PASSWORD=password
            )
        self._template: dict = {}
        self._pool: Optional[aioodbc.Pool] = None
        self._connection = None

    async def create_connection(self, with_db: bool) -> None:
        self._template = {
            "dsn": self.dsn,
            "minsize": self.pool_minsize,
            "maxsize": self.pool_maxsize,
            "executor": _g_thread_executor,
            "pool_recycle": self.pool_recycle,
            "timeout": self.timeout,
        }
        try:

            self._pool = await aioodbc.create_pool(**self._template)

            if isinstance(self._pool, aioodbc.Pool):
                async with self.acquire_connection() as connection:
                    async with connection.cursor() as cursor:
                        if self.storage_engine:
                            await cursor.execute(
                                f"SET default_storage_engine='{self.storage_engine}';"
                            )
                            if self.storage_engine.lower() != "innodb":  # pragma: nobranch
                                self.capabilities.__dict__["supports_transactions"] = False

            self.log.debug("Created connection %s pool with params: %s", self._pool, self._template)
        except pyodbc.OperationalError as e:
            raise DBConnectionError(f"Can't connect to MySQL server: {self._template}")

    async def _expire_connections(self) -> None:
        if self._pool:  # pragma: nobranch
            for conn in self._pool._free:
                conn._reader.set_exception(EOFError("EOF"))

    async def _close(self) -> None:
        if self._pool:  # pragma: nobranch
            self._pool.close()
            await self._pool.wait_closed()
            self.log.debug("Closed connection %s with params: %s", self._connection, self._template)
            self._pool = None

    async def close(self) -> None:
        await self._close()
        self._template.clear()

    async def db_create(self) -> None:
        await self.create_connection(with_db=False)
        await self.execute_script(f"CREATE DATABASE {self.database}")
        await self.close()

    async def db_delete(self) -> None:
        await self.create_connection(with_db=False)

        try:
            await self.execute_script(f"DROP DATABASE {self.database}")
        except pyodbc.DatabaseError:  # pragma: nocoverage
            pass
        await self.close()

    def acquire_connection(self) -> Union["ConnectionWrapper", "PoolConnectionWrapper"]:
        return PoolConnectionWrapper(self._pool)

    def _in_transaction(self) -> "TransactionContext":
        return TransactionContextPooled(TransactionWrapper(self))

    @translate_exceptions
    async def execute_insert(self, query: str, values: list) -> int:
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            async with connection.cursor() as cursor:
                await cursor.execute(query, values)
                if "output inserted" in query:
                    row = await cursor.fetchone()
                    return row[0]
                # return cursor.lastrowid  # return auto-generated id

    @translate_exceptions
    async def execute_many(self, query: str, values: list) -> None:
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            async with connection.cursor() as cursor:
                if self.capabilities.supports_transactions:
                    await connection.begin()
                    try:
                        await cursor.executemany(query, values)
                    except Exception:
                        await connection.rollback()
                        raise
                    else:
                        await connection.commit()
                else:
                    await cursor.executemany(query, values)

    @translate_exceptions
    async def execute_query(
            self, query: str, values: Optional[list] = None
    ) -> Tuple[int, List[dict]]:
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            async with connection.cursor() as cursor:
                if not values:
                    values = tuple()
                await cursor.execute(query, values)
                if "UPDATE" in query:
                    return cursor.rowcount, []
                rows = await cursor.fetchall()
                if rows:
                    return cursor.rowcount, [dict(zip([l[0].lower() for l in row.cursor_description], row)) for row in
                                             rows]
                return cursor.rowcount, []

    async def execute_query_dict(self, query: str, values: Optional[list] = None) -> List[dict]:
        return (await self.execute_query(query, values))[1]

    @translate_exceptions
    async def execute_script(self, query: str) -> None:
        async with self.acquire_connection() as connection:
            self.log.debug(query)
            async with connection.cursor() as cursor:
                await cursor.execute(query)


class TransactionWrapper(MSSqlClient, BaseTransactionWrapper):
    def __init__(self, connection: MSSqlClient) -> None:
        self.connection_name = connection.connection_name
        self._connection: aioodbc.Connection = connection._connection
        self._lock = asyncio.Lock()
        self._trxlock = asyncio.Lock()
        self.log = connection.log
        self._finalized: Optional[bool] = None
        self.fetch_inserted = connection.fetch_inserted
        self._parent = connection

    def _in_transaction(self) -> "TransactionContext":
        return NestedTransactionPooledContext(self)

    def acquire_connection(self) -> ConnectionWrapper:
        return ConnectionWrapper(self._connection, self._lock)

    @translate_exceptions
    async def execute_many(self, query: str, values: list) -> None:
        async with self.acquire_connection() as connection:
            self.log.debug("%s: %s", query, values)
            async with connection.cursor() as cursor:
                await cursor.executemany(query, values)

    @translate_exceptions
    async def start(self) -> None:
        await self._connection.begin()
        self._finalized = False

    async def commit(self) -> None:
        if self._finalized:
            raise TransactionManagementError("Transaction already finalised")
        await self._connection.commit()
        self._finalized = True

    async def rollback(self) -> None:
        if self._finalized:
            raise TransactionManagementError("Transaction already finalised")
        await self._connection.rollback()
        self._finalized = True
