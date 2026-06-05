"""视觉识别：将图片转为文字描述

只在 DeepSeek 报 image_url 错误时才触发，不影响正常请求速度。
支持多种后端，国内推荐通义千问 Qwen-VL（阿里云，实名即用）。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 默认配置（可通过环境变量覆盖）
VISION_MODEL = 'qwen-vl-max'       # 通义千问视觉模型
VISION_MAX_TOKENS = 300
VISION_TIMEOUT = 10                 # 秒


def describe_image(base64_data: str, media_type: str = 'image/png') -> str | None:
    """调用视觉模型描述图片内容。

    返回简洁描述，失败时返回 None（调用方应回退到 [图片]）。
    支持 OpenAI 兼容接口（通义千问/GLM-4V/豆包 等）。
    """
    import time as _time

    api_key = _get_vision_api_key()
    base_url = _get_vision_base_url()
    model = _get_vision_model()
    if not api_key:
        logger.warning('[视觉] 未配置 VISION_API_KEY，跳过')
        return None

    # OpenAI 兼容格式（通义千问、GLM-4V、豆包 等都支持）
    payload = {
        'model': model,
        'max_tokens': VISION_MAX_TOKENS,
        'messages': [{
            'role': 'user',
            'content': [
                {
                    'type': 'image_url',
                    'image_url': {
                        'url': f'data:{media_type};base64,{base64_data}',
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
        'Authorization': f'Bearer {api_key}',
    }

    t0 = _time.time()
    try:
        import requests as _requests
        resp = _requests.post(
            base_url.rstrip('/') + '/v1/chat/completions',
            json=payload,
            headers=headers,
            timeout=VISION_TIMEOUT,
        )
        elapsed = int((_time.time() - t0) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            text = ''
            for choice in data.get('choices', []):
                msg = choice.get('message', {})
                text += msg.get('content', '')
            logger.info('[视觉] 成功 %dms: %s', elapsed, text[:100])
            return text.strip() or None
        else:
            logger.warning('[视觉] 失败 HTTP %d: %s', resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        elapsed = int((_time.time() - t0) * 1000)
        logger.warning('[视觉] 异常 %dms: %s', elapsed, e)
        return None


def _get_vision_api_key() -> str:
    import os
    return os.getenv('VISION_API_KEY', '').strip()


def _get_vision_base_url() -> str:
    import os
    return os.getenv('VISION_BASE_URL', '').strip() or 'https://dashscope.aliyuncs.com/compatible-mode'


def _get_vision_model() -> str:
    import os
    return os.getenv('VISION_MODEL', '').strip() or VISION_MODEL
