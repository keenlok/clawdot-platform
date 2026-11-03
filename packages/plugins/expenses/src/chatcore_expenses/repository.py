"""Ledger persistence implementations; every method scopes rows by namespaced chat key."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from chatcore.events import ChatRef, UserRef
from chatcore.store.base import Database

from .currency import currency_for_country, format_amount


def _apply_trip_changes(trip: dict, changes: dict) -> dict:
    countries = list(trip["countries"])
    currencies = list(dict.fromkeys(code.upper() for code in trip["currencies"]))

    for country in changes.get("remove_countries") or []:
        remaining = [value for value in countries if value.casefold() != country.casefold()]
        if len(remaining) == len(countries):
            raise ValueError(f"{country} is not on this trip.")
        countries = remaining
    for country in changes.get("add_countries") or []:
        if not any(value.casefold() == country.casefold() for value in countries):
            countries.append(country)

    for code in changes.get("remove_currencies") or []:
        code = code.upper()
        if code not in currencies:
            raise ValueError(f"{code} is not on this trip.")
        currencies.remove(code)

    for code in [
        *(currency_for_country(country) for country in changes.get("add_countries") or []),
        *(changes.get("add_currencies") or []),
    ]:
        code = code.upper()
        if code not in currencies:
            currencies.append(code)

    if not countries:
        raise ValueError("A trip needs at least one country.")
    if not currencies:
        raise ValueError("A trip needs at least one currency.")

    updated = {
        "name": changes.get("name") if changes.get("name") is not None else trip["name"],
        "countries": countries,
        "currencies": currencies,
        "settle_mode": changes.get("settle_mode")
        if changes.get("settle_mode") is not None
        else trip["settle_mode"],
        "settle_currency": changes.get("settle_currency")
        if changes.get("settle_currency") is not None
        else trip["settle_currency"].upper(),
        "start_date": _as_date(changes.get("start_date"))
        if changes.get("start_date") is not None
        else trip.get("start_date"),
        "end_date": _as_date(changes.get("end_date"))
        if changes.get("end_date") is not None
        else trip.get("end_date"),
    }
    if (
        updated["start_date"] is not None
        and updated["end_date"] is not None
        and updated["end_date"] < updated["start_date"]
    ):
        raise ValueError("End date must be on or after start date.")
    return updated


def _as_date(value: date | str | None) -> date | None:
    return date.fromisoformat(value) if isinstance(value, str) else value


def _expense_snapshot(expense: dict) -> dict:
    return {
        "description": expense["description"],
        "amount": expense["amount"],
        "currency": expense["currency"],
        "paid_by": expense["paid_by"],
        "spent_on": _as_date(expense["spent_on"]).isoformat(),
        "trip_id": str(expense["trip_id"]) if expense.get("trip_id") else None,
        "fx_rate_to_settle": (
            str(expense["fx_rate_to_settle"])
            if expense.get("fx_rate_to_settle") is not None
            else None
        ),
        "shares": {share["user_key"]: share["amount"] for share in expense["shares"]}
        if isinstance(expense["shares"], list)
        else dict(expense["shares"]),
    }


def _inverse_echo(action: str, snapshot: dict, trip_name: str | None) -> str:
    trip = f"({trip_name})" if trip_name else "(general ledger)"
    amount = format_amount(snapshot["amount"], snapshot["currency"])
    if action == "delete":
        return f"Deleted: {snapshot['description']} {amount} {trip}."
    if action == "restore":
        return f"Restored: {snapshot['description']} {amount} {trip}."
    return f"Updated: {snapshot['description']} {trip}."


def _trip_change_lines(old: dict, new: dict) -> list[str]:
    lines = []
    for key, label, formatter in (
        ("name", "Name", str),
        ("countries", "Countries", lambda values: ", ".join(values)),
        ("currencies", "Currencies", lambda values: ", ".join(values)),
        ("settle_mode", "Settle mode", str),
        ("settle_currency", "Settle currency", str),
    ):
        old_value = old[key]
        new_value = new[key]
        if key in {"currencies", "settle_currency"}:
            old_value = (
                [value.upper() for value in old_value] if key == "currencies" else old_value.upper()
            )
            new_value = (
                [value.upper() for value in new_value] if key == "currencies" else new_value.upper()
            )
        if old_value != new_value:
            lines.append(f"{label}: {formatter(old_value)} → {formatter(new_value)}")
    old_dates = (old.get("start_date"), old.get("end_date"))
    new_dates = (new.get("start_date"), new.get("end_date"))
    if old_dates != new_dates:

        def fmt(values):
            return " → ".join(value.isoformat() if value else "Not set" for value in values)

        lines.append(f"Dates: {fmt(old_dates)} → {fmt(new_dates)}")
    return lines


def _check_trip_change_usage(
    old: dict, new: dict, changes: dict, expenses: list, settlements: list
):
    removed = {code.upper() for code in changes.get("remove_currencies") or []}
    for code in removed:
        used = any(expense["currency"].upper() == code for expense in expenses) or any(
            settlement["currency"].upper() == code for settlement in settlements
        )
        if used:
            raise ValueError(
                f"{code} is used by expenses or settlements on this trip; it can't be removed."
            )
    settle_changed = (
        old["settle_mode"] != new["settle_mode"]
        or old["settle_currency"].upper() != new["settle_currency"].upper()
    )
    if settle_changed and (expenses or settlements):
        raise ValueError(
            "Settle mode and settle currency can't change after expenses or settlements "
            "are recorded."
        )


def _check_trip_settle_settings(trip: dict, payload: dict) -> None:
    if (
        trip["settle_mode"] != payload["trip_settle_mode"]
        or trip["settle_currency"].upper() != payload.get("trip_settle_currency", "").upper()
    ):
        raise ValueError("The trip's settle settings changed since; add it again instead.")


def _snapshot_with_trip_settings(snapshot: dict, trip: dict | None) -> dict:
    result = dict(snapshot)
    if trip is not None:
        result["trip_settle_mode"] = trip["settle_mode"]
        result["trip_settle_currency"] = trip["settle_currency"].upper()
    return result


def _check_legacy_restore(trip: dict, snapshot: dict) -> None:
    """An expense deleted before change history existed has no record of the settle settings
    its FX rate was taken for, so refuse when that rate could now be for the wrong currency."""
    if (
        trip["settle_mode"] == "convert"
        and snapshot.get("fx_rate_to_settle") is not None
        and snapshot["currency"].upper() != trip["settle_currency"].upper()
    ):
        raise ValueError(
            "This expense was deleted before change history existed, so its FX rate can't be "
            "checked; add it again instead."
        )


def _check_destination_trip(trip: dict, snapshot: dict) -> None:
    if trip["status"] == "closed":
        raise ValueError("The trip is closed. Reopen it first.")
    if "trip_settle_mode" in snapshot and (
        trip["settle_mode"] != snapshot["trip_settle_mode"]
        or trip["settle_currency"].upper() != snapshot.get("trip_settle_currency", "").upper()
    ):
        raise ValueError("The trip's settle settings changed since; add it again instead.")


class LedgerRepo(Protocol):
    async def trips(
        self, chat: ChatRef, *, statuses: Any = None, closed_limit: int | None = None
    ) -> list[dict]: ...
    async def default_trip_id(self, chat: ChatRef) -> str | None: ...
    async def trip(self, chat: ChatRef, trip_id: str | None = None) -> dict | None: ...
    async def expenses(
        self,
        chat: ChatRef,
        *,
        trip_id: str | None = None,
        general_only: bool = False,
        limit: int | None = 1000,
    ) -> list[dict]: ...
    async def settlements(
        self, chat: ChatRef, *, trip_id: str | None = None, general_only: bool = False
    ) -> list[dict]: ...
    async def get_expense(self, chat: ChatRef, expense_id: str) -> dict | None: ...
    async def trip_members(self, chat: ChatRef, trip_id: str) -> list[dict]: ...
    async def has_undo(self, chat: ChatRef, actor: UserRef) -> bool: ...
    async def latest_undoable(self, chat: ChatRef, actor: UserRef) -> dict | None: ...
    async def fx_override(
        self, chat: ChatRef, trip_id: str, currency: str, settle_currency: str
    ) -> Decimal | str | None: ...
    async def commit(
        self, chat: ChatRef, kind: str, payload: dict, *, actor: UserRef, admin: bool = False
    ) -> str: ...
    async def purge_chat(self, chat: ChatRef) -> None: ...


class MemoryLedgerRepo:
    """Isolated in-memory ledger backend for tests and small offline apps."""

    def __init__(self) -> None:
        self.trip_rows: dict[str, dict] = {}
        self.defaults: dict[str, str] = {}
        self.expense_rows: dict[str, dict] = {}
        self.settlement_rows: dict[str, dict] = {}
        self.overrides: list[dict] = []
        self.member_rows: dict[tuple[str, str], dict] = {}
        self.expense_events: list[dict] = []
        self._event_id = 0

    async def trips(
        self, chat: ChatRef, *, statuses=None, closed_limit: int | None = None
    ) -> list[dict]:
        rows = [t for t in self.trip_rows.values() if t["chat_key"] == chat.key]
        if statuses is not None:
            wanted = set(statuses)
            rows = [t for t in rows if t["status"] in wanted]
        opened = sorted(
            (t for t in rows if t["status"] != "closed"),
            key=lambda t: (
                t.get("start_date") is None,
                t.get("start_date") or date.max,
                t["started_at"],
            ),
        )
        closed = sorted(
            (t for t in rows if t["status"] == "closed"),
            key=lambda t: t["ended_at"] or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        if closed_limit is not None:
            closed = closed[:closed_limit]
        default = self.defaults.get(chat.key)
        return [
            dict(t)
            | {
                "is_default": t["id"] == default,
                "members": [row["user_key"] for row in await self.trip_members(chat, t["id"])],
            }
            for t in [*opened, *closed]
        ]

    async def default_trip_id(self, chat: ChatRef) -> str | None:
        trip_id = self.defaults.get(chat.key)
        trip = self.trip_rows.get(trip_id) if trip_id else None
        if trip and trip["chat_key"] == chat.key and trip["status"] == "active":
            return trip_id
        return None

    async def trip(self, chat: ChatRef, trip_id: str | None = None) -> dict | None:
        if trip_id:
            found = self.trip_rows.get(trip_id)
            if found and found["chat_key"] == chat.key:
                return dict(found) | {
                    "is_default": self.defaults.get(chat.key) == trip_id,
                    "members": [r["user_key"] for r in await self.trip_members(chat, trip_id)],
                }
            if len(trip_id) == 8 and all(c in "0123456789abcdefABCDEF" for c in trip_id):
                prefixes = [
                    trip
                    for trip in self.trip_rows.values()
                    if trip["chat_key"] == chat.key
                    and trip["id"].replace("-", "").lower().startswith(trip_id.lower())
                ]
                if len(prefixes) == 1:
                    found = prefixes[0]
                    return dict(found) | {
                        "is_default": self.defaults.get(chat.key) == found["id"],
                        "members": [
                            r["user_key"] for r in await self.trip_members(chat, found["id"])
                        ],
                    }
            matches = [
                trip
                for trip in self.trip_rows.values()
                if trip["chat_key"] == chat.key and trip["name"].casefold() == trip_id.casefold()
            ]
            if matches:
                opened = [trip for trip in matches if trip["status"] != "closed"]
                found = opened[0] if opened else max(matches, key=lambda trip: trip["started_at"])
                return dict(found) | {
                    "is_default": self.defaults.get(chat.key) == found["id"],
                    "members": [r["user_key"] for r in await self.trip_members(chat, found["id"])],
                }
            return None
        all_trips = [t for t in self.trip_rows.values() if t["chat_key"] == chat.key]
        if not all_trips:
            return None
        found = max(all_trips, key=lambda t: t["started_at"])
        return dict(found) | {
            "is_default": self.defaults.get(chat.key) == found["id"],
            "members": [r["user_key"] for r in await self.trip_members(chat, found["id"])],
        }

    async def trip_members(self, chat: ChatRef, trip_id: str) -> list[dict]:
        trip = self.trip_rows.get(trip_id)
        if not trip or trip["chat_key"] != chat.key:
            return []
        return sorted(
            (dict(row) for (tid, _), row in self.member_rows.items() if tid == trip_id),
            key=lambda row: (row["added_at"], row["user_key"]),
        )

    async def has_undo(self, chat: ChatRef, actor: UserRef) -> bool:
        return any(
            event["chat_key"] == chat.key
            and event["actor_key"] == actor.key
            and event["undone_at"] is None
            and event["undo_of"] is None
            for event in self.expense_events
        )

    async def latest_undoable(self, chat: ChatRef, actor: UserRef) -> dict | None:
        candidates = [
            event
            for event in self.expense_events
            if event["chat_key"] == chat.key
            and event["actor_key"] == actor.key
            and event["undone_at"] is None
            and event["undo_of"] is None
        ]
        if not candidates:
            return None
        event = max(candidates, key=lambda item: item["id"])
        snapshot = event["before"] or event["after"]
        trip = self.trip_rows.get(snapshot.get("trip_id")) if snapshot.get("trip_id") else None
        return {
            "id": event["id"],
            "action": event["action"],
            "snapshot": dict(snapshot),
            "trip_name": trip["name"] if trip else None,
        }

    async def expenses(
        self,
        chat: ChatRef,
        *,
        trip_id: str | None = None,
        general_only: bool = False,
        limit: int | None = 1000,
    ) -> list[dict]:
        rows = [
            e
            for e in self.expense_rows.values()
            if e["chat_key"] == chat.key and e["deleted_at"] is None
        ]
        if trip_id is not None:
            rows = [e for e in rows if e["trip_id"] == trip_id]
        if general_only:
            rows = [e for e in rows if e["trip_id"] is None]
        selected = sorted(rows, key=lambda e: e["created_at"], reverse=True)[:limit]
        return [
            dict(expense)
            | {
                "shares": [
                    {"user_key": user_key, "amount": amount}
                    for user_key, amount in expense["shares"].items()
                ]
            }
            for expense in selected
        ]

    async def settlements(
        self, chat: ChatRef, *, trip_id: str | None = None, general_only: bool = False
    ) -> list[dict]:
        return [
            dict(s)
            for s in self.settlement_rows.values()
            if s["chat_key"] == chat.key
            and (trip_id is None or s["trip_id"] == trip_id)
            and (not general_only or s["trip_id"] is None)
        ]

    async def get_expense(
        self, chat: ChatRef, expense_id: str, *, include_deleted: bool = False
    ) -> dict | None:
        matches = [
            row
            for row in self.expense_rows.values()
            if row["chat_key"] == chat.key
            and (include_deleted or row["deleted_at"] is None)
            and _matches_expense_id(row["id"], expense_id)
        ]
        if len(matches) != 1:
            return None
        row = dict(matches[0])
        row["shares"] = [
            {"user_key": user_key, "amount": amount} for user_key, amount in row["shares"].items()
        ]
        return row

    async def fx_override(
        self, chat: ChatRef, trip_id: str, currency: str, settle_currency: str
    ) -> Decimal | str | None:
        item = next(
            (
                o
                for o in reversed(self.overrides)
                if o["chat_key"] == chat.key
                and o["trip_id"] == trip_id
                and o["currency"] == currency
                and o["settle_currency"] == settle_currency
            ),
            None,
        )
        return item["rate"] if item else None

    async def commit(
        self, chat: ChatRef, kind: str, payload: dict, *, actor: UserRef, admin: bool = False
    ) -> str:
        now = datetime.now(UTC)
        if kind == "start_trip":
            status = payload.get("status", "active")
            if status not in {"planned", "active"}:
                raise ValueError("Trip status must be planned or active.")
            start_date = _as_date(payload.get("start_date"))
            end_date = _as_date(payload.get("end_date"))
            if start_date is not None and end_date is not None and end_date < start_date:
                raise ValueError("End date must be on or after start date.")
            if any(
                t["chat_key"] == chat.key
                and t["status"] != "closed"
                and t["name"].casefold() == payload["name"].casefold()
                for t in self.trip_rows.values()
            ):
                raise ValueError(
                    f"A trip named {payload['name']} is already planned or active in this chat."
                )
            trip_id = str(uuid4())
            self.trip_rows[trip_id] = {
                **payload,
                "id": trip_id,
                "chat_key": chat.key,
                "status": status,
                "start_date": start_date,
                "end_date": end_date,
                "started_at": now,
                "ended_at": None,
            }
            for user_key in dict.fromkeys(payload.get("members", [])):
                self.member_rows[(trip_id, user_key)] = {
                    "trip_id": trip_id,
                    "user_key": user_key,
                    "added_by": actor.key,
                    "added_at": now,
                }
            if status == "active":
                self.defaults[chat.key] = trip_id
            return f"{'Started' if status == 'active' else 'Planned'} trip {payload['name']}."
        if kind == "add_expense":
            if payload.get("trip_id") is not None:
                trip = self.trip_rows.get(str(payload["trip_id"]))
                if not trip or trip["chat_key"] != chat.key:
                    raise ValueError("The trip changed; propose this again.")
                if trip["status"] == "closed":
                    raise ValueError("The trip was closed; propose this again.")
                if "trip_settle_mode" in payload:
                    _check_trip_settle_settings(trip, payload)
            if sum(payload["shares"].values()) != payload["amount"]:
                raise ValueError("Expense shares must sum exactly to the expense amount.")
            expense_id = str(payload.get("expense_id") or uuid4())
            self.expense_rows[expense_id] = {
                **payload,
                "id": expense_id,
                "chat_key": chat.key,
                "created_at": now,
                "deleted_at": None,
            }
            self._append_expense_event(
                chat,
                expense_id,
                actor,
                "add",
                None,
                self._event_snapshot(self.expense_rows[expense_id]),
            )
            return f"Saved {payload['description']}."
        if kind == "delete_expense":
            expense = self.expense_rows.get(payload["expense_id"])
            if not expense or expense["chat_key"] != chat.key or expense["deleted_at"] is not None:
                raise ValueError("Expense not found.")
            if not admin and expense["created_by"] != actor.key:
                raise PermissionError("Only the creator or an admin can delete this expense.")
            before = self._event_snapshot(expense)
            expense["deleted_at"] = now
            self._append_expense_event(chat, expense["id"], actor, "delete", before, None)
            return f"Deleted: {expense['description']} {before['currency']} {before['amount']}"
        if kind == "restore_expense":
            expense = self.expense_rows.get(payload["expense_id"])
            if not expense or expense["chat_key"] != chat.key or expense["deleted_at"] is None:
                raise ValueError("Expense not found or is not deleted.")
            if not admin and expense["created_by"] != actor.key:
                raise PermissionError("Only the creator or an admin can restore this expense.")
            trip = self.trip_rows.get(expense["trip_id"]) if expense.get("trip_id") else None
            if expense.get("trip_id") and not trip:
                raise ValueError("The trip was not found.")
            prior = next(
                (
                    event
                    for event in reversed(self.expense_events)
                    if event["expense_id"] == expense["id"]
                    and event["chat_key"] == chat.key
                    and (event["before"] is not None or event["after"] is not None)
                ),
                None,
            )
            saved = (
                (prior or {}).get("before")
                or (prior or {}).get("after")
                or _expense_snapshot(expense)
            )
            if trip:
                _check_destination_trip(trip, saved)
                if prior is None:
                    _check_legacy_restore(trip, saved)
            expense["deleted_at"] = None
            after = self._event_snapshot(expense)
            self._append_expense_event(chat, expense["id"], actor, "restore", None, after)
            return f"Restored: {expense['description']} {after['currency']} {after['amount']}"
        if kind == "update_expense":
            expense = self.expense_rows.get(payload["expense_id"])
            if not expense or expense["chat_key"] != chat.key or expense["deleted_at"] is not None:
                raise ValueError("Expense not found.")
            if not admin and expense["created_by"] != actor.key:
                raise PermissionError("Only the creator or an admin can update this expense.")
            before = _expense_snapshot(expense)
            if before != payload["expected"]:
                raise ValueError("The expense changed since; ask again.")
            after = payload["after"]
            if before == after:
                raise ValueError("Nothing to change.")
            if after.get("trip_id"):
                trip = self.trip_rows.get(str(after["trip_id"]))
                if not trip or trip["chat_key"] != chat.key or trip["status"] == "closed":
                    raise ValueError("The trip is closed or was not found.")
                if "trip_settle_mode" in payload:
                    _check_trip_settle_settings(trip, payload)
            self._write_snapshot(expense, after)
            before_event = self._event_snapshot(expense, before)
            after_event = self._event_snapshot(expense, after)
            self._append_expense_event(
                chat, expense["id"], actor, "update", before_event, after_event
            )
            return f"Updated: {after['description']}"
        if kind == "undo_expense_change":
            candidates = [
                event
                for event in self.expense_events
                if event["chat_key"] == chat.key
                and event["actor_key"] == actor.key
                and event["undone_at"] is None
                and event["undo_of"] is None
            ]
            if not candidates:
                raise ValueError("You have no expense change to undo in this chat.")
            event = max(candidates, key=lambda item: item["id"])
            if payload.get("event_id") is not None and int(payload["event_id"]) != event["id"]:
                raise ValueError("Your latest change is different now; ask again.")
            expense = self.expense_rows.get(event["expense_id"])
            if not expense or expense["chat_key"] != chat.key:
                raise ValueError("Expense not found.")
            later = [
                item
                for item in self.expense_events
                if item["chat_key"] == chat.key
                and item["expense_id"] == event["expense_id"]
                and item["id"] > event["id"]
                and item["undo_of"] is None
                and item["undone_at"] is None
            ]
            if later:
                raise ValueError(
                    "It was changed again after that; undo that change first or edit it directly."
                )
            current = _expense_snapshot(expense)
            trip = self.trip_rows.get(current["trip_id"]) if current["trip_id"] else None
            if current["trip_id"] and not trip:
                raise ValueError("The trip was not found.")
            if trip and trip["status"] == "closed":
                raise ValueError("The trip is closed. Reopen it first.")
            if event["action"] in {"add", "restore"}:
                inverse, before, after = "delete", current, None
            elif event["action"] == "delete":
                original = event["before"] or current
                destination = (
                    self.trip_rows.get(original.get("trip_id")) if original.get("trip_id") else None
                )
                if original.get("trip_id") and not destination:
                    raise ValueError("The trip was not found.")
                if destination:
                    _check_destination_trip(destination, original)
                inverse, before, after = "restore", None, current
            else:
                before = current
                after = event["before"]
                destination = (
                    self.trip_rows.get(after.get("trip_id")) if after.get("trip_id") else None
                )
                if after.get("trip_id") and not destination:
                    raise ValueError("The trip was not found.")
                if destination:
                    _check_destination_trip(destination, after)
                inverse = "update"
            echo_snapshot = current if inverse in {"delete", "restore"} else after
            # All validation completes before any in-memory state is changed.
            if inverse == "delete":
                expense["deleted_at"] = now
            elif inverse == "restore":
                expense["deleted_at"] = None
            else:
                self._write_snapshot(expense, after)
            event["undone_at"] = now
            self._append_expense_event(
                chat,
                expense["id"],
                actor,
                inverse,
                self._event_snapshot(expense, before) if before else None,
                self._event_snapshot(expense, after) if after else None,
                undo_of=event["id"],
            )
            echo_trip = (
                self.trip_rows.get(echo_snapshot.get("trip_id"))
                if echo_snapshot.get("trip_id")
                else None
            )
            return "Undid your last change: " + _inverse_echo(
                inverse, echo_snapshot, echo_trip["name"] if echo_trip else None
            )
        if kind == "update_trip_members":
            trip = self.trip_rows.get(str(payload["trip_id"]))
            if not trip or trip["chat_key"] != chat.key or trip["status"] == "closed":
                raise ValueError("The trip is closed or was not found.")
            added, removed = [], []
            for user_key in dict.fromkeys(payload.get("add", [])):
                key = (trip["id"], user_key)
                if key not in self.member_rows:
                    self.member_rows[key] = {
                        "trip_id": trip["id"],
                        "user_key": user_key,
                        "added_by": actor.key,
                        "added_at": now,
                    }
                    added.append(user_key)
            for user_key in dict.fromkeys(payload.get("remove", [])):
                if self.member_rows.pop((trip["id"], user_key), None):
                    removed.append(user_key)
            return {"added": added, "removed": removed}
        if kind == "settle_up":
            if payload.get("trip_id") is not None:
                trip = self.trip_rows.get(str(payload["trip_id"]))
                if not trip or trip["chat_key"] != chat.key:
                    raise ValueError("The trip changed; propose this again.")
                if trip["status"] == "closed":
                    raise ValueError("The trip was closed; propose this again.")
                if "trip_settle_mode" in payload:
                    _check_trip_settle_settings(trip, payload)
            settlement_id = str(uuid4())
            self.settlement_rows[settlement_id] = {
                **payload,
                "id": settlement_id,
                "chat_key": chat.key,
                "created_at": now,
            }
            return "Settlement recorded."
        if kind == "set_fx_rate":
            trip = self.trip_rows.get(str(payload.get("trip_id")))
            if trip and (trip["chat_key"] != chat.key or trip["status"] == "closed"):
                trip = None
            if "trip_settle_mode" in payload:
                if not trip or trip["id"] != str(payload.get("trip_id")):
                    raise ValueError("The trip's settle settings changed; propose this again.")
                _check_trip_settle_settings(trip, payload)
            if not trip:
                raise ValueError(
                    "FX rates can only be set during a planned or active convert trip."
                )
            if trip["settle_mode"] != "convert":
                raise ValueError("FX rates can only be set during a convert trip.")
            self.overrides.append(
                {
                    **payload,
                    "trip_id": trip["id"],
                    "settle_currency": trip["settle_currency"],
                    "chat_key": chat.key,
                    "set_by": actor.key,
                    "created_at": now,
                }
            )
            for expense_id in payload.get("expense_ids", []):
                expense = self.expense_rows.get(expense_id)
                if (
                    expense
                    and expense["chat_key"] == chat.key
                    and expense["trip_id"] == trip["id"]
                    and expense["currency"] == payload["currency"]
                    and expense["fx_rate_to_settle"] is None
                ):
                    expense["fx_rate_to_settle"] = payload["rate"]
            return "FX rate updated."
        if kind == "end_trip":
            trip = self.trip_rows.get(str(payload["trip_id"]))
            expected_status = payload.get("expected_status")
            if (
                not trip
                or trip["chat_key"] != chat.key
                or expected_status not in {"planned", "active"}
                or trip["status"] != expected_status
            ):
                raise ValueError("The trip changed; propose this again.")
            stored = self.trip_rows[trip["id"]]
            stored["status"] = "closed"
            stored["ended_at"] = now
            if self.defaults.get(chat.key) == trip["id"]:
                self.defaults.pop(chat.key, None)
            return "Trip closed."
        if kind in {"activate_trip", "reopen_trip"}:
            trip = self.trip_rows.get(str(payload["trip_id"]))
            required_status = "planned" if kind == "activate_trip" else "closed"
            if (
                not trip
                or trip["chat_key"] != chat.key
                or payload.get("expected_status") != required_status
                or trip["status"] != payload.get("expected_status")
            ):
                raise ValueError("The trip changed; propose this again.")
            new_status = "active" if kind == "activate_trip" else payload["status"]
            if any(
                t["chat_key"] == chat.key
                and t["id"] != trip["id"]
                and t["status"] != "closed"
                and t["name"].casefold() == trip["name"].casefold()
                for t in self.trip_rows.values()
            ):
                raise ValueError(
                    f"A trip named {trip['name']} is already planned or active in this chat."
                )
            trip["status"] = new_status
            trip["ended_at"] = None
            if new_status == "active":
                self.defaults[chat.key] = trip["id"]
            elif self.defaults.get(chat.key) == trip["id"]:
                self.defaults.pop(chat.key, None)
            verb = "Activated" if kind == "activate_trip" else "Reopened"
            return f"{verb} trip {trip['name']} as {new_status}."
        if kind == "set_default_trip":
            trip = self.trip_rows.get(str(payload["trip_id"]))
            if not trip or trip["chat_key"] != chat.key or trip["status"] != "active":
                raise ValueError("Only an active trip can be the default.")
            self.defaults[chat.key] = trip["id"]
            return f"Default trip set to {trip['name']}."
        if kind == "update_trip":
            trip = self.trip_rows.get(str(payload["trip_id"]))
            if (
                not trip
                or trip["chat_key"] != chat.key
                or trip["status"] not in {"planned", "active"}
            ):
                raise ValueError("The trip changed or ended; propose the update again.")
            new = _apply_trip_changes(trip, payload)
            if any(
                t["chat_key"] == chat.key
                and t["id"] != trip["id"]
                and t["status"] != "closed"
                and t["name"].casefold() == new["name"].casefold()
                for t in self.trip_rows.values()
            ):
                raise ValueError(
                    f"A trip named {new['name']} is already planned or active in this chat."
                )
            expenses = await self.expenses(chat, trip_id=trip["id"], limit=None)
            settlements = await self.settlements(chat, trip_id=trip["id"])
            _check_trip_change_usage(trip, new, payload, expenses, settlements)
            lines = _trip_change_lines(trip, new)
            if not lines:
                raise ValueError("Nothing to change on this trip.")
            stored = self.trip_rows[trip["id"]]
            stored.update(new)
            return (
                f"Updated trip {new['name']}:\n"
                + "\n".join(lines)
                + '\nSay "undo" to change it back.'
            )
        raise ValueError(f"Unknown action kind: {kind}")

    def _append_expense_event(
        self, chat, expense_id, actor, action, before, after, *, undo_of=None
    ) -> dict:
        self._event_id += 1
        event = {
            "id": self._event_id,
            "chat_key": chat.key,
            "expense_id": str(expense_id),
            "actor_key": actor.key,
            "action": action,
            "before": before,
            "after": after,
            "undo_of": undo_of,
            "undone_at": None,
            "created_at": datetime.now(UTC),
        }
        self.expense_events.append(event)
        return event

    def _event_snapshot(self, expense: dict, snapshot: dict | None = None) -> dict:
        value = snapshot or _expense_snapshot(expense)
        trip = self.trip_rows.get(value.get("trip_id")) if value.get("trip_id") else None
        return _snapshot_with_trip_settings(value, trip)

    @staticmethod
    def _write_snapshot(expense: dict, snapshot: dict) -> None:
        for key in (
            "description",
            "amount",
            "currency",
            "paid_by",
            "spent_on",
            "trip_id",
            "fx_rate_to_settle",
        ):
            expense[key] = snapshot[key]
        expense["shares"] = dict(snapshot["shares"])

    async def purge_chat(self, chat: ChatRef) -> None:
        trip_ids = {key for key, row in self.trip_rows.items() if row["chat_key"] == chat.key}
        for store in (self.expense_rows, self.settlement_rows, self.trip_rows):
            for key in [key for key, row in store.items() if row["chat_key"] == chat.key]:
                del store[key]
        self.defaults.pop(chat.key, None)
        self.overrides = [row for row in self.overrides if row["chat_key"] != chat.key]
        self.member_rows = {
            key: row for key, row in self.member_rows.items() if key[0] not in trip_ids
        }
        self.expense_events = [row for row in self.expense_events if row["chat_key"] != chat.key]


class PostgresLedgerRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def trips(
        self, chat: ChatRef, *, statuses=None, closed_limit: int | None = None
    ) -> list[dict]:
        rows = await self.db.fetch(
            """SELECT t.*, (d.trip_id=t.id) AS is_default, COALESCE((SELECT array_agg(
                   tm.user_key ORDER BY tm.added_at, tm.user_key
               ) FROM trip_members tm WHERE tm.trip_id=t.id), ARRAY[]::text[]) AS members
               FROM trips t
               LEFT JOIN trip_defaults d ON d.chat_key=t.chat_key
               WHERE t.chat_key=$1 AND ($2::text[] IS NULL OR t.status=ANY($2::text[]))
               ORDER BY (t.status='closed'),
                 CASE WHEN t.status <> 'closed' THEN t.start_date END ASC NULLS LAST,
                 CASE WHEN t.status <> 'closed' THEN t.started_at END ASC,
                 CASE WHEN t.status='closed' THEN t.ended_at END DESC""",
            chat.key,
            list(statuses) if statuses is not None else None,
        )
        items = [_pg_row(row) for row in rows]
        if closed_limit is not None:
            opened = [item for item in items if item["status"] != "closed"]
            closed = [item for item in items if item["status"] == "closed"][:closed_limit]
            items = opened + closed
        return items

    async def default_trip_id(self, chat: ChatRef) -> str | None:
        row = await self.db.fetchrow(
            """SELECT d.trip_id FROM trip_defaults d JOIN trips t ON t.id=d.trip_id
               WHERE d.chat_key=$1 AND t.chat_key=$1 AND t.status='active'""",
            chat.key,
        )
        return str(row["trip_id"]) if row else None

    async def trip(self, chat: ChatRef, trip_id: str | None = None) -> dict | None:
        if trip_id:
            if _is_uuid(trip_id):
                row = await self.db.fetchrow(
                    """SELECT t.*,(d.trip_id=t.id) AS is_default, COALESCE((SELECT array_agg(
                   tm.user_key ORDER BY tm.added_at, tm.user_key
               ) FROM trip_members tm WHERE tm.trip_id=t.id), ARRAY[]::text[]) AS members
               FROM trips t
                       LEFT JOIN trip_defaults d ON d.chat_key=t.chat_key
                       WHERE t.chat_key=$1 AND t.id=$2""",
                    chat.key,
                    UUID(trip_id),
                )
            elif len(trip_id) == 8 and all(c in "0123456789abcdefABCDEF" for c in trip_id):
                rows = await self.db.fetch(
                    """SELECT t.*,(d.trip_id=t.id) AS is_default, COALESCE((SELECT array_agg(
                   tm.user_key ORDER BY tm.added_at, tm.user_key
               ) FROM trip_members tm WHERE tm.trip_id=t.id), ARRAY[]::text[]) AS members
               FROM trips t
                       LEFT JOIN trip_defaults d ON d.chat_key=t.chat_key
                       WHERE t.chat_key=$1 AND replace(t.id::text,'-','') LIKE $2 LIMIT 2""",
                    chat.key,
                    trip_id.lower() + "%",
                )
                row = rows[0] if len(rows) == 1 else None
            else:
                row = await self.db.fetchrow(
                    """SELECT t.*,(d.trip_id=t.id) AS is_default, COALESCE((SELECT array_agg(
                   tm.user_key ORDER BY tm.added_at, tm.user_key
               ) FROM trip_members tm WHERE tm.trip_id=t.id), ARRAY[]::text[]) AS members
               FROM trips t
                       LEFT JOIN trip_defaults d ON d.chat_key=t.chat_key
                       WHERE t.chat_key=$1 AND lower(t.name)=lower($2)
                       ORDER BY (t.status='closed'), t.started_at DESC LIMIT 1""",
                    chat.key,
                    trip_id,
                )
        else:
            row = await self.db.fetchrow(
                """SELECT t.*,(d.trip_id=t.id) AS is_default, COALESCE((SELECT array_agg(
                   tm.user_key ORDER BY tm.added_at, tm.user_key
               ) FROM trip_members tm WHERE tm.trip_id=t.id), ARRAY[]::text[]) AS members
               FROM trips t
                   LEFT JOIN trip_defaults d ON d.chat_key=t.chat_key
                   WHERE t.chat_key=$1 ORDER BY started_at DESC LIMIT 1""",
                chat.key,
            )
        return _pg_row(row) if row else None

    async def trip_members(self, chat: ChatRef, trip_id: str) -> list[dict]:
        rows = await self.db.fetch(
            """SELECT tm.trip_id,tm.user_key,tm.added_by,tm.added_at
               FROM trip_members tm JOIN trips t ON t.id=tm.trip_id
               WHERE t.chat_key=$1 AND tm.trip_id=$2 ORDER BY tm.added_at,tm.user_key""",
            chat.key,
            UUID(trip_id),
        )
        return [_pg_row(row) for row in rows]

    async def has_undo(self, chat: ChatRef, actor: UserRef) -> bool:
        return bool(
            await self.db.fetchval(
                """SELECT EXISTS(SELECT 1 FROM expense_events WHERE chat_key=$1 AND actor_key=$2
               AND undone_at IS NULL AND undo_of IS NULL)""",
                chat.key,
                actor.key,
            )
        )

    async def latest_undoable(self, chat: ChatRef, actor: UserRef) -> dict | None:
        row = await self.db.fetchrow(
            """SELECT e.id,e.action,e.before,e.after,t.name AS trip_name
               FROM expense_events e LEFT JOIN trips t ON t.id=COALESCE(
                 NULLIF(e.before->>'trip_id','')::uuid,NULLIF(e.after->>'trip_id','')::uuid)
                 AND t.chat_key=e.chat_key
               WHERE e.chat_key=$1 AND e.actor_key=$2 AND e.undone_at IS NULL
                 AND e.undo_of IS NULL ORDER BY e.id DESC LIMIT 1""",
            chat.key,
            actor.key,
        )
        if not row:
            return None
        return {
            "id": row["id"],
            "action": row["action"],
            "snapshot": _json_object(row["before"]) or _json_object(row["after"]),
            "trip_name": row["trip_name"],
        }

    async def expenses(
        self,
        chat: ChatRef,
        *,
        trip_id: str | None = None,
        general_only: bool = False,
        limit: int | None = 1000,
    ) -> list[dict]:
        rows = await self.db.fetch(
            """SELECT e.*,
                      COALESCE(
                          json_agg(json_build_object(
                                  'user_key', s.user_key, 'amount', s.amount
                              ))
                          FILTER (WHERE s.expense_id IS NOT NULL), '[]'
                      ) AS shares
               FROM expenses e LEFT JOIN expense_shares s ON s.expense_id = e.id
               WHERE e.chat_key = $1 AND e.deleted_at IS NULL
                 AND ($2::uuid IS NULL OR e.trip_id = $2)
                 AND (NOT $3::boolean OR e.trip_id IS NULL)
               GROUP BY e.id ORDER BY e.created_at DESC LIMIT $4""",
            chat.key,
            _uuid_or_none(trip_id),
            general_only,
            limit,
        )
        return [_pg_row(row) | {"shares": _json_list(row["shares"])} for row in rows]

    async def get_expense(
        self, chat: ChatRef, expense_id: str, *, include_deleted: bool = False
    ) -> dict | None:
        if _is_uuid(expense_id):
            rows = await self.db.fetch(
                """SELECT e.*,
                          COALESCE(
                              json_agg(json_build_object(
                                  'user_key', s.user_key, 'amount', s.amount
                              ))
                              FILTER (WHERE s.expense_id IS NOT NULL), '[]'
                          ) AS shares
                   FROM expenses e LEFT JOIN expense_shares s ON s.expense_id=e.id
                   WHERE e.chat_key=$1 AND ($3::boolean OR e.deleted_at IS NULL) AND e.id=$2
                   GROUP BY e.id""",
                chat.key,
                UUID(expense_id),
                include_deleted,
            )
        elif len(expense_id) == 8 and all(char in "0123456789abcdefABCDEF" for char in expense_id):
            rows = await self.db.fetch(
                """SELECT e.*,
                          COALESCE(
                              json_agg(json_build_object(
                                  'user_key', s.user_key, 'amount', s.amount
                              ))
                              FILTER (WHERE s.expense_id IS NOT NULL), '[]'
                          ) AS shares
                   FROM expenses e LEFT JOIN expense_shares s ON s.expense_id=e.id
                   WHERE e.chat_key=$1 AND ($3::boolean OR e.deleted_at IS NULL)
                     AND e.id::text LIKE $2
                   GROUP BY e.id LIMIT 2""",
                chat.key,
                f"{expense_id.lower()}%",
                include_deleted,
            )
        else:
            return None
        if len(rows) != 1:
            return None
        return _pg_row(rows[0]) | {"shares": _json_list(rows[0]["shares"])}

    async def settlements(
        self, chat: ChatRef, *, trip_id: str | None = None, general_only: bool = False
    ) -> list[dict]:
        rows = await self.db.fetch(
            """SELECT * FROM settlements WHERE chat_key=$1
               AND ($2::uuid IS NULL OR trip_id=$2) AND (NOT $3::boolean OR trip_id IS NULL)""",
            chat.key,
            _uuid_or_none(trip_id),
            general_only,
        )
        return [_pg_row(row) for row in rows]

    async def fx_override(
        self, chat: ChatRef, trip_id: str, currency: str, settle_currency: str
    ) -> Decimal | str | None:
        return await self.db.fetchval(
            """SELECT rate FROM fx_overrides
               WHERE chat_key=$1 AND trip_id=$2 AND currency=$3 AND settle_currency=$4
               ORDER BY created_at DESC LIMIT 1""",
            chat.key,
            UUID(trip_id),
            currency,
            settle_currency,
        )

    async def commit(
        self, chat: ChatRef, kind: str, payload: dict, *, actor: UserRef, admin: bool = False
    ) -> str:
        async with self.db.transaction() as tx:
            return await self._commit(tx, chat, kind, payload, actor=actor, admin=admin)

    async def _commit(
        self, tx: Database, chat: ChatRef, kind: str, payload: dict, *, actor: UserRef, admin: bool
    ) -> str:
        if kind == "start_trip":
            row = await tx.fetchrow(
                """INSERT INTO trips(
                   chat_key,name,countries,currencies,settle_mode,settle_currency,status,start_date,end_date
               ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                chat.key,
                payload["name"],
                payload["countries"],
                payload["currencies"],
                payload["settle_mode"],
                payload["settle_currency"],
                payload.get("status", "active"),
                _as_date(payload.get("start_date")),
                _as_date(payload.get("end_date")),
            )
            for user_key in dict.fromkeys(payload.get("members", [])):
                await tx.execute(
                    "INSERT INTO trip_members(trip_id,user_key,added_by) VALUES($1,$2,$3)",
                    row["id"],
                    user_key,
                    actor.key,
                )
            if payload.get("status", "active") == "active":
                await tx.execute(
                    """INSERT INTO trip_defaults(chat_key,trip_id) VALUES($1,$2)
                       ON CONFLICT(chat_key) DO UPDATE SET trip_id=EXCLUDED.trip_id,set_at=now()""",
                    chat.key,
                    row["id"],
                )
            verb = "Started" if payload.get("status", "active") == "active" else "Planned"
            return f"{verb} trip {payload['name']}."
        if kind == "add_expense":
            trip = None
            if payload.get("trip_id") is not None:
                trip = await tx.fetchrow(
                    """SELECT settle_mode, settle_currency, status FROM trips
                       WHERE chat_key=$1 AND id=$2 FOR SHARE""",
                    chat.key,
                    UUID(payload["trip_id"]),
                )
                if not trip:
                    raise ValueError("The trip changed; propose this again.")
                if trip["status"] == "closed":
                    raise ValueError("The trip was closed; propose this again.")
                if "trip_settle_mode" in payload:
                    _check_trip_settle_settings(dict(trip), payload)
            if sum(payload["shares"].values()) != payload["amount"]:
                raise ValueError("Expense shares must sum exactly to the expense amount.")
            row = await tx.fetchrow(
                """INSERT INTO expenses(
                   id,chat_key,trip_id,description,amount,currency,paid_by,created_by,spent_on,
                   fx_rate_to_settle
               ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
                UUID(payload["expense_id"]) if payload.get("expense_id") else uuid4(),
                chat.key,
                _uuid_or_none(payload["trip_id"]),
                payload["description"],
                payload["amount"],
                payload["currency"],
                payload["paid_by"],
                payload["created_by"],
                date.fromisoformat(payload["spent_on"]),
                Decimal(payload["fx_rate_to_settle"])
                if payload["fx_rate_to_settle"] is not None
                else None,
            )
            for user_key, amount in payload["shares"].items():
                await tx.execute(
                    "INSERT INTO expense_shares(expense_id,user_key,amount) VALUES($1,$2,$3)",
                    row["id"],
                    user_key,
                    amount,
                )
            after = {
                "description": payload["description"],
                "amount": payload["amount"],
                "currency": payload["currency"],
                "paid_by": payload["paid_by"],
                "spent_on": _as_date(payload["spent_on"]).isoformat(),
                "trip_id": str(payload["trip_id"]) if payload.get("trip_id") else None,
                "fx_rate_to_settle": payload.get("fx_rate_to_settle"),
                "shares": payload["shares"],
            }
            after = _snapshot_with_trip_settings(after, dict(trip) if trip else None)
            await tx.execute(
                """INSERT INTO expense_events(chat_key,expense_id,actor_key,action,after)
                   VALUES($1,$2,$3,'add',$4::jsonb)""",
                chat.key,
                row["id"],
                actor.key,
                json.dumps(after),
            )
            return f"Saved {payload['description']}."
        if kind == "delete_expense":
            await tx.fetchval(
                "SELECT id FROM expenses WHERE chat_key=$1 AND id=$2 FOR UPDATE",
                chat.key,
                UUID(payload["expense_id"]),
            )
            row = await tx.fetchrow(
                """SELECT e.*, COALESCE((SELECT json_agg(json_build_object(
                       'user_key',s.user_key,'amount',s.amount)) FROM expense_shares s
                       WHERE s.expense_id=e.id),'[]') AS shares FROM expenses e
                   WHERE e.chat_key=$1 AND e.id=$2 AND e.deleted_at IS NULL""",
                chat.key,
                UUID(payload["expense_id"]),
            )
            if not row:
                raise ValueError("Expense not found.")
            if row["created_by"] != actor.key and not admin:
                raise PermissionError("Only the creator or an admin can delete this expense.")
            before = _expense_snapshot(_pg_row(row) | {"shares": _json_list(row["shares"])})
            trip_settings = None
            if before["trip_id"]:
                trip_settings = await tx.fetchrow(
                    """SELECT settle_mode,settle_currency FROM trips
                       WHERE chat_key=$1 AND id=$2 FOR SHARE""",
                    chat.key,
                    UUID(before["trip_id"]),
                )
            before = _snapshot_with_trip_settings(
                before, dict(trip_settings) if trip_settings else None
            )
            await tx.execute(
                "UPDATE expenses SET deleted_at=now() WHERE chat_key=$1 AND id=$2",
                chat.key,
                UUID(payload["expense_id"]),
            )
            await tx.execute(
                """INSERT INTO expense_events(chat_key,expense_id,actor_key,action,before)
                   VALUES($1,$2,$3,'delete',$4::jsonb)""",
                chat.key,
                UUID(payload["expense_id"]),
                actor.key,
                json.dumps(before),
            )
            return "Expense deleted."
        if kind in {"update_expense", "restore_expense"}:
            await tx.fetchval(
                "SELECT id FROM expenses WHERE chat_key=$1 AND id=$2 FOR UPDATE",
                chat.key,
                UUID(payload["expense_id"]),
            )
            row = await tx.fetchrow(
                """SELECT e.*, COALESCE((SELECT json_agg(json_build_object(
                       'user_key',s.user_key,'amount',s.amount)) FROM expense_shares s
                       WHERE s.expense_id=e.id),'[]') AS shares FROM expenses e
                   WHERE e.chat_key=$1 AND e.id=$2""",
                chat.key,
                UUID(payload["expense_id"]),
            )
            if not row:
                raise ValueError("Expense not found.")
            expense = _pg_row(row) | {"shares": _json_list(row["shares"])}
            if kind == "restore_expense":
                if expense["deleted_at"] is None:
                    raise ValueError("Expense not found or is not deleted.")
                if expense["created_by"] != actor.key and not admin:
                    raise PermissionError("Only the creator or an admin can restore this expense.")
                trip_id = expense.get("trip_id")
                stored = None
                prior = await tx.fetchrow(
                    """SELECT before,after FROM expense_events WHERE chat_key=$1 AND expense_id=$2
                       ORDER BY id DESC LIMIT 1""",
                    chat.key,
                    UUID(payload["expense_id"]),
                )
                if prior:
                    stored = _json_object(prior["before"]) or _json_object(prior["after"])
                trip_settings = None
                if trip_id:
                    trip_row = await tx.fetchrow(
                        """SELECT status,settle_mode,settle_currency FROM trips
                           WHERE chat_key=$1 AND id=$2 FOR SHARE""",
                        chat.key,
                        UUID(trip_id),
                    )
                    if not trip_row:
                        raise ValueError("The trip was not found.")
                    trip_settings = dict(trip_row)
                    _check_destination_trip(trip_settings, stored or _expense_snapshot(expense))
                    if not prior:
                        _check_legacy_restore(trip_settings, _expense_snapshot(expense))
                after = _snapshot_with_trip_settings(_expense_snapshot(expense), trip_settings)
                await tx.execute(
                    "UPDATE expenses SET deleted_at=NULL WHERE chat_key=$1 AND id=$2",
                    chat.key,
                    UUID(payload["expense_id"]),
                )
                await tx.execute(
                    """INSERT INTO expense_events(chat_key,expense_id,actor_key,action,after)
                       VALUES($1,$2,$3,'restore',$4::jsonb)""",
                    chat.key,
                    UUID(payload["expense_id"]),
                    actor.key,
                    json.dumps(after),
                )
                return "Expense restored."
            if expense["deleted_at"] is not None:
                raise ValueError("Expense not found.")
            if expense["created_by"] != actor.key and not admin:
                raise PermissionError("Only the creator or an admin can update this expense.")
            before = _expense_snapshot(expense)
            if before != payload["expected"]:
                raise ValueError("The expense changed since; ask again.")
            after = payload["after"]
            if before == after:
                raise ValueError("Nothing to change.")
            trip_id = after["trip_id"]
            trip_settings = None
            if trip_id:
                trip_row = await tx.fetchrow(
                    """SELECT status,settle_mode,settle_currency FROM trips
                       WHERE chat_key=$1 AND id=$2 FOR SHARE""",
                    chat.key,
                    UUID(trip_id),
                )
                if not trip_row or trip_row["status"] == "closed":
                    raise ValueError("The trip is closed or was not found.")
                trip_settings = dict(trip_row)
                if "trip_settle_mode" in payload:
                    _check_trip_settle_settings(trip_settings, payload)
            old_trip_settings = None
            if before["trip_id"]:
                old_trip_settings = await tx.fetchrow(
                    """SELECT settle_mode,settle_currency FROM trips
                       WHERE chat_key=$1 AND id=$2 FOR SHARE""",
                    chat.key,
                    UUID(before["trip_id"]),
                )
            before_event = _snapshot_with_trip_settings(
                before, dict(old_trip_settings) if old_trip_settings else None
            )
            after_event = _snapshot_with_trip_settings(after, trip_settings)
            await tx.execute(
                """UPDATE expenses SET trip_id=$3,description=$4,amount=$5,currency=$6,
                   paid_by=$7,spent_on=$8,fx_rate_to_settle=$9 WHERE chat_key=$1 AND id=$2""",
                chat.key,
                UUID(payload["expense_id"]),
                _uuid_or_none(after["trip_id"]),
                after["description"],
                after["amount"],
                after["currency"],
                after["paid_by"],
                date.fromisoformat(after["spent_on"]),
                Decimal(after["fx_rate_to_settle"]) if after["fx_rate_to_settle"] else None,
            )
            await tx.execute(
                "DELETE FROM expense_shares WHERE expense_id=$1", UUID(payload["expense_id"])
            )
            for user_key, amount in after["shares"].items():
                await tx.execute(
                    "INSERT INTO expense_shares(expense_id,user_key,amount) VALUES($1,$2,$3)",
                    UUID(payload["expense_id"]),
                    user_key,
                    amount,
                )
            await tx.execute(
                """INSERT INTO expense_events(chat_key,expense_id,actor_key,action,before,after)
                   VALUES($1,$2,$3,'update',$4::jsonb,$5::jsonb)""",
                chat.key,
                UUID(payload["expense_id"]),
                actor.key,
                json.dumps(before_event),
                json.dumps(after_event),
            )
            return "Expense updated."
        if kind == "update_trip_members":
            trip = await tx.fetchrow(
                "SELECT status,name FROM trips WHERE chat_key=$1 AND id=$2 FOR UPDATE",
                chat.key,
                UUID(payload["trip_id"]),
            )
            if not trip or trip["status"] == "closed":
                raise ValueError("The trip is closed or was not found.")
            added = []
            removed = []
            for user_key in dict.fromkeys(payload.get("add", [])):
                inserted = await tx.fetchrow(
                    """INSERT INTO trip_members(trip_id,user_key,added_by) VALUES($1,$2,$3)
                       ON CONFLICT(trip_id,user_key) DO NOTHING RETURNING user_key""",
                    UUID(payload["trip_id"]),
                    user_key,
                    actor.key,
                )
                if inserted:
                    added.append(inserted["user_key"])
            for user_key in dict.fromkeys(payload.get("remove", [])):
                deleted = await tx.fetchrow(
                    "DELETE FROM trip_members WHERE trip_id=$1 AND user_key=$2 RETURNING user_key",
                    UUID(payload["trip_id"]),
                    user_key,
                )
                if deleted:
                    removed.append(deleted["user_key"])
            return {"added": added, "removed": removed}
        if kind == "undo_expense_change":
            event_ref = await tx.fetchrow(
                """SELECT id,expense_id FROM expense_events WHERE chat_key=$1 AND actor_key=$2
                   AND undone_at IS NULL AND undo_of IS NULL ORDER BY id DESC LIMIT 1""",
                chat.key,
                actor.key,
            )
            if not event_ref:
                raise ValueError("You have no expense change to undo in this chat.")
            await tx.fetchval(
                "SELECT id FROM expenses WHERE chat_key=$1 AND id=$2 FOR UPDATE",
                chat.key,
                event_ref["expense_id"],
            )
            row = await tx.fetchrow(
                """SELECT e.*, COALESCE((SELECT json_agg(json_build_object(
                       'user_key',s.user_key,'amount',s.amount)) FROM expense_shares s
                       WHERE s.expense_id=e.id),'[]') AS shares FROM expenses e
                   WHERE e.chat_key=$1 AND e.id=$2""",
                chat.key,
                event_ref["expense_id"],
            )
            if not row:
                raise ValueError("Expense not found.")
            event = await tx.fetchrow(
                """SELECT * FROM expense_events WHERE id=$1 AND chat_key=$2 AND actor_key=$3
                   AND undone_at IS NULL AND undo_of IS NULL FOR UPDATE""",
                event_ref["id"],
                chat.key,
                actor.key,
            )
            if not event:
                raise ValueError("You have no expense change to undo in this chat.")
            latest_id = await tx.fetchval(
                """SELECT id FROM expense_events WHERE chat_key=$1 AND actor_key=$2
                   AND undone_at IS NULL AND undo_of IS NULL ORDER BY id DESC LIMIT 1""",
                chat.key,
                actor.key,
            )
            if payload.get("event_id") is not None and latest_id != int(payload["event_id"]):
                raise ValueError("Your latest change is different now; ask again.")
            if await tx.fetchval(
                """SELECT EXISTS(SELECT 1 FROM expense_events WHERE chat_key=$1 AND expense_id=$2
                   AND id>$3 AND undo_of IS NULL AND undone_at IS NULL)""",
                chat.key,
                event["expense_id"],
                event["id"],
            ):
                raise ValueError(
                    "It was changed again after that; undo that change first or edit it directly."
                )
            expense = _pg_row(row) | {"shares": _json_list(row["shares"])}
            current = _expense_snapshot(expense)
            action = event["action"]
            before = _json_object(event["before"])
            after = _json_object(event["after"])
            echo_snapshot = _snapshot_with_trip_settings(current, None)
            inv_before_trip = None
            inv_after_trip = None
            current_trip = None
            if current["trip_id"]:
                current_trip_row = await tx.fetchrow(
                    """SELECT status,settle_mode,settle_currency FROM trips
                       WHERE chat_key=$1 AND id=$2 FOR SHARE""",
                    chat.key,
                    UUID(current["trip_id"]),
                )
                if current_trip_row:
                    current_trip = dict(current_trip_row)
                    _check_destination_trip(current_trip, current)
            if action in {"add", "restore"}:
                await tx.execute(
                    "UPDATE expenses SET deleted_at=now() WHERE chat_key=$1 AND id=$2",
                    chat.key,
                    event["expense_id"],
                )
                inverse, inv_before, inv_after = "delete", current, None
                echo_snapshot = _snapshot_with_trip_settings(current, current_trip)
                inv_before_trip = current_trip
            elif action == "delete":
                target = before or current
                destination = None
                if target.get("trip_id"):
                    target_row = await tx.fetchrow(
                        """SELECT status,settle_mode,settle_currency FROM trips
                           WHERE chat_key=$1 AND id=$2 FOR SHARE""",
                        chat.key,
                        UUID(target["trip_id"]),
                    )
                    if not target_row:
                        raise ValueError("The trip was not found.")
                    destination = dict(target_row)
                    _check_destination_trip(destination, target)
                await tx.execute(
                    "UPDATE expenses SET deleted_at=NULL WHERE chat_key=$1 AND id=$2",
                    chat.key,
                    event["expense_id"],
                )
                inverse, inv_before, inv_after = "restore", None, current
                echo_snapshot = _snapshot_with_trip_settings(target, destination)
                inv_after_trip = destination
            else:
                target = before
                destination = None
                if target.get("trip_id"):
                    target_row = await tx.fetchrow(
                        """SELECT status,settle_mode,settle_currency FROM trips
                           WHERE chat_key=$1 AND id=$2 FOR SHARE""",
                        chat.key,
                        UUID(target["trip_id"]),
                    )
                    if not target_row:
                        raise ValueError("The trip was not found.")
                    destination = dict(target_row)
                    _check_destination_trip(destination, target)
                await tx.execute(
                    """UPDATE expenses SET description=$3,amount=$4,currency=$5,paid_by=$6,
                       spent_on=$7,trip_id=$8,fx_rate_to_settle=$9 WHERE chat_key=$1 AND id=$2""",
                    chat.key,
                    event["expense_id"],
                    target["description"],
                    target["amount"],
                    target["currency"],
                    target["paid_by"],
                    date.fromisoformat(target["spent_on"]),
                    _uuid_or_none(target["trip_id"]),
                    Decimal(target["fx_rate_to_settle"]) if target["fx_rate_to_settle"] else None,
                )
                await tx.execute(
                    "DELETE FROM expense_shares WHERE expense_id=$1", event["expense_id"]
                )
                for user_key, value in target["shares"].items():
                    await tx.execute(
                        "INSERT INTO expense_shares(expense_id,user_key,amount) VALUES($1,$2,$3)",
                        event["expense_id"],
                        user_key,
                        value,
                    )
                inverse, inv_before, inv_after = "update", current, target
                echo_snapshot = _snapshot_with_trip_settings(target, destination)
                inv_before_trip = current_trip
                inv_after_trip = destination
            await tx.execute("UPDATE expense_events SET undone_at=now() WHERE id=$1", event["id"])
            await tx.execute(
                """INSERT INTO expense_events(
                   chat_key,expense_id,actor_key,action,before,after,undo_of
               )
                   VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7)""",
                chat.key,
                event["expense_id"],
                actor.key,
                inverse,
                json.dumps(_snapshot_with_trip_settings(inv_before, inv_before_trip))
                if inv_before is not None
                else None,
                json.dumps(_snapshot_with_trip_settings(inv_after, inv_after_trip))
                if inv_after is not None
                else None,
                event["id"],
            )
            trip_name = None
            if echo_snapshot.get("trip_id"):
                trip_name = await tx.fetchval(
                    "SELECT name FROM trips WHERE chat_key=$1 AND id=$2",
                    chat.key,
                    UUID(echo_snapshot["trip_id"]),
                )
            return "Undid your last change: " + _inverse_echo(inverse, echo_snapshot, trip_name)
        if kind == "settle_up":
            if payload.get("trip_id") is not None:
                trip = await tx.fetchrow(
                    """SELECT settle_mode, settle_currency, status FROM trips
                       WHERE chat_key=$1 AND id=$2 FOR SHARE""",
                    chat.key,
                    UUID(payload["trip_id"]),
                )
                if not trip:
                    raise ValueError("The trip changed; propose this again.")
                if trip["status"] == "closed":
                    raise ValueError("The trip was closed; propose this again.")
                if "trip_settle_mode" in payload:
                    _check_trip_settle_settings(dict(trip), payload)
            await tx.execute(
                """INSERT INTO settlements(chat_key,trip_id,from_user,to_user,amount,currency)
               VALUES($1,$2,$3,$4,$5,$6)""",
                chat.key,
                _uuid_or_none(payload["trip_id"]),
                payload["from_user"],
                payload["to_user"],
                payload["amount"],
                payload["currency"],
            )
            return "Settlement recorded."
        if kind == "set_fx_rate":
            trip = await tx.fetchrow(
                """SELECT id,settle_mode,settle_currency,status FROM trips
                   WHERE chat_key=$1 AND id=$2 FOR UPDATE""",
                chat.key,
                UUID(payload["trip_id"]),
            )
            if "trip_settle_mode" in payload:
                if not trip or str(trip["id"]) != str(payload.get("trip_id")):
                    raise ValueError("The trip's settle settings changed; propose this again.")
                _check_trip_settle_settings(dict(trip), payload)
            if not trip:
                raise ValueError("The trip's settle settings changed; propose this again.")
            if trip["status"] == "closed":
                raise ValueError("The trip was closed; propose this again.")
            if trip["settle_mode"] != "convert":
                raise ValueError("FX rates can only be set during a convert trip.")
            await tx.execute(
                """INSERT INTO fx_overrides(chat_key,trip_id,currency,settle_currency,rate,set_by)
               VALUES($1,$2,$3,$4,$5,$6)""",
                chat.key,
                trip["id"],
                payload["currency"],
                trip["settle_currency"],
                Decimal(payload["rate"]),
                actor.key,
            )
            if payload.get("expense_ids"):
                await tx.execute(
                    """UPDATE expenses SET fx_rate_to_settle=$4
                       WHERE chat_key=$1 AND trip_id=$2 AND currency=$3 AND id=ANY($5::uuid[])
                         AND fx_rate_to_settle IS NULL""",
                    chat.key,
                    trip["id"],
                    payload["currency"],
                    Decimal(payload["rate"]),
                    [UUID(item) for item in payload["expense_ids"]],
                )
            return "FX rate updated."
        if kind == "end_trip":
            expected_status = payload.get("expected_status")
            row = await tx.fetchrow(
                "SELECT status FROM trips WHERE chat_key=$1 AND id=$2 FOR UPDATE",
                chat.key,
                UUID(payload["trip_id"]),
            )
            if (
                expected_status not in {"planned", "active"}
                or not row
                or row["status"] != expected_status
            ):
                raise ValueError("The trip changed; propose this again.")
            await tx.execute(
                "UPDATE trips SET status='closed',ended_at=now() WHERE chat_key=$1 AND id=$2",
                chat.key,
                UUID(payload["trip_id"]),
            )
            await tx.execute(
                "DELETE FROM trip_defaults WHERE chat_key=$1 AND trip_id=$2",
                chat.key,
                UUID(payload["trip_id"]),
            )
            return "Trip closed."
        if kind in {"activate_trip", "reopen_trip"}:
            row = await tx.fetchrow(
                "SELECT * FROM trips WHERE chat_key=$1 AND id=$2 FOR UPDATE",
                chat.key,
                UUID(payload["trip_id"]),
            )
            required_status = "planned" if kind == "activate_trip" else "closed"
            if (
                payload.get("expected_status") != required_status
                or not row
                or row["status"] != payload.get("expected_status")
            ):
                raise ValueError("The trip changed; propose this again.")
            status = "active" if kind == "activate_trip" else payload["status"]
            duplicate = await tx.fetchval(
                """SELECT EXISTS(SELECT 1 FROM trips WHERE chat_key=$1 AND id<>$2
                   AND status<>'closed' AND lower(name)=lower($3))""",
                chat.key,
                row["id"],
                row["name"],
            )
            if duplicate:
                raise ValueError(
                    f"A trip named {row['name']} is already planned or active in this chat."
                )
            await tx.execute(
                "UPDATE trips SET status=$3,ended_at=NULL WHERE chat_key=$1 AND id=$2",
                chat.key,
                row["id"],
                status,
            )
            if status == "active":
                await tx.execute(
                    """INSERT INTO trip_defaults(chat_key,trip_id) VALUES($1,$2)
                       ON CONFLICT(chat_key) DO UPDATE SET trip_id=EXCLUDED.trip_id,set_at=now()""",
                    chat.key,
                    row["id"],
                )
            else:
                await tx.execute(
                    "DELETE FROM trip_defaults WHERE chat_key=$1 AND trip_id=$2",
                    chat.key,
                    row["id"],
                )
            verb = "Activated" if kind == "activate_trip" else "Reopened"
            return f"{verb} trip {row['name']} as {status}."
        if kind == "set_default_trip":
            row = await tx.fetchrow(
                "SELECT id,name,status FROM trips WHERE chat_key=$1 AND id=$2 FOR SHARE",
                chat.key,
                UUID(payload["trip_id"]),
            )
            if not row or row["status"] != "active":
                raise ValueError("Only an active trip can be the default.")
            await tx.execute(
                """INSERT INTO trip_defaults(chat_key,trip_id) VALUES($1,$2)
                   ON CONFLICT(chat_key) DO UPDATE SET trip_id=EXCLUDED.trip_id,set_at=now()""",
                chat.key,
                row["id"],
            )
            return f"Default trip set to {row['name']}."
        if kind == "update_trip":
            row = await tx.fetchrow(
                """SELECT * FROM trips WHERE chat_key=$1 AND id=$2
                   AND status IN ('planned','active') FOR UPDATE""",
                chat.key,
                UUID(payload["trip_id"]),
            )
            trip = _pg_row(row) if row else None
            if not trip:
                raise ValueError("The trip changed or ended; propose the update again.")
            new = _apply_trip_changes(trip, payload)
            expenses = await tx.fetch(
                """SELECT currency FROM expenses
                   WHERE chat_key=$1 AND trip_id=$2 AND deleted_at IS NULL""",
                chat.key,
                UUID(trip["id"]),
            )
            settlements = await tx.fetch(
                "SELECT currency FROM settlements WHERE chat_key=$1 AND trip_id=$2",
                chat.key,
                UUID(trip["id"]),
            )
            expense_rows = [dict(item) for item in expenses]
            settlement_rows = [dict(item) for item in settlements]
            _check_trip_change_usage(trip, new, payload, expense_rows, settlement_rows)
            lines = _trip_change_lines(trip, new)
            if not lines:
                raise ValueError("Nothing to change on this trip.")
            await tx.execute(
                """UPDATE trips SET name=$3,countries=$4,currencies=$5,settle_mode=$6,
                   settle_currency=$7,start_date=$8,end_date=$9 WHERE chat_key=$1 AND id=$2""",
                chat.key,
                UUID(trip["id"]),
                new["name"],
                new["countries"],
                new["currencies"],
                new["settle_mode"],
                new["settle_currency"],
                new.get("start_date"),
                new.get("end_date"),
            )
            return (
                f"Updated trip {new['name']}:\n"
                + "\n".join(lines)
                + '\nSay "undo" to change it back.'
            )
        raise ValueError(f"Unknown action kind: {kind}")

    async def purge_chat(self, chat: ChatRef) -> None:
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM trip_defaults WHERE chat_key=$1", chat.key)
            await tx.execute("DELETE FROM expenses WHERE chat_key=$1", chat.key)
            await tx.execute("DELETE FROM settlements WHERE chat_key=$1", chat.key)
            await tx.execute("DELETE FROM fx_overrides WHERE chat_key=$1", chat.key)
            await tx.execute("DELETE FROM trips WHERE chat_key=$1", chat.key)


def _uuid_or_none(value: str | UUID | None) -> UUID | None:
    return UUID(str(value)) if value is not None else None


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _matches_expense_id(stored_id: str, requested_id: str) -> bool:
    if _is_uuid(requested_id):
        return stored_id.lower() == requested_id.lower()
    if len(requested_id) != 8 or any(c not in "0123456789abcdefABCDEF" for c in requested_id):
        return False
    return stored_id.replace("-", "").lower().startswith(requested_id.lower())


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        import json

        return json.loads(value)
    return list(value or [])


def _json_object(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _pg_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in ("id", "trip_id"):
        if result.get(key) is not None:
            result[key] = str(result[key])
        if "is_default" in result:
            result["is_default"] = bool(result["is_default"])
    return result
