from password_validator import validate_password


def register_user(username, password):
    """模拟用户注册流程"""
    is_valid, messages = validate_password(password)

    if not is_valid:
        print(f"[注册失败] 用户 {username}，原因：")
        for msg in messages:
            print(f"  - {msg}")
        return False

    print(f"[注册成功] 用户 {username}")
    return True


if __name__ == "__main__":
    print("=== 用户注册系统 ===\n")

    # 模拟几组典型注册场景
    registrations = [
        ("alice", "Str0ng!Pass"),       # 强密码，应成功
        ("bob", "weak"),                # 太短，应失败
        ("charlie", "NoSpecial123"),    # 缺特殊字符，应失败
        ("dave", "alllowercase1!"),     # 缺大写，应失败
        ("eve", "ALLUPPERCASE1!"),      # 缺小写，应失败
        ("frank", "MyPassword123!"),   # 包含 password（黑名单），应失败
        ("grace", "ADMIN2024!secure"), # 包含 admin（黑名单），应失败
    ]

    for username, pwd in registrations:
        print(f"尝试注册 -> 用户: {username}, 密码: {pwd}")
        register_user(username, pwd)
        print()
