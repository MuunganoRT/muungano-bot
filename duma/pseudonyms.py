"""Names the model never sees.

When rows about people go back to the model, each person becomes a code
(`ATLETA_07`). The map from code to name stays in the session, in memory, and
is applied to the model's answer on its way to the chat.
"""

from __future__ import annotations

import re
from typing import Optional

CODE = re.compile(r"ATLETA_(\d+)")


class Pseudonyms:
    def __init__(self) -> None:
        self._code_by_id: dict[int, str] = {}
        self._person_by_code: dict[str, tuple[int, str]] = {}

    def code(self, athlete_id: int, name: str) -> str:
        """The code for this person: the same one every time within a session."""
        if athlete_id not in self._code_by_id:
            code = f"ATLETA_{len(self._code_by_id) + 1:02d}"
            self._code_by_id[athlete_id] = code
            self._person_by_code[code] = (athlete_id, name)
        return self._code_by_id[athlete_id]

    def athlete_id(self, text: str) -> Optional[int]:
        """The person a code stands for, when `text` is exactly one known code."""
        person = self._person_by_code.get(text.strip().upper())
        return person[0] if person else None

    def to_list(self) -> list[list]:
        """[athlete id, name] in the order the codes were handed out, for saving the session."""
        return [list(person) for person in self._person_by_code.values()]

    @classmethod
    def from_list(cls, saved: list[list]) -> "Pseudonyms":
        names = cls()
        for athlete_id, name in saved:
            names.code(athlete_id, name)
        return names

    def restore(self, text: str) -> str:
        """Put the names back. A code nobody handed out is left as it is."""

        def name(match: re.Match[str]) -> str:
            person = self._person_by_code.get(match.group(0))
            return person[1] if person else match.group(0)

        return CODE.sub(name, text)
