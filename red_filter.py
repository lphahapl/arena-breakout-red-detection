"""Load the editable, per-run OCR exclusion list. No per-frame file I/O."""
import json
from pathlib import Path

FILTER_PATH = Path(__file__).resolve().with_name('red_filter.json')


def load_exclude_words(path=None) -> tuple[str, ...]:
    path = Path(path) if path is not None else FILTER_PATH
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict) or set(value) != {'exclude_words'}:
            raise ValueError('只允许 exclude_words 字段')
        words = value['exclude_words']
        if not isinstance(words, list) or any(
                not isinstance(w, str) or not w or any(c.isspace() for c in w)
                for w in words):
            raise ValueError('exclude_words 必须是字符串数组，词语不能为空或含空白；[] 可关闭词语屏蔽')
        return tuple(dict.fromkeys(words))
    except (OSError, ValueError) as error:
        raise ValueError(f'屏蔽词配置 {path} 无效：{error}') from error
