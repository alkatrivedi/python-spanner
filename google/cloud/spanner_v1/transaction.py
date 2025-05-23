# Copyright 2016 Google LLC All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Spanner read-write transaction support."""
import functools
import threading
from google.protobuf.struct_pb2 import Struct
from typing import Optional

from google.cloud.spanner_v1._helpers import (
    _make_value_pb,
    _merge_query_options,
    _metadata_with_prefix,
    _metadata_with_leader_aware_routing,
    _retry,
    _check_rst_stream_error,
    _merge_Transaction_Options,
)
from google.cloud.spanner_v1 import CommitRequest
from google.cloud.spanner_v1 import ExecuteBatchDmlRequest
from google.cloud.spanner_v1 import ExecuteSqlRequest
from google.cloud.spanner_v1 import TransactionSelector
from google.cloud.spanner_v1 import TransactionOptions
from google.cloud.spanner_v1._helpers import AtomicCounter
from google.cloud.spanner_v1.snapshot import _SnapshotBase
from google.cloud.spanner_v1.batch import _BatchBase
from google.cloud.spanner_v1._opentelemetry_tracing import add_span_event, trace_call
from google.cloud.spanner_v1 import RequestOptions
from google.cloud.spanner_v1.metrics.metrics_capture import MetricsCapture
from google.api_core import gapic_v1
from google.api_core.exceptions import InternalServerError
from dataclasses import dataclass, field
from typing import Any


