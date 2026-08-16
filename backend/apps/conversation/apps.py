from __future__ import annotations

from django.apps import AppConfig


class ConversationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.conversation"
    label = "conversation"
