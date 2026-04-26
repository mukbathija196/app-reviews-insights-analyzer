"""SQLite cache for raw reviews, normalized reviews, embeddings, and fetch cursors."""

from __future__ import annotations

import sqlite3
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pulse.ingestion.base import RawReview, Review


class ReviewStore:
    """Manages the local SQLite cache for the ingestion and reasoning layers."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def migrate(self) -> None:
        """Apply schema migrations."""
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS raw_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_id TEXT NOT NULL,
                    product TEXT NOT NULL,
                    source TEXT NOT NULL,
                    native_id TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    title TEXT,
                    body TEXT NOT NULL,
                    lang TEXT NOT NULL,
                    country TEXT NOT NULL,
                    posted_at TEXT NOT NULL,
                    app_version TEXT,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    UNIQUE(source, product, native_id, content_hash)
                );

                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    product TEXT NOT NULL,
                    source TEXT NOT NULL,
                    native_id TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    title TEXT,
                    body TEXT NOT NULL,
                    lang TEXT NOT NULL,
                    country TEXT NOT NULL,
                    posted_at TEXT NOT NULL,
                    app_version TEXT,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    truncated INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS fetch_cursors (
                    product TEXT NOT NULL,
                    source TEXT NOT NULL,
                    cursor_value TEXT,
                    etag TEXT,
                    last_modified TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(product, source)
                );

                CREATE TABLE IF NOT EXISTS embeddings (
                    review_id TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(review_id, model_name)
                );

                CREATE INDEX IF NOT EXISTS idx_reviews_product_source
                    ON reviews (product, source);
                CREATE INDEX IF NOT EXISTS idx_reviews_posted_at
                    ON reviews (posted_at);
                CREATE INDEX IF NOT EXISTS idx_raw_reviews_product_source
                    ON raw_reviews (product, source);
                CREATE INDEX IF NOT EXISTS idx_embeddings_model
                    ON embeddings (model_name);
                """
            )

    def upsert_raw_review(self, raw: RawReview) -> bool:
        """Insert a raw review snapshot if this content_hash is unseen."""
        review_id = f"{raw.source}:{raw.product}:{raw.native_id}"
        content_hash = _content_hash(raw.title, raw.body)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO raw_reviews (
                    review_id, product, source, native_id, rating, title, body, lang,
                    country, posted_at, app_version, fetched_at, content_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    raw.product,
                    raw.source,
                    raw.native_id,
                    raw.rating,
                    raw.title,
                    raw.body,
                    raw.lang,
                    raw.country,
                    _dt(raw.posted_at),
                    raw.app_version,
                    _dt(raw.fetched_at),
                    content_hash,
                ),
            )
            return cursor.rowcount > 0

    def upsert_review(self, review: Review) -> bool:
        """Upsert normalized review; update only when newer or content changed."""
        source, product, native_id = _split_review_id(review.review_id)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reviews (
                    review_id, product, source, native_id, rating, title, body, lang,
                    country, posted_at, app_version, fetched_at, content_hash, truncated
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id) DO UPDATE SET
                    rating = excluded.rating,
                    title = excluded.title,
                    body = excluded.body,
                    lang = excluded.lang,
                    country = excluded.country,
                    posted_at = excluded.posted_at,
                    app_version = excluded.app_version,
                    fetched_at = excluded.fetched_at,
                    content_hash = excluded.content_hash,
                    truncated = excluded.truncated
                WHERE
                    excluded.fetched_at > reviews.fetched_at
                    OR excluded.content_hash != reviews.content_hash
                """,
                (
                    review.review_id,
                    review.product,
                    review.source,
                    native_id,
                    review.rating,
                    review.title,
                    review.body,
                    review.lang,
                    review.country,
                    _dt(review.posted_at),
                    review.app_version,
                    _dt(review.fetched_at),
                    review.content_hash,
                    int(review.truncated),
                ),
            )
            return cursor.rowcount > 0

    def get_fetch_cursor(self, product: str, source: str) -> str | None:
        """Return stored fetch cursor for (product, source)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cursor_value FROM fetch_cursors WHERE product = ? AND source = ?",
                (product, source),
            ).fetchone()
            if row is None:
                return None
            return str(row["cursor_value"]) if row["cursor_value"] else None

    def set_fetch_cursor(
        self,
        product: str,
        source: str,
        cursor_value: str | None,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        """Upsert fetch cursor metadata for a source."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO fetch_cursors (
                    product, source, cursor_value, etag, last_modified, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(product, source) DO UPDATE SET
                    cursor_value = excluded.cursor_value,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    updated_at = excluded.updated_at
                """,
                (
                    product,
                    source,
                    cursor_value,
                    etag,
                    last_modified,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def iter_reviews(
        self,
        product: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Review]:
        """Return cached normalized reviews for a product within an optional window."""
        query = "SELECT * FROM reviews WHERE product = ?"
        params: list[object] = [product]
        if since is not None:
            query += " AND posted_at >= ?"
            params.append(_dt(since))
        if until is not None:
            query += " AND posted_at <= ?"
            params.append(_dt(until))
        query += " ORDER BY posted_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_review(row) for row in rows]

    def get_embedding(self, review_id: str, model_name: str) -> list[float] | None:
        """Return cached embedding vector for (review_id, model_name) or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT dim, vector FROM embeddings WHERE review_id = ? AND model_name = ?",
                (review_id, model_name),
            ).fetchone()
        if row is None:
            return None
        return _unpack_vector(row["vector"], int(row["dim"]))

    def put_embedding(
        self,
        review_id: str,
        model_name: str,
        vector: list[float],
    ) -> None:
        """Upsert an embedding vector."""
        payload = _pack_vector(vector)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO embeddings (review_id, model_name, dim, vector, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(review_id, model_name) DO UPDATE SET
                    dim = excluded.dim,
                    vector = excluded.vector,
                    created_at = excluded.created_at
                """,
                (
                    review_id,
                    model_name,
                    len(vector),
                    payload,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def count_reviews(self, product: str, source: str | None = None) -> int:
        """Return review count for a product (optionally scoped to source)."""
        with self._connect() as conn:
            if source is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM reviews WHERE product = ?",
                    (product,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM reviews WHERE product = ? AND source = ?",
                    (product, source),
                ).fetchone()
            return int(row["c"]) if row else 0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


__all__ = ["ReviewStore"]


def _dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).isoformat()
    return dt.astimezone(UTC).isoformat()


def _content_hash(title: str | None, body: str) -> str:
    import hashlib
    import re

    normalized_title = re.sub(r"\s+", " ", title).strip() if title else ""
    normalized_body = re.sub(r"\s+", " ", body).strip()
    return hashlib.sha256(f"{normalized_title}\n{normalized_body}".encode()).hexdigest()


def _split_review_id(review_id: str) -> tuple[str, str, str]:
    parts = review_id.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid review_id: {review_id}")
    return parts[0], parts[1], parts[2]


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"<{dim}f", blob))


def _row_to_review(row: sqlite3.Row) -> Review:
    posted_at = datetime.fromisoformat(row["posted_at"])
    fetched_at = datetime.fromisoformat(row["fetched_at"])
    source = cast(Literal["app_store", "play_store"], row["source"])
    return Review(
        review_id=row["review_id"],
        product=row["product"],
        source=source,
        rating=int(row["rating"]),
        title=row["title"],
        body=row["body"],
        lang=row["lang"],
        country=row["country"],
        posted_at=posted_at,
        app_version=row["app_version"],
        fetched_at=fetched_at,
        content_hash=row["content_hash"],
        truncated=bool(row["truncated"]),
    )
