#!/usr/bin/env python3
"""利用可能なAnthropicモデル一覧を確認する。"""
from __future__ import annotations

import os

import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
page = client.models.list(limit=50)
for model in page.data:
    print(model.id, "|", getattr(model, "display_name", ""))
