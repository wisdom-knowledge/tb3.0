import re


def validate_password(password):
    """
    校验密码强度，返回 (is_valid, messages) 元组。
    is_valid: 密码是否满足所有规则
    messages: 不满足的规则提示列表
    """
    messages = []

    # 规则1：至少8个字符
    if len(password) < 8:
        messages.append("密码长度至少8个字符")

    # 规则2：包含大写字母
    if not re.search(r'[A-Z]', password):
        messages.append("密码需包含至少一个大写字母")

    # 规则3：包含小写字母
    if not re.search(r'[a-z]', password):
        messages.append("密码需包含至少一个小写字母")

    # 规则4：包含数字
    if not re.search(r'\d', password):
        messages.append("密码需包含至少一个数字")

    # 规则5：包含特殊字符
    if not re.search(r'[!@#$%^&*()\-_=+\[\]{};:\'",.<>?/\\|`~]', password):
        messages.append("密码需包含至少一个特殊字符")

    is_valid = len(messages) == 0
    return is_valid, messages