class Transaction(_SnapshotBase, _BatchBase):
    """Implement read-write transaction semantics for a session.

    :type session: :class:`~google.cloud.spanner_v1.session.Session`
    :param session: the session used to perform the commit

    :raises ValueError: if session has an existing transaction
    """

    committed = None
    """Timestamp at which the transaction was successfully committed."""
    rolled_back = False
    commit_stats = None
    _multi_use = True
    _execute_sql_count = 0
    _lock = threading.Lock()
    _read_only = False
    exclude_txn_from_change_streams = False
    isolation_level = TransactionOptions.IsolationLevel.ISOLATION_LEVEL_UNSPECIFIED

    def __init__(self, session):
        if session._transaction is not None:
            raise ValueError("Session has existing transaction.")

        super(Transaction, self).__init__(session)

    def _check_state(self):
        """Helper for :meth:`commit` et al.

        :raises: :exc:`ValueError` if the object's state is invalid for making
                 API requests.
        """

        if self.committed is not None:
            raise ValueError("Transaction is already committed")

        if self.rolled_back:
            raise ValueError("Transaction is already rolled back")

    def _make_txn_selector(self):
        """Helper for :meth:`read`.

        :rtype:
            :class:`~.transaction_pb2.TransactionSelector`
        :returns: a selector configured for read-write transaction semantics.
        """
        self._check_state()

        if self._transaction_id is None:
            txn_options = TransactionOptions(
                read_write=TransactionOptions.ReadWrite(),
                exclude_txn_from_change_streams=self.exclude_txn_from_change_streams,
                isolation_level=self.isolation_level,
            )

            txn_options = _merge_Transaction_Options(
                self._session._database.default_transaction_options.default_read_write_transaction_options,
                txn_options,
            )
            return TransactionSelector(begin=txn_options)
        else:
            return TransactionSelector(id=self._transaction_id)

    def _execute_request(
        self,
        method,
        request,
        metadata,
        trace_name=None,
        session=None,
        attributes=None,
        observability_options=None,
    ):
        """Helper method to execute request after fetching transaction selector.

        :type method: callable
        :param method: function returning iterator

        :type request: proto
        :param request: request proto to call the method with
        """
        transaction = self._make_txn_selector()
        request.transaction = transaction
        with trace_call(
            trace_name,
            session,
            attributes,
            observability_options=observability_options,
            metadata=metadata,
        ), MetricsCapture():
            method = functools.partial(method, request=request)
            response = _retry(
                method,
                allowed_exceptions={InternalServerError: _check_rst_stream_error},
            )

        return response

    def begin(self):
        """Begin a transaction on the database.

        :rtype: bytes
        :returns: the ID for the newly-begun transaction.
        :raises ValueError:
            if the transaction is already begun, committed, or rolled back.
        """
        if self._transaction_id is not None:
            raise ValueError("Transaction already begun")

        if self.committed is not None:
            raise ValueError("Transaction already committed")

        if self.rolled_back:
            raise ValueError("Transaction is already rolled back")

        database = self._session._database
        api = database.spanner_api
        metadata = _metadata_with_prefix(database.name)
        if database._route_to_leader_enabled:
            metadata.append(
                _metadata_with_leader_aware_routing(database._route_to_leader_enabled)
            )
        txn_options = TransactionOptions(
            read_write=TransactionOptions.ReadWrite(),
            exclude_txn_from_change_streams=self.exclude_txn_from_change_streams,
            isolation_level=self.isolation_level,
        )
        txn_options = _merge_Transaction_Options(
            database.default_transaction_options.default_read_write_transaction_options,
            txn_options,
        )
        observability_options = getattr(database, "observability_options", None)
        with trace_call(
            f"CloudSpanner.{type(self).__name__}.begin",
            self._session,
            observability_options=observability_options,
            metadata=metadata,
        ) as span, MetricsCapture():
            attempt = AtomicCounter(0)
            nth_request = database._next_nth_request

            def wrapped_method(*args, **kwargs):
                method = functools.partial(
                    api.begin_transaction,
                    session=self._session.name,
                    options=txn_options,
                    metadata=database.metadata_with_request_id(
                        nth_request,
                        attempt.increment(),
                        metadata,
                        span,
                    ),
                )
                return method(*args, **kwargs)

            def beforeNextRetry(nthRetry, delayInSeconds):
                add_span_event(
                    span,
                    "Transaction Begin Attempt Failed. Retrying",
                    {"attempt": nthRetry, "sleep_seconds": delayInSeconds},
                )

            response = _retry(
                wrapped_method,
                allowed_exceptions={InternalServerError: _check_rst_stream_error},
                beforeNextRetry=beforeNextRetry,
            )
        self._transaction_id = response.id
        return self._transaction_id

    def rollback(self):
        """Roll back a transaction on the database."""
        self._check_state()

        if self._transaction_id is not None:
            database = self._session._database
            api = database.spanner_api
            metadata = _metadata_with_prefix(database.name)
            if database._route_to_leader_enabled:
                metadata.append(
                    _metadata_with_leader_aware_routing(
                        database._route_to_leader_enabled
                    )
                )

            observability_options = getattr(database, "observability_options", None)
            with trace_call(
                f"CloudSpanner.{type(self).__name__}.rollback",
                self._session,
                observability_options=observability_options,
                metadata=metadata,
            ) as span, MetricsCapture():
                attempt = AtomicCounter(0)
                nth_request = database._next_nth_request

                def wrapped_method(*args, **kwargs):
                    attempt.increment()
                    method = functools.partial(
                        api.rollback,
                        session=self._session.name,
                        transaction_id=self._transaction_id,
                        metadata=database.metadata_with_request_id(
                            nth_request,
                            attempt.value,
                            metadata,
                            span,
                        ),
                    )
                    return method(*args, **kwargs)

                _retry(
                    wrapped_method,
                    allowed_exceptions={InternalServerError: _check_rst_stream_error},
                )

        self.rolled_back = True
        del self._session._transaction

    def commit(
        self, return_commit_stats=False, request_options=None, max_commit_delay=None
    ):
        """Commit mutations to the database.

        :type return_commit_stats: bool
        :param return_commit_stats:
          If true, the response will return commit stats which can be accessed though commit_stats.

        :type request_options:
            :class:`google.cloud.spanner_v1.types.RequestOptions`
        :param request_options:
                (Optional) Common options for this request.
                If a dict is provided, it must be of the same form as the protobuf
                message :class:`~google.cloud.spanner_v1.types.RequestOptions`.

        :type max_commit_delay: :class:`datetime.timedelta`
        :param max_commit_delay:
                (Optional) The amount of latency this request is willing to incur
                in order to improve throughput.
                :class:`~google.cloud.spanner_v1.types.MaxCommitDelay`.

        :rtype: datetime
        :returns: timestamp of the committed changes.
        :raises ValueError: if there are no mutations to commit.
        """
        database = self._session._database
        trace_attributes = {"num_mutations": len(self._mutations)}
        observability_options = getattr(database, "observability_options", None)
        api = database.spanner_api
        metadata = _metadata_with_prefix(database.name)
        if database._route_to_leader_enabled:
            metadata.append(
                _metadata_with_leader_aware_routing(database._route_to_leader_enabled)
            )
        with trace_call(
            f"CloudSpanner.{type(self).__name__}.commit",
            self._session,
            trace_attributes,
            observability_options,
            metadata=metadata,
        ) as span, MetricsCapture():
            self._check_state()
            if self._transaction_id is None and len(self._mutations) > 0:
                self.begin()
            elif self._transaction_id is None and len(self._mutations) == 0:
                raise ValueError("Transaction is not begun")

            if request_options is None:
                request_options = RequestOptions()
            elif type(request_options) is dict:
                request_options = RequestOptions(request_options)
            if self.transaction_tag is not None:
                request_options.transaction_tag = self.transaction_tag

            # Request tags are not supported for commit requests.
            request_options.request_tag = None

            request = CommitRequest(
                session=self._session.name,
                mutations=self._mutations,
                transaction_id=self._transaction_id,
                return_commit_stats=return_commit_stats,
                max_commit_delay=max_commit_delay,
                request_options=request_options,
            )

            add_span_event(span, "Starting Commit")

            attempt = AtomicCounter(0)
            nth_request = database._next_nth_request

            def wrapped_method(*args, **kwargs):
                attempt.increment()
                method = functools.partial(
                    api.commit,
                    request=request,
                    metadata=database.metadata_with_request_id(
                        nth_request,
                        attempt.value,
                        metadata,
                        span,
                    ),
                )
                return method(*args, **kwargs)

            def beforeNextRetry(nthRetry, delayInSeconds):
                add_span_event(
                    span,
                    "Transaction Commit Attempt Failed. Retrying",
                    {"attempt": nthRetry, "sleep_seconds": delayInSeconds},
                )

            response = _retry(
                wrapped_method,
                allowed_exceptions={InternalServerError: _check_rst_stream_error},
                beforeNextRetry=beforeNextRetry,
            )

            add_span_event(span, "Commit Done")

        self.committed = response.commit_timestamp
        if return_commit_stats:
            self.commit_stats = response.commit_stats
        del self._session._transaction
        return self.committed

    @staticmethod
    def _make_params_pb(params, param_types):
        """Helper for :meth:`execute_update`.

        :type params: dict, {str -> column value}
        :param params: values for parameter replacement.  Keys must match
                       the names used in ``dml``.

        :type param_types: dict[str -> Union[dict, .types.Type]]
        :param param_types:
            (Optional) maps explicit types for one or more param values;
            required if parameters are passed.

        :rtype: Union[None, :class:`Struct`]
        :returns: a struct message for the passed params, or None
        :raises ValueError:
            If ``param_types`` is None but ``params`` is not None.
        :raises ValueError:
            If ``params`` is None but ``param_types`` is not None.
        """
        if params:
            return Struct(
                fields={key: _make_value_pb(value) for key, value in params.items()}
            )

        return {}

    def execute_update(
        self,
        dml,
        params=None,
        param_types=None,
        query_mode=None,
        query_options=None,
        request_options=None,
        last_statement=False,
        *,
        retry=gapic_v1.method.DEFAULT,
        timeout=gapic_v1.method.DEFAULT,
    ):
        """Perform an ``ExecuteSql`` API request with DML.

        :type dml: str
        :param dml: SQL DML statement

        :type params: dict, {str -> column value}
        :param params: values for parameter replacement.  Keys must match
                       the names used in ``dml``.

        :type param_types: dict[str -> Union[dict, .types.Type]]
        :param param_types:
            (Optional) maps explicit types for one or more param values;
            required if parameters are passed.

        :type query_mode:
            :class:`~google.cloud.spanner_v1.types.ExecuteSqlRequest.QueryMode`
        :param query_mode: Mode governing return of results / query plan.
            See:
            `QueryMode <https://cloud.google.com/spanner/reference/rpc/google.spanner.v1#google.spanner.v1.ExecuteSqlRequest.QueryMode>`_.

        :type query_options:
            :class:`~google.cloud.spanner_v1.types.ExecuteSqlRequest.QueryOptions`
            or :class:`dict`
        :param query_options: (Optional) Options that are provided for query plan stability.

        :type request_options:
            :class:`google.cloud.spanner_v1.types.RequestOptions`
        :param request_options:
                (Optional) Common options for this request.
                If a dict is provided, it must be of the same form as the protobuf
                message :class:`~google.cloud.spanner_v1.types.RequestOptions`.

        :type last_statement: bool
        :param last_statement:
                If set to true, this option marks the end of the transaction. The
                transaction should be committed or aborted after this statement
                executes, and attempts to execute any other requests against this
                transaction (including reads and queries) will be rejected. Mixing
                mutations with statements that are marked as the last statement is
                not allowed.
                For DML statements, setting this option may cause some error
                reporting to be deferred until commit time (e.g. validation of
                unique constraints). Given this, successful execution of a DML
                statement should not be assumed until the transaction commits.

        :type retry: :class:`~google.api_core.retry.Retry`
        :param retry: (Optional) The retry settings for this request.

        :type timeout: float
        :param timeout: (Optional) The timeout for this request.

        :rtype: int
        :returns: Count of rows affected by the DML statement.
        """
        params_pb = self._make_params_pb(params, param_types)
        database = self._session._database
        metadata = _metadata_with_prefix(database.name)
        if database._route_to_leader_enabled:
            metadata.append(
                _metadata_with_leader_aware_routing(database._route_to_leader_enabled)
            )
        api = database.spanner_api

        seqno, self._execute_sql_count = (
            self._execute_sql_count,
            self._execute_sql_count + 1,
        )

        # Query-level options have higher precedence than client-level and
        # environment-level options
        default_query_options = database._instance._client._query_options
        query_options = _merge_query_options(default_query_options, query_options)
        observability_options = getattr(
            database._instance._client, "observability_options", None
        )

        if request_options is None:
            request_options = RequestOptions()
        elif type(request_options) is dict:
            request_options = RequestOptions(request_options)
        request_options.transaction_tag = self.transaction_tag

        trace_attributes = {"db.statement": dml}

        request = ExecuteSqlRequest(
            session=self._session.name,
            sql=dml,
            params=params_pb,
            param_types=param_types,
            query_mode=query_mode,
            query_options=query_options,
            seqno=seqno,
            request_options=request_options,
            last_statement=last_statement,
        )

        nth_request = database._next_nth_request
        attempt = AtomicCounter(0)

        def wrapped_method(*args, **kwargs):
            attempt.increment()
            method = functools.partial(
                api.execute_sql,
                request=request,
                metadata=database.metadata_with_request_id(
                    nth_request, attempt.value, metadata
                ),
                retry=retry,
                timeout=timeout,
            )
            return method(*args, **kwargs)

        if self._transaction_id is None:
            # lock is added to handle the inline begin for first rpc
            with self._lock:
                response = self._execute_request(
                    wrapped_method,
                    request,
                    metadata,
                    f"CloudSpanner.{type(self).__name__}.execute_update",
                    self._session,
                    trace_attributes,
                    observability_options=observability_options,
                )
                # Setting the transaction id because the transaction begin was inlined for first rpc.
                if (
                    self._transaction_id is None
                    and response is not None
                    and response.metadata is not None
                    and response.metadata.transaction is not None
                ):
                    self._transaction_id = response.metadata.transaction.id
        else:
            response = self._execute_request(
                wrapped_method,
                request,
                metadata,
                f"CloudSpanner.{type(self).__name__}.execute_update",
                self._session,
                trace_attributes,
                observability_options=observability_options,
            )

        return response.stats.row_count_exact

    def batch_update(
        self,
        statements,
        request_options=None,
        last_statement=False,
        *,
        retry=gapic_v1.method.DEFAULT,
        timeout=gapic_v1.method.DEFAULT,
    ):
        """Perform a batch of DML statements via an ``ExecuteBatchDml`` request.

        :type statements:
            Sequence[Union[ str, Tuple[str, Dict[str, Any], Dict[str, Union[dict, .types.Type]]]]]

        :param statements:
            List of DML statements, with optional params / param types.
            If passed, 'params' is a dict mapping names to the values
            for parameter replacement.  Keys must match the names used in the
            corresponding DML statement.  If 'params' is passed, 'param_types'
            must also be passed, as a dict mapping names to the type of
            value passed in 'params'.

        :type request_options:
            :class:`google.cloud.spanner_v1.types.RequestOptions`
        :param request_options:
                (Optional) Common options for this request.
                If a dict is provided, it must be of the same form as the protobuf
                message :class:`~google.cloud.spanner_v1.types.RequestOptions`.

        :type last_statement: bool
        :param last_statement:
                If set to true, this option marks the end of the transaction. The
                transaction should be committed or aborted after this statement
                executes, and attempts to execute any other requests against this
                transaction (including reads and queries) will be rejected. Mixing
                mutations with statements that are marked as the last statement is
                not allowed.
                For DML statements, setting this option may cause some error
                reporting to be deferred until commit time (e.g. validation of
                unique constraints). Given this, successful execution of a DML
                statement should not be assumed until the transaction commits.

        :type retry: :class:`~google.api_core.retry.Retry`
        :param retry: (Optional) The retry settings for this request.

        :type timeout: float
        :param timeout: (Optional) The timeout for this request.

        :rtype:
            Tuple(status, Sequence[int])
        :returns:
            Status code, plus counts of rows affected by each completed DML
            statement.  Note that if the status code is not ``OK``, the
            statement triggering the error will not have an entry in the
            list, nor will any statements following that one.
        """
        parsed = []
        for statement in statements:
            if isinstance(statement, str):
                parsed.append(ExecuteBatchDmlRequest.Statement(sql=statement))
            else:
                dml, params, param_types = statement
                params_pb = self._make_params_pb(params, param_types)
                parsed.append(
                    ExecuteBatchDmlRequest.Statement(
                        sql=dml, params=params_pb, param_types=param_types
                    )
                )

        database = self._session._database
        metadata = _metadata_with_prefix(database.name)
        if database._route_to_leader_enabled:
            metadata.append(
                _metadata_with_leader_aware_routing(database._route_to_leader_enabled)
            )
        api = database.spanner_api
        observability_options = getattr(database, "observability_options", None)

        seqno, self._execute_sql_count = (
            self._execute_sql_count,
            self._execute_sql_count + 1,
        )

        if request_options is None:
            request_options = RequestOptions()
        elif type(request_options) is dict:
            request_options = RequestOptions(request_options)
        request_options.transaction_tag = self.transaction_tag

        trace_attributes = {
            # Get just the queries from the DML statement batch
            "db.statement": ";".join([statement.sql for statement in parsed])
        }
        request = ExecuteBatchDmlRequest(
            session=self._session.name,
            statements=parsed,
            seqno=seqno,
            request_options=request_options,
            last_statements=last_statement,
        )

        nth_request = database._next_nth_request
        attempt = AtomicCounter(0)

        def wrapped_method(*args, **kwargs):
            attempt.increment()
            method = functools.partial(
                api.execute_batch_dml,
                request=request,
                metadata=database.metadata_with_request_id(
                    nth_request, attempt.value, metadata
                ),
                retry=retry,
                timeout=timeout,
            )
            return method(*args, **kwargs)

        if self._transaction_id is None:
            # lock is added to handle the inline begin for first rpc
            with self._lock:
                response = self._execute_request(
                    wrapped_method,
                    request,
                    metadata,
                    "CloudSpanner.DMLTransaction",
                    self._session,
                    trace_attributes,
                    observability_options=observability_options,
                )
                # Setting the transaction id because the transaction begin was inlined for first rpc.
                for result_set in response.result_sets:
                    if (
                        self._transaction_id is None
                        and result_set.metadata is not None
                        and result_set.metadata.transaction is not None
                    ):
                        self._transaction_id = result_set.metadata.transaction.id
                        break
        else:
            response = self._execute_request(
                wrapped_method,
                request,
                metadata,
                "CloudSpanner.DMLTransaction",
                self._session,
                trace_attributes,
                observability_options=observability_options,
            )

        row_counts = [
            result_set.stats.row_count_exact for result_set in response.result_sets
        ]

        return response.status, row_counts

    def __enter__(self):
        """Begin ``with`` block."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """End ``with`` block."""
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


@dataclass
class BatchTransactionId:
    transaction_id: str
    session_id: str
    read_timestamp: Any


@dataclass
class DefaultTransactionOptions:
    isolation_level: str = TransactionOptions.IsolationLevel.ISOLATION_LEVEL_UNSPECIFIED
    _defaultReadWriteTransactionOptions: Optional[TransactionOptions] = field(
        init=False, repr=False
    )

    def __post_init__(self):
        """Initialize _defaultReadWriteTransactionOptions automatically"""
        self._defaultReadWriteTransactionOptions = TransactionOptions(
            isolation_level=self.isolation_level
        )

    @property
    def default_read_write_transaction_options(self) -> TransactionOptions:
        """Public accessor for _defaultReadWriteTransactionOptions"""
        return self._defaultReadWriteTransactionOptions
