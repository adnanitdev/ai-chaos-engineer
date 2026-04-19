"""
reporter/slack_notifier.py
Posts a rich Slack message with the resilience report summary.
"""
from __future__ import annotations
import os
import requests
from config_loader import load_config
from reporter.ai_analyzer import ResilienceReport


SCORE_EMOJI = {range(80, 101): ":white_check_mark:", range(50, 80): ":warning:", range(0, 50): ":red_circle:"}


def _score_emoji(score: int) -> str:
    for r, emoji in SCORE_EMOJI.items():
        if score in r:
            return emoji
    return ":question:"


class SlackNotifier:
    def __init__(self, config_path: str = "config.yaml"):
        cfg = load_config(config_path)
        self.enabled = cfg["slack"]["enabled"]
        self.webhook_url = cfg["slack"].get("webhook_url") or os.getenv("SLACK_WEBHOOK_URL", "")
        self.channel = cfg["slack"].get("channel", "#chaos-reports")

    def notify(
        self,
        report: ResilienceReport,
        cluster_context: str = "unknown",
        report_path: str = "",
    ) -> bool:
        if not self.enabled or not self.webhook_url:
            return False

        emoji = _score_emoji(report.overall_resilience_score)
        color = "#2eb886" if report.overall_resilience_score >= 80 else \
                "#daa038" if report.overall_resilience_score >= 50 else "#cc0000"

        top_recs = "\n".join(
            f"• [{r['priority']}] {r['action']}"
            for r in report.recommendations[:3]
        )

        payload = {
            "channel": self.channel,
            "attachments": [
                {
                    "color": color,
                    "blocks": [
                        {
                            "type": "header",
                            "text": {
                                "type": "plain_text",
                                "text": f"{emoji} Chaos Engineering Report — {cluster_context}",
                            },
                        },
                        {
                            "type": "section",
                            "fields": [
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Resilience Score*\n{report.overall_resilience_score}/100",
                                },
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Verdict*\n{report.overall_verdict.upper()}",
                                },
                            ],
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Summary*\n{report.executive_summary}",
                            },
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Top Recommendations*\n{top_recs}",
                            },
                        },
                        *(
                            [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": f"*Full Report*\n`{report_path}`",
                                    },
                                }
                            ]
                            if report_path
                            else []
                        ),
                    ],
                }
            ],
        }

        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            print(f"[warn] Slack notification failed: {e}")
            return False
