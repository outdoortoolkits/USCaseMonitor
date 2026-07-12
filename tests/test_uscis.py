from app.services.uscis import classify_status, clean_text


def test_clean_html_status_text():
    assert clean_text("Approved &amp; sent <a href='x'>address</a>") == "Approved & sent address"


def test_classification():
    assert classify_status("Request for Evidence Was Sent", "") == "RFE"
    assert classify_status("Case Was Approved", "") == "APPROVED"

