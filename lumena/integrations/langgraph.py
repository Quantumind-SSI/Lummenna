"""LangGraph integration for Lumena.

Provides ``LumenaCheckpointSaver`` — implements LangGraph's
``BaseCheckpointSaver`` protocol so Lumena can persist graph state
sovereignly, and ``LumenaGraphStore`` for cross-thread long-term memory.

Usage::

    from langgraph.graph import StateGraph
    from lumena.integrations.langgraph import LumenaCheckpointSaver

    graph = StateGraph(...)
    ...
    checkpointer = LumenaCheckpointSaver()
    app = graph.compile(checkpointer=checkpointer)
    app.invoke({"input": "hello"}, config={"configurable": {"thread_id": "t1"}})
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

from lumena.config import LumenaConfig
from lumena.data.schema import get_connection
from lumena.logging import get_console_logger

logger = get_console_logger(__name__)

# ---------------------------------------------------------------------------
# Conditional LangGraph imports
# ---------------------------------------------------------------------------
try:
    from langgraph.checkpoint.base import (
        BaseCheckpointSaver,
        Checkpoint,
        CheckpointMetadata,
        CheckpointTuple,
    )
except Exception:
    from collections import namedtuple

    BaseCheckpointSaver = None  # type: ignore[assignment,misc]
    Checkpoint = Any  # type: ignore[misc]
    CheckpointMetadata = Any  # type: ignore[misc]
    CheckpointTuple = namedtuple(  # type: ignore[misc]
        "CheckpointTuple",
        ["config", "checkpoint", "metadata", "parent_config", "pending_writes"],
    )

_LumenaSaverBase = BaseCheckpointSaver if BaseCheckpointSaver is not None else object


# ---------------------------------------------------------------------------
# LumenaCheckpointSaver
# ---------------------------------------------------------------------------
class LumenaCheckpointSaver(_LumenaSaverBase):
    """Lumena-backed checkpoint saver for LangGraph.

    Stores LangGraph checkpoints as structured memories in the SQLite palace.
    Each ``thread_id`` gets its own locus inside a dedicated ``langgraph`` room,
    providing natural isolation and thread-scoped retrieval.

    When ``langgraph`` is installed (``pip install lumena[langgraph]``), this
    class extends ``BaseCheckpointSaver`` for native compatibility.  When
    ``langgraph`` is absent it falls back to a standalone mode that still
    exposes the same API surface.
    """

    ROOM_NAME = "langgraph"
    SOURCE_TYPE = "agent_reasoning"

    def __init__(
        self,
        config: LumenaConfig | None = None,
        room: str = ROOM_NAME,
    ) -> None:
        self.config = config or LumenaConfig()
        self.room = room
        self._conn: Any = None
        self._room_id: int | None = None

    # -----------------------------------------------------------------------
    # Connection helpers
    # -----------------------------------------------------------------------
    @property
    def conn(self) -> Any:
        if self._conn is None:
            self._conn = get_connection(self.config)
            self._ensure_room()
        return self._conn

    def _ensure_room(self) -> int:
        if self._room_id is not None:
            return self._room_id
        conn = self.conn
        row = conn.execute(
            "SELECT room_id FROM room WHERE name = ?", (self.room,)
        ).fetchone()
        if row:
            self._room_id = row[0]
        else:
            cur = conn.execute(
                "INSERT INTO room(name, room_type) VALUES (?, 'ephemeral')",
                (self.room,),
            )
            self._room_id = cur.lastrowid
            conn.commit()
        return self._room_id

    def _resolve_locus(self, thread_id: str) -> int:
        room_id = self._ensure_room()
        row = self._conn.execute(
            "SELECT locus_id FROM locus WHERE room_id = ? AND name = ?",
            (room_id, thread_id),
        ).fetchone()
        if row:
            return row[0]
        cur = self._conn.execute(
            "INSERT INTO locus(room_id, name) VALUES (?, ?)",
            (room_id, thread_id),
        )
        self._conn.commit()
        return cur.lastrowid

    def _thread_id(self, config: dict[str, Any]) -> str:
        return str(
            config.get("configurable", {}).get("thread_id", "default")
        )

    def _checkpoint_id(self, config: dict[str, Any]) -> str | None:
        return config.get("configurable", {}).get("checkpoint_id")

    # -----------------------------------------------------------------------
    # Serialization
    # -----------------------------------------------------------------------
    @staticmethod
    def _serialize(
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        parent_config: dict[str, Any] | None,
        pending_writes: list[tuple[str, str, Any]] | None,
    ) -> str:
        payload = {
            "checkpoint": checkpoint,
            "metadata": metadata,
            "parent_config": parent_config,
            "pending_writes": pending_writes or [],
        }
        return json.dumps(payload, default=str)

    @staticmethod
    def _deserialize(content: str) -> dict[str, Any]:
        return json.loads(content)

    # -----------------------------------------------------------------------
    # Core protocol (sync)
    # -----------------------------------------------------------------------
    def get_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        """Retrieve the latest checkpoint tuple for *config*."""
        thread_id = self._thread_id(config)
        locus_id = self._resolve_locus(thread_id)
        checkpoint_id = self._checkpoint_id(config)

        if checkpoint_id:
            row = self.conn.execute(
                """SELECT content, created_at
                   FROM chunk
                   WHERE locus_id = ?
                     AND valid_to IS NULL
                     AND content_hash = ?
                   ORDER BY created_at DESC, chunk_id DESC
                   LIMIT 1""",
                (locus_id, checkpoint_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                """SELECT content, created_at
                   FROM chunk
                   WHERE locus_id = ? AND valid_to IS NULL
                   ORDER BY created_at DESC, chunk_id DESC
                   LIMIT 1""",
                (locus_id,),
            ).fetchone()

        if not row:
            return None

        data = self._deserialize(row["content"])
        checkpoint = data["checkpoint"]
        return CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=data["metadata"],
            parent_config=data.get("parent_config"),
            pending_writes=self._load_pending_writes(
                thread_id, checkpoint.get("id") if isinstance(checkpoint, dict) else None
            ),
        )

    def put(
        self,
        config: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist a checkpoint and return an updated config."""
        thread_id = self._thread_id(config)
        locus_id = self._resolve_locus(thread_id)
        room_id = self._ensure_room()
        checkpoint_id = checkpoint.get("id", "")

        parent_config = None
        if config.get("configurable", {}).get("checkpoint_id"):
            parent_config = {
                "configurable": {
                    **config.get("configurable", {}),
                    "checkpoint_ns": "",
                }
            }

        payload = self._serialize(checkpoint, metadata, parent_config, [])
        content_hash = hashlib.sha256(
            (checkpoint_id + thread_id).encode()
        ).hexdigest()[:32]

        self.conn.execute(
            """INSERT INTO chunk
               (locus_id, room_id, content, content_hash,
                vm_score, resolution, provenance_root)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                locus_id,
                room_id,
                payload,
                checkpoint_id or content_hash,
                0.5,
                "FP32",
                None,
            ),
        )
        self.conn.commit()

        logger.info(
                "langgraph_checkpoint_saved",
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
                step=metadata.get("step") if isinstance(metadata, dict) else None,
            )

        return {
            **config,
            "configurable": {
                **config.get("configurable", {}),
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
            },
        }

    def put_writes(
        self,
        config: dict[str, Any],
        writes: list[tuple[str, Any]],
        task_id: str = "",
        task_path: str = "",
    ) -> None:
        """Store pending writes for a thread/checkpoint.

        Signature matches ``BaseCheckpointSaver.put_writes(config, writes,
        task_id, task_path)`` so LangGraph's positional invocation works.

        Writes are stored as ephemeral chunks in a ``writes`` locus under the
        same room, tagged with the checkpoint_id from *config* so that
        ``get_tuple``/``list`` can return them as ``pending_writes``.
        """
        if not writes:
            return
        thread_id = self._thread_id(config)
        checkpoint_id = self._checkpoint_id(config) or ""
        room_id = self._ensure_room()
        write_locus = self._resolve_locus(f"{thread_id}__writes")

        for channel, value in writes:
            payload = json.dumps(
                {
                    "thread_id": thread_id,
                    "checkpoint_id": checkpoint_id,
                    "channel": channel,
                    "value": value,
                    "task_id": task_id,
                    "task_path": task_path,
                },
                default=str,
            )
            self.conn.execute(
                """INSERT INTO chunk
                   (locus_id, room_id, content, content_hash,
                    vm_score, resolution)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (write_locus, room_id, payload, "lg_write", 0.1, "FP32"),
            )
        self.conn.commit()

    def _load_pending_writes(
        self, thread_id: str, checkpoint_id: str | None
    ) -> list[tuple[str, str, Any]]:
        """Return pending writes for a checkpoint as (task_id, channel, value).

        Writes tagged with a checkpoint_id are returned only for that
        checkpoint; untagged writes (legacy rows, or writes stored before the
        first checkpoint existed) are returned for any checkpoint of the
        thread.
        """
        write_locus = self._resolve_locus(f"{thread_id}__writes")
        rows = self.conn.execute(
            """SELECT content FROM chunk
               WHERE locus_id = ? AND valid_to IS NULL AND content_hash = 'lg_write'
               ORDER BY created_at ASC, chunk_id ASC""",
            (write_locus,),
        ).fetchall()
        out: list[tuple[str, str, Any]] = []
        for row in rows:
            try:
                data = json.loads(row["content"])
            except (ValueError, TypeError):
                continue
            row_cid = data.get("checkpoint_id", "")
            if row_cid and checkpoint_id and row_cid != checkpoint_id:
                continue
            out.append((data.get("task_id", ""), data.get("channel", ""), data.get("value")))
        return out

    def _checkpoint_row_position(self, locus_id: int, checkpoint_id: str) -> tuple | None:
        """Return (created_at, chunk_id) of a stored checkpoint, or None."""
        row = self.conn.execute(
            """SELECT created_at, chunk_id FROM chunk
               WHERE locus_id = ? AND valid_to IS NULL AND content_hash = ?
               ORDER BY created_at DESC, chunk_id DESC LIMIT 1""",
            (locus_id, checkpoint_id),
        ).fetchone()
        return (row["created_at"], row["chunk_id"]) if row else None

    def list(
        self,
        config: dict[str, Any],
        *,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
        filter: dict[str, Any] | None = None,
    ) -> Iterator[CheckpointTuple]:
        """Yield checkpoint tuples for *thread_id*, newest first.

        ``before`` (a config whose ``configurable.checkpoint_id`` names a
        stored checkpoint) restricts results to checkpoints created strictly
        before that checkpoint.
        """
        thread_id = self._thread_id(config)
        locus_id = self._resolve_locus(thread_id)

        # Optional filter on checkpoint metadata source/step
        where_extra = ""
        params: list[Any] = [locus_id]

        if filter:
            # Very basic filter support: exact match on metadata fields.
            # The JSON path is passed as a bound parameter — never
            # interpolated into the SQL — so hostile filter keys cannot
            # inject SQL.
            for key, val in filter.items():
                where_extra += " AND json_extract(content, ?) = ?"
                params.append(f"$.metadata.{key}")
                params.append(val)

        if before is not None:
            before_id = self._checkpoint_id(before)
            if before_id:
                pos = self._checkpoint_row_position(locus_id, before_id)
                if pos is None:
                    return  # unknown cursor: nothing is strictly before it
                where_extra += (
                    " AND (created_at < ? OR (created_at = ? AND chunk_id < ?))"
                )
                params.extend([pos[0], pos[0], pos[1]])

        # where_extra contains only fixed, parameterised clauses built above.
        sql = f"""SELECT content
                   FROM chunk
                   WHERE locus_id = ? AND valid_to IS NULL
                   {where_extra}
                   ORDER BY created_at DESC, chunk_id DESC
                   LIMIT ?"""  # nosec B608
        params.append(limit or 100)

        for row in self.conn.execute(sql, params):
            data = self._deserialize(row["content"])
            checkpoint = data["checkpoint"]
            yield CheckpointTuple(
                config=config,
                checkpoint=checkpoint,
                metadata=data["metadata"],
                parent_config=data.get("parent_config"),
                pending_writes=self._load_pending_writes(
                    thread_id, checkpoint.get("id") if isinstance(checkpoint, dict) else None
                ),
            )

    # -----------------------------------------------------------------------
    # Async methods
    # -----------------------------------------------------------------------
    async def aget_tuple(self, config: dict[str, Any]) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: list[tuple[str, Any]],
        task_id: str = "",
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def alist(
        self,
        config: dict[str, Any],
        *,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
        filter: dict[str, Any] | None = None,
    ):
        for item in self.list(config, before=before, limit=limit, filter=filter):
            yield item


