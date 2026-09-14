from __future__ import annotations

from hh_mcp.runtime import Runtime


class FakeCredentialBackend:
    priority = 10

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


async def test_runtime_accepts_injected_credentials_and_encrypts_secrets(settings) -> None:
    backend = FakeCredentialBackend()
    runtime = Runtime(
        settings,
        credential_backend=backend,
        secure_directory=lambda path: path.mkdir(parents=True, exist_ok=True),
    )
    try:
        runtime.auth.configure_application("client-id", "visible-secret-marker")
        runtime.state.create_draft(
            account_id="account",
            auth_generation=1,
            vacancy_id="vacancy",
            resume_id="resume",
            message="visible-draft-marker",
            summary={},
        )
        ciphertext = (settings.secrets_dir / "application.bin").read_bytes()
        assert b"visible-secret-marker" not in ciphertext
        assert b"visible-draft-marker" not in settings.state_db.read_bytes()
        assert runtime.auth.application_credentials() == ("client-id", "visible-secret-marker")
        assert runtime.credential_backend_name == "injected"
    finally:
        await runtime.close()
