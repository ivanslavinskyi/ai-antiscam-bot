from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command

router = Router(name="commands-router")


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.reply(
        "Привет! Я антискам-бот (альфа-версия).\n"
    )


@router.message(Command("chatid"))
async def cmd_chatid(message: Message):
    chat_id = message.chat.id
    await message.reply(f"ID этого чата: <code>{chat_id}</code>")
