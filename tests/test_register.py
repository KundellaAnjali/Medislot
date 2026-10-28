import re


def test_password_policy():

    password = "Password@123"

    pattern = (
        r"^(?=.*[A-Z])"
        r"(?=.*[a-z])"
        r"(?=.*\d)"
        r"(?=.*[@$!%*?&])"
    )

    assert re.search(pattern, password)
