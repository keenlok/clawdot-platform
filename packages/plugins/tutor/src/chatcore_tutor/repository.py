"""Postgres and in-memory repositories for private tutor profiles."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from chatcore.store.pg import PgDatabase

from chatcore_tutor.languages import LEVELS, lookup_language, parse_level
from chatcore_tutor.models import TutorProfile, TutorTopic

log = logging.getLogger(__name__)


def _validated_explanations(raw: Any, target: str) -> tuple[str, ...] | None:
    if not isinstance(raw, str) or not raw:
        return None
    explanations = tuple(lookup_language(name) for name in raw.split(","))
    if (
        len(explanations) > 2
        or any(language is None for language in explanations)
        or any(language.name == target for language in explanations if language is not None)
        or len({language.name for language in explanations if language is not None})
        != len(explanations)
    ):
        return None
    return tuple(language.name for language in explanations if language is not None)


def _encode_explanations(explain: str | tuple[str, ...] | None, target: str) -> str | None:
    if explain is None:
        return None
    raw_names = (explain,) if isinstance(explain, str) else explain
    names = tuple(lookup_language(name) for name in raw_names)
    if (
        not names
        or len(names) > 2
        or any(language is None for language in names)
        or any(language.name == target for language in names if language is not None)
        or len({language.name for language in names if language is not None}) != len(names)
    ):
        raise ValueError("Invalid tutor explanation languages")
    encoded = ",".join(language.name for language in names if language is not None)
    if len(encoded) > 40:
        raise ValueError("Tutor explanation languages are too long")
    return encoded


def _validated_profile(row: Any) -> TutorProfile | None:
    values = dict(row)
    target = lookup_language(str(values.get("target", "")))
    explain = _validated_explanations(values.get("explain"), target.name if target else "")
    level = parse_level(str(values.get("level", "")))
    updated_at = values.get("updated_at")
    active = values.get("active")
    user_key = values.get("user_key")
    if (
        target is None
        or explain is None
        or level not in LEVELS
        or not isinstance(updated_at, datetime)
        or type(active) is not bool
        or not isinstance(user_key, str)
    ):
        log.warning("tutor profile invalid")
        return None
    return TutorProfile(user_key, target.name, level, explain, active, updated_at)


def _validated_topic(row: Any) -> TutorTopic | None:
    values = dict(row)
    raw_target = values.get("target")
    raw_level = values.get("level")
    raw_explain = values.get("explain")
    target = lookup_language(str(raw_target)) if raw_target is not None else None
    explain = (
        _validated_explanations(raw_explain, target.name if target else "")
        if raw_explain is not None
        else None
    )
    level = parse_level(str(raw_level)) if raw_level is not None else None
    chat_key = values.get("chat_key")
    thread_key = values.get("thread_key")
    auto_reply = values.get("auto_reply")
    set_by = values.get("set_by")
    updated_at = values.get("updated_at")
    valid_default = raw_target is None and raw_level is None
    valid_default |= target is not None and level in LEVELS
    if (
        not valid_default
        or (raw_explain is not None and target is None)
        or (raw_explain is not None and explain is None)
        or not isinstance(chat_key, str)
        or not isinstance(thread_key, str)
        or type(auto_reply) is not bool
        or not isinstance(set_by, str)
        or not isinstance(updated_at, datetime)
    ):
        log.warning("tutor topic invalid")
        return None
    return TutorTopic(
        chat_key,
        thread_key,
        target.name if target else None,
        level,
        explain,
        auto_reply,
        set_by,
        updated_at,
    )


def _validate_values(
    target: str, level: str, explain: str | tuple[str, ...]
) -> tuple[str, str, str]:
    target_language = lookup_language(target)
    parsed_level = parse_level(level)
    if target_language is None or parsed_level is None:
        raise ValueError("Invalid tutor profile values")
    explain_names = _encode_explanations(explain, target_language.name)
    if explain_names is None:
        raise ValueError("Invalid tutor profile values")
    return target_language.name, parsed_level, explain_names


class TutorRepo:
    def __init__(self, db: PgDatabase) -> None:
        if not isinstance(db, PgDatabase):
            raise TypeError("TutorRepo requires PgDatabase")
        self.db = db

    async def get(self, user_key: str) -> TutorProfile | None:
        row = await self.db.fetchrow("SELECT * FROM tutor_profiles WHERE user_key=$1", user_key)
        return _validated_profile(row) if row else None

    async def upsert(
        self, user_key: str, target: str, level: str, explain: str | tuple[str, ...]
    ) -> TutorProfile:
        target, level, explain = _validate_values(target, level, explain)
        row = await self.db.fetchrow(
            "INSERT INTO tutor_profiles (user_key,target,level,explain) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (user_key) DO UPDATE SET target=EXCLUDED.target,level=EXCLUDED.level,"
            "explain=EXCLUDED.explain,active=true,updated_at=now() RETURNING *",
            user_key,
            target,
            level,
            explain,
        )
        profile = _validated_profile(row)
        if profile is None:
            raise ValueError("Saved tutor profile is invalid")
        return profile

    async def set_active(self, user_key: str, active: bool) -> bool:
        row = await self.db.fetchrow(
            "UPDATE tutor_profiles SET active=$2,updated_at=now() "
            "WHERE user_key=$1 RETURNING user_key",
            user_key,
            active,
        )
        return row is not None

    async def delete(self, user_key: str) -> bool:
        result = await self.db.execute("DELETE FROM tutor_profiles WHERE user_key=$1", user_key)
        return result.endswith(" 1")

    async def get_topic(self, chat_key: str, thread_key: str) -> TutorTopic | None:
        row = await self.db.fetchrow(
            "SELECT * FROM tutor_topics WHERE chat_key=$1 AND thread_key=$2",
            chat_key,
            thread_key,
        )
        return _validated_topic(row) if row else None

    async def upsert_topic(
        self,
        chat_key: str,
        thread_key: str,
        target: str | None,
        level: str | None,
        explain: str | tuple[str, ...] | None,
        set_by: str,
        auto_reply: bool | None = None,
    ) -> TutorTopic:
        if target is None:
            if level is not None or explain is not None:
                raise ValueError("A topic default requires a target and level")
        else:
            target_language = lookup_language(target)
            parsed_level = parse_level(level or "")
            if target_language is None or parsed_level is None:
                raise ValueError("Invalid tutor topic values")
            explain = _encode_explanations(explain, target_language.name)
            target, level = target_language.name, parsed_level
        if auto_reply is not None and type(auto_reply) is not bool:
            raise ValueError("Auto-reply setting must be boolean or None")
        if isinstance(explain, tuple):
            explain = ",".join(explain)
        row = await self.db.fetchrow(
            "INSERT INTO tutor_topics (chat_key,thread_key,target,level,explain,set_by,auto_reply) "
            "VALUES ($1,$2,$3,$4,$5,$6,COALESCE($7,false)) "
            "ON CONFLICT (chat_key,thread_key) DO UPDATE SET "
            "target=EXCLUDED.target,level=EXCLUDED.level,explain=EXCLUDED.explain,"
            "set_by=EXCLUDED.set_by,auto_reply=COALESCE($7,tutor_topics.auto_reply),"
            "updated_at=now() RETURNING *",
            chat_key,
            thread_key,
            target,
            level,
            explain,
            set_by,
            auto_reply,
        )
        topic = _validated_topic(row)
        if topic is None:
            raise ValueError("Saved tutor topic is invalid")
        return topic

    async def delete_topic(self, chat_key: str, thread_key: str) -> bool:
        result = await self.db.execute(
            "DELETE FROM tutor_topics WHERE chat_key=$1 AND thread_key=$2",
            chat_key,
            thread_key,
        )
        return result.endswith(" 1")

    async def set_topic_auto_reply(self, chat_key: str, thread_key: str, auto_reply: bool) -> bool:
        if type(auto_reply) is not bool:
            raise ValueError("Auto-reply setting must be boolean")
        row = await self.db.fetchrow(
            "UPDATE tutor_topics SET auto_reply=$3,updated_at=now() "
            "WHERE chat_key=$1 AND thread_key=$2 RETURNING chat_key",
            chat_key,
            thread_key,
            auto_reply,
        )
        return row is not None

    async def purge_chat(self, chat_key: str) -> None:
        await self.db.execute("DELETE FROM tutor_topics WHERE chat_key=$1", chat_key)


class MemoryTutorRepo:
    def __init__(self) -> None:
        self._profiles: dict[str, TutorProfile] = {}
        self._topics: dict[tuple[str, str], TutorTopic] = {}

    async def get(self, user_key: str) -> TutorProfile | None:
        return self._profiles.get(user_key)

    async def upsert(
        self, user_key: str, target: str, level: str, explain: str | tuple[str, ...]
    ) -> TutorProfile:
        target, level, explain = _validate_values(target, level, explain)
        profile = TutorProfile(
            user_key, target, level, tuple(explain.split(",")), True, datetime.now(UTC)
        )
        self._profiles[user_key] = profile
        return profile

    async def set_active(self, user_key: str, active: bool) -> bool:
        profile = self._profiles.get(user_key)
        if profile is None:
            return False
        self._profiles[user_key] = TutorProfile(
            profile.user_key,
            profile.target,
            profile.level,
            profile.explain,
            active,
            datetime.now(UTC),
        )
        return True

    async def delete(self, user_key: str) -> bool:
        return self._profiles.pop(user_key, None) is not None

    async def get_topic(self, chat_key: str, thread_key: str) -> TutorTopic | None:
        return self._topics.get((chat_key, thread_key))

    async def upsert_topic(
        self,
        chat_key: str,
        thread_key: str,
        target: str | None,
        level: str | None,
        explain: str | tuple[str, ...] | None,
        set_by: str,
        auto_reply: bool | None = None,
    ) -> TutorTopic:
        if target is None:
            if level is not None or explain is not None:
                raise ValueError("A topic default requires a target and level")
        else:
            target_language = lookup_language(target)
            parsed_level = parse_level(level or "")
            if target_language is None or parsed_level is None:
                raise ValueError("Invalid tutor topic values")
            explain = _encode_explanations(explain, target_language.name)
            target, level = target_language.name, parsed_level
        if auto_reply is not None and type(auto_reply) is not bool:
            raise ValueError("Auto-reply setting must be boolean or None")
        if isinstance(explain, tuple):
            explain = ",".join(explain)
        existing = self._topics.get((chat_key, thread_key))
        topic = TutorTopic(
            chat_key,
            thread_key,
            target,
            level,
            tuple(explain.split(",")) if explain is not None else None,
            auto_reply if auto_reply is not None else (existing.auto_reply if existing else False),
            set_by,
            datetime.now(UTC),
        )
        self._topics[(chat_key, thread_key)] = topic
        return topic

    async def set_topic_auto_reply(self, chat_key: str, thread_key: str, auto_reply: bool) -> bool:
        if type(auto_reply) is not bool:
            raise ValueError("Auto-reply setting must be boolean")
        topic = self._topics.get((chat_key, thread_key))
        if topic is None:
            return False
        self._topics[(chat_key, thread_key)] = TutorTopic(
            topic.chat_key,
            topic.thread_key,
            topic.target,
            topic.level,
            topic.explain,
            auto_reply,
            topic.set_by,
            datetime.now(UTC),
        )
        return True

    async def delete_topic(self, chat_key: str, thread_key: str) -> bool:
        return self._topics.pop((chat_key, thread_key), None) is not None

    async def purge_chat(self, chat_key: str) -> None:
        self._topics = {key: value for key, value in self._topics.items() if key[0] != chat_key}
