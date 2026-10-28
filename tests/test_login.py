def test_email_normalization():

    email = "Doctor@Test.com"

    assert email.strip().lower() == "doctor@test.com"