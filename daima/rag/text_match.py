"""匹配原文引句时容忍排版变化，保留字词、数字、小数点和运算符。"""
import re


_TYPOGRAPHY = str.maketrans({'，': ',', '。': '.', '；': ';', '：': ':', '！': '!', '？': '?',
                            '（': '(', '）': ')', '“': '', '”': '', '‘': '', '’': '',
                            '"': '', "'": ''})


def normalize_quote(text):
    return re.sub(r'\s+', '', text.translate(_TYPOGRAPHY))


def quote_in_source(quote, source):
    normalized = normalize_quote(quote)
    return bool(normalized) and normalized in normalize_quote(source)
