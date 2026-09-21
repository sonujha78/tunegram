from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    title TEXT,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def add_user(self, user_id: int) -> None:
        await self._db.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
        await self._db.commit()

    async def add_chat(self, chat_id: int, title: str | None) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO chats (id, title) VALUES (?, ?)", (chat_id, title)
        )
        await self._db.commit()

    async def counts(self) -> tuple[int, int]:
        async with self._db.execute("SELECT COUNT(*) FROM users") as cur:
            users = (await cur.fetchone())[0]
        async with self._db.execute("SELECT COUNT(*) FROM chats") as cur:
            chats = (await cur.fetchone())[0]
        return users, chats
