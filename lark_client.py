"""Thin wrapper around the lark-oapi REST client.

Covers exactly what AlertBot needs: emoji reactions (add/remove), replying to a
message with a card, sending a proactive card to a chat, and image upload.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageReactionRequest,
    DeleteMessageRequest,
    Emoji,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from config import CONFIG

log = logging.getLogger("alertbot.lark")


class LarkClient:
    def __init__(self) -> None:
        self._client = (
            lark.Client.builder()
            .app_id(CONFIG.lark_app_id)
            .app_secret(CONFIG.lark_app_secret)
            .domain(CONFIG.lark_domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

    # ---------------------------------------------------------- reactions
    def add_reaction(self, message_id: str, emoji_type: str) -> str | None:
        """Add an emoji reaction to a message; returns reaction_id (needed to remove)."""
        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message_reaction.create(req)
        if not resp.success():
            log.error("add_reaction(%s) failed: code=%s msg=%s log_id=%s", emoji_type, resp.code, resp.msg, resp.get_log_id())
            return None
        return resp.data.reaction_id

    def remove_reaction(self, message_id: str, reaction_id: str) -> bool:
        if not reaction_id:
            return False
        req = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        resp = self._client.im.v1.message_reaction.delete(req)
        if not resp.success():
            log.error("remove_reaction failed: code=%s msg=%s log_id=%s", resp.code, resp.msg, resp.get_log_id())
            return False
        return True

    # ------------------------------------------------------------- reply
    def delete_message(self, message_id: str) -> bool:
        """Remove a message the bot sent. This is Lark's only removal API
        (named 'recall'); for the bot's own cards it takes them out of the chat."""
        if not message_id:
            return False
        req = DeleteMessageRequest.builder().message_id(message_id).build()
        resp = self._client.im.v1.message.delete(req)
        if not resp.success():
            log.warning(
                "delete_message(%s) failed: code=%s msg=%s log_id=%s",
                message_id, resp.code, resp.msg, resp.get_log_id(),
            )
            return False
        return True

    def reply_card(self, message_id: str, card: dict[str, Any], *, in_thread: bool = True) -> str | None:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .msg_type("interactive")
                .reply_in_thread(in_thread)
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message.reply(req)
        if not resp.success():
            log.error("reply_card failed: code=%s msg=%s log_id=%s", resp.code, resp.msg, resp.get_log_id())
            return None
        return resp.data.message_id

    # ---------------------------------------------------------- proactive
    def send_card(self, chat_id: str, card: dict[str, Any]) -> str | None:
        """Send a card to a chat. Returns the sent message_id (truthy) on
        success, or None on failure."""
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message.create(req)
        if not resp.success():
            log.error("send_card failed: code=%s msg=%s log_id=%s", resp.code, resp.msg, resp.get_log_id())
            return None
        return resp.data.message_id

    def reply_text(self, message_id: str, text: str, *, in_thread: bool = False) -> bool:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .msg_type("text")
                .reply_in_thread(in_thread)
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message.reply(req)
        if not resp.success():
            log.error("reply_text failed: code=%s msg=%s log_id=%s", resp.code, resp.msg, resp.get_log_id())
            return False
        return True

    # ------------------------------------------------------------- image
    def upload_image(self, path: str) -> str | None:
        """Upload a local image file and return its image_key for use in cards."""
        try:
            with open(path, "rb") as fh:
                req = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder().image_type("message").image(fh).build()
                    )
                    .build()
                )
                resp = self._client.im.v1.image.create(req)
        except OSError:
            log.exception("Could not open image %s", path)
            return None
        if not resp.success():
            log.error("upload_image failed: code=%s msg=%s log_id=%s", resp.code, resp.msg, resp.get_log_id())
            return None
        return resp.data.image_key
