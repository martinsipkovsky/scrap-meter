"""The Tutorial: docs/wiki pages shown in the app, with their pictures."""
import re

from app import wiki


def login(client):
    assert client.post("/login", data={"username": "Admin", "password": "1234"},
                       follow_redirects=False).status_code == 303


def test_every_page_and_picture_is_there():
    pages = wiki.contents()
    assert len(pages) >= 15 and pages[0]["name"] == "getting-started"
    for p in pages:
        doc = wiki.render("wiki", p["name"])
        assert doc is not None, p["name"]
        for src in re.findall(r'src="/tutorial/images/([^"]+)"', str(doc["html"])):
            assert wiki.image(src) is not None, (p["name"], src)
        for name in re.findall(r'href="/tutorial/([a-z0-9-]+)[#"]', str(doc["html"])):
            assert name == "docs" or wiki.render("wiki", name) is not None, (p["name"], name)
        for name in re.findall(r'href="/tutorial/docs/([a-z0-9-]+)', str(doc["html"])):
            assert wiki.render("docs", name) is not None, (p["name"], name)


def test_tutorial_pages_in_the_app(client):
    assert client.get("/tutorial", follow_redirects=False).status_code in (302, 303, 307)  # sign in first
    login(client)
    home = client.get("/tutorial")
    assert home.status_code == 200 and "Getting started" in home.text and 'href="/tutorial"' in client.get("/").text
    page = client.get("/tutorial/dashboard")
    assert page.status_code == 200 and 'src="/tutorial/images/dashboard.png"' in page.text
    img = client.get("/tutorial/images/dashboard.png")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    guide = client.get("/tutorial/docs/user-guide")
    assert guide.status_code == 200 and "User guide" in guide.text and 'href="/tutorial"' in guide.text
    for bad in ("/tutorial/..%2Fsecret", "/tutorial/nope", "/tutorial/images/..%2F..%2Fapp.py", "/tutorial/docs/NOPE"):
        assert client.get(bad).status_code == 404, bad
