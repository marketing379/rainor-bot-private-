#!/usr/bin/env python3
"""
Rain Builders Bot (@RainBuildersBot)
Telegram bot for managing Rain Protocol builder relationships.

Features:
  - /newbuilder @handle ProjectName  -> Deep link to create a builder group
  - /broadcast message               -> Broadcast to all builder groups
  - /help                            -> Show available commands
  - SDK Q&A in builder groups        -> AI-powered answers from Rain docs
"""

import os
import json
import logging
import html
import asyncio
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMemberUpdated,
    Chat,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatMemberStatus

from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN = "8778171032:AAHs-UG8DVwaxY7lhwkTsfPoNXvYiPb2kgI"
BOT_USERNAME = "RainBuildersBot"
DEFAULT_MEMBERS = ["@OmHiErMi"]  # Always invite to new groups
DATA_FILE = Path(__file__).parent / "groups_data.json"
DOCS_FILE = Path(__file__).parent / "rain_sdk_docs.txt"

# Admin tracking: the first user who sends a private command becomes admin,
# or we accept any private-chat command sender as admin.
ADMIN_IDS: set[int] = set()

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistent data helpers
# ---------------------------------------------------------------------------

def load_data() -> dict:
    """Load groups data from JSON file."""
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"groups": {}, "pending": {}}
    return {"groups": {}, "pending": {}}


def save_data(data: dict) -> None:
    """Save groups data to JSON file."""
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# SDK documentation loader
# ---------------------------------------------------------------------------

_sdk_docs_content: str = ""


def get_sdk_docs() -> str:
    """Load the SDK documentation content."""
    global _sdk_docs_content
    if not _sdk_docs_content:
        if DOCS_FILE.exists():
            _sdk_docs_content = DOCS_FILE.read_text(encoding="utf-8")
        else:
            _sdk_docs_content = (
                "Rain Protocol SDK documentation is not available at the moment. "
                "Please visit https://rain.one/docs/For-Developers/Rain-Builders"
            )
    return _sdk_docs_content


# ---------------------------------------------------------------------------
# OpenAI helper
# ---------------------------------------------------------------------------

openai_client = OpenAI()  # uses OPENAI_API_KEY env var


