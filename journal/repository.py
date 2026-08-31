from __future__ import annotations

import secrets
import string
from datetime import date, timedelta
from pathlib import Path
from typing import Any

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

    async def _current_circle_id(
        self, db: aiosqlite.Connection, user_id: int
    ) -> int | None:
        cursor = await db.execute(
            "SELECT circle_id FROM circle_members WHERE user_id=?", (user_id,)
        )
        row = await cursor.fetchone()
        return int(row["circle_id"]) if row else None

    async def ping(self) -> None:
        db = await connect(self.database_path)
        try:
            await db.execute_fetchall("SELECT 1")
        finally:
            await db.close()

    async def get_settings(self, user_id: int) -> dict[str, Any]:
        db = await connect(self.database_path)
        try:
            await db.execute(
                "INSERT OR IGNORE INTO account_settings(user_id) VALUES (?)",
                (user_id,),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM account_settings WHERE user_id=?", (user_id,)
            )
            return dict(await cursor.fetchone())
        finally:
            await db.close()

    async def update_settings(
        self, user_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        db = await connect(self.database_path)
        try:
            await db.execute(
                "INSERT OR IGNORE INTO account_settings(user_id) VALUES (?)",
                (user_id,),
            )
            columns = [
                "account_name", "balance", "currency", "profit_target_pct",
                "daily_loss_limit_pct", "max_loss_limit_pct",
                "risk_per_trade_pct", "max_trades_day",
            ]
            assignments = ", ".join(f"{name}=?" for name in columns)
            await db.execute(
                f"UPDATE account_settings SET {assignments}, updated_at=CURRENT_TIMESTAMP "
                "WHERE user_id=?",
                [*[values[name] for name in columns], user_id],
            )
            await db.commit()
        finally:
            await db.close()
        return await self.get_settings(user_id)

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
            user = dict(row[0])
            await db.execute(
                "INSERT OR IGNORE INTO account_settings(user_id) VALUES (?)",
                (user["id"],),
            )
            await db.commit()
            return user
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
            # A circle is an explicit two-person privacy boundary. Dissolving it
            # prevents a future partner from inheriting access to old entries.
            await db.execute("DELETE FROM circles WHERE id=?", (circle_id,))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def upsert_mood(self, user_id: int, entry: dict[str, Any]) -> dict[str, Any]:
        db = await connect(self.database_path)
        try:
            circle_id = await self._current_circle_id(db, user_id)
            if entry["visibility"] != "team":
                circle_id = None
            await db.execute(
                """
                INSERT INTO moods(
                    user_id, entry_date, mood, energy, confidence, discipline,
                    emotion, note, focus, lesson, journal_mode, market_bias,
                    day_idea, key_levels, day_invalidation, news_context,
                    visibility, circle_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, entry_date) DO UPDATE SET
                    mood=excluded.mood,
                    energy=excluded.energy,
                    confidence=excluded.confidence,
                    discipline=excluded.discipline,
                    emotion=excluded.emotion,
                    note=excluded.note,
                    focus=excluded.focus,
                    lesson=excluded.lesson,
                    journal_mode=excluded.journal_mode,
                    market_bias=excluded.market_bias,
                    day_idea=excluded.day_idea,
                    key_levels=excluded.key_levels,
                    day_invalidation=excluded.day_invalidation,
                    news_context=excluded.news_context,
                    visibility=excluded.visibility,
                    circle_id=excluded.circle_id,
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
                    entry.get("focus", ""),
                    entry.get("lesson", ""),
                    entry.get("journal_mode", "backtest"),
                    entry.get("market_bias", "NEUTRAL"),
                    entry.get("day_idea", ""),
                    entry.get("key_levels", ""),
                    entry.get("day_invalidation", ""),
                    entry.get("news_context", ""),
                    entry["visibility"],
                    circle_id,
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

    async def save_weekly_plan(
        self, user_id: int, values: dict[str, Any], plan_id: int | None = None
    ) -> dict[str, Any]:
        columns = [
            "week_start", "symbol", "bias", "title", "idea", "trade_plan",
            "invalidation", "status", "week_summary", "week_lesson", "rating",
            "visibility",
        ]
        db = await connect(self.database_path)
        try:
            circle_id = await self._current_circle_id(db, user_id)
            if values["visibility"] != "team":
                circle_id = None
            try:
                if plan_id is None:
                    placeholders = ", ".join("?" for _ in range(len(columns) + 2))
                    cursor = await db.execute(
                        f"INSERT INTO weekly_plans(user_id, {', '.join(columns)}, circle_id) "
                        f"VALUES ({placeholders})",
                        [user_id, *[values[column] for column in columns], circle_id],
                    )
                    plan_id = int(cursor.lastrowid)
                else:
                    assignments = ", ".join(f"{column}=?" for column in columns)
                    cursor = await db.execute(
                        f"UPDATE weekly_plans SET {assignments}, circle_id=?, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                        [*[values[column] for column in columns], circle_id, plan_id, user_id],
                    )
                    if cursor.rowcount != 1:
                        raise RepositoryError("Недельный план не найден")
                await db.commit()
            except aiosqlite.IntegrityError as exc:
                await db.rollback()
                raise RepositoryError(
                    "Для этого инструмента на выбранной неделе план уже существует"
                ) from exc
            return await self._get_owned_weekly_plan(db, user_id, int(plan_id))
        finally:
            await db.close()

    async def _get_owned_weekly_plan(
        self, db: aiosqlite.Connection, user_id: int, plan_id: int
    ) -> dict[str, Any]:
        cursor = await db.execute(
            "SELECT * FROM weekly_plans WHERE id=? AND user_id=?", (plan_id, user_id)
        )
        row = await cursor.fetchone()
        if row is None:
            raise RepositoryError("Недельный план не найден")
        return dict(row)

    async def list_weekly_plans(
        self, user_id: int, *, week_start: str, scope: str = "me"
    ) -> list[dict[str, Any]]:
        db = await connect(self.database_path)
        try:
            clause, params = await self._scope_clause(db, user_id, scope, "p")
            cursor = await db.execute(
                f"""
                SELECT p.*, u.first_name, u.last_name, u.username
                FROM weekly_plans p JOIN users u ON u.id=p.user_id
                WHERE {clause} AND p.week_start=?
                ORDER BY p.symbol, p.id
                """,
                [*params, week_start],
            )
            plans = [dict(row) for row in await cursor.fetchall()]
            if not plans:
                return []
            by_id = {int(plan["id"]): plan for plan in plans}
            placeholders = ",".join("?" for _ in by_id)
            cursor = await db.execute(
                f"""
                SELECT id, plan_id, original_name, mime_type, size_bytes, created_at
                FROM weekly_plan_images WHERE plan_id IN ({placeholders})
                ORDER BY id
                """,
                list(by_id),
            )
            for plan in plans:
                plan["images"] = []
                plan["days"] = []
                plan["trades"] = 0
                plan["pnl"] = 0.0
                plan["idea_followed"] = 0
                plan["idea_broken"] = 0
                plan["idea_rate"] = None
            for image in await cursor.fetchall():
                item = dict(image)
                item["url"] = f"/api/weekly-plan-images/{item['id']}"
                by_id[int(item["plan_id"])]["images"].append(item)

            trade_clause, trade_params = await self._scope_clause(db, user_id, scope)
            cursor = await db.execute(
                f"""
                SELECT weekly_plan_id, substr(traded_at, 1, 10) AS day,
                       COUNT(*) AS trades,
                       ROUND(COALESCE(SUM(CASE WHEN status='closed' THEN pnl ELSE 0 END), 0), 2) AS pnl,
                       SUM(CASE WHEN idea_followed=1 THEN 1 ELSE 0 END) AS idea_followed,
                       SUM(CASE WHEN idea_followed=0 THEN 1 ELSE 0 END) AS idea_broken
                FROM trades WHERE {trade_clause}
                  AND weekly_plan_id IN ({placeholders})
                GROUP BY weekly_plan_id, day ORDER BY day
                """,
                [*trade_params, *list(by_id)],
            )
            for row in await cursor.fetchall():
                day = dict(row)
                plan = by_id[int(day["weekly_plan_id"])]
                plan["days"].append(day)
                plan["trades"] += int(day["trades"] or 0)
                plan["pnl"] = round(float(plan["pnl"]) + float(day["pnl"] or 0), 2)
                plan["idea_followed"] += int(day["idea_followed"] or 0)
                plan["idea_broken"] += int(day["idea_broken"] or 0)
            for plan in plans:
                marked = plan["idea_followed"] + plan["idea_broken"]
                if marked:
                    plan["idea_rate"] = round(plan["idea_followed"] / marked * 100, 1)
            return plans
        finally:
            await db.close()

    async def add_weekly_plan_image(
        self, user_id: int, plan_id: int, image: dict[str, Any]
    ) -> dict[str, Any]:
        db = await connect(self.database_path)
        try:
            await self._get_owned_weekly_plan(db, user_id, plan_id)
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM weekly_plan_images WHERE plan_id=?",
                (plan_id,),
            )
            if int((await cursor.fetchone())["count"]) >= 4:
                raise RepositoryError("К одному недельному плану можно добавить до четырёх фотографий")
            cursor = await db.execute(
                """
                INSERT INTO weekly_plan_images(
                    plan_id, storage_name, original_name, mime_type, size_bytes
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    plan_id, image["storage_name"], image["original_name"],
                    image["mime_type"], image["size_bytes"],
                ),
            )
            image_id = int(cursor.lastrowid)
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM weekly_plan_images WHERE id=?", (image_id,)
            )
            return dict(await cursor.fetchone())
        finally:
            await db.close()

    async def get_weekly_plan_image(
        self, user_id: int, image_id: int
    ) -> dict[str, Any]:
        db = await connect(self.database_path)
        try:
            circle_id = await self._current_circle_id(db, user_id)
            cursor = await db.execute(
                """
                SELECT i.* FROM weekly_plan_images i
                JOIN weekly_plans p ON p.id=i.plan_id
                WHERE i.id=? AND (
                    p.user_id=? OR
                    (? IS NOT NULL AND p.circle_id=? AND p.visibility='team')
                )
                """,
                (image_id, user_id, circle_id, circle_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RepositoryError("Фотография плана не найдена")
            return dict(row)
        finally:
            await db.close()

    async def delete_weekly_plan_image(self, user_id: int, image_id: int) -> str:
        db = await connect(self.database_path)
        try:
            cursor = await db.execute(
                """
                SELECT i.storage_name FROM weekly_plan_images i
                JOIN weekly_plans p ON p.id=i.plan_id
                WHERE i.id=? AND p.user_id=?
                """,
                (image_id, user_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RepositoryError("Фотография плана не найдена")
            await db.execute("DELETE FROM weekly_plan_images WHERE id=?", (image_id,))
            await db.commit()
            return str(row["storage_name"])
        finally:
            await db.close()

    async def delete_weekly_plan(self, user_id: int, plan_id: int) -> list[str]:
        db = await connect(self.database_path)
        try:
            await self._get_owned_weekly_plan(db, user_id, plan_id)
            cursor = await db.execute(
                "SELECT storage_name FROM weekly_plan_images WHERE plan_id=?", (plan_id,)
            )
            files = [str(row["storage_name"]) for row in await cursor.fetchall()]
            await db.execute(
                "UPDATE trades SET weekly_plan_id=NULL, idea_followed=NULL, "
                "countertrend_confirmed=NULL "
                "WHERE weekly_plan_id=? AND user_id=?",
                (plan_id, user_id),
            )
            await db.execute(
                "DELETE FROM weekly_plans WHERE id=? AND user_id=?", (plan_id, user_id)
            )
            await db.commit()
            return files
        finally:
            await db.close()

    async def _validate_weekly_plan_link(
        self, db: aiosqlite.Connection, user_id: int, trade: dict[str, Any]
    ) -> None:
        plan_id = trade.get("weekly_plan_id")
        is_live_entry = (
            str(trade.get("journal_mode", "backtest")) == "live"
            and str(trade.get("status", "closed")) in {"open", "closed"}
        )
        entered_without_plan = trade.get("entered_without_plan") == 1
        if plan_id is None:
            if is_live_entry and not entered_without_plan:
                raise RepositoryError("Для реальной сделки выберите недельную идею")
            return
        cursor = await db.execute(
            "SELECT week_start, symbol, bias FROM weekly_plans WHERE id=? AND user_id=?",
            (plan_id, user_id),
        )
        plan = await cursor.fetchone()
        if plan is None:
            raise RepositoryError("Недельный план не найден")
        traded_on = date.fromisoformat(str(trade["traded_at"])[:10])
        week_start = date.fromisoformat(str(plan["week_start"]))
        if not week_start <= traded_on <= week_start + timedelta(days=6):
            raise RepositoryError("Дата сделки не входит в неделю выбранного плана")
        if str(plan["symbol"]).upper() != str(trade["symbol"]).upper():
            raise RepositoryError("Инструмент сделки не совпадает с недельным планом")
        opposite = (
            (str(plan["bias"]) == "LONG" and str(trade["direction"]) == "SELL")
            or (str(plan["bias"]) == "SHORT" and str(trade["direction"]) == "BUY")
        )
        cursor = await db.execute(
            "SELECT market_bias FROM moods WHERE user_id=? AND entry_date=?",
            (user_id, str(trade["traded_at"])[:10]),
        )
        day_plan = await cursor.fetchone()
        day_bias = str(day_plan["market_bias"]) if day_plan is not None else "NEUTRAL"
        opposite = opposite or (
            (day_bias == "LONG" and str(trade["direction"]) == "SELL")
            or (day_bias == "SHORT" and str(trade["direction"]) == "BUY")
        )
        if (
            opposite
            and str(trade.get("status", "closed")) in {"open", "closed"}
            and not entered_without_plan
            and trade.get("countertrend_confirmed") != 1
        ):
            raise RepositoryError(
                "Контртрендовая сделка требует подтверждения разворота"
            )
        if opposite and entered_without_plan:
            trade["idea_followed"] = 0
        if is_live_entry and not entered_without_plan:
            if trade.get("trigger_confirmed") != 1:
                raise RepositoryError("Сначала подтвердите, что триггер входа уже появился")
            if len(str(trade.get("trigger_evidence", "")).strip()) < 12:
                raise RepositoryError(
                    "Опишите фактический сигнал входа минимум в 12 символах"
                )
            required_text = {
                "trade_plan": "план сделки",
                "entry_trigger": "триггер входа",
                "trade_invalidation": "условие отмены сделки",
            }
            for field, label in required_text.items():
                if not str(trade.get(field, "")).strip():
                    raise RepositoryError(f"Перед входом заполните {label}")
            if not trade.get("risk_amount") or trade.get("stop_loss") is None:
                raise RepositoryError("Перед входом укажите риск и стоп")
            if opposite and len(str(trade.get("countertrend_evidence", "")).strip()) < 12:
                raise RepositoryError(
                    "Опишите фактическое подтверждение разворота минимум в 12 символах"
                )

    async def create_trade(self, user_id: int, trade: dict[str, Any]) -> dict[str, Any]:
        defaults = {
            "status": "closed", "client_entry_id": "", "session": "",
            "grade": "", "market_context": "", "journal_mode": "backtest",
            "outcome_type": "", "trade_plan": "", "entry_trigger": "",
            "trade_invalidation": "",
            "weekly_plan_id": None, "idea_followed": None,
            "countertrend_confirmed": None,
            "countertrend_evidence": "", "trigger_confirmed": None,
            "trigger_evidence": "",
            "entered_without_plan": 0,
        }
        columns = [
            "traded_at", "symbol", "direction", "status", "client_entry_id",
            "timeframe", "session", "setup",
            "grade", "market_context", "journal_mode", "confidence_before",
            "trade_plan", "entry_trigger", "trade_invalidation", "outcome_type",
            "weekly_plan_id", "idea_followed", "countertrend_confirmed",
            "countertrend_evidence", "trigger_confirmed", "trigger_evidence",
            "entered_without_plan",
            "entry_price", "stop_loss", "take_profit", "volume", "risk_amount",
            "pnl", "r_multiple", "emotion_before", "emotion_after", "plan_followed",
            "mistake", "note", "screenshot_url", "visibility",
        ]
        db = await connect(self.database_path)
        try:
            if (
                str(trade.get("journal_mode", "backtest")) == "live"
                and str(trade.get("status", "closed")) in {"open", "closed"}
                and trade.get("entered_without_plan") != 1
            ):
                raise RepositoryError(
                    "Сначала сохраните план сделки. Открыть её можно из сохранённого плана после появления триггера"
                )
            await self._validate_weekly_plan_link(db, user_id, trade)
            circle_id = await self._current_circle_id(db, user_id)
            if trade["visibility"] != "team":
                circle_id = None
            all_columns = [*columns, "circle_id"]
            placeholders = ", ".join("?" for _ in range(len(all_columns) + 1))
            try:
                cursor = await db.execute(
                    f"INSERT INTO trades(user_id, {', '.join(all_columns)}) VALUES ({placeholders})",
                    [user_id, *[trade.get(column, defaults.get(column)) for column in columns], circle_id],
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                if trade.get("client_entry_id"):
                    cursor = await db.execute(
                        "SELECT * FROM trades WHERE user_id=? AND client_entry_id=?",
                        (user_id, trade["client_entry_id"]),
                    )
                    existing = await cursor.fetchone()
                    if existing is not None:
                        return await self._get_owned_trade(
                            db, user_id, int(existing["id"])
                        )
                raise
            trade_id = int(cursor.lastrowid)
            await db.commit()
            return await self._get_owned_trade(db, user_id, trade_id)
        finally:
            await db.close()

    async def update_trade(
        self, user_id: int, trade_id: int, trade: dict[str, Any]
    ) -> dict[str, Any]:
        defaults = {
            "status": "closed", "session": "", "grade": "", "market_context": "",
            "journal_mode": "backtest", "outcome_type": "", "trade_plan": "",
            "entry_trigger": "", "trade_invalidation": "",
            "weekly_plan_id": None, "idea_followed": None,
            "countertrend_confirmed": None,
            "countertrend_evidence": "", "trigger_confirmed": None,
            "trigger_evidence": "",
            "entered_without_plan": 0,
        }
        columns = [
            "traded_at", "symbol", "direction", "status", "timeframe", "session", "setup",
            "grade", "market_context", "journal_mode", "confidence_before",
            "trade_plan", "entry_trigger", "trade_invalidation", "outcome_type",
            "weekly_plan_id", "idea_followed", "countertrend_confirmed",
            "countertrend_evidence", "trigger_confirmed", "trigger_evidence",
            "entered_without_plan",
            "entry_price", "stop_loss", "take_profit", "volume", "risk_amount",
            "pnl", "r_multiple", "emotion_before", "emotion_after", "plan_followed",
            "mistake", "note", "screenshot_url", "visibility",
        ]
        db = await connect(self.database_path)
        try:
            await self._validate_weekly_plan_link(db, user_id, trade)
            circle_id = await self._current_circle_id(db, user_id)
            if trade["visibility"] != "team":
                circle_id = None
            assignments = ", ".join(f"{column}=?" for column in [*columns, "circle_id"])
            cursor = await db.execute(
                f"UPDATE trades SET {assignments}, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND user_id=?",
                [*[trade.get(column, defaults.get(column)) for column in columns], circle_id, trade_id, user_id],
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
        trade = dict(row)
        await self._attach_trade_images(db, [trade])
        return trade

    async def save_trade_image(
        self, user_id: int, trade_id: int, kind: str, image: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        if kind not in {"entry", "result"}:
            raise RepositoryError("Некорректный тип фотографии сделки")
        db = await connect(self.database_path)
        try:
            # Serialize replacements so concurrent retries cannot leave the
            # previous successful upload orphaned on disk.
            await db.execute("BEGIN IMMEDIATE")
            await self._get_owned_trade(db, user_id, trade_id)
            cursor = await db.execute(
                "SELECT storage_name FROM trade_images WHERE trade_id=? AND kind=?",
                (trade_id, kind),
            )
            previous = await cursor.fetchone()
            await db.execute(
                """
                INSERT INTO trade_images(
                    trade_id, kind, storage_name, original_name, mime_type, size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id, kind) DO UPDATE SET
                    storage_name=excluded.storage_name,
                    original_name=excluded.original_name,
                    mime_type=excluded.mime_type,
                    size_bytes=excluded.size_bytes,
                    created_at=CURRENT_TIMESTAMP
                """,
                (
                    trade_id, kind, image["storage_name"], image["original_name"],
                    image["mime_type"], image["size_bytes"],
                ),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM trade_images WHERE trade_id=? AND kind=?",
                (trade_id, kind),
            )
            saved = dict(await cursor.fetchone())
            old_storage = str(previous["storage_name"]) if previous else None
            return saved, old_storage
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def get_trade_image(self, user_id: int, image_id: int) -> dict[str, Any]:
        db = await connect(self.database_path)
        try:
            circle_id = await self._current_circle_id(db, user_id)
            cursor = await db.execute(
                """
                SELECT i.* FROM trade_images i
                JOIN trades t ON t.id=i.trade_id
                WHERE i.id=? AND (
                    t.user_id=? OR
                    (? IS NOT NULL AND t.circle_id=? AND t.visibility='team')
                )
                """,
                (image_id, user_id, circle_id, circle_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RepositoryError("Фотография сделки не найдена")
            return dict(row)
        finally:
            await db.close()

    async def delete_trade_image(self, user_id: int, image_id: int) -> str:
        db = await connect(self.database_path)
        try:
            cursor = await db.execute(
                """
                SELECT i.storage_name FROM trade_images i
                JOIN trades t ON t.id=i.trade_id
                WHERE i.id=? AND t.user_id=?
                """,
                (image_id, user_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RepositoryError("Фотография сделки не найдена")
            await db.execute("DELETE FROM trade_images WHERE id=?", (image_id,))
            await db.commit()
            return str(row["storage_name"])
        finally:
            await db.close()

    async def delete_trade(self, user_id: int, trade_id: int) -> list[str]:
        db = await connect(self.database_path)
        try:
            cursor = await db.execute(
                """
                SELECT i.storage_name FROM trade_images i
                JOIN trades t ON t.id=i.trade_id
                WHERE t.id=? AND t.user_id=?
                """,
                (trade_id, user_id),
            )
            files = [str(row["storage_name"]) for row in await cursor.fetchall()]
            cursor = await db.execute(
                "DELETE FROM trades WHERE id=? AND user_id=?", (trade_id, user_id)
            )
            if cursor.rowcount != 1:
                raise RepositoryError("Сделка не найдена")
            await db.commit()
            return files
        finally:
            await db.close()

    async def _attach_trade_images(
        self, db: aiosqlite.Connection, trades: list[dict[str, Any]]
    ) -> None:
        if not trades:
            return
        by_id = {int(trade["id"]): trade for trade in trades}
        for trade in trades:
            trade["images"] = []
        placeholders = ",".join("?" for _ in by_id)
        cursor = await db.execute(
            f"""
            SELECT id, trade_id, kind, original_name, mime_type, size_bytes, created_at
            FROM trade_images WHERE trade_id IN ({placeholders})
            ORDER BY CASE kind WHEN 'entry' THEN 0 ELSE 1 END, id
            """,
            list(by_id),
        )
        for row in await cursor.fetchall():
            item = dict(row)
            item["url"] = f"/api/trade-images/{item['id']}"
            by_id[int(item["trade_id"])]["images"].append(item)

    async def _scope_clause(
        self, db: aiosqlite.Connection, user_id: int, scope: str, table_alias: str = ""
    ) -> tuple[str, list[Any]]:
        prefix = f"{table_alias}." if table_alias else ""
        if scope != "team":
            return f"{prefix}user_id=?", [user_id]
        circle_id = await self._current_circle_id(db, user_id)
        if circle_id is None:
            return f"{prefix}user_id=?", [user_id]
        return (
            f"({prefix}user_id=? OR ({prefix}circle_id=? AND {prefix}visibility='team'))",
            [user_id, circle_id],
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
            clause, params = await self._scope_clause(db, user_id, scope, "t")
            current_circle_id = await self._current_circle_id(db, user_id)
            cursor = await db.execute(
                f"""
                SELECT t.*, u.first_name, u.last_name, u.username,
                       CASE WHEN t.user_id=? THEN COALESCE(
                           (SELECT m.market_bias FROM moods m
                            WHERE m.user_id=t.user_id
                              AND m.entry_date=substr(t.traded_at, 1, 10)),
                           'NEUTRAL') ELSE 'NEUTRAL' END AS day_plan_bias,
                       CASE WHEN wp.user_id=? OR
                                      (wp.circle_id=? AND wp.visibility='team')
                            THEN wp.title ELSE '' END AS weekly_plan_title,
                       CASE WHEN wp.user_id=? OR
                                      (wp.circle_id=? AND wp.visibility='team')
                            THEN wp.symbol ELSE '' END AS weekly_plan_symbol,
                       CASE WHEN wp.user_id=? OR
                                      (wp.circle_id=? AND wp.visibility='team')
                            THEN wp.bias ELSE '' END AS weekly_plan_bias
                FROM trades t JOIN users u ON u.id=t.user_id
                LEFT JOIN weekly_plans wp ON wp.id=t.weekly_plan_id
                WHERE {clause} AND substr(traded_at, 1, 10) BETWEEN ? AND ?
                ORDER BY traded_at DESC, t.id DESC
                """,
                [user_id, user_id, current_circle_id, user_id, current_circle_id,
                 user_id, current_circle_id,
                 *params, start_date, end_date],
            )
            trades = [dict(row) for row in await cursor.fetchall()]
            await self._attach_trade_images(db, trades)
            return trades
        finally:
            await db.close()

    async def export_dataset(self, user_id: int) -> list[dict[str, Any]]:
        """Return personal trades enriched with the state and thesis for that day."""
        db = await connect(self.database_path)
        try:
            cursor = await db.execute(
                """
                SELECT t.*,
                       m.mood AS day_mood,
                       m.energy AS day_energy,
                       m.confidence AS day_confidence,
                       m.discipline AS day_discipline,
                       m.emotion AS day_emotion,
                       m.focus AS day_focus,
                       m.lesson AS day_lesson,
                       m.journal_mode AS day_mode,
                       m.market_bias AS day_bias,
                       m.day_idea,
                       m.key_levels AS day_key_levels,
                       m.day_invalidation,
                       m.news_context AS day_news_context,
                       wp.title AS weekly_plan_title,
                       wp.idea AS weekly_plan_idea,
                       wp.trade_plan AS weekly_plan_trade_plan,
                       wp.invalidation AS weekly_plan_invalidation
                FROM trades t
                LEFT JOIN moods m
                  ON m.user_id=t.user_id
                 AND m.entry_date=substr(t.traded_at, 1, 10)
                LEFT JOIN weekly_plans wp ON wp.id=t.weekly_plan_id
                WHERE t.user_id=?
                ORDER BY t.traded_at, t.id
                """,
                (user_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]
        finally:
            await db.close()

    async def calendar(
        self, user_id: int, *, start_date: str, end_date: str, scope: str = "team"
    ) -> list[dict[str, Any]]:
        db = await connect(self.database_path)
        try:
            clause, params = await self._scope_clause(db, user_id, scope)
            cursor = await db.execute(
                f"""
                SELECT substr(traded_at, 1, 10) AS day,
                       COUNT(*) AS trades,
                       ROUND(COALESCE(SUM(pnl), 0), 2) AS pnl,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN plan_followed = 1 THEN 1 ELSE 0 END) AS planned,
                       SUM(CASE WHEN idea_followed = 1 THEN 1 ELSE 0 END) AS idea_followed,
                       SUM(CASE WHEN idea_followed = 0 THEN 1 ELSE 0 END) AS idea_broken
                FROM trades WHERE {clause}
                  AND substr(traded_at, 1, 10) BETWEEN ? AND ?
                GROUP BY day
                """,
                [*params, start_date, end_date],
            )
            days: dict[str, dict[str, Any]] = {
                row["day"]: dict(row) for row in await cursor.fetchall()
            }
            mood_clause, mood_params = await self._scope_clause(db, user_id, scope)
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
                    {"day": row["day"], "trades": 0, "pnl": 0.0, "wins": 0,
                     "planned": 0, "idea_followed": 0, "idea_broken": 0},
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
            clause, params = await self._scope_clause(db, user_id, scope)
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
                  AND status='closed'
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
            row["expectancy"] = round(float(row["pnl"] or 0) / trades, 2) if trades else 0

            cursor = await db.execute(
                f"""
                SELECT ROUND(COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0), 2) AS gross_profit,
                       ROUND(COALESCE(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END), 0), 2) AS gross_loss,
                       ROUND(AVG(CASE WHEN pnl > 0 THEN pnl END), 2) AS avg_win,
                       ROUND(AVG(CASE WHEN pnl < 0 THEN pnl END), 2) AS avg_loss
                FROM trades WHERE {clause}
                  AND status='closed'
                  AND substr(traded_at, 1, 10) BETWEEN ? AND ?
                """,
                [*params, start_date, end_date],
            )
            money = dict(await cursor.fetchone())
            row.update(money)
            gross_loss = abs(float(row["gross_loss"] or 0))
            row["profit_factor"] = (
                round(float(row["gross_profit"] or 0) / gross_loss, 2)
                if gross_loss else None
            )

            cursor = await db.execute(
                f"""
                SELECT substr(traded_at, 1, 10) AS day,
                       ROUND(SUM(pnl), 2) AS pnl
                FROM trades WHERE {clause}
                  AND status='closed'
                  AND substr(traded_at, 1, 10) BETWEEN ? AND ?
                GROUP BY day ORDER BY day
                """,
                [*params, start_date, end_date],
            )
            equity = 0.0
            peak = 0.0
            max_drawdown = 0.0
            curve: list[dict[str, Any]] = []
            for item in await cursor.fetchall():
                equity = round(equity + float(item["pnl"] or 0), 2)
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, peak - equity)
                curve.append({"day": item["day"], "pnl": item["pnl"], "equity": equity})
            row["equity_curve"] = curve
            row["max_drawdown"] = round(max_drawdown, 2)

            async def breakdown(field: str, *, limit: int = 5) -> list[dict[str, Any]]:
                cursor = await db.execute(
                    f"""
                    SELECT {field} AS label, COUNT(*) AS trades,
                           ROUND(SUM(pnl), 2) AS pnl,
                           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins
                    FROM trades WHERE {clause}
                      AND status='closed'
                      AND substr(traded_at, 1, 10) BETWEEN ? AND ?
                      AND TRIM({field}) != ''
                    GROUP BY {field}
                    ORDER BY trades DESC, pnl DESC LIMIT ?
                    """,
                    [*params, start_date, end_date, limit],
                )
                result = []
                for item in await cursor.fetchall():
                    value = dict(item)
                    value["win_rate"] = round(
                        int(value["wins"] or 0) / int(value["trades"]) * 100, 1
                    )
                    result.append(value)
                return result

            row["setups"] = await breakdown("setup")
            row["sessions"] = await breakdown("session")
            row["mistakes"] = await breakdown("mistake")
            row["outcomes"] = await breakdown("outcome_type")
            row["modes"] = await breakdown("journal_mode")

            async def psychology_breakdown(
                label_expression: str, *, source: str = "trades"
            ) -> list[dict[str, Any]]:
                cursor = await db.execute(
                    f"""
                    SELECT {label_expression} AS label,
                           COUNT(*) AS trades,
                           ROUND(COALESCE(SUM(pnl), 0), 2) AS pnl,
                           ROUND(AVG(r_multiple), 2) AS avg_r,
                           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN outcome_type='take' THEN 1 ELSE 0 END) AS takes,
                           SUM(CASE WHEN outcome_type='stop' THEN 1 ELSE 0 END) AS stops
                    FROM {source} WHERE {clause}
                      AND status='closed'
                      AND substr(traded_at, 1, 10) BETWEEN ? AND ?
                      AND {label_expression} IS NOT NULL
                    GROUP BY {label_expression}
                    ORDER BY {label_expression}
                    """,
                    [*params, start_date, end_date],
                )
                result = []
                for item in await cursor.fetchall():
                    value = dict(item)
                    value["win_rate"] = round(
                        int(value["wins"] or 0) / int(value["trades"]) * 100, 1
                    )
                    result.append(value)
                return result

            row["confidence_patterns"] = await psychology_breakdown(
                "confidence_before"
            )
            current_circle_id = (
                await self._current_circle_id(db, user_id) if scope == "team" else None
            )
            mood_visibility = ""
            if current_circle_id is not None:
                mood_visibility = (
                    f"AND (t.user_id={int(user_id)} OR "
                    f"(m.circle_id={int(current_circle_id)} AND m.visibility='team'))"
                )
            mood_source = f"""
                (SELECT t.*,
                        (SELECT m.mood FROM moods m
                         WHERE m.user_id=t.user_id
                           AND m.entry_date=substr(t.traded_at, 1, 10)
                           {mood_visibility}
                         LIMIT 1) AS day_mood
                 FROM trades t) AS trades
            """
            row["mood_patterns"] = await psychology_breakdown(
                "day_mood", source=mood_source
            )

            mood_clause, mood_params = await self._scope_clause(db, user_id, scope)
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
            row["streak"] = await self._streak(db, user_id, end_date)
            row["stability_score"] = self._stability_score(row)
            return row
        finally:
            await db.close()

    async def _streak(
        self, db: aiosqlite.Connection, user_id: int, anchor_date: str
    ) -> int:
        cursor = await db.execute(
            """
            SELECT day FROM (
                SELECT entry_date AS day FROM moods WHERE user_id=?
                UNION
                SELECT substr(traded_at, 1, 10) AS day FROM trades WHERE user_id=?
            ) WHERE day <= ? ORDER BY day DESC
            """,
            (user_id, user_id, anchor_date),
        )
        dates = [date.fromisoformat(row["day"]) for row in await cursor.fetchall()]
        if not dates:
            return 0
        current = date.fromisoformat(anchor_date)
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
