import io
import json
import sqlite3
import subprocess
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

from pointersearch.geo import LocalFrame
from pointersearch.web.app import create_web_app
from pointersearch.web.auth import AuthError, Throttle, Users, hash_password, verify_password

ADMIN = ("boss@example.org", "initial-password-1")
FRAME = LocalFrame(-41.5630, 146.9990)
AREA = [list(FRAME.to_latlon(n, e)) for n, e in [(0, 0), (0, 400), (300, 400), (300, 0)]]


def make(tmp_path, **kw):
    return create_web_app(tmp_path, *ADMIN, **kw)


def sign_in(app, email, password, **kw):
    c = TestClient(app, **kw)
    r = c.post("/api/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


def admin_client(tmp_path):
    """An administrator who has already chosen their own password."""
    app = make(tmp_path)
    c = sign_in(app, *ADMIN)
    assert c.post("/api/account/password", json={"current": ADMIN[1], "new": "my-own-long-password"}).status_code == 200
    return app, c


def add_member(c, email="pilot@example.org", name="Pat Pilot", password="temporary-pass-9"):
    r = c.post("/api/users", json={"email": email, "name": name, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def member_client(app, admin, email="pilot@example.org", name="Pat Pilot"):
    add_member(admin, email, name)
    c = sign_in(app, email, "temporary-pass-9")
    assert c.post("/api/account/password", json={"current": "temporary-pass-9", "new": "pilots-own-password"}).status_code == 200
    return c


# ---- the hosted app must not need the image libraries ---------------------------
def test_hosted_app_imports_without_image_libraries():
    code = ("import sys, tempfile; from pointersearch.web.app import create_web_app; "
            "create_web_app(tempfile.mkdtemp(), 'a@b.co', 'long-enough-pass'); "
            "print([m for m in ('cv2', 'numpy', 'PIL') if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ---- passwords and throttling -------------------------------------------------
def test_password_hashing():
    h = hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct horse" not in h
    assert verify_password("correct horse battery", h) and not verify_password("wrong", h)
    assert hash_password("correct horse battery") != h            # salted
    assert not verify_password("x", "garbage") and not verify_password("x", "md5$1$2$3$4$5")


def test_throttle_locks_after_repeated_failures():
    t = Throttle(free_attempts=3, base_lock_s=30)
    for _ in range(2):
        t.fail("k", now=1000)
    assert t.wait_s("k", now=1000) == 0
    t.fail("k", now=1000)
    assert t.wait_s("k", now=1000) == 30
    t.fail("k", now=1030)
    assert t.wait_s("k", now=1030) == 60                           # grows
    t.ok("k")
    assert t.wait_s("k", now=1030) == 0


def test_first_admin_is_created_once(tmp_path):
    u = Users(tmp_path / "users.db")
    assert u.bootstrap_admin(None, None) is None and u.count() == 0
    a = u.bootstrap_admin(*ADMIN)
    assert a["role"] == "admin" and a["must_change"]
    # later restarts with different values must not create or reset anything
    assert u.bootstrap_admin("other@example.org", "another-password") is None
    assert u.bootstrap_admin(ADMIN[0], "attacker-set-password") is None
    assert u.count() == 1 and u.login(*ADMIN)[1]["email"] == ADMIN[0]
    raw = sqlite3.connect(tmp_path / "users.db").execute("SELECT pw_hash FROM users").fetchone()[0]
    assert ADMIN[1] not in raw
    with pytest.raises(AuthError, match="at least 10"):
        u.create("x@example.org", "X", "short")
    with pytest.raises(AuthError, match="valid email"):
        u.create("not-an-email", "X", "long-enough-pass")
    with pytest.raises(AuthError, match="already has a login"):
        u.create(ADMIN[0].upper(), "Dup", "long-enough-pass")


# ---- nothing is reachable without signing in --------------------------------------
def test_signed_out_visitors_see_only_the_sign_in_page(tmp_path):
    app = make(tmp_path)
    c = TestClient(app, follow_redirects=False)
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/login").status_code == 200 and c.get("/static/app.css").status_code == 200
    for page in ("/", "/account", "/users", "/m/Anything/plan"):
        r = c.get(page)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next="), page
    for api in ("/api/missions", "/api/me", "/api/app", "/api/users", "/m/Anything/api/routes"):
        r = c.get(api)
        assert r.status_code == 401 and r.json()["code"] == "signed_out", api
    assert c.post("/api/missions", json={"name": "X"}).status_code == 401
    assert c.post("/api/users", json={"email": "a@b.co", "name": "A", "password": "long-enough-pass"}).status_code == 401
    assert not any((tmp_path / "missions").iterdir())

    r = c.post("/api/login", json={"email": ADMIN[0], "password": "wrong"})
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert c.post("/api/login", json={"email": "nobody@example.org", "password": "wrong"}).json() == r.json()   # no hint which


def test_login_cookie_and_first_sign_in_flow(tmp_path):
    app = make(tmp_path)
    c = TestClient(app, follow_redirects=False)
    r = c.post("/api/login", json={"email": ADMIN[0].upper(), "password": ADMIN[1]})       # email is case-insensitive
    assert r.status_code == 200 and r.json()["user"]["must_change"] is True
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie    # plain http in this test
    secure = TestClient(app, base_url="https://planner.example").post("/api/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert "secure" in secure.headers["set-cookie"].lower()

    # until they choose their own password, only the account page works
    assert c.get("/").headers["location"] == "/account?first=1"
    r = c.get("/api/missions")
    assert r.status_code == 403 and r.json()["code"] == "password_change_required"
    assert c.post("/api/missions", json={"name": "X"}).status_code == 403
    assert c.get("/account").status_code == 200 and c.get("/api/me").json()["email"] == ADMIN[0]
    assert c.post("/api/account/name", json={"name": "  Robin   Boss "}).json()["name"] == "Robin Boss"
    assert c.post("/api/account/name", json={"name": " "}).status_code == 400

    bad = [({"current": "wrong", "new": "a-new-long-password"}, "current password"),
           ({"current": ADMIN[1], "new": "short"}, "at least 10"),
           ({"current": ADMIN[1], "new": ADMIN[1]}, "different")]
    for body, msg in bad:
        r = c.post("/api/account/password", json=body)
        assert r.status_code == 400 and msg in r.json()["detail"]
    assert c.post("/api/account/password", json={"current": ADMIN[1], "new": "a-new-long-password"}).status_code == 200
    assert c.get("/").status_code == 200 and c.get("/api/missions").status_code == 200       # same session carries on
    assert c.get("/login").status_code == 303                                                # already signed in

    # the old password no longer works; signing out ends the session
    assert TestClient(app).post("/api/login", json={"email": ADMIN[0], "password": ADMIN[1]}).status_code == 401
    token = c.cookies.get("ps_session")
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/me").status_code == 401
    c.cookies.set("ps_session", token)
    assert c.get("/api/me").status_code == 401                                               # a copied cookie is dead too


def test_repeated_wrong_passwords_are_slowed(tmp_path):
    app = make(tmp_path)
    c = TestClient(app)
    for _ in range(5):
        assert c.post("/api/login", json={"email": ADMIN[0], "password": "guess"}).status_code == 401
    r = c.post("/api/login", json={"email": ADMIN[0], "password": ADMIN[1]})                 # even the right one waits
    assert r.status_code == 429 and "too many attempts" in r.json()["detail"]


def test_cross_site_requests_are_refused(tmp_path):
    app, c = admin_client(tmp_path)
    evil = {"Origin": "https://evil.example"}
    assert c.post("/api/missions", json={"name": "X"}, headers=evil).status_code == 403
    assert c.post("/api/logout", headers=evil).status_code == 403
    assert c.post("/api/login", json={"email": ADMIN[0], "password": "x"}, headers=evil).status_code == 403
    assert c.post("/api/missions", json={"name": "X"}, headers={"Origin": "http://testserver"}).status_code == 200
    r = c.get("/")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"


# ---- people -----------------------------------------------------------------------
def test_administrators_manage_people_and_members_cannot(tmp_path):
    app, admin = admin_client(tmp_path)
    pat = add_member(admin)
    assert pat["role"] == "member" and pat["must_change"] and "pw_hash" not in pat
    assert admin.post("/api/users", json={"email": "pilot@example.org", "name": "Dup", "password": "temporary-pass-9"}).status_code == 400
    assert admin.post("/api/users", json={"email": "q@example.org", "name": "Q", "password": "short"}).status_code == 400

    member = sign_in(app, "pilot@example.org", "temporary-pass-9")
    member.post("/api/account/password", json={"current": "temporary-pass-9", "new": "pilots-own-password"})
    assert member.get("/api/users").status_code == 403
    assert member.post("/api/users", json={"email": "z@example.org", "name": "Z", "password": "long-enough-pass"}).status_code == 403
    assert member.patch(f"/api/users/{pat['id']}", json={"role": "admin"}).status_code == 403    # cannot promote themselves
    assert member.get("/users", follow_redirects=False).status_code == 303
    assert member.get("/api/missions").status_code == 200

    me = admin.get("/api/me").json()
    assert admin.patch(f"/api/users/{me['id']}", json={"active": False}).status_code == 400      # cannot lock yourself out
    assert admin.patch(f"/api/users/{me['id']}", json={"role": "member"}).status_code == 400
    assert admin.patch("/api/users/9999", json={"active": False}).status_code == 404

    # resetting a password signs the person out and forces a new choice
    assert admin.patch(f"/api/users/{pat['id']}", json={"password": "fresh-temporary-1"}).json()["must_change"] is True
    assert member.get("/api/me").status_code == 401
    member = sign_in(app, "pilot@example.org", "fresh-temporary-1")
    assert member.get("/api/missions").status_code == 403

    # disabling signs them out at once and blocks signing in; enabling restores it
    assert admin.patch(f"/api/users/{pat['id']}", json={"active": False}).json()["active"] is False
    assert member.get("/api/me").status_code == 401
    assert TestClient(app).post("/api/login", json={"email": "pilot@example.org", "password": "fresh-temporary-1"}).status_code == 401
    admin.patch(f"/api/users/{pat['id']}", json={"active": True})
    sign_in(app, "pilot@example.org", "fresh-temporary-1")

    # promote, then the original administrator can be demoted by the new one, but never the last
    admin.patch(f"/api/users/{pat['id']}", json={"role": "admin"})
    users = Users(tmp_path / "users.db")
    users.update(me["id"], role="member")
    with pytest.raises(AuthError, match="at least one active administrator"):
        users.update(pat["id"], active=False)


# ---- missions, planning and export --------------------------------------------------
def test_plan_save_and_export_as_a_member(tmp_path):
    app, admin = admin_client(tmp_path)
    c = member_client(app, admin)
    info = c.get("/api/app").json()
    assert info["hosted"] is True and info["user"]["name"] == "Pat Pilot"

    r = c.post("/api/missions", json={"name": "2026-11-10 Oaks Road"})
    assert r.status_code == 200
    url = r.json()["url"]
    assert c.post("/api/missions", json={"name": "../escape"}).status_code == 400
    assert c.post("/api/missions", json={"name": "2026-11-10 oaks road"}).status_code == 409

    assert "route planner" in c.get(url + "plan").text
    assert c.get(url, follow_redirects=False).headers["location"] == "plan"               # no review map here
    s = c.get(url + "api/summary").json()
    assert s["hosted"] is True and s["route"]["height_m"] == 90
    assert c.get(url + "api/overlays").json() == [] and c.get(url + "api/images").json() == []
    assert c.get(url + "api/groups").status_code == 404 and c.post(url + "api/ingest", json={"folder": "/"}).status_code in (404, 405)

    body = {"mode": "area", "points": AREA, "name": "North block", "lens": "thermal"}
    assert c.post(url + "api/route/plan", json=body).json()["n_photos"] > 10
    saved = c.post(url + "api/routes", json=body).json()
    assert saved["id"] == "north-block" and saved["created_by"] == saved["updated_by"] == "Pat Pilot"
    r = c.get(url + "api/routes/north-block/kmz")
    assert sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == ["wpmz/template.kml", "wpmz/waylines.wpml"]
    assert c.post(url + "api/route/kmz", json=body).status_code == 200

    # another person edits it: both names are kept
    edited = admin.post(url + "api/routes", json={**body, "height_m": 110, "overwrite": True}).json()
    assert edited["created_by"] == "Pat Pilot" and edited["updated_by"] == "Administrator"

    card = c.get("/api/missions").json()["missions"][0]
    assert card["name"] == "2026-11-10 Oaks Road" and card["routes"] == 1 and card["created_by"] == "Pat Pilot"
    assert card["route_list"][0]["id"] == "north-block"
    assert c.delete(url + "api/routes/north-block").status_code == 200                    # members may delete routes
    assert c.get("/m/..%2F..%2Fetc/plan").status_code == 404 and c.get("/m/Nope/plan").status_code == 404


def test_route_files_move_between_missions_and_to_the_laptop_app(tmp_path):
    from pointersearch.config import Config
    from pointersearch.review.server import create_app

    app, c = admin_client(tmp_path / "web")
    a = c.post("/api/missions", json={"name": "A"}).json()["url"]
    b = c.post("/api/missions", json={"name": "B"}).json()["url"]
    c.post(a + "api/routes", json={"mode": "area", "points": AREA, "name": "North block", "height_m": 100})

    r = c.get(a + "api/routes/north-block", params={"download": 1})
    assert 'filename="north-block.route.json"' in r.headers["content-disposition"]
    doc = json.loads(r.content)
    assert doc["format"] == "pointersearch-route" and doc["settings"]["height_m"] == 100

    # into another hosted mission
    assert c.post(b + "api/routes/import", json={"route": doc}).json()["id"] == "north-block"
    assert c.post(b + "api/routes/import", json={"route": doc}).status_code == 409
    assert c.post(b + "api/routes/import", json={"route": doc, "overwrite": True}).status_code == 200
    assert c.post(b + "api/routes/import", json={"route": {"format": "other", "points": []}}).status_code == 400
    assert c.post(b + "api/routes/import", json={"route": {**doc, "settings": {"height_m": 999}}}).status_code == 400
    assert c.post(b + "api/routes/import", json={"route": {**doc, "settings": {"height_m": "tall"}}}).status_code == 400

    # into the laptop app, where it shows on the review map
    laptop_dir = tmp_path / "laptop"
    laptop_dir.mkdir()
    Config().save(laptop_dir)
    laptop = TestClient(create_app(laptop_dir))
    assert laptop.post("/api/routes/import", json={"route": doc}).status_code == 200
    layer = laptop.get("/api/routes", params={"paths": 1}).json()[0]
    assert layer["name"] == "North block" and len(layer["path"]) >= 4 and layer["summary"]["height_m"] == 100