# ---------------------------------------------------------------------------
# LumenaGraphStore — cross-thread long-term memory for LangGraph
# ---------------------------------------------------------------------------
try:
    from langgraph.store.base import BaseStore as LangGraphBaseStore
except Exception:
    LangGraphBaseStore = None  # type: ignore[assignment,misc]

_LumenaGraphStoreBase = LangGraphBaseStore if LangGraphBaseStore is not None else object


class LumenaGraphStore(_LumenaGraphStoreBase):
    """Lumena-backed store for LangGraph cross-thread memory.

    LangGraph's ``Store`` API lets graphs read and write long-term facts
    that persist across conversation threads.  LumenaGraphStore maps each
    ``namespace`` to a room and each ``key`` to a chunked memory.
    """

    def __init__(
        self,
        config: LumenaConfig | None = None,
        default_namespace: str = "memories",
        embedder: Any = None,
    ) -> None:
        self.config = config or LumenaConfig()
        self.default_namespace = default_namespace
        self._embedder = embedder
        self._conn: Any = None

    @property
    def conn(self) -> Any:
        if self._conn is None:
            self._conn = get_connection(self.config)
        return self._conn

    def _embedding(self, text: str) -> Any:
        if self._embedder is not None:
            return self._embedder.encode_single(text)
        from lumena.force.contextual.embed import get_embedder

        # Raises ModelNotAvailableError unless mock embeddings were
        # explicitly opted into (config.allow_mock_embedder — testing only).
        self._embedder = get_embedder(
            self.config, allow_mock=self.config.allow_mock_embedder
        )
        return self._embedder.encode_single(text)

    def _namespace_to_room(self, namespace: tuple[str, ...] | str) -> str:
        if isinstance(namespace, tuple):
            return "_".join(namespace) or self.default_namespace
        return namespace or self.default_namespace

    def get(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        refresh_ttl: bool | None = None,
    ) -> dict[str, Any] | None:
        """Fetch the latest value for *key* in *namespace*.

        ``refresh_ttl`` is accepted for BaseStore signature compatibility and
        ignored — Lumena manages lifecycle via V(m) decay, not per-item TTLs.

        Note: this convenience method returns a plain dict. Callers using the
        LangGraph ``BaseStore`` contract (``batch``/``abatch``/``aget``)
        receive proper ``Item`` objects via ``_batch_get``.
        """
        room = self._namespace_to_room(namespace)
        row = self.conn.execute(
            """SELECT c.content, c.created_at, c.vm_score
               FROM chunk c
               JOIN locus l ON l.locus_id = c.locus_id
               JOIN room r ON r.room_id = c.room_id
               WHERE r.name = ? AND l.name = ? AND c.valid_to IS NULL
               ORDER BY c.created_at DESC
               LIMIT 1""",
            (room, key),
        ).fetchone()
        if not row:
            return None
        return {
            "key": key,
            "namespace": namespace,
            "value": row["content"],
            "created_at": row["created_at"],
            "score": row["vm_score"],
        }

    def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: Any,
        index: Any = None,
        *,
        ttl: Any = None,
    ) -> None:
        """Store *value* under *key* in *namespace*.

        ``index`` and ``ttl`` are accepted for BaseStore signature
        compatibility. Custom index fields are not supported (the whole value
        is embedded); per-item TTLs are not supported (Lumena uses V(m) decay
        and L3 eviction). Both are logged if provided rather than silently
        ignored.
        """
        from lumena.force.mnemonic.store import store_memory

        if index is not None:
            logger.debug("lumena_graph_store_index_unsupported", index=str(index))
        if ttl is not None:
            logger.debug("lumena_graph_store_ttl_unsupported", ttl=str(ttl))

        room = self._namespace_to_room(namespace)
        content = json.dumps({"key": key, "value": value}, default=str)
        emb = self._embedding(content)
        store_memory(
            self.conn,
            content=content,
            room_name=room,
            locus_name=key,
            embedding=emb,
            config=self.config,
        )

    def delete(
        self,
        namespace: tuple[str, ...],
        key: str,
    ) -> None:
        room = self._namespace_to_room(namespace)
        self.conn.execute(
            """UPDATE chunk
               SET valid_to = unixepoch()
               WHERE room_id = (SELECT room_id FROM room WHERE name = ?)
                 AND locus_id = (SELECT locus_id FROM locus WHERE room_id = (SELECT room_id FROM room WHERE name = ?) AND name = ?)
                 AND valid_to IS NULL""",
            (room, room, key),
        )
        self.conn.commit()

    def search(
        self,
        namespace: tuple[str, ...],
        query: str | None = None,
        *,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
        refresh_ttl: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Search within a namespace.

        With *query*, runs Lumena's semantic pipeline; without it, returns
        the most recent items in the namespace. ``filter`` does exact-match
        filtering on top-level keys of the stored value dict. ``refresh_ttl``
        is accepted for BaseStore signature compatibility and ignored.
        """
        room = self._namespace_to_room(namespace)
        # Ensure room exists so SearchPipeline can find chunks
        self.conn.execute(
            "INSERT OR IGNORE INTO room(name, room_type) VALUES (?, 'ephemeral')",
            (room,),
        )
        self.conn.commit()

        if query is None:
            rows = self.conn.execute(
                """SELECT l.name AS key, c.content, c.vm_score
                   FROM chunk c
                   JOIN locus l ON l.locus_id = c.locus_id
                   JOIN room r ON r.room_id = c.room_id
                   WHERE r.name = ? AND c.valid_to IS NULL
                   ORDER BY c.created_at DESC, c.chunk_id DESC""",
                (room,),
            ).fetchall()
            candidates = [
                {
                    "key": row["key"],
                    "namespace": namespace,
                    "value": row["content"],
                    "score": row["vm_score"],
                }
                for row in rows
            ]
        else:
            from lumena.search import SearchPipeline

            embedder_obj = self._embedder
            if embedder_obj is None:
                from lumena.force.contextual.embed import get_embedder

                # Raises ModelNotAvailableError unless mock embeddings were
                # explicitly opted into (config.allow_mock_embedder — testing
                # only).
                embedder_obj = get_embedder(
                    self.config, allow_mock=self.config.allow_mock_embedder
                )
                self._embedder = embedder_obj

            pipeline = SearchPipeline(self.conn, self.config, embedder=embedder_obj)
            results = pipeline.execute(query, k=limit + offset)
            candidates = [
                {
                    "key": r.locus_name,
                    "namespace": namespace,
                    "value": r.content,
                    "score": r.final_score,
                }
                for r in results
                if r.room_name == room
            ]

        if filter:
            filtered = []
            for cand in candidates:
                try:
                    value = json.loads(cand["value"])
                    stored = value.get("value") if isinstance(value, dict) else None
                except (ValueError, TypeError):
                    stored = None
                if isinstance(stored, dict) and all(
                    stored.get(k) == v for k, v in filter.items()
                ):
                    filtered.append(cand)
            candidates = filtered

        return candidates[offset : offset + limit]

    def list_namespaces(
        self,
        *,
        prefix: tuple[str, ...] | None = None,
        suffix: tuple[str, ...] | None = None,
        max_depth: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[str, ...]]:
        """Return namespaces (rooms) that match prefix/suffix filters."""
        rows = self.conn.execute(
            "SELECT name FROM room WHERE name LIKE ?",
            (f"{self.default_namespace}%",),
        ).fetchall()
        namespaces = []
        for row in rows:
            name = row["name"]
            ns = tuple(name.split("_"))
            if prefix and ns[: len(prefix)] != prefix:
                continue
            if suffix and ns[-len(suffix) :] != suffix:
                continue
            if max_depth is not None and len(ns) > max_depth:
                continue
            namespaces.append(ns)
        return namespaces[offset : offset + limit]

    # -----------------------------------------------------------------------
    # BaseStore batch API (abstract in langgraph >= 0.6)
    # -----------------------------------------------------------------------
    def batch(self, ops: Iterable[Any]) -> list[Any]:
        """Execute a batch of store operations sequentially.

        Dispatches each ``GetOp`` / ``PutOp`` / ``SearchOp`` /
        ``ListNamespacesOp`` to the corresponding store method and returns
        one result per operation, in order, as required by the LangGraph
        ``BaseStore`` contract.
        """
        from langgraph.store.base import GetOp, ListNamespacesOp, PutOp, SearchOp

        results: list[Any] = []
        for op in ops:
            if isinstance(op, GetOp):
                results.append(self._batch_get(op))
            elif isinstance(op, PutOp):
                if op.value is None:
                    self.delete(op.namespace, op.key)
                else:
                    self.put(op.namespace, op.key, op.value)
                results.append(None)
            elif isinstance(op, SearchOp):
                results.append(self._batch_search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(self._batch_list_namespaces(op))
            else:
                raise TypeError(f"Unsupported store operation: {type(op).__name__}")
        return results

    async def abatch(self, ops: Iterable[Any]) -> list[Any]:
        """Async variant of :meth:`batch` (SQLite work runs synchronously)."""
        return self.batch(ops)

    @staticmethod
    def _parse_value(content: Any) -> dict[str, Any]:
        """Convert stored chunk content into the dict LangGraph items require."""
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return {"value": content}
        if isinstance(data, dict) and set(data) == {"key", "value"}:
            inner = data["value"]
            return inner if isinstance(inner, dict) else {"value": inner}
        return data if isinstance(data, dict) else {"value": data}

    def _created_at(self, namespace: tuple[str, ...], key: str) -> datetime:
        """Fetch a chunk's creation time (UTC); falls back to 'now' if absent."""
        room = self._namespace_to_room(namespace)
        row = self.conn.execute(
            """SELECT c.created_at FROM chunk c
               JOIN locus l ON l.locus_id = c.locus_id
               JOIN room r ON r.room_id = c.room_id
               WHERE r.name = ? AND l.name = ? AND c.valid_to IS NULL
               ORDER BY c.created_at DESC LIMIT 1""",
            (room, key),
        ).fetchone()
        if row and row["created_at"] is not None:
            return datetime.fromtimestamp(row["created_at"], tz=timezone.utc)
        return datetime.now(tz=timezone.utc)

    def _batch_get(self, op: Any) -> Any:
        from langgraph.store.base import Item

        found = self.get(op.namespace, op.key)
        if found is None:
            return None
        created = datetime.fromtimestamp(found["created_at"], tz=timezone.utc)
        return Item(
            value=self._parse_value(found["value"]),
            key=op.key,
            namespace=op.namespace,
            created_at=created,
            updated_at=created,
        )

    def _batch_search(self, op: Any) -> list[Any]:
        from langgraph.store.base import SearchItem

        namespace = op.namespace_prefix
        if op.query:
            hits = self.search(namespace, op.query, limit=op.limit + op.offset)
            candidates = [
                (h["key"], h["value"], h.get("score")) for h in hits
            ]
        else:
            room = self._namespace_to_room(namespace)
            rows = self.conn.execute(
                """SELECT l.name AS key, c.content, c.created_at FROM chunk c
                   JOIN locus l ON l.locus_id = c.locus_id
                   JOIN room r ON r.room_id = c.room_id
                   WHERE r.name = ? AND c.valid_to IS NULL
                   ORDER BY c.created_at DESC""",
                (room,),
            ).fetchall()
            candidates = [(row["key"], row["content"], None) for row in rows]

        items: list[Any] = []
        for key, content, score in candidates:
            value = self._parse_value(content)
            if op.filter and any(value.get(fk) != fv for fk, fv in op.filter.items()):
                continue
            # One timestamp lookup per result; bounded by op.limit (<= ~10s
            # of rows on a local SQLite connection).
            created = self._created_at(namespace, key)
            items.append(
                SearchItem(
                    namespace=namespace,
                    key=key,
                    value=value,
                    created_at=created,
                    updated_at=created,
                    score=score,
                )
            )
        return items[op.offset : op.offset + op.limit]

    def _batch_list_namespaces(self, op: Any) -> list[tuple[str, ...]]:
        prefix: tuple[str, ...] | None = None
        suffix: tuple[str, ...] | None = None
        for cond in op.match_conditions or ():
            # Wildcard segments ("*") are not supported by the room-name
            # mapping; treat them as an unconstrained match on that side.
            path = tuple(str(p) for p in cond.path)
            if "*" in path:
                continue
            if cond.match_type == "prefix":
                prefix = path
            elif cond.match_type == "suffix":
                suffix = path
        namespaces = self.list_namespaces(
            prefix=prefix, suffix=suffix, max_depth=op.max_depth
        )
        return namespaces[op.offset : op.offset + op.limit]
