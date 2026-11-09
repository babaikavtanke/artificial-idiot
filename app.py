import os
import re
import logging
import threading
import asyncio
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Logging Setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# Read credentials from Render Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# OpenRouter Client via OpenAI compatibility
ai_client = OpenAI(
    api_key=OPENAI_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)

DEFAULT_SYSTEM_PROMPT = (
    "You are a comically clueless bot. Always give wildly unhelpful, silly, "
    "or absurd answers while acting like you are completely confident. "
    "Keep answers relatively short and goofy."
)


def fetch_url_content(url: str) -> str:
    """Fetch text from a link found in the user prompt."""
    try:
        response = requests.get(url, timeout=5)
        soup = BeautifulSoup(response.text, "html.parser")
        return " ".join(soup.stripped_strings)[:1200]
    except Exception as e:
        logging.warning("Failed to fetch %s: %s", url, e)
        return ""


# Telegram Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.info("Received /start from user %s", update.effective_user.id)
    await update.message.reply_text(
        "Hey! I am set to give goofy answers by default.\n\n"
        "Commands:\n"
        "• /setprompt <text> - Set a custom system prompt\n"
        "• /showprompt - Check the active prompt\n"
        "• /resetprompt - Revert to default silly mode"
    )


async def set_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Please provide a prompt! Example:\n"
            "/setprompt You speak only in pirate rhymes."
        )
        return
    new_prompt = " ".join(context.args)
    context.chat_data["custom_prompt"] = new_prompt
    await update.message.reply_text(f"Prompt updated to:\n\n\"{new_prompt}\"")


async def show_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active_prompt = context.chat_data.get("custom_prompt", DEFAULT_SYSTEM_PROMPT)
    await update.message.reply_text(f"Current prompt:\n\n{active_prompt}")


async def reset_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data.pop("custom_prompt", None)
    await update.message.reply_text("Prompt reset back to silly mode!")


async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_type = update.message.chat.type
    user_text = update.message.text
    bot_username = context.bot.username or ""

    # In groups/supergroups, only reply if mentioned or if the message is a direct reply to the bot
    if chat_type in ["group", "supergroup"]:
        is_reply_to_bot = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        is_mentioned = bool(bot_username and f"@{bot_username}".lower() in user_text.lower())

        if not (is_reply_to_bot or is_mentioned):
            return  # Skip messages not directed at the bot

        # Strip out the @mention from the message before passing to AI
        if bot_username:
            user_text = re.sub(rf"@{re.escape(bot_username)}", "", user_text, flags=re.IGNORECASE).strip()

    logging.info("Incoming message: %s", user_text)

    urls = re.findall(r"https?://[^\s]+", user_text)
    scraped_content = fetch_url_content(urls[0]) if urls else ""

    base_prompt = context.chat_data.get("custom_prompt", DEFAULT_SYSTEM_PROMPT)
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    system_prompt = f"{base_prompt}\n(Current time: {current_time_str})"
    if scraped_content:
        system_prompt += f"\nContext from URL:\n{scraped_content}"

    # Try preferred model first, then fallback to openrouter/free
    candidate_models = ["recursive/flint-v1.0:free", "openrouter/free"]
    reply = None

    for model_name in candidate_models:
        try:
            logging.info("Attempting generation with model: %s", model_name)
            response = ai_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.9,
            )
            reply = response.choices[0].message.content
            if reply:
                break
        except Exception as exc:
            logging.warning("Model %s failed: %s", model_name, exc)

    if not reply:
        reply = "My brain crashed. Try asking again in a moment."

    await update.message.reply_text(reply)


# Flask Web App
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


def start_bot_background():
    """Initializes and runs the Telegram bot polling inside its own thread and loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def bot_runner():
        tg_app = (
            ApplicationBuilder()
            .token(TELEGRAM_BOT_TOKEN)
            .concurrent_updates(True)
            .build()
        )

        tg_app.add_handler(CommandHandler("start", start_command))
        tg_app.add_handler(CommandHandler("setprompt", set_prompt_command))
        tg_app.add_handler(CommandHandler("showprompt", show_prompt_command))
        tg_app.add_handler(CommandHandler("resetprompt", reset_prompt_command))
        tg_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), chat_handler))

        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling()
        logging.info(">>> TELEGRAM BOT POLLING IS RUNNING SUCCESSFULLY <<<")

        while True:
            await asyncio.sleep(3600)

    loop.run_until_complete(bot_runner())


# Start background bot thread
bot_thread = threading.Thread(target=start_bot_background, daemon=True)
bot_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
