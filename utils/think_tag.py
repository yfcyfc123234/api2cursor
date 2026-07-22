"""<think> 标签提取器

部分上游 API（如 DeepSeek）使用 <think>...</think> 标签包裹思考内容，
而 Cursor 期望 reasoning_content 字段。本模块在流式和非流式响应中
提取 <think> 标签内容并转为 reasoning_content。
"""

import re

_THINK_RE = re.compile(r'<think>(.*?)</think>', re.DOTALL)


def extract_from_text(content):
    """从文本中提取 <think> 标签（非流式）

    返回: (cleaned_content, reasoning_content)
    """
    if not isinstance(content, str) or '<think>' not in content:
        return content, None
    m = _THINK_RE.search(content)
    if not m:
        return content, None
    reasoning = m.group(1).strip()
    cleaned = (content[:m.start()] + content[m.end():]).strip() or None
    return cleaned, reasoning


class ThinkTagExtractor:
    """流式 <think> 标签提取器 + reasoning_content → content 桥接

    处理跨 chunk 的 <think>...</think> 标签，将标签内的文本
    转为 reasoning_content delta，标签外的文本保持为 content delta。

    桥接逻辑：部分上游（如 DeepSeek）在 delta 中直接返回 reasoning_content，
    但 Cursor 的 OpenAI 兼容客户端不渲染该字段。这里将 reasoning_content
    额外复制到 content，并用 `▸` 前缀区分思考过程与最终回答。

    额外处理：
    - content 和 tool_calls 同时出现时拆分为两个独立 chunk（Cursor 会丢弃同时包含两者的 content）
    - tool_calls 首次出现时在前面插入换行，确保文本以换行结束
    - 流结束时如果 think 标签仍未关闭，自动合成关闭 chunk
    """

    def __init__(self, enable_bridge: bool = False):
        """初始化跨 chunk 的 thinking 状态跟踪。

        enable_bridge: 是否将上游 reasoning_content 复制到 content（供 Cursor 显示）
        """
        self._in_thinking = False
        self._tool_calls_seen = False
        self._enable_bridge = enable_bridge
        self._reasoning_seen = False   # 是否已输出过 reasoning 头部标记
        self._reasoning_ended = False  # reasoning 阶段是否已结束

    def process_chunk(self, chunk):
        """处理一个流式 chunk，返回转换后的 chunk 列表"""
        for choice in (chunk.get('choices') or []):
            delta = choice.get('delta') or {}

            has_tool_calls = bool(delta.get('tool_calls'))
            has_content = delta.get('content') is not None and delta.get('content') != ''

            # content 和 tool_calls 同时出现：拆分为两个独立事件
            if has_content and has_tool_calls:
                results = []
                content_chunk = self._make(chunk, content=delta['content'])
                results.extend(self._process_content(content_chunk, delta['content']))
                tc_chunk = {
                    'id': chunk.get('id', ''),
                    'object': 'chat.completion.chunk',
                    'model': chunk.get('model', ''),
                    'choices': [{'index': 0, 'delta': {'tool_calls': delta['tool_calls']},
                                 'finish_reason': choice.get('finish_reason')}],
                }
                results.extend(self._handle_tool_calls_chunk(tc_chunk))
                return results

            if has_tool_calls:
                return self._handle_tool_calls_chunk(chunk)

            if delta.get('reasoning_content'):
                if self._enable_bridge:
                    return self._bridge_reasoning(chunk, delta['reasoning_content'])
                return [chunk]
            content = delta.get('content')
            if content is None or content == '':
                return [chunk]
            return self._process_content(chunk, content)
        return [chunk]

    # ─── reasoning → content 桥接 ──────────────────────

    def _bridge_reasoning(self, chunk, reasoning_text):
        """将上游 reasoning_content 同时写入 delta.content。

        上游的 reasoning_content 字段保留不动，额外把同一个文本塞进
        content，这样 Cursor 即使不渲染 reasoning_content 也能显示思考过程。
        """
        results = []

        # 首次 reasoning：输出视觉分隔头部
        if not self._reasoning_seen:
            self._reasoning_seen = True
            results.append(self._make(chunk, content='\n💭 思考过程：\n\n'))

        # 同一个 chunk：delta 里既有 reasoning_content 也有 content
        results.append(self._make(chunk, content=reasoning_text, reasoning=reasoning_text))
        return results

    def _end_reasoning_if_needed(self, chunk):
        """如果刚结束 reasoning 阶段，输出关闭标记。"""
        if self._reasoning_seen and not self._reasoning_ended:
            self._reasoning_ended = True
            return [self._make(chunk, content='\n\n')]
        return []

    def finalize(self):
        """流结束时调用，返回需要补充的关闭 chunk 列表。"""
        results = []
        if self._in_thinking:
            self._in_thinking = False
            results.append({
                'id': '',
                'object': 'chat.completion.chunk',
                'model': '',
                'choices': [{'index': 0, 'delta': {'content': '\n</think>\n\n'}, 'finish_reason': None}],
            })
        if self._reasoning_seen and not self._reasoning_ended:
            self._reasoning_ended = True
            results.append({
                'id': '',
                'object': 'chat.completion.chunk',
                'model': '',
                'choices': [{'index': 0, 'delta': {'content': '\n\n'}, 'finish_reason': None}],
            })
        return results

    def _process_content(self, chunk, content):
        """处理包含 content 的 chunk"""
        results = list(self._end_reasoning_if_needed(chunk))
        results.extend(self._split(chunk, content))
        return results

    def _handle_tool_calls_chunk(self, chunk):
        """处理包含 tool_calls 的 chunk，首次出现时在前面插入换行"""
        results = []
        if not self._tool_calls_seen:
            self._tool_calls_seen = True
            if self._in_thinking:
                self._in_thinking = False
                results.append(self._make(chunk, content='\n</think>\n\n'))
            else:
                results.append(self._make(chunk, content='\n'))
        elif self._in_thinking:
            self._in_thinking = False
            results.append(self._make(chunk, content='\n</think>\n\n'))
        results.append(chunk)
        return results

    def _split(self, chunk, text):
        """根据 <think> 标签拆分文本为多个 chunk"""
        results = []

        if self._in_thinking:
            end = text.find('</think>')
            if end >= 0:
                self._in_thinking = False
                if text[:end]:
                    results.append(self._make(chunk, reasoning=text[:end]))
                rest = text[end + 8:].lstrip('\n')
                if rest:
                    results.append(self._make(chunk, content=rest))
            else:
                results.append(self._make(chunk, reasoning=text))
        else:
            start = text.find('<think>')
            if start >= 0:
                before = text[:start]
                after = text[start + 7:]
                if before:
                    results.append(self._make(chunk, content=before))
                end = after.find('</think>')
                if end >= 0:
                    if after[:end]:
                        results.append(self._make(chunk, reasoning=after[:end]))
                    rest = after[end + 8:].lstrip('\n')
                    if rest:
                        results.append(self._make(chunk, content=rest))
                else:
                    self._in_thinking = True
                    if after:
                        results.append(self._make(chunk, reasoning=after))
            else:
                results.append(chunk)

        return results or [chunk]

    @staticmethod
    def _make(template, content=None, reasoning=None):
        """根据模板 chunk 构造新的 delta chunk"""
        delta = {}
        if content is not None:
            delta['content'] = content
        if reasoning is not None:
            delta['reasoning_content'] = reasoning
        return {
            'id': template.get('id', ''),
            'object': 'chat.completion.chunk',
            'model': template.get('model', ''),
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}],
        }
