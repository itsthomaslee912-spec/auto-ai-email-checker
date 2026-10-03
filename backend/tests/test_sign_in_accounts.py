from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import session as auth_session
from app.db import Base, get_db
from app.main import create_app
from app.models import EmailMessage, MailboxConnection, User


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(auth_session, "SessionLocal", factory)
    auth_session._session_cache.clear()

    with factory() as db:
        admin = User(external_id="a", email="admin@example.com", password_hash=auth_session.hash_password("secret1"), is_admin=True)
        other = User(external_id="b", email="other@example.com", password_hash=auth_session.hash_password("secret2"))
        db.add_all([admin, other])
        db.flush()
        boxes = {}
        for user, address in ((admin, "admin.box@gmail.com"), (other, "other.box@gmail.com")):
            box = MailboxConnection(user_id=user.id, provider="google", email_address=address, access_token_enc="x")
            db.add(box)
            db.flush()
            mail = EmailMessage(
                mailbox_id=box.id, provider_message_id=f"m-{box.id}", subject=f"hello {address}",
                received_at=datetime(2026, 10, 1, tzinfo=timezone.utc), is_read=True,
            )
            db.add(mail)
            db.flush()
            boxes[address] = (box.id, mail.id)
        db.commit()

    app = create_app()

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, boxes


def _signed_in(app, email, password):
    client = TestClient(app)
    response = client.post("/api/session/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return client


def test_protected_endpoints_require_sign_in(env):
    app, _ = env
    client = TestClient(app)
    assert client.get("/api/mailboxes").status_code == 401
    assert client.get("/api/emails").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_wrong_password_is_rejected(env):
    app, _ = env
    response = TestClient(app).post("/api/session/login", json={"email": "admin@example.com", "password": "nope"})
    assert response.status_code == 401


def test_each_sign_in_account_sees_only_its_own_mailboxes_and_mail(env):
    app, boxes = env
    other_box, other_mail = boxes["other.box@gmail.com"]
    admin_box, admin_mail = boxes["admin.box@gmail.com"]

    client = _signed_in(app, "other@example.com", "secret2")
    assert [m["email_address"] for m in client.get("/api/mailboxes").json()] == ["other.box@gmail.com"]
    page = client.get("/api/emails").json()
    assert [item["id"] for item in page["items"]] == [other_mail]
    assert client.get(f"/api/emails/{admin_mail}").status_code == 404
    assert client.delete(f"/api/mailboxes/{admin_box}").status_code == 404
    assert client.get("/api/users").status_code == 403

    admin = _signed_in(app, "admin@example.com", "secret1")
    assert [m["email_address"] for m in admin.get("/api/mailboxes").json()] == ["admin.box@gmail.com"]
    assert admin.get(f"/api/emails/{other_mail}").status_code == 404


def test_admin_manages_sign_in_accounts(env):
    app, _ = env
    admin = _signed_in(app, "admin@example.com", "secret1")

    created = admin.post("/api/users", json={"email": "New@Example.com", "password": "abcdef"})
    assert created.status_code == 201
    new_id = created.json()["id"]
    assert created.json()["email"] == "new@example.com"
    assert admin.post("/api/users", json={"email": "new@example.com", "password": "abcdef"}).status_code == 409

    newcomer = _signed_in(app, "new@example.com", "abcdef")
    assert newcomer.get("/api/mailboxes").json() == []

    assert admin.patch(f"/api/users/{new_id}", json={"password": "changed1"}).status_code == 200
    assert newcomer.get("/api/mailboxes").status_code == 401
    _signed_in(app, "new@example.com", "changed1")

    me = admin.get("/api/session/me").json()
    assert admin.delete(f"/api/users/{me['id']}").status_code == 409
    assert admin.delete(f"/api/users/{new_id}").status_code == 200
    assert TestClient(app).post("/api/session/login", json={"email": "new@example.com", "password": "changed1"}).status_code == 401


def test_logout_ends_the_session(env):
    app, _ = env
    client = _signed_in(app, "admin@example.com", "secret1")
    assert client.post("/api/session/logout").status_code == 200
    assert client.get("/api/mailboxes").status_code == 401
