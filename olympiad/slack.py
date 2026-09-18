"""Posting alerts to Slack through an Incoming Webhook.

The webhook URL is a password: anyone holding it can post to the channel as
this app. It is read from the SLACK_WEBHOOK_URL environment variable and never
written to a file, a log line or an error message.

If the variable is not set, the module runs in dry-run mode and prints the
message it would have sent. That is what makes it safe to try the tracker out
on a finished round without anyone's phone buzzing.
"""

from __future__ import annotations

import json
import os
import time

import requests

from .captions import draft_caption, lint_caption
from .detect import BLUNDER
from .evalscale import describe_position

WEBHOOK_ENV = "SLACK_WEBHOOK_URL"
TIMEOUT = 20


def _headline(finding) -> str:
    if finding.kind == BLUNDER:
        icon = "🩸" if finding.subkind == "threw_game" else "😬"
        label = "BLUNDER" if finding.subkind == "threw_game" else "THREW AWAY A WIN"
    else:
        icon = "💎"
        label = "BRILLIANCY"
    return "%s %s  ·  %s" % (icon, label, finding.player.short)


def _context_line(finding) -> str:
    game = finding.game
    bits = [game.tour_name, game.round_name]
    if game.board:
        bits.append("Board %d" % game.board)
    teams = "%s vs %s" % (finding.player.team or "?", finding.opponent.team or "?")
    bits.append(teams)
    return "  ·  ".join(b for b in bits if b)


def _players_line(finding) -> str:
    game = finding.game
    def label(player):
        rating = " (%d)" % player.rating if player.rating else ""
        return "%s%s" % (player.display, rating)
    return "*%s*  —  *%s*" % (label(game.white), label(game.black))


def _numbers_line(finding) -> str:
    move = "*%s*" % finding.move.numbered_san
    swing = "`%s → %s`" % (finding.eval_before_text, finding.eval_after_text)
    chances = "winning chances %.0f%% → %.0f%%" % (
        finding.chances_before, finding.chances_after)
    parts = [move, swing, chances]
    if finding.better_move:
        parts.append("best was *%s*" % finding.better_move)
    if finding.kind != BLUNDER and finding.only_move_gap:
        parts.append("next best move is %.0f points worse" % finding.only_move_gap)
    return "  ·  ".join(parts)


def build_message(finding) -> dict:
    """The Slack payload for one finding, as Block Kit blocks."""
    caption = draft_caption(finding)
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": _headline(finding), "emoji": True}},
        {"type": "context",
         "elements": [{"type": "mrkdwn", "text": _context_line(finding)}]},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": _players_line(finding)}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": _numbers_line(finding)}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "_%s_" % finding.detail}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": "*Caption draft*\n>>> %s" % caption}},
        {"type": "actions",
         "elements": [{
             "type": "button",
             "text": {"type": "plain_text", "text": "Watch the game", "emoji": True},
             "url": finding.game.url,
         }]},
        {"type": "divider"},
    ]
    return {
        "text": "%s: %s %s" % (_headline(finding), finding.move.numbered_san,
                              finding.game.headline),
        "blocks": blocks,
    }


class Slack:
    def __init__(self, webhook_url: str | None = None, dry_run: bool | None = None):
        self.webhook_url = webhook_url or os.getenv(WEBHOOK_ENV, "").strip()
        if dry_run is None:
            dry_run = not self.webhook_url
        self.dry_run = dry_run
        self.sent = 0

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def post(self, finding) -> bool:
        payload = build_message(finding)
        caption = draft_caption(finding)
        problems = lint_caption(caption)
        if problems:
            # Do not silently ship off-brand copy; say so in the log.
            print("  ! caption uses off-brand words %s" % problems)

        if self.dry_run:
            print("\n----- would post to Slack -----")
            print(_headline(finding))
            print(_context_line(finding))
            print(_numbers_line(finding).replace("*", "").replace("`", ""))
            print(finding.detail)
            print("CAPTION:")
            print(caption)
            print("LINK: %s" % finding.game.url)
            print("-------------------------------")
            self.sent += 1
            return True

        for attempt in range(3):
            try:
                response = requests.post(self.webhook_url, json=payload,
                                         timeout=TIMEOUT)
                if response.status_code == 200:
                    self.sent += 1
                    return True
                # Never print the response body verbatim: it can echo the URL.
                print("  ! Slack refused the message (HTTP %d)" % response.status_code)
            except Exception as exc:
                print("  ! Slack request failed: %s" % type(exc).__name__)
            time.sleep(2 * (attempt + 1))
        return False

    def post_text(self, text: str) -> bool:
        """A plain one-line message, used for start-up and error notices."""
        if self.dry_run:
            print("[slack] %s" % text)
            return True
        try:
            response = requests.post(self.webhook_url, json={"text": text},
                                     timeout=TIMEOUT)
            return response.status_code == 200
        except Exception:
            return False
