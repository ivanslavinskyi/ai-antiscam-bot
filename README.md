# AI Anti-Scam Bot 🛡️

**AI Anti-Scam Bot** is a Telegram bot that automatically detects, deletes, and tracks scam messages in group chats using OpenAI and PostgreSQL.

It is designed for vulnerable communities (e.g. refugees and migrants) and focuses on transparent moderation and building a quality labeled dataset for future ML models.

---

## ✨ Features

- 🤖 LLM-powered scam detection (OpenAI Chat Completions)
- 🧹 Automatic deletion of scam messages
- ⚠️ Strike system with warning → mute → ban
- 👮 Per-chat admin control via dedicated admin chats
- 🎛️ Inline buttons for “Not scam” / “Confirm scam”
- 🧾 All inspected messages stored in PostgreSQL
- 🙅 Skips admins and whitelisted users (their messages are not sent to LLM)
- 📊 Ready for dataset export and analytics

---

## 🚀 Tech Stack

- **Python 3.10+**
- **aiogram 3** (Telegram Bot API)
- **OpenAI API** (e.g. `gpt-4o-mini`)
- **PostgreSQL** + SQLAlchemy ORM
- Environment configuration via **pydantic-settings**

---

## 📦 Installation

```bash
git clone https://github.com/your-username/antiscam-bot.git
cd antiscam-bot

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
````

Make sure PostgreSQL is installed and running.

Create a database and user, for example:

```bash
sudo -u postgres psql

CREATE USER antiscam_user WITH PASSWORD 'strong_password';
CREATE DATABASE antiscam_db OWNER antiscam_user ENCODING 'UTF8';

\q
```

---

## ⚙️ Configuration

Create a `.env` file in the project root:

```env
# Telegram bot
TELEGRAM_BOT_TOKEN=1234567890:AA...

# OpenAI
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini-2024-07-18

# PostgreSQL
DB_URL=postgresql+psycopg2://antiscam_user:strong_password@localhost:5432/antiscam_db

# Optional: global admin chats (comma-separated chat IDs)
# Example: ADMIN_CHAT_IDS=-1001111111111,-1002222222222
ADMIN_CHAT_IDS=

# Confidence threshold for automatic scam actions
DEFAULT_CONFIDENCE_THRESHOLD=0.85

# Debug logging
DEBUG=true
```

---

## 🧪 Run the Bot

From the project directory:

```bash
source venv/bin/activate
python -m app.main
```

On first run, the bot will:

* connect to PostgreSQL,
* create tables if they don’t exist,
* start polling Telegram.

---

## 💬 Telegram Setup

1. **Working chats (protected groups)**

   * Add the bot to the group/supergroup.
   * Promote it to **admin** with permissions to delete messages and restrict members.

2. **Admin chats**

   * Create a separate admin group for moderators.
   * Add the bot and human admins there.

3. **Link working chat to admin chat**
   In the working chat (as a Telegram admin):

   ```text
   /as_set_admin_chat <admin_chat_id>
   ```

   After linking, scam events from that chat will be reported to the selected admin chat as rich cards with inline buttons.

---

## 🛠️ Admin Commands

* `/as_set_admin_chat <id>` — link this chat to an admin chat
* `/as_status` — show status and basic stats for the current chat
* `/as_stats` — aggregated stats in admin chats (scams, labels, top offenders)
* `/as_recent [N]` — show recent detected scam messages
* `/help` or `/as_help` — show help and list of commands

In working chats, bot commands are restricted to Telegram admins of that chat.

---

## 📊 Data & Dataset

All messages that are sent to the LLM are stored in the database with:

* raw text,
* model label, category, confidence,
* explanation and raw JSON,
* optional human label and who set it,
* per-chat user strike counts.

This makes it easy to:

* analyze scam patterns,
* export labeled data for ML,
* build future commercial or research products on top of the dataset.

---

## 📄 License

MIT — open to use and contribution.
Maintained by **Ivan Slavinskyi**.
