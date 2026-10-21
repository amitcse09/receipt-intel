from tests.conftest import HEADERS, RECEIPT_TEXT, good_extraction

CLAIM = {"store": "Big Bazaar", "total": "499.50", "purchased_on": "2026-10-01", "raw_text": RECEIPT_TEXT}


async def test_requires_api_key(client):
    r = await client.get("/receipts")
    assert r.status_code == 401


async def test_create_and_validate(client):
    r = await client.post("/receipts", json=CLAIM, headers=HEADERS)
    assert r.status_code == 201
    assert "raw_text" not in r.json()  # response_model filters it out
    # httpx's ASGITransport runs background tasks before returning, so status is already updated.
    r = await client.get(f"/receipts/{r.json()['id']}", headers=HEADERS)
    assert r.json()["status"] == "validated"
    assert r.json()["review_notes"] == []


async def test_inflated_total_is_rejected(client):
    r = await client.post("/receipts", json=CLAIM | {"total": "4995.00"}, headers=HEADERS)
    r = await client.get(f"/receipts/{r.json()['id']}", headers=HEADERS)
    assert r.json()["status"] == "rejected"
    assert "total" in r.json()["review_notes"][0]


async def test_llm_outage_goes_to_human_review(client, fake_llm):
    fake_llm.fail = True
    r = await client.post("/receipts", json=CLAIM, headers=HEADERS)
    r = await client.get(f"/receipts/{r.json()['id']}", headers=HEADERS)
    assert r.json()["status"] == "needs_review"


async def test_extract_playground(client, fake_llm):
    r = await client.post("/receipts/extract", json={"raw_text": RECEIPT_TEXT}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json() == good_extraction().model_dump()
    assert "<receipt>" in fake_llm.calls[0]  # untrusted input is delimited


async def test_validation_errors(client):
    r = await client.post("/receipts", json={"store": "A", "total": -5, "purchased_on": "2026-13-01"}, headers=HEADERS)
    assert r.status_code == 422
    assert len(r.json()["detail"]) == 4  # all errors reported at once (incl. missing raw_text)


async def test_members_cannot_read_each_others_receipts(client):
    """IDOR check: another member's receipt must look exactly like a missing one."""
    r = await client.post("/receipts", json=CLAIM, headers=HEADERS | {"X-Member-Id": "alice"})
    rid = r.json()["id"]
    assert (await client.get(f"/receipts/{rid}", headers=HEADERS | {"X-Member-Id": "alice"})).status_code == 200
    r = await client.get(f"/receipts/{rid}", headers=HEADERS | {"X-Member-Id": "bob"})
    assert r.status_code == 404 and r.json()["detail"] == "Receipt not found"
    assert (await client.get("/receipts", headers=HEADERS | {"X-Member-Id": "bob"})).json() == []