def ask_openai(question: str) -> str:
    """Ask a question about the Rain SDK using OpenAI."""
    docs = get_sdk_docs()
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful technical assistant for Rain Protocol. "
                        "You answer questions about the Rain SDK, its architecture, "
                        "and how to build on Rain Protocol. Use the documentation "
                        "provided below to answer questions accurately and concisely. "
                        "If the answer is not in the documentation, say so and suggest "
                        "visiting https://rain.one/docs for more information. "
                        "Always respond in English.\n\n"
                        "--- RAIN SDK DOCUMENTATION ---\n"
                        f"{docs}\n"
                        "--- END DOCUMENTATION ---"
                    ),
                },
                {"role": "user", "content": question},
            ],
            max_tokens=1024,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error("OpenAI API error: %s", e)
        return (
            "Sorry, I couldn't process your question right now. "
            "Please try again later or visit "
            "https://rain.one/docs/For-Developers/Rain-Builders"
        )


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command, including startgroup deep-link callbacks."""
    chat = update.effective_chat
    user = update.effective_user

    # --- Deep-link from startgroup (bot was just added to a new group) ---
    if chat.type in (Chat.GROUP, Chat.SUPERGROUP):
        args = context.args
        if args and args[0].startswith("newbuilder_"):
            payload = args[0]  # e.g. newbuilder_ProjectName_handle
            parts = payload.split("_", 2)
            project_name = parts[1] if len(parts) > 1 else "Unknown"
            builder_handle = parts[2] if len(parts) > 2 else "unknown"

            data = load_data()
            group_id = str(chat.id)
            data["groups"][group_id] = {
                "project_name": project_name,
                "builder_handle": builder_handle,
                "group_id": chat.id,
                "group_title": chat.title or f"Rain Builders <> {project_name}",
            }
            # Remove from pending if present
            for key in list(data.get("pending", {}).keys()):
                if data["pending"][key].get("project_name") == project_name:
                    del data["pending"][key]
            save_data(data)

            # Try to invite default members
            for member_handle in DEFAULT_MEMBERS:
                try:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=f"Please manually add {member_handle} to this group — bots cannot invite users by username.",
                    )
                except Exception as e:
                    logger.warning("Could not send invite reminder for %s: %s", member_handle, e)

            # Send welcome message
            welcome = (
                f"🌧 <b>Welcome to Rain Builders &lt;&gt; {html.escape(project_name)}!</b>\n\n"
                f"This group has been created for the <b>{html.escape(project_name)}</b> team "
                f"to collaborate with Rain Protocol.\n\n"
                "Rain Protocol is a prediction markets protocol built on Arbitrum One, "
                "designed for AI agents and developers. Our SDK provides TypeScript tools "
                "to build, sign, and send transactions for creating markets, trading options, "
                "and managing liquidity.\n\n"
                "<b>What you can do with the Rain SDK:</b>\n"
                "• Create permissionless prediction markets\n"
                "• Build trading interfaces with AMM liquidity\n"
                "• Use gas-sponsored execution via account abstraction\n"
                "• Stream live data via WebSockets\n\n"
                "📚 <b>Documentation:</b> https://rain.one/docs/For-Developers/Rain-Builders\n"
                "📦 <b>NPM:</b> https://www.npmjs.com/package/@buidlrrr/rain-sdk\n"
                "💻 <b>GitHub:</b> https://github.com/rain1-labs/rain-sdk\n\n"
                "Feel free to ask any questions about the SDK in this group — "
                "just end your message with <b>?</b> and I'll do my best to help!\n\n"
                f"Builder: {html.escape(builder_handle)}"
            )
            await context.bot.send_message(
                chat_id=chat.id, text=welcome, parse_mode=ParseMode.HTML
            )
            logger.info(
                "Builder group created: %s for %s (%s)",
                chat.title, project_name, builder_handle,
            )
        return

    # --- Normal /start in private chat ---
    if chat.type == Chat.PRIVATE:
        ADMIN_IDS.add(user.id)
        await update.message.reply_text(
            "🌧 <b>Rain Builders Bot</b>\n\n"
            "Welcome! I help manage Rain Protocol builder relationships.\n\n"
            "Use /help to see available commands.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show available commands."""
    chat = update.effective_chat
    if chat.type != Chat.PRIVATE:
        return

    ADMIN_IDS.add(update.effective_user.id)
    help_text = (
        "🌧 <b>Rain Builders Bot — Commands</b>\n\n"
        "<b>/newbuilder</b> <code>@handle ProjectName</code>\n"
        "Create a new builder group. The bot generates a deep link button that, "
        "when clicked, opens Telegram's group creation flow with the bot pre-added. "
        "After the group is created, the bot sends a welcome message and stores the group info.\n\n"
        "<b>/broadcast</b> <code>message text here</code>\n"
        "Send a message to ALL stored builder groups. "
        "The bot confirms how many groups received the message.\n\n"
        "<b>/listgroups</b>\n"
        "List all registered builder groups.\n\n"
        "<b>/help</b>\n"
        "Show this help message.\n\n"
        "<b>SDK Q&amp;A (in builder groups):</b>\n"
        "Any message ending with <b>?</b> in a builder group will trigger "
        "an AI-powered answer based on the Rain SDK documentation."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


async def cmd_newbuilder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /newbuilder @handle ProjectName command."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != Chat.PRIVATE:
        await update.message.reply_text("Please use this command in a private chat with me.")
        return

    ADMIN_IDS.add(user.id)

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /newbuilder <code>@handle ProjectName</code>\n\n"
            "Example: /newbuilder <code>@alice MyDeFiProject</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    builder_handle = context.args[0]
    project_name = " ".join(context.args[1:])

    # Sanitize project name for deep-link payload (only alphanumeric + underscore allowed)
    safe_project = "".join(c if c.isalnum() else "" for c in project_name)
    safe_handle = "".join(c if c.isalnum() else "" for c in builder_handle)

    # Store pending info
    data = load_data()
    if "pending" not in data:
        data["pending"] = {}
    pending_key = f"{safe_project}_{safe_handle}"
    data["pending"][pending_key] = {
        "project_name": project_name,
        "builder_handle": builder_handle,
        "created_by": user.id,
    }
    save_data(data)

    # Build the deep link
    # startgroup parameter: newbuilder_ProjectName_handle
    payload = f"newbuilder_{safe_project}_{safe_handle}"
    deep_link = f"https://t.me/{BOT_USERNAME}?startgroup={payload}"
    group_name = f"Rain Builders <> {project_name}"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text=f"📂 Create Group: {group_name}",
                    url=deep_link,
                )
            ]
        ]
    )

    await update.message.reply_text(
        f"🌧 <b>New Builder Group Setup</b>\n\n"
        f"<b>Project:</b> {html.escape(project_name)}\n"
        f"<b>Builder:</b> {html.escape(builder_handle)}\n"
        f"<b>Group name:</b> {html.escape(group_name)}\n\n"
        "Click the button below to create the group. Telegram will open a group "
        "creation flow with the bot pre-added. After creating the group:\n\n"
        f"1. Name the group <b>{html.escape(group_name)}</b>\n"
        f"2. The bot will automatically send a welcome message\n"
        f"3. Please add {', '.join(DEFAULT_MEMBERS)} to the group\n"
        f"4. Add the builder ({html.escape(builder_handle)}) to the group",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast a message to all builder groups."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != Chat.PRIVATE:
        await update.message.reply_text("Please use this command in a private chat with me.")
        return

    ADMIN_IDS.add(user.id)

    if not context.args:
        await update.message.reply_text(
            "Usage: /broadcast <code>message text here</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    message_text = " ".join(context.args)
    data = load_data()
    groups = data.get("groups", {})

    if not groups:
        await update.message.reply_text("No builder groups registered yet.")
        return

    success = 0
    failed = 0
    failed_groups = []

    for group_id, info in groups.items():
        try:
            broadcast_msg = (
                f"📢 <b>Rain Protocol Announcement</b>\n\n"
                f"{html.escape(message_text)}"
            )
            await context.bot.send_message(
                chat_id=int(group_id),
                text=broadcast_msg,
                parse_mode=ParseMode.HTML,
            )
            success += 1
        except Exception as e:
            failed += 1
            failed_groups.append(info.get("project_name", group_id))
            logger.error("Failed to broadcast to %s: %s", group_id, e)

    result_text = f"📢 <b>Broadcast Complete</b>\n\n✅ Sent to: {success} group(s)"
    if failed > 0:
        result_text += f"\n❌ Failed: {failed} group(s) ({', '.join(failed_groups)})"
    await update.message.reply_text(result_text, parse_mode=ParseMode.HTML)


async def cmd_listgroups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all registered builder groups."""
    chat = update.effective_chat
    if chat.type != Chat.PRIVATE:
        return

    ADMIN_IDS.add(update.effective_user.id)
    data = load_data()
    groups = data.get("groups", {})

    if not groups:
        await update.message.reply_text("No builder groups registered yet.")
        return

    lines = ["🌧 <b>Registered Builder Groups</b>\n"]
    for i, (gid, info) in enumerate(groups.items(), 1):
        lines.append(
            f"{i}. <b>{html.escape(info.get('project_name', 'Unknown'))}</b>\n"
            f"   Builder: {html.escape(info.get('builder_handle', 'N/A'))}\n"
            f"   Group ID: <code>{gid}</code>"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Group event handlers
# ---------------------------------------------------------------------------

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle when the bot is added to a new group (my_chat_member update)."""
    result: ChatMemberUpdated = update.my_chat_member
    if result is None:
        return

    chat = result.chat
    old_status = result.old_chat_member.status if result.old_chat_member else None
    new_status = result.new_chat_member.status if result.new_chat_member else None

    # Bot was added to a group
    was_not_member = old_status in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.BANNED,
        None,
    )
    is_member = new_status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    )

    if was_not_member and is_member and chat.type in (Chat.GROUP, Chat.SUPERGROUP):
        logger.info("Bot added to group: %s (%s)", chat.title, chat.id)

        # Check if this matches a pending builder group
        data = load_data()
        group_id = str(chat.id)

        # Try to match from pending data or from group title
        matched_pending = None
        for key, pending_info in list(data.get("pending", {}).items()):
            # Check if the group title matches the expected pattern
            project_name = pending_info.get("project_name", "")
            if project_name and (
                project_name.lower() in (chat.title or "").lower()
                or f"rain builders" in (chat.title or "").lower()
            ):
                matched_pending = pending_info
                del data["pending"][key]
                break

        if matched_pending:
            project_name = matched_pending["project_name"]
            builder_handle = matched_pending["builder_handle"]
        else:
            # If no pending match, try to extract from group title
            title = chat.title or ""
            if "<>" in title:
                project_name = title.split("<>")[-1].strip()
            else:
                project_name = title
            builder_handle = "unknown"

        # Store group info
        data["groups"][group_id] = {
            "project_name": project_name,
            "builder_handle": builder_handle,
            "group_id": chat.id,
            "group_title": chat.title or f"Rain Builders <> {project_name}",
        }
        save_data(data)

        # Remind to add default members
        for member_handle in DEFAULT_MEMBERS:
            try:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=f"📌 Please add {member_handle} to this group.",
                )
            except Exception as e:
                logger.warning("Could not send invite reminder: %s", e)

        # Send welcome message
        welcome = (
            f"🌧 <b>Welcome to Rain Builders &lt;&gt; {html.escape(project_name)}!</b>\n\n"
            f"This group has been created for the <b>{html.escape(project_name)}</b> team "
            f"to collaborate with Rain Protocol.\n\n"
            "Rain Protocol is a prediction markets protocol built on Arbitrum One, "
            "designed for AI agents and developers. Our SDK provides TypeScript tools "
            "to build, sign, and send transactions for creating markets, trading options, "
            "and managing liquidity.\n\n"
            "<b>What you can do with the Rain SDK:</b>\n"
            "• Create permissionless prediction markets\n"
            "• Build trading interfaces with AMM liquidity\n"
            "• Use gas-sponsored execution via account abstraction\n"
            "• Stream live data via WebSockets\n\n"
            "📚 <b>Documentation:</b> https://rain.one/docs/For-Developers/Rain-Builders\n"
            "📦 <b>NPM:</b> https://www.npmjs.com/package/@buidlrrr/rain-sdk\n"
            "💻 <b>GitHub:</b> https://github.com/rain1-labs/rain-sdk\n\n"
            "Feel free to ask any questions about the SDK in this group — "
            "just end your message with <b>?</b> and I'll do my best to help!"
        )
        await context.bot.send_message(
            chat_id=chat.id, text=welcome, parse_mode=ParseMode.HTML
        )


