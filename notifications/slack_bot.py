"""Interactive Slack bot for managing keywords and forums via buttons and modals."""

import asyncio
import json
import re
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ..models import SlackConfig, ForumConfig
from ..monitoring.analyzer import ContentAnalyzer
from ..monitoring.state_manager import StateManager
from ..forums.registry import create_forum
from ..utils.logger import get_logger

logger = get_logger("slack_bot")


class SlackBot:

    def __init__(self, bot_token, app_token, signing_secret,
                 analyzer, state, config=None, http_client=None):
        self.analyzer = analyzer
        self.state = state
        self.app_token = app_token
        self.config = config
        self.http_client = http_client
        self.app = App(token=bot_token, signing_secret=signing_secret)
        self._register_handlers()
        logger.info("slack_bot_initialized")

    def _register_handlers(self):

        @self.app.command("/keywords")
        def handle_keywords_command(ack, body, client):
            ack()
            self._show_keywords_home(body["channel_id"], body["user_id"], client)

        @self.app.action("view_keywords")
        def handle_view(ack, body, client):
            ack()
            self._show_keywords_list(body["channel"]["id"], client)

        @self.app.action("add_keyword")
        def handle_add_button(ack, body, client):
            ack()
            self._open_add_modal(body["trigger_id"], client)

        @self.app.action("remove_keyword")
        def handle_remove_button(ack, body, client):
            ack()
            self._open_remove_modal(body["trigger_id"], client)

        @self.app.action("run_scan")
        def handle_scan_button(ack, body, client):
            ack()
            self._open_scan_modal(body["trigger_id"], client)

        @self.app.view("add_keyword_modal")
        def handle_add_submission(ack, body, view, client):
            values = view["state"]["values"]
            group = values["group_block"]["group_select"]["selected_option"]["value"]
            keyword = values["keyword_block"]["keyword_input"]["value"].strip()
            days_back = values["backfill_block"]["backfill_select"]["selected_option"]["value"]
            try:
                re.compile(keyword, re.IGNORECASE)
            except re.error as e:
                ack(response_action="errors", errors={"keyword_block": f"Invalid pattern: {e}"})
                return
            ack()
            user_id = body["user"]["id"]
            self.state.add_user_keyword(group, keyword, added_by=user_id)
            self.analyzer.add_keyword(group, keyword)
            if days_back == "0":
                client.chat_postMessage(channel=user_id, text=f"Keyword added to *{group}*: `{keyword}`\nIt will be used in the next monitoring cycle.", mrkdwn=True)
            else:
                client.chat_postMessage(channel=user_id, text=f"Keyword added to *{group}*: `{keyword}`\nStarting backfill scan for the last *{days_back} days*...", mrkdwn=True)
                self._run_backfill_scan(keyword, group, int(days_back), user_id, client)
            logger.info("keyword_added_via_slack", group=group, pattern=keyword, user=user_id, backfill_days=days_back)

        @self.app.view("remove_keyword_modal")
        def handle_remove_submission(ack, body, view, client):
            ack()
            values = view["state"]["values"]
            selected = values["remove_block"]["remove_select"]["selected_options"]
            user_id = body["user"]["id"]
            removed = []
            for option in selected:
                val = option["value"]
                if val.startswith("db:"):
                    # User-added keyword — delete from database
                    parts = val[3:].split(":", 2)
                    if len(parts) == 3:
                        kw_id, group, pattern = int(parts[0]), parts[1], parts[2]
                        self.state.remove_user_keyword(kw_id)
                        self.analyzer.remove_keyword(group, pattern)
                        removed.append(f"`{pattern}` from *{group}* (deleted)")
                elif val.startswith("cfg:"):
                    # Built-in keyword — disable it
                    parts = val[4:].split(":", 1)
                    if len(parts) == 2:
                        group, pattern = parts[0], parts[1]
                        item_key = f"{group}:{pattern}"
                        self.state.disable_item("keyword", item_key, disabled_by=user_id)
                        self.analyzer.remove_keyword(group, pattern)
                        removed.append(f"`{pattern}` from *{group}* (disabled)")
            if removed:
                client.chat_postMessage(channel=user_id, text="Removed keywords:\n" + "\n".join(f"- {r}" for r in removed), mrkdwn=True)
                logger.info("keywords_removed_via_slack", count=len(removed), user=user_id)

        @self.app.action("enable_keywords")
        def handle_enable_keywords(ack, body, client):
            ack()
            self._open_enable_keywords_modal(body["trigger_id"], client)

        @self.app.view("enable_keyword_modal")
        def handle_enable_keyword_submission(ack, body, view, client):
            ack()
            values = view["state"]["values"]
            selected = values["enable_block"]["enable_select"]["selected_options"]
            user_id = body["user"]["id"]
            enabled = []
            for option in selected:
                item_key = option["value"]
                parts = item_key.split(":", 1)
                if len(parts) == 2:
                    group, pattern = parts[0], parts[1]
                    self.state.enable_item("keyword", item_key)
                    self.analyzer.add_keyword(group, pattern)
                    enabled.append(f"`{pattern}` in *{group}*")
            if enabled:
                client.chat_postMessage(channel=user_id, text="Re-enabled keywords:\n" + "\n".join(f"- {e}" for e in enabled), mrkdwn=True)
                logger.info("keywords_enabled_via_slack", count=len(enabled), user=user_id)

        @self.app.view("scan_modal")
        def handle_scan_submission(ack, body, view, client):
            ack()
            values = view["state"]["values"]
            days_back = int(values["scan_days_block"]["scan_days_select"]["selected_option"]["value"])
            user_id = body["user"]["id"]
            client.chat_postMessage(channel=user_id, text=f"Starting full scan for the last *{days_back} days*... This may take a few minutes.", mrkdwn=True)
            self._run_full_scan(days_back, user_id, client)

        # ── Forum Handlers ────────────────────────────────────────

        @self.app.command("/forums")
        def handle_forums_command(ack, body, client):
            ack()
            self._show_forums_home(body["channel_id"], body["user_id"], client)

        @self.app.action("view_forums")
        def handle_view_forums(ack, body, client):
            ack()
            self._show_forums_list(body["channel"]["id"], client)

        @self.app.action("add_forum")
        def handle_add_forum_button(ack, body, client):
            ack()
            self._open_add_forum_modal(body["trigger_id"], client)

        @self.app.action("remove_forum")
        def handle_remove_forum_button(ack, body, client):
            ack()
            self._open_remove_forum_modal(body["trigger_id"], client)

        @self.app.view("add_forum_modal")
        def handle_add_forum_submission(ack, body, view, client):
            values = view["state"]["values"]
            name = values["forum_name_block"]["forum_name_input"]["value"].strip().lower().replace(" ", "_")
            url = values["forum_url_block"]["forum_url_input"]["value"].strip().rstrip("/")
            if not url.startswith("http"):
                url = "https://" + url
            if not name:
                ack(response_action="errors", errors={"forum_name_block": "Name is required"})
                return
            if not url:
                ack(response_action="errors", errors={"forum_url_block": "URL is required"})
                return
            ack()
            user_id = body["user"]["id"]
            self.state.add_user_forum(name, url, added_by=user_id)
            fc = ForumConfig(name=name, url=url, type="discourse", enabled=True, categories=[])
            if self.config:
                self.config.forums.append(fc)
            if self.http_client:
                forum = create_forum(fc, self.http_client)
                if not hasattr(self, '_user_forums'):
                    self._user_forums = []
                self._user_forums.append(forum)
            client.chat_postMessage(channel=user_id, text=f"Forum added: *{name}* (`{url}`)\nIt will be monitored starting next cycle.", mrkdwn=True)
            logger.info("forum_added_via_slack", name=name, url=url, user=user_id)

        @self.app.view("remove_forum_modal")
        def handle_remove_forum_submission(ack, body, view, client):
            ack()
            values = view["state"]["values"]
            selected = values["remove_forum_block"]["remove_forum_select"]["selected_options"]
            user_id = body["user"]["id"]
            removed = []
            for option in selected:
                val = option["value"]
                if val.startswith("db:"):
                    # User-added forum — delete from database
                    parts = val[3:].split(":", 1)
                    if len(parts) == 2:
                        forum_id, forum_name = int(parts[0]), parts[1]
                        self.state.remove_user_forum(forum_id)
                        if self.config:
                            self.config.forums = [f for f in self.config.forums if f.name != forum_name]
                        removed.append(f"*{forum_name}* (deleted)")
                elif val.startswith("cfg:"):
                    # Built-in forum — disable it
                    forum_name = val[4:]
                    self.state.disable_item("forum", forum_name, disabled_by=user_id)
                    if self.config:
                        for f in self.config.forums:
                            if f.name == forum_name:
                                f.enabled = False
                    removed.append(f"*{forum_name}* (disabled)")
            if removed:
                client.chat_postMessage(channel=user_id, text="Removed forums:\n" + "\n".join(f"- {r}" for r in removed), mrkdwn=True)
                logger.info("forums_removed_via_slack", count=len(removed), user=user_id)

        @self.app.action("enable_forums")
        def handle_enable_forums(ack, body, client):
            ack()
            self._open_enable_forums_modal(body["trigger_id"], client)

        @self.app.view("enable_forum_modal")
        def handle_enable_forum_submission(ack, body, view, client):
            ack()
            values = view["state"]["values"]
            selected = values["enable_forum_block"]["enable_forum_select"]["selected_options"]
            user_id = body["user"]["id"]
            enabled = []
            for option in selected:
                forum_name = option["value"]
                self.state.enable_item("forum", forum_name)
                if self.config:
                    for f in self.config.forums:
                        if f.name == forum_name:
                            f.enabled = True
                enabled.append(f"*{forum_name}*")
            if enabled:
                client.chat_postMessage(channel=user_id, text="Re-enabled forums:\n" + "\n".join(f"- {e}" for e in enabled), mrkdwn=True)
                logger.info("forums_enabled_via_slack", count=len(enabled), user=user_id)

    def _show_keywords_home(self, channel_id, user_id, client):
        all_keywords = self.analyzer.get_all_keywords()
        total = sum(len(v) for v in all_keywords.values())
        disabled_kws = self.state.list_disabled_items("keyword")
        disabled_count = len(disabled_kws)
        status_text = f"Currently monitoring *{total} keywords* across *{len(all_keywords)} groups*."
        if disabled_count > 0:
            status_text += f"\n_{disabled_count} keyword(s) disabled._"
        buttons = [
            {"type": "button", "text": {"type": "plain_text", "text": "View All Keywords"}, "action_id": "view_keywords", "style": "primary"},
            {"type": "button", "text": {"type": "plain_text", "text": "Add Keyword"}, "action_id": "add_keyword"},
            {"type": "button", "text": {"type": "plain_text", "text": "Remove Keyword"}, "action_id": "remove_keyword", "style": "danger"},
        ]
        if disabled_count > 0:
            buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Re-enable Keywords"}, "action_id": "enable_keywords"})
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "DAO Governance Monitor - Keywords"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": status_text}},
            {"type": "divider"},
            {"type": "actions", "elements": buttons},
            {"type": "divider"},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Run Historical Scan"}, "action_id": "run_scan"},
            ]}
        ]
        client.chat_postMessage(channel=channel_id, blocks=blocks, text="Keyword Management")

    def _show_keywords_list(self, channel_id, client):
        all_keywords = self.analyzer.get_all_keywords()
        db_keywords = self.state.list_keywords()
        db_patterns = {kw.keyword_text for kw in db_keywords}
        disabled_kws = self.state.list_disabled_items("keyword")
        disabled_keys = {d.item_key for d in disabled_kws}
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Current Keywords"}}]
        for group, patterns in all_keywords.items():
            keyword_lines = []
            for p in patterns[:25]:
                marker = " (user-added)" if p in db_patterns else ""
                keyword_lines.append(f"`{p}`{marker}")
            remaining = len(patterns) - 25
            text = "\n".join(keyword_lines)
            if remaining > 0:
                text += f"\n_...and {remaining} more_"
            blocks.append({"type": "divider"})
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{group.upper()}* ({len(patterns)} active)\n{text}"}})
        # Show disabled keywords
        if disabled_keys:
            disabled_lines = []
            for key in disabled_keys:
                parts = key.split(":", 1)
                if len(parts) == 2:
                    disabled_lines.append(f"~`{parts[1]}`~ from *{parts[0]}*")
            if disabled_lines:
                blocks.append({"type": "divider"})
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*DISABLED* ({len(disabled_lines)})\n" + "\n".join(disabled_lines[:15])}})
        client.chat_postMessage(channel=channel_id, blocks=blocks, text="Current Keywords List")

    def _open_add_modal(self, trigger_id, client):
        all_keywords = self.analyzer.get_all_keywords()
        group_options = [{"text": {"type": "plain_text", "text": g.capitalize()}, "value": g} for g in all_keywords.keys()]
        group_options.append({"text": {"type": "plain_text", "text": "New Group..."}, "value": "_new_"})
        backfill_options = [
            {"text": {"type": "plain_text", "text": "Don't scan past posts"}, "value": "0"},
            {"text": {"type": "plain_text", "text": "Last 2 days"}, "value": "2"},
            {"text": {"type": "plain_text", "text": "Last 7 days"}, "value": "7"},
            {"text": {"type": "plain_text", "text": "Last 14 days"}, "value": "14"},
            {"text": {"type": "plain_text", "text": "Last 30 days"}, "value": "30"},
            {"text": {"type": "plain_text", "text": "Last 60 days"}, "value": "60"},
            {"text": {"type": "plain_text", "text": "Last 90 days"}, "value": "90"},
        ]
        modal = {
            "type": "modal",
            "callback_id": "add_keyword_modal",
            "title": {"type": "plain_text", "text": "Add Keyword"},
            "submit": {"type": "plain_text", "text": "Add"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Add a new keyword pattern to monitor across all DAO forums."}},
                {"type": "input", "block_id": "group_block", "element": {"type": "static_select", "action_id": "group_select", "placeholder": {"type": "plain_text", "text": "Select a group"}, "options": group_options}, "label": {"type": "plain_text", "text": "Keyword Group"}},
                {"type": "input", "block_id": "keyword_block", "element": {"type": "plain_text_input", "action_id": "keyword_input", "placeholder": {"type": "plain_text", "text": "e.g., buyback or treasury"}}, "label": {"type": "plain_text", "text": "Keyword or Pattern"}, "hint": {"type": "plain_text", "text": "Enter a word or regex pattern. Case-insensitive by default."}},
                {"type": "input", "block_id": "backfill_block", "element": {"type": "static_select", "action_id": "backfill_select", "placeholder": {"type": "plain_text", "text": "Select timeframe"}, "initial_option": backfill_options[0], "options": backfill_options}, "label": {"type": "plain_text", "text": "Scan Past Posts"}, "hint": {"type": "plain_text", "text": "Optionally scan historical forum posts for this keyword."}},
            ]
        }
        client.views_open(trigger_id=trigger_id, view=modal)

    def _open_remove_modal(self, trigger_id, client):
        db_keywords = self.state.list_keywords()
        all_keywords = self.analyzer.get_all_keywords()
        db_patterns = {kw.keyword_text for kw in db_keywords}
        options = []
        # Add user-added keywords (these get fully deleted)
        for kw in db_keywords:
            label = f"[{kw.group}] {kw.keyword_text} (user-added)"
            if len(label) > 75:
                label = label[:72] + "..."
            options.append({"text": {"type": "plain_text", "text": label}, "value": f"db:{kw.id}:{kw.group}:{kw.keyword_text}"})
        # Add built-in keywords (these get disabled)
        for group, patterns in all_keywords.items():
            for p in patterns:
                if p not in db_patterns:
                    label = f"[{group}] {p} (built-in)"
                    if len(label) > 75:
                        label = label[:72] + "..."
                    options.append({"text": {"type": "plain_text", "text": label}, "value": f"cfg:{group}:{p}"})
        if not options:
            modal = {"type": "modal", "callback_id": "remove_keyword_modal", "title": {"type": "plain_text", "text": "Remove Keywords"}, "close": {"type": "plain_text", "text": "Close"}, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "No keywords available to remove or disable."}}]}
            client.views_open(trigger_id=trigger_id, view=modal)
            return
        modal = {"type": "modal", "callback_id": "remove_keyword_modal", "title": {"type": "plain_text", "text": "Remove Keywords"}, "submit": {"type": "plain_text", "text": "Remove / Disable"}, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Select keywords to remove. User-added keywords are deleted. Built-in keywords are disabled (can be re-enabled later)."}}, {"type": "input", "block_id": "remove_block", "element": {"type": "multi_static_select", "action_id": "remove_select", "placeholder": {"type": "plain_text", "text": "Search and select keywords..."}, "options": options[:100]}, "label": {"type": "plain_text", "text": "Keywords"}}]}
        client.views_open(trigger_id=trigger_id, view=modal)

    def _open_scan_modal(self, trigger_id, client):
        scan_options = [
            {"text": {"type": "plain_text", "text": "Last 2 days"}, "value": "2"},
            {"text": {"type": "plain_text", "text": "Last 7 days"}, "value": "7"},
            {"text": {"type": "plain_text", "text": "Last 14 days"}, "value": "14"},
            {"text": {"type": "plain_text", "text": "Last 30 days"}, "value": "30"},
            {"text": {"type": "plain_text", "text": "Last 60 days"}, "value": "60"},
            {"text": {"type": "plain_text", "text": "Last 90 days"}, "value": "90"},
        ]
        modal = {
            "type": "modal",
            "callback_id": "scan_modal",
            "title": {"type": "plain_text", "text": "Historical Scan"},
            "submit": {"type": "plain_text", "text": "Start Scan"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Run a scan across all forums using all current keywords."}},
                {"type": "input", "block_id": "scan_days_block", "element": {"type": "static_select", "action_id": "scan_days_select", "placeholder": {"type": "plain_text", "text": "Select timeframe"}, "options": scan_options}, "label": {"type": "plain_text", "text": "How far back to scan"}},
            ]
        }
        client.views_open(trigger_id=trigger_id, view=modal)

    # ── Forum UI Methods ────────────────────────────────────────

    def _show_forums_home(self, channel_id, user_id, client):
        active_forums = len([f for f in self.config.forums if f.enabled]) if self.config else 0
        db_forums = self.state.list_user_forums()
        disabled_forums = self.state.list_disabled_items("forum")
        disabled_count = len(disabled_forums)
        status_text = f"Currently monitoring *{active_forums} forums*. *{len(db_forums)}* added via Slack."
        if disabled_count > 0:
            status_text += f"\n_{disabled_count} forum(s) disabled._"
        buttons = [
            {"type": "button", "text": {"type": "plain_text", "text": "View All Forums"}, "action_id": "view_forums", "style": "primary"},
            {"type": "button", "text": {"type": "plain_text", "text": "Add Forum"}, "action_id": "add_forum"},
            {"type": "button", "text": {"type": "plain_text", "text": "Remove Forum"}, "action_id": "remove_forum", "style": "danger"},
        ]
        if disabled_count > 0:
            buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Re-enable Forums"}, "action_id": "enable_forums"})
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "DAO Governance Monitor - Forums"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": status_text}},
            {"type": "divider"},
            {"type": "actions", "elements": buttons},
        ]
        client.chat_postMessage(channel=channel_id, blocks=blocks, text="Forum Management")

    def _show_forums_list(self, channel_id, client):
        db_forums = self.state.list_user_forums()
        db_names = {f.name for f in db_forums}
        disabled_forums = self.state.list_disabled_items("forum")
        disabled_names = {d.item_key for d in disabled_forums}
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Monitored Forums"}}]
        if self.config:
            active_lines = []
            for f in self.config.forums:
                if f.enabled:
                    marker = " (user-added)" if f.name in db_names else ""
                    active_lines.append(f"`{f.name}` - {f.url}{marker}")
            text = "\n".join(active_lines[:50])
            remaining = len(active_lines) - 50
            if remaining > 0:
                text += f"\n_...and {remaining} more_"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text or "_No active forums._"}})
            # Show disabled forums
            if disabled_names:
                disabled_lines = [f"~`{name}`~" for name in disabled_names]
                blocks.append({"type": "divider"})
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*DISABLED* ({len(disabled_lines)})\n" + "\n".join(disabled_lines[:20])}})
        client.chat_postMessage(channel=channel_id, blocks=blocks, text="Forums List")

    def _open_add_forum_modal(self, trigger_id, client):
        modal = {
            "type": "modal",
            "callback_id": "add_forum_modal",
            "title": {"type": "plain_text", "text": "Add Forum"},
            "submit": {"type": "plain_text", "text": "Add"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Add a new Discourse forum to monitor."}},
                {"type": "input", "block_id": "forum_name_block", "element": {"type": "plain_text_input", "action_id": "forum_name_input", "placeholder": {"type": "plain_text", "text": "e.g., cowswap"}}, "label": {"type": "plain_text", "text": "Forum Name"}, "hint": {"type": "plain_text", "text": "A short unique name (no spaces)."}},
                {"type": "input", "block_id": "forum_url_block", "element": {"type": "plain_text_input", "action_id": "forum_url_input", "placeholder": {"type": "plain_text", "text": "e.g., https://forum.cow.fi"}}, "label": {"type": "plain_text", "text": "Forum URL"}, "hint": {"type": "plain_text", "text": "The base URL of the Discourse forum."}},
            ]
        }
        client.views_open(trigger_id=trigger_id, view=modal)

    def _open_remove_forum_modal(self, trigger_id, client):
        db_forums = self.state.list_user_forums()
        db_names = {f.name for f in db_forums}
        options = []
        # User-added forums (fully deleted)
        for f in db_forums:
            label = f"{f.name} (user-added)"
            if len(label) > 75:
                label = label[:72] + "..."
            options.append({"text": {"type": "plain_text", "text": label}, "value": f"db:{f.id}:{f.name}"})
        # Built-in forums (disabled)
        if self.config:
            for f in self.config.forums:
                if f.enabled and f.name not in db_names:
                    label = f"{f.name} (built-in)"
                    if len(label) > 75:
                        label = label[:72] + "..."
                    options.append({"text": {"type": "plain_text", "text": label}, "value": f"cfg:{f.name}"})
        if not options:
            modal = {"type": "modal", "callback_id": "remove_forum_modal", "title": {"type": "plain_text", "text": "Remove Forums"}, "close": {"type": "plain_text", "text": "Close"}, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "No forums available to remove or disable."}}]}
            client.views_open(trigger_id=trigger_id, view=modal)
            return
        modal = {"type": "modal", "callback_id": "remove_forum_modal", "title": {"type": "plain_text", "text": "Remove Forums"}, "submit": {"type": "plain_text", "text": "Remove / Disable"}, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Select forums to remove. User-added forums are deleted. Built-in forums are disabled (can be re-enabled later)."}}, {"type": "input", "block_id": "remove_forum_block", "element": {"type": "multi_static_select", "action_id": "remove_forum_select", "placeholder": {"type": "plain_text", "text": "Search and select forums..."}, "options": options[:100]}, "label": {"type": "plain_text", "text": "Forums"}}]}
        client.views_open(trigger_id=trigger_id, view=modal)

    # ── Re-enable Modals ────────────────────────────────────────

    def _open_enable_keywords_modal(self, trigger_id, client):
        disabled = self.state.list_disabled_items("keyword")
        if not disabled:
            modal = {"type": "modal", "callback_id": "enable_keyword_modal", "title": {"type": "plain_text", "text": "Re-enable Keywords"}, "close": {"type": "plain_text", "text": "Close"}, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "No disabled keywords to re-enable."}}]}
            client.views_open(trigger_id=trigger_id, view=modal)
            return
        options = []
        for d in disabled:
            parts = d.item_key.split(":", 1)
            if len(parts) == 2:
                label = f"[{parts[0]}] {parts[1]}"
                if len(label) > 75:
                    label = label[:72] + "..."
                options.append({"text": {"type": "plain_text", "text": label}, "value": d.item_key})
        modal = {"type": "modal", "callback_id": "enable_keyword_modal", "title": {"type": "plain_text", "text": "Re-enable Keywords"}, "submit": {"type": "plain_text", "text": "Re-enable"}, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Select disabled keywords to re-enable:"}}, {"type": "input", "block_id": "enable_block", "element": {"type": "multi_static_select", "action_id": "enable_select", "placeholder": {"type": "plain_text", "text": "Search and select keywords..."}, "options": options[:100]}, "label": {"type": "plain_text", "text": "Disabled Keywords"}}]}
        client.views_open(trigger_id=trigger_id, view=modal)

    def _open_enable_forums_modal(self, trigger_id, client):
        disabled = self.state.list_disabled_items("forum")
        if not disabled:
            modal = {"type": "modal", "callback_id": "enable_forum_modal", "title": {"type": "plain_text", "text": "Re-enable Forums"}, "close": {"type": "plain_text", "text": "Close"}, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "No disabled forums to re-enable."}}]}
            client.views_open(trigger_id=trigger_id, view=modal)
            return
        options = []
        for d in disabled:
            label = d.item_key
            if len(label) > 75:
                label = label[:72] + "..."
            options.append({"text": {"type": "plain_text", "text": label}, "value": d.item_key})
        modal = {"type": "modal", "callback_id": "enable_forum_modal", "title": {"type": "plain_text", "text": "Re-enable Forums"}, "submit": {"type": "plain_text", "text": "Re-enable"}, "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Select disabled forums to re-enable:"}}, {"type": "input", "block_id": "enable_forum_block", "element": {"type": "multi_static_select", "action_id": "enable_forum_select", "placeholder": {"type": "plain_text", "text": "Search and select forums..."}, "options": options[:100]}, "label": {"type": "plain_text", "text": "Disabled Forums"}}]}
        client.views_open(trigger_id=trigger_id, view=modal)

    # ── Scanning ──────────────────────────────────────────────────

    def _run_backfill_scan(self, keyword, group, days, user_id, client):
        if not self.config or not self.http_client:
            client.chat_postMessage(channel=user_id, text="Backfill scanning not available.")
            return
        def _scan():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                found = loop.run_until_complete(self._async_backfill(keyword, group, days, user_id, client))
                client.chat_postMessage(channel=user_id, text=f"Backfill complete! Found *{found}* posts matching `{keyword}` in the last {days} days.", mrkdwn=True)
            except Exception as e:
                logger.error("backfill_scan_error", error=str(e))
                client.chat_postMessage(channel=user_id, text=f"Backfill scan error: {str(e)[:200]}")
            finally:
                loop.close()
        threading.Thread(target=_scan, daemon=True).start()

    def _run_full_scan(self, days, user_id, client):
        if not self.config or not self.http_client:
            client.chat_postMessage(channel=user_id, text="Scanning not available.")
            return
        def _scan():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                found = loop.run_until_complete(self._async_full(days, user_id, client))
                client.chat_postMessage(channel=user_id, text=f"Full scan complete! Found *{found}* matching posts in the last {days} days.", mrkdwn=True)
            except Exception as e:
                logger.error("full_scan_error", error=str(e))
                client.chat_postMessage(channel=user_id, text=f"Full scan error: {str(e)[:200]}")
            finally:
                loop.close()
        threading.Thread(target=_scan, daemon=True).start()

    async def _async_backfill(self, keyword, group, days, user_id, client):
        from ..forums.registry import create_forum
        from ..notifications.slack import SlackNotifier
        pattern = re.compile(keyword, re.IGNORECASE)
        since_minutes = days * 24 * 60
        found = 0
        scan_state = StateManager(db_path="backfill_scan.db")
        slack = SlackNotifier(self.config.slack) if self.config.slack.webhook_url else None
        forums = [create_forum(fc, self.http_client) for fc in self.config.forums if fc.enabled]
        for forum in forums:
            try:
                posts = await forum.fetch_latest_posts(since_minutes=since_minutes)
                for post in posts:
                    try:
                        detailed = await forum.fetch_topic_details(post.topic_id)
                        if detailed:
                            post = detailed
                    except Exception:
                        pass
                    title_match = pattern.search(post.title)
                    body_match = pattern.search(post.body) if post.body else None
                    if title_match or body_match:
                        if scan_state.should_notify(post.post_id, 1.0):
                            if slack:
                                blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"Backfill: {keyword}"}}, {"type": "section", "text": {"type": "mrkdwn", "text": f"*Forum:* {post.forum_name}\n*Title:* {post.title}\n*Author:* {post.author}"}}, {"type": "section", "text": {"type": "mrkdwn", "text": f"*Preview:* {(post.body or '')[:300]}..."}}, {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "View Post"}, "url": post.url}]}]
                                await slack._send({"blocks": blocks, "text": f"Backfill match: {post.title}"})
                            scan_state.mark_notified(post_id=post.post_id, forum_name=post.forum_name, title=post.title, url=post.url, score=1.0, keywords=keyword, slack_response="sent")
                            found += 1
            except Exception as e:
                logger.error("backfill_forum_error", forum=forum.name, error=str(e))
        return found

    async def _async_full(self, days, user_id, client):
        from ..forums.registry import create_forum
        from ..notifications.slack import SlackNotifier
        since_minutes = days * 24 * 60
        found = 0
        scan_state = StateManager(db_path="full_scan.db")
        slack = SlackNotifier(self.config.slack) if self.config.slack.webhook_url else None
        forums = [create_forum(fc, self.http_client) for fc in self.config.forums if fc.enabled]
        for forum in forums:
            try:
                posts = await forum.fetch_latest_posts(since_minutes=since_minutes)
                for post in posts:
                    try:
                        detailed = await forum.fetch_topic_details(post.topic_id)
                        if detailed:
                            post = detailed
                    except Exception:
                        pass
                    result = self.analyzer.analyze(post)
                    if result.triggered and scan_state.should_notify(post.post_id, result.score):
                        if slack:
                            from .formatter import format_alert
                            message = format_alert(result)
                            await slack._send(message)
                        scan_state.mark_notified(post_id=post.post_id, forum_name=post.forum_name, title=post.title, url=post.url, score=result.score, keywords=json.dumps([m.matched_text for m in result.matches]), slack_response="sent")
                        found += 1
            except Exception as e:
                logger.error("full_scan_forum_error", forum=forum.name, error=str(e))
        return found

    def start(self):
        self._handler = SocketModeHandler(self.app, self.app_token)
        self._thread = threading.Thread(target=self._handler.start, daemon=True)
        self._thread.start()
        logger.info("slack_bot_started", mode="socket_mode")

    def stop(self):
        if hasattr(self, '_handler'):
            self._handler.close()
            logger.info("slack_bot_stopped")
