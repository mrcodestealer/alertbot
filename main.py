"""AlertBot entrypoint.

Starts:
  * the MonitorFlow API client,
  * the Lark REST client,
  * a background watcher thread (new-alert detection + resolution tracking),
  * a Lark WebSocket long-connection that receives IM events and handles /check.
"""
from __future__ import annotations

import json
import logging
import re
import signal
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from commands import CommandHandler
from config import CONFIG
from lark_client import LarkClient
from monitor_client import MonitorClient
from state import State
from watcher import Watcher

logging.basicConfig(
    level=getattr(logging, CONFIG.log_level, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("alertbot")

# Shared singletons
monitor = MonitorClient()
lark_client = LarkClient()
state = State(CONFIG.state_file)
commands = CommandHandler(monitor, lark_client, state)

# Handle command work off the WebSocket receive thread so we never block it.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cmd")

# De-duplicate redelivered events.
_seen_ids: deque[str] = deque(maxlen=500)
_seen_set: set[str] = set()

_MENTION_RE = re.compile(r"@_(user_\d+|all)\b")


def _already_seen(message_id: str) -> bool:
    if message_id in _seen_set:
        return True
    _seen_ids.append(message_id)
    _seen_set.add(message_id)
    while len(_seen_set) > len(_seen_ids):
        # trim set to match deque after evictions
        _seen_set.intersection_update(_seen_ids)
    return False


def _extract_text(content: str) -> str:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return ""
    text = data.get("text", "") if isinstance(data, dict) else ""
    return _MENTION_RE.sub("", text).strip()


def on_message(data: P2ImMessageReceiveV1) -> None:
    try:
        msg = data.event.message
        if msg.message_type != "text":
            return
        message_id = msg.message_id
        if _already_seen(message_id):
            return

        text = _extract_text(msg.content)
        tokens = text.lower().split()  # whole-word match so "/checkout" != "/check"
        chat_type = msg.chat_type
        mentioned = bool(msg.mentions)
        sender = _sender_open_id(data)

        # Log chat_id/sender so the operator can fill LARK_ALERT_CHAT_ID / DEPLOY_ADMIN_IDS.
        log.info("Message chat_id=%s type=%s sender=%s text=%r", msg.chat_id, chat_type, sender, text)

        # Helper: reply with the current chat_id.
        if "/chatid" in tokens:
            lark_client.reply_text(message_id, f"chat_id: {msg.chat_id}")
            return

        # Helper: reply with the sender's open_id (to fill DEPLOY_ADMIN_IDS).
        if "/whoami" in tokens:
            lark_client.reply_text(message_id, f"your open_id: {sender}")
            return

        # Self-deploy — DM only, opt-in, authorized users only. Never executes
        # text from the message; runs a fixed git-pull + restart.
        if chat_type == "p2p" and _is_deploy_command(tokens, text):
            _handle_deploy_request(message_id, sender)
            return

        # /check must be directed at the bot: either a DM, or the bot @-mentioned.
        if CONFIG.check_command in tokens and (chat_type == "p2p" or mentioned):
            log.info("/check triggered by %s in %s", sender, msg.chat_id)
            _executor.submit(commands.handle_check, message_id, sender)
    except Exception:
        log.exception("Failed handling incoming message")


def _sender_open_id(data: P2ImMessageReceiveV1) -> str | None:
    try:
        return data.event.sender.sender_id.open_id
    except AttributeError:
        return None


def _is_deploy_command(tokens: list[str], text: str) -> bool:
    # "/deploy" as a whole token, or the natural phrase "git pull".
    return "/deploy" in tokens or "git pull" in text.lower()


def _handle_deploy_request(message_id: str, sender: str | None) -> None:
    if not CONFIG.deploy_enabled:
        lark_client.reply_text(message_id, "Deploy is disabled. Set DEPLOY_ENABLED=true in .env to enable it.")
        return
    if CONFIG.deploy_admin_ids and sender not in CONFIG.deploy_admin_ids:
        log.warning("Unauthorized deploy attempt by %s", sender)
        lark_client.reply_text(message_id, "⛔ You are not authorized to deploy.")
        return
    if not CONFIG.deploy_admin_ids:
        log.warning("DEPLOY_ADMIN_IDS is empty — allowing deploy from DM sender %s. Set an allowlist!", sender)
    _executor.submit(commands.handle_deploy, message_id, sender)


def main() -> int:
    problems = CONFIG.validate()
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        log.error("Fix your .env and restart.")
        return 1

    # Flush state on `systemctl stop/restart` (SIGTERM). SIGINT is handled by the
    # KeyboardInterrupt path below.
    def _graceful_shutdown(signum, _frame):
        log.info("Received signal %s; saving state and exiting", signum)
        try:
            state.save()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)

    # Warm up the dashboard session (non-fatal; watcher retries).
    try:
        monitor.login()
    except Exception:
        log.exception("Initial MonitorFlow login failed (will retry in watcher)")

    Watcher(monitor, lark_client, state).start()

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    log.info("Connecting to Lark (%s) via WebSocket long connection…", CONFIG.lark_domain)
    ws = lark.ws.Client(
        CONFIG.lark_app_id,
        CONFIG.lark_app_secret,
        event_handler=event_handler,
        domain=CONFIG.lark_domain,
        log_level=lark.LogLevel.INFO,
    )
    ws.start()  # blocking; auto-reconnects
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
        state.save()
