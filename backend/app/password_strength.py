"""共享密码/密钥强度校验（R-1/R-4）。

只依赖标准库，不导入 app 内任何模块，避免循环依赖
（config.py、security.py、routers、services 都可直接引用）。

统一标准：用户密码与实例自定义密码均为 12-64 位，且同时包含
小写字母、大写字母、数字和符号。
"""

import re
import string
from collections import Counter

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 64

MIN_SECRET_KEY_LENGTH = 32
# SECRET_KEY 须包含大小写字母、数字、符号中的至少 3 类。
MIN_SECRET_KEY_CLASSES = 3
# SECRET_KEY 至少 8 个不同字符。
MIN_SECRET_KEY_DISTINCT_CHARS = 8
# 任一字符占比超过一半即视为低熵。
MAX_SECRET_KEY_CHAR_SHARE = 0.5


def _char_classes(value: str) -> set[str]:
    classes = set()
    if re.search(r"[a-z]", value):
        classes.add("lower")
    if re.search(r"[A-Z]", value):
        classes.add("upper")
    if re.search(r"[0-9]", value):
        classes.add("digit")
    if any(ch in string.punctuation for ch in value):
        classes.add("symbol")
    return classes


def validate_strong_password(password: str | None, *, field_name: str = "密码") -> None:
    """12-64 位，且同时含大小写字母、数字、符号；不满足抛 ValueError。"""
    if not password or len(password) < MIN_PASSWORD_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"{field_name}长度需为 12-64 位")
    if password != password.strip() or re.search(r"[\x00-\x1f\x7f]", password):
        raise ValueError(f"{field_name}不能包含首尾空格或控制字符")
    if not {"lower", "upper", "digit", "symbol"} <= _char_classes(password):
        raise ValueError(f"{field_name}须同时包含小写字母、大写字母、数字和符号")


def validate_secret_key(secret: str | None) -> None:
    """SECRET_KEY：32 位以上且有足够熵；不满足抛 ValueError。

    熵检查（任一不满足即拒绝）：
    - 包含大小写字母、数字、符号中的至少 3 类；
    - 至少 8 个不同字符；
    - 任一字符占比不超过一半。
    """
    if not secret:
        raise ValueError("SECRET_KEY 未配置")
    if len(secret) < MIN_SECRET_KEY_LENGTH:
        raise ValueError("SECRET_KEY 太短：至少 32 位")
    if len(_char_classes(secret)) < MIN_SECRET_KEY_CLASSES:
        raise ValueError("SECRET_KEY 熵不足：须包含大小写字母、数字、符号中的至少 3 类")
    if len(set(secret)) < MIN_SECRET_KEY_DISTINCT_CHARS:
        raise ValueError("SECRET_KEY 熵不足：不同字符过少（至少 8 个不同字符）")
    most_common_count = Counter(secret).most_common(1)[0][1]
    if most_common_count / len(secret) > MAX_SECRET_KEY_CHAR_SHARE:
        raise ValueError("SECRET_KEY 熵不足：单一字符占比过高")
