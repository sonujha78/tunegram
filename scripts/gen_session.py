"""Userbot ka session string banao. Sirf ek baar chalana."""
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from tunegram.config import load_config


async def main() -> None:
    cfg = load_config()
    # `async with` khud phone number, OTP aur (agar ho to) 2FA password puchega
    async with TelegramClient(StringSession(), cfg.api_id, cfg.api_hash) as client:
        me = await client.get_me()
        print(f"\nLogged in as: {me.first_name} (@{me.username})")
        print("\nSESSION_STRING (sirf .env me daalna, kisi ke saath share mat karna):\n")
        print(client.session.save())


asyncio.run(main())
