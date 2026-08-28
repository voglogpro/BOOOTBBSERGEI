from __future__ import annotations

import secrets
import string
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from journal.auth import Principal
from journal.db import connect


class RepositoryError(ValueError):
    pass


def _dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class JournalRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    async def upsert_user(self, principal: Principal) -> dict[str, Any]:
        db = await connect(self.database_path)
        try:
            await db.execute(
                """
                INSERT INTO users (telegram_id, first_name, last_name, username, photo_url)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    username=excluded.username,
                    photo_url=excluded.photo_url,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    principal.telegram_id,
                    principal.first_name,
                    principal.last_name,
                    principal.username,
                    principal.photo_url,
                ),
            )
            await db.commit()
            row = await db.execute_fetchall(
                "SELECT * FROM users WHERE telegram_id=?", (principal.telegram_id,)
            )
            return dict(row[0])
        finally:
            await db.close()

    async def get_circle(self, user_id: int) -> dict[str, Any] | None:
        db = await connect(self.database_path)
        try:
            cursor = await db.execute(
                """
                SELECT c.* FROM circles c
                JOIN circle_members cm ON cm.circle_id=c.id
                WHERE cm.user_id=?
                """,
                (user_id,),
            )
            circle_row = await cursor.fetchone()
            if circle_row is None:
                return None
            circle = dict(circle_row)
            cursor = await db.execute(
                """
                SELECT u.id, u.telegram_id, u.first_name, u.last_name, u.username,
                       u.photo_url, cm.joined_at
                FROM circle_members cm
                JOIN users u ON u.id=cm.user_id
                WHERE cm.circle_id=? ORDER BY cm.joined_at
                """,
                (circle["id"],),
            )
            circle["members"] = [dict(row) for row in await cursor.fetchall()]
            return circle
        finally:
            await db.close()

    async def create_circle(self, user_id: int, name: str) -> dict[str, Any]:
        clean_name = name.strip()[:40] or "Команда трейдеров"
        alphabet = string.ascii_uppercase + string.digits
        db = await connect(self.database_path)
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT 1 FROM circle_members WHERE user_id=?", (user_id,)
            )
            if await cursor.fetchone():
                raise RepositoryError("Вы уже состоите в команде")
            circle_id = None
            for _ in range(10):
                code = "".join(secrets.choice(alphabet) for _ in range(8))
                try:
                    cursor = await db.execute(
                        "INSERT INTO circles(name, invite_code, created_by) VALUES (?, ?, ?)",
                        (clean_name, code, user_id),
                    )
                    circle_id = cursor.lastrowid
                    break
                except aiosqlite.IntegrityError:
                    continue
            if circle_id is None:
                raise RepositoryError("Не удалось создать код приглашения")
            await db.execute(
                "INSERT INTO circle_members(circle_id, user_id) VALUES (?, ?)",
                (circle_id, user_id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
        result = await self.get_circle(user_id)
        assert result is not None
        return result

    async def join_circle(self, user_id: int, invite_code: str) -> dict[str, Any]:
        code = invite_code.strip().upper()
        if len(code) != 8 or not code.isalnum():
            raise RepositoryError("Неверный код приглашения")
        db = await connect(self.database_path)
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT 1 FROM circle_members WHERE user_id=?", (user_id,)
            )
            if await cursor.fetchone():
                raise RepositoryError("Вы уже состоите в команде")
            cursor = await db.execute(
                "SELECT id FROM circles WHERE invite_code=?", (code,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise RepositoryError("Команда с таким кодом не найдена")
            circle_id = int(row["id"])
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM circle_members WHERE circle_id=?",
                (circle_id,),
            )
            count = int((await cursor.fetchone())["count"])
            if count >= 2:
                raise RepositoryError("В этой команде уже два участника")
            await db.execute(
                "INSERT INTO circle_members(circle_id, user_id) VALUES (?, ?)",
                (circle_id, user_id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
        result = await self.get_circle(user_id)
        assert result is not None
        return result

    async def leave_circle(self, user_id: int) -> None:
        db = await connect(self.database_path)
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT circle_id FROM circle_members WHERE user_id=?", (user_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise RepositoryError("Вы не состоите в команде")
            circle_id = int(row["circle_id"])
            await db.execute(
                "DELETE FROM circle_members WHERE circle_id=? AND user_id=?",
                (circle_id, user_id),
            )
            cursor = await db.execute(
                "SELECT user_id FROM circle_members WHERE circle_id=? LIMIT 1",
                (circle_id,),
            )
            survivor = await cursor.fetchone()
            if survivor is None:
                await db.execute("DELETE FROM circles WHERE id=?", (circle_id,))
            else:
                await db.execute(
                    "UPDATE circles SET created_by=? WHERE id=? AND created_by=?",
                    (int(survivor["user_id"]), circle_id, user_id),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def upsert_mood(self, user_id: int, entry: dict[str, Any]) -> dict[str, Any]:
        db = await connect(self.database_path)
        try:
            await db.execute(
                """
                INSERT INTO moods(
                    user_id, entry_date, mood, energy, confidence, discipline,
                    emotion, note, visibility
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, entry_date) DO UPDATE SET
                    mood=excluded.mood,
                    energy=excluded.energy,
                    confidence=excluded.confidence,
                    discipline=excluded.discipline,
                    emotion=excluded.emotion,
                    note=excluded.note,
                    visibility=excluded.visibility,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    entry["entry_date"],
                    entry["mood"],
                    entry["energy"],
                    entry["confidence"],
                    entry["discipline"],
                    entry["emotion"],
                    entry["note"],
                    entry["visibility"],
                ),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM moods WHERE user_id=? AND entry_date=?",
                (user_id, entry["entry_date"]),
            )
            return dict(await cursor.fetchone())
        finally:
            await db.close()

    async def get_mood(self, user_id: int, entry_date: str) -> dict[str, Any] | None:
        db = await connect(self.database_path)
        try:
            cursor = await db.execute(
                "SELECT * FROM moods WHERE user_id=? AND entry_date=?",
                (user_id, entry_date),
            )
            return _dict(await cursor.fetchone())
        finally:
            await db.close()

    async def create_trade(self, user_id: int, trade: dict[str, Any]) -> dict[str, Any]:
        columns = [
            "traded_at", "symbol", "direction", "timeframe", "setup",
            "entry_price", "stop_loss", "take_profit", "volume", "risk_amount",
            "pnl", "r_multiple", "emotion_before", "emotion_after", "plan_followed",
            "mistake", "note", "screenshot_url", "visibility",
        ]
        db = await connect(self.database_path)
        try:
            placeholders = ", ".join("?" for _ in range(len(columns) + 1))
            cursor = await db.execute(
                f"INSERT INTO trades(user_id, {', '.join(columns)}) VALUES ({placeholders})",
                [user_id, *[trade[column] for column in columns]],
            )
            trade_id = int(cursor.lastrowid)
            await db.commit()
            return await self._get_owned_trade(db, user_id, trade_id)
        finally:
            await db.close()

    async def update_trade(
        self, user_id: int, trade_id: int, trade: dict[str, Any]
    ) -> dict[str, Any]:
        columns = [
            "traded_at", "symbol", "direction", "timeframe", "setup",
            "entry_price", "stop_loss", "take_profit", "volume", "risk_amount",
            "pnl", "r_multiple", "emotion_before", "emotion_after", "plan_followed",
            "mistake", "note", "screenshot_url", "visibility",
        ]
        db = await connect(self.database_path)
        try:
            assignments = ", ".join(f"{column}=?" for column in columns)
            cursor = await db.execute(
                f"UPDATE trades SET {assignments}, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND user_id=?",
                [*[trade[column] for column in columns], trade_id, user_id],
            )
            if cursor.rowcount != 1:
                raise RepositoryError("Сделка не найдена")
            await db.commit()
            return await self._get_owned_trade(db, user_id, trade_id)
        finally:
            await db.close()

    async def _get_owned_trade(
        self, db: aiosqlite.Connection, user_id: int, trade_id: int
    ) -> dict[str, Any]:
        cursor = await db.execute(
            "SELECT * FROM trades WHERE id=? AND user_id=?", (trade_id, user_id)
        )
        row = await cursor.fetchone()
        if row is None:
            raise RepositoryError("Сделка не найдена")
        return dict(row)

    async def delete_trade(self, user_id: int, trade_id: int) -> None:
        db = await connect(self.database_path)
        try:
            cursor = await db.execute(
                "DELETE FROM trades WHERE id=? AND user_id=?", (trade_id, user_id)
            )
            if cursor.rowcount != 1:
                raise RepositoryError("Сделка не найдена")
            await db.commit()
        finally:
            await db.close()

    async def _scope_user_ids(self, db: aiosqlite.Connection, user_id: int, scope: str) -> list[int]:
        if scope != "team":
            return [user_id]
        cursor = await db.execute(
            """
            SELECT cm2.user_id FROM circle_members cm1
            JOIN circle_members cm2 ON cm2.circle_id=cm1.circle_id
            WHERE cm1.user_id=?
            """,
            (user_id,),
        )
        ids = [int(row["user_id"]) for row in await cursor.fetchall()]
        return ids or [user_id]

    @staticmethod
    def _scope_clause(user_ids: Iterable[int], owner_id: int) -> tuple[str, list[Any]]:
        ids = list(user_ids)
        placeholders = ",".join("?" for _ in ids)
        return (
            f"user_id IN ({placeholders}) AND (user_id=? OR visibility='team')",
            [*ids, owner_id],
        )

    async def list_trades(
        self,
        user_id: int,
        *,
        start_date: str,
        end_date: str,
        scope: str = "me",
    ) -> list[dict[str, Any]]:
        db = await connect(self.database_path)
        try:
            ids = await self._scope_user_ids(db, user_id, scope)
            clause, params = self._scope_clause(ids, user_id)
            cursor = await db.execute(
                f"""
                SELECT t.*, u.first_name, u.last_name, u.username
                FROM trades t JOIN users u ON u.id=t.user_id
                WHERE {clause} AND substr(traded_at, 1, 10) BETWEEN ? AND ?
                ORDER BY traded_at DESC, t.id DESC
                """,
                [*params, start_date, end_date],
            )
            return [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def calendar(
        self, user_id: int, *, start_date: str, end_date: str, scope: str = "team"
    ) -> list[dict[str, Any]]:
        db = await connect(self.database_path)
        try:
            ids = await self._scope_user_ids(db, user_id, scope)
            clause, params = self._scope_clause(ids, user_id)
            cursor = await db.execute(
                f"""
                SELECT substr(traded_at, 1, 10) AS day,
                       COUNT(*) AS trades,
                       ROUND(COALESCE(SUM(pnl), 0), 2) AS pnl,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN plan_followed = 1 THEN 1 ELSE 0 END) AS planned
                FROM trades WHERE {clause}
                  AND substr(traded_at, 1, 10) BETWEEN ? AND ?
                GROUP BY day
                """,
                [*params, start_date, end_date],
            )
            days: dict[str, dict[str, Any]] = {
                row["day"]: dict(row) for row in await cursor.fetchall()
            }
            mood_clause, mood_params = self._scope_clause(ids, user_id)
            cursor = await db.execute(
                f"""
                SELECT entry_date AS day, ROUND(AVG(mood), 1) AS mood,
                       ROUND(AVG(discipline), 1) AS discipline, COUNT(*) AS mood_entries
                FROM moods WHERE {mood_clause} AND entry_date BETWEEN ? AND ?
                GROUP BY entry_date
                """,
                [*mood_params, start_date, end_date],
            )
            for row in await cursor.fetchall():
                days.setdefault(
                    row["day"],
                    {"day": row["day"], "trades": 0, "pnl": 0.0, "wins": 0, "planned": 0},
                ).update(
                    mood=row["mood"],
                    discipline=row["discipline"],
                    mood_entries=row["mood_entries"],
                )
            return [days[key] for key in sorted(days)]
        finally:
            await db.close()

    async def stats(
        self, user_id: int, *, start_date: str, end_date: str, scope: str = "me"
    ) -> dict[str, Any]:
        db = await connect(self.database_path)
        try:
            ids = await self._scope_user_ids(db, user_id, scope)
            clause, params = self._scope_clause(ids, user_id)
            cursor = await db.execute(
                f"""
                SELECT COUNT(*) AS trades,
                       ROUND(COALESCE(SUM(pnl), 0), 2) AS pnl,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losses,
                       ROUND(AVG(r_multiple), 2) AS avg_r,
                       ROUND(MAX(pnl), 2) AS best_trade,
                       ROUND(MIN(pnl), 2) AS worst_trade,
                       SUM(CASE WHEN plan_followed=1 THEN 1 ELSE 0 END) AS planned
                FROM trades WHERE {clause}
                  AND substr(traded_at, 1, 10) BETWEEN ? AND ?
                """,
                [*params, start_date, end_date],
            )
            row = dict(await cursor.fetchone())
            trades = int(row["trades"] or 0)
            wins = int(row["wins"] or 0)
            losses = int(row["losses"] or 0)
            row["wins"] = wins
            row["losses"] = losses
            row["win_rate"] = round(wins / (wins + losses) * 100, 1) if wins + losses else 0
            row["plan_rate"] = round(int(row["planned"] or 0) / trades * 100, 1) if trades else 0

            mood_clause, mood_params = self._scope_clause(ids, user_id)
            cursor = await db.execute(
                f"""
                SELECT ROUND(AVG(mood), 2) AS avg_mood,
                       ROUND(AVG(discipline), 2) AS avg_discipline,
                       COUNT(DISTINCT entry_date) AS journal_days
                FROM moods WHERE {mood_clause} AND entry_date BETWEEN ? AND ?
                """,
                [*mood_params, start_date, end_date],
            )
            row.update(dict(await cursor.fetchone()))
            row["streak"] = await self._streak(db, user_id)
            row["stability_score"] = self._stability_score(row)
            return row
        finally:
            await db.close()

    async def _streak(self, db: aiosqlite.Connection, user_id: int) -> int:
        cursor = await db.execute(
            """
            SELECT day FROM (
                SELECT entry_date AS day FROM moods WHERE user_id=?
                UNION
                SELECT substr(traded_at, 1, 10) AS day FROM trades WHERE user_id=?
            ) ORDER BY day DESC
            """,
            (user_id, user_id),
        )
        dates = [date.fromisoformat(row["day"]) for row in await cursor.fetchall()]
        if not dates:
            return 0
        current = date.today()
        if dates[0] < current - timedelta(days=1):
            return 0
        expected = dates[0]
        streak = 0
        for item in dates:
            if item != expected:
                break
            streak += 1
            expected -= timedelta(days=1)
        return streak

    @staticmethod
    def _stability_score(stats: dict[str, Any]) -> int:
        plan_rate = float(stats.get("plan_rate") or 0)
        discipline = float(stats.get("avg_discipline") or 0) / 5 * 100
        journal_days = min(float(stats.get("journal_days") or 0) / 20 * 100, 100)
        return round(plan_rate * 0.5 + discipline * 0.3 + journal_days * 0.2)