# ---------------------------------------------------------------------------
# SDK Q&A handler for group messages
# ---------------------------------------------------------------------------

async def handle_group_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer questions in builder groups using the SDK knowledge base."""
    if update.message is None or update.message.text is None:
        return

    chat = update.effective_chat
    if chat.type not in (Chat.GROUP, Chat.SUPERGROUP):
        return

    text = update.message.text.strip()

    # Only respond to messages ending with ?
    if not text.endswith("?"):
        return

    # Check if this is a registered builder group
    data = load_data()
    group_id = str(chat.id)
    if group_id not in data.get("groups", {}):
        # Still answer if the bot is in the group, just register it
        pass

    # Show typing indicator
    await context.bot.send_chat_action(chat_id=chat.id, action="typing")

    # Get AI answer
    answer = ask_openai(text)

    reply = f"🌧 <b>Rain SDK Assistant</b>\n\n{html.escape(answer)}"

    # Truncate if too long for Telegram (4096 char limit)
    if len(reply) > 4000:
        reply = reply[:3997] + "..."

    await update.message.reply_text(reply, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors."""
    logger.error("Exception while handling an update:", exc_info=context.error)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    """Set bot commands after initialization."""
    commands = [
        BotCommand("newbuilder", "Create a new builder group"),
        BotCommand("broadcast", "Broadcast message to all groups"),
        BotCommand("listgroups", "List all registered builder groups"),
        BotCommand("help", "Show available commands"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot commands set successfully.")

    # Pre-load SDK docs
    get_sdk_docs()
    logger.info("SDK documentation loaded.")


def main() -> None:
    """Start the bot."""
    logger.info("Starting Rain Builders Bot...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Command handlers (private chat)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("newbuilder", cmd_newbuilder))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("listgroups", cmd_listgroups))

    # Chat member handler (bot added to group)
    application.add_handler(
        ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # Group message handler for SDK Q&A (messages ending with ?)
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            handle_group_question,
        )
    )

    # Error handler
    application.add_error_handler(error_handler)

    # Run the bot
    logger.info("Bot is running. Press Ctrl+C to stop.")
    application.run_polling(
        allowed_updates=[
            Update.MESSAGE,
            Update.MY_CHAT_MEMBER,
            Update.CHAT_MEMBER,
        ],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
