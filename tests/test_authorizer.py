import jwt


def test_valid_jwt():

    secret = "test-secret"

    token = jwt.encode(
        {
            "sub": "user-1",
            "role": "DOCTOR"
        },
        secret,
        algorithm="HS256"
    )

    decoded = jwt.decode(
        token,
        secret,
        algorithms=["HS256"]
    )

    assert decoded["sub"] == "user-1"
    assert decoded["role"] == "DOCTOR"