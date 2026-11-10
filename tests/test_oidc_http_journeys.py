"""Full-assembly OIDC browser boundaries; execute only in hosted CI.

Only the provider protocol client is replaced, at its verified-subject boundary.
The ordinary application owns tenant resolution, CSRF, state and transactions.
"""

from tests.issue2_wave3_support import current_account, grant_std
from tests.test_http_journeys import _run
from tests.test_oidc_login import SUBJECT

assert current_account  # Imported pytest fixture.


_PROTOCOL = r"""
import time
from http.cookies import SimpleCookie
from uuid import uuid4
from dotmac_auth_oidc import AuthorizationRedirect, LoginState, VerifiedSubject
from dotmac_auth_oidc.errors import StateError
from dotmac_kernel.models import AuthSession
from app.models.oidc_login import AcademyOidcLoginState
from app.services import oidc_login
from app.services.security import hash_token

class ProtocolClient:
    def start_login(self, *, return_to="/", ttl_seconds=600, state_store=None):
        state_id = uuid4().hex
        state_store.put(LoginState(
            state_id=state_id, nonce="provider-nonce", code_verifier="provider-verifier",
            redirect_uri=origin + "/login/oidc/callback", issued_at=int(time.time()), return_to=return_to,
        ), ttl_seconds=ttl_seconds)
        return AuthorizationRedirect(url="https://provider.example.test/auth?state=" + state_id, state=state_id)

    def complete_login(self, *, code, state_parameter, stored_state, ttl_seconds=600, state_store=None):
        if not stored_state or state_parameter != stored_state:
            raise StateError("state mismatch")
        claimed = state_store.take(state_parameter)
        if claimed is None:
            raise StateError("ceremony unavailable")
        return VerifiedSubject(issuer=oidc_login.settings.oidc_issuer,
            subject=os.environ["JOURNEY_SUBJECT"], return_to=claimed.return_to)

def response_cookie(response, name):
    cookies = SimpleCookie()
    for value in response.headers.get_list("set-cookie"):
        cookies.load(value)
    return cookies[name]

def refused(response):
    assert response.status_code == 303
    assert response.headers["location"] == "/login?oidc_error=1"
    assert response_cookie(response, oidc_login.STATE_COOKIE)["max-age"] == "0"
    assert not client.cookies.get("session")
    assert rows(AuthSession, external_identity_binding_id=UUID(os.environ["JOURNEY_BINDING"])) == []

oidc_login.build_oidc_client = lambda: ProtocolClient()
"""


_ENABLED = (
    _PROTOCOL
    + r"""
page = csrf_get("/login")
assert 'hx-post="/login/oidc"' in page.text
assert client.post("/login/oidc", headers={"Origin": origin}).status_code == 403
assert client.post("/login/oidc", headers={
    "Origin": "https://foreign.example.test", "x-csrf-token": client.cookies.get("csrf_token"),
}).status_code == 403
assert rows(AcademyOidcLoginState) == []

def start():
    response = post("/login/oidc")
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://provider.example.test/auth?")
    cookie = response_cookie(response, oidc_login.STATE_COOKIE)
    assert cookie["secure"] and cookie["httponly"]
    assert cookie["samesite"].lower() == "lax" and cookie["path"] == "/login/oidc"
    assert not cookie["domain"] and cookie["expires"]
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    assert cookie.value == state
    assert "provider-verifier" not in response.headers["set-cookie"]
    assert "provider-nonce" not in response.headers["set-cookie"]
    return state

# Missing query state, missing browser proof, mismatched state and provider errors
# all refuse through the genuine callback route and create no federated session.
state = start()
refused(client.get("/login/oidc/callback", params={"code": "provider-code"}))
state = start()
client.cookies.delete(oidc_login.STATE_COOKIE, domain=urlsplit(origin).hostname, path="/login/oidc")
refused(client.get("/login/oidc/callback", params={"code": "provider-code", "state": state}))
state = start()
refused(client.get("/login/oidc/callback", params={"code": "provider-code", "state": "wrong-state"}))
state = start()
refused(client.get("/login/oidc/callback", params={"error": "access_denied", "state": state}))

state = start()
callback = {"code": "provider-code", "state": state}
completed = client.get("/login/oidc/callback", params=callback)
assert completed.status_code == 303 and completed.headers["location"] == "/"
cookie = response_cookie(completed, "session")
assert cookie["secure"] and cookie["httponly"] and cookie["samesite"].lower() == "lax"
assert not client.cookies.get(oidc_login.STATE_COOKIE)
sessions = rows(AuthSession, external_identity_binding_id=UUID(os.environ["JOURNEY_BINDING"]))
assert len(sessions) == 1
assert str(sessions[0].party_id) == os.environ["JOURNEY_PERSON"]
assert sessions[0].token_hash == hash_token(cookie.value) and sessions[0].revoked_at is None
assert client.get("/account").status_code == 200

# Replay with the original browser proof must still fail after the first commit.
client.cookies.delete("session", domain=urlsplit(origin).hostname, path="/")
replay = client.get("/login/oidc/callback", params=callback,
    headers={"Cookie": oidc_login.STATE_COOKIE + "=" + state})
assert replay.status_code == 303 and replay.headers["location"] == "/login?oidc_error=1"
assert not client.cookies.get("session")
assert len(rows(AuthSession, external_identity_binding_id=UUID(os.environ["JOURNEY_BINDING"]))) == 1
"""
)


def test_enabled_oidc_browser_csrf_cookies_callback_and_replay(current_account):
    from dotmac_kernel.models import Party

    from app.services import oidc_login

    a = current_account
    grant_std(a.admin, tenant_id=a.tid, party_id=a.pid, role="admin")
    a.admin.commit()
    party = a.db.get(Party, a.pid)
    binding = oidc_login.bind_staff_identity(
        a.db, tenant=a.tenant, party=party, subject=SUBJECT, bound_by="test-operator", reason="HTTP boundary proof"
    )
    binding_id = binding.id
    a.db.commit()
    _run(
        a,
        _ENABLED,
        OIDC_ENABLED="true",
        OIDC_CLIENT_SECRET="fixture-protocol-only",
        JOURNEY_ORIGIN=f"https://{a.tenant.slug}.localhost",
        JOURNEY_SUBJECT=SUBJECT,
        JOURNEY_BINDING=binding_id,
        JOURNEY_PERSON=a.pid,
    )


_DISABLED = r"""
from dotmac_kernel.models import AuthSession
from app.models.oidc_login import AcademyOidcLoginState

page = csrf_get("/login")
assert 'hx-post="/login/oidc"' not in page.text
assert post("/login/oidc").status_code == 503
response = client.get("/login/oidc/callback", params={"code": "unused", "state": "unused"})
assert response.status_code == 303 and response.headers["location"] == "/login?oidc_error=1"
assert not client.cookies.get("session") and not client.cookies.get("academy_oidc_state")
assert rows(AcademyOidcLoginState) == []
assert all(row.external_identity_binding_id is None for row in rows(AuthSession))
login(os.environ["JOURNEY_EMAIL"], "chosen-password9")
assert client.get("/account").status_code == 200
"""


def test_disabled_oidc_has_no_front_door_and_password_still_works(current_account):
    grant_std(current_account.admin, tenant_id=current_account.tid, party_id=current_account.pid)
    current_account.admin.commit()
    _run(current_account, _DISABLED, OIDC_ENABLED="false", OIDC_CLIENT_SECRET="")
