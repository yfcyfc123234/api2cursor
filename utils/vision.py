"""视觉识别：将图片转为文字描述

只在 DeepSeek 报 image_url 错误时才触发，不影响正常请求速度。
使用 Anthropic Messages API（Claude Haiku），最快最便宜。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

VISION_MODEL = 'claude-haiku-4-5-20251001'  # 最快最便宜
VISION_MAX_TOKENS = 300
VISION_TIMEOUT = 10  # 秒


def describe_image(base64_data: str, media_type: str = 'image/png') -> str | None:
    """调用 Claude Vision 描述图片内容。

    返回不超过 300 token 的简洁描述，用于替换 image_url 传给 DeepSeek。
    失败时返回 None，调用方应回退到 [图片] 占位符。
    """
    import time as _time
    import os

    api_key = _get_vision_api_key()
    base_url = _get_vision_base_url()
    if not api_key:
        logger.warning('[视觉] 未配置 Vision API key，跳过')
        return None

    payload = {
        'model': VISION_MODEL,
        'max_tokens': VISION_MAX_TOKENS,
        'messages': [{
            'role': 'user',
            'content': [
                {
                    'type': 'image',
                    'source': {
                        'type': 'base64',
                        'media_type': media_type,
                        'data': base64_data,
                    },
                },
                {
                    'type': 'text',
                    'text': (
                        '请用中文简洁描述这张图片的内容。如果是代码截图，描述代码功能和关键逻辑。'
                        '如果是 UI 设计图，描述布局和关键组件。不超过 150 字。'
                    ),
                },
            ],
        }],
    }

    headers = {
        'Content-Type': 'application/json',
        'x-api-key': api_key,
        'anthropic-version': '2023-06-01',
    }

    t0 = _time.time()
    try:
        import requests as _requests
        resp = _requests.post(
            base_url.rstrip('/') + '/v1/messages',
            json=payload,
            headers=headers,
            timeout=VISION_TIMEOUT,
        )
        elapsed = int((_time.time() - t0) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            text = ''
            for block in data.get('content', []):
                if block.get('type') == 'text':
                    text += block.get('text', '')
            logger.info('[视觉] 成功，%dms: %s', elapsed, text[:100])
            return text.strip() or None
        else:
            logger.warning('[视觉] 失败 HTTP %d: %s', resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        elapsed = int((_time.time() - t0) * 1000)
        logger.warning('[视觉] 异常 %dms: %s', elapsed, e)
        return None


def _get_vision_api_key() -> str:
    """获取 Vision API key：优先环境变量，其次全局代理配置。"""
    import os
    key = os.getenv('VISION_API_KEY', '').strip()
    if key:
        return key
    # 回退到全局代理 API key（如果上游支持 Vision）
    from config import Config
    import settings
    return settings.get_key() or Config.PROXY_API_KEY


def _get_vision_base_url() -> str:
    """获取 Vision API 地址。"""
    import os
    url = os.getenv('VISION_BASE_URL', '').strip()
    if url:
        return url
    from config import Config
    import settings
    return settings.get_url() or Config.PROXY_TARGET_URL
