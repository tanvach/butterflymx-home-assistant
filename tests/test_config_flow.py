import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from butterflymx import (
    ButterflyMXApiError,
    ButterflyMXAuthError,
    ButterflyMXConnectionError,
    ButterflyMXInvalidVerificationCode,
    ButterflyMXVerificationRequired,
)
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.butterflymx.const import CONF_CODE, CONF_EMAIL, CONF_PASSWORD, CONF_RESEND, CONF_TOKENS, DOMAIN

from .conftest import EMAIL, PASSWORD, TOKENS

ROOT = Path(__file__).resolve().parents[1]
CODE = "918273"


async def start_flow(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


def challenge(*, can_resend=False, invalid=False, message=None):
    """A verification error whose submit/resend can be awaited without a real session."""
    cls = ButterflyMXInvalidVerificationCode if invalid else ButterflyMXVerificationRequired
    err = cls(can_resend=can_resend) if message is None else cls(message, can_resend=can_resend)
    err.submit_code = AsyncMock()
    err.resend = AsyncMock()
    return err


def schema_keys(result):
    return {key.schema for key in result["data_schema"].schema}


async def test_user_flow_creates_entry_with_tokens(hass, mock_client):
    result = await start_flow(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: f" {EMAIL} ", CONF_PASSWORD: PASSWORD}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD, CONF_TOKENS: TOKENS}
    assert result["result"].unique_id == EMAIL
    assert "session" in mock_client.init_kwargs  # uses HA's shared session


@pytest.mark.parametrize(
    ("error", "key"),
    [(ButterflyMXAuthError("bad"), "invalid_auth"), (ButterflyMXConnectionError("down"), "cannot_connect")],
)
async def test_user_flow_errors_then_recovers(hass, mock_client, error, key):
    mock_client.login.side_effect = error
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_account_aborts(hass, mock_client, config_entry):
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL.upper(), CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password_and_tokens(hass, setup_integration, mock_client):
    entry = setup_integration
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_client.login.side_effect = ButterflyMXAuthError("bad")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "still-wrong"})
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.login.side_effect = None
    mock_client.tokens = {"access_token": "new", "refresh_token": "new", "expires_at": 1}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new-password"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert entry.data[CONF_TOKENS]["access_token"] == "new"


async def _login_until_code(hass, mock_client, pending):
    mock_client.login.side_effect = pending
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "verify"
    assert mock_client.login.await_count == 1
    return result


async def test_user_flow_asks_for_texted_code_then_creates_entry(hass, mock_client):
    pending = challenge()
    result = await _login_until_code(hass, mock_client, pending)
    assert schema_keys(result) == {CONF_CODE}

    mock_client.tokens = {"access_token": "from-code", "refresh_token": "r2", "expires_at": 5}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: f" {CODE} "})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_EMAIL] == EMAIL
    assert result["data"][CONF_PASSWORD] == PASSWORD
    assert result["data"][CONF_TOKENS]["access_token"] == "from-code"
    pending.submit_code.assert_awaited_once_with(CODE)
    assert mock_client.login.await_count == 1


async def test_invalid_code_can_be_retried(hass, mock_client):
    pending = challenge(can_resend=True)
    rejected = challenge(invalid=True, can_resend=True)
    pending.submit_code.side_effect = rejected
    result = await _login_until_code(hass, mock_client, pending)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: "000000"})
    assert result["step_id"] == "verify"
    assert result["errors"] == {"base": "invalid_code"}
    assert CONF_RESEND in schema_keys(result)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: CODE, CONF_RESEND: False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    rejected.submit_code.assert_awaited_once_with(CODE)


async def test_expired_code_message_stays_on_the_form(hass, mock_client):
    pending = challenge()
    pending.submit_code.side_effect = challenge(invalid=True, message="Verification code expired")
    result = await _login_until_code(hass, mock_client, pending)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: CODE})
    assert result["step_id"] == "verify"
    assert result["errors"] == {"base": "verification_expired"}


async def test_lost_verification_session_returns_to_sign_in(hass, mock_client):
    pending = challenge()
    pending.submit_code.side_effect = ButterflyMXApiError("No verification login is in progress")
    result = await _login_until_code(hass, mock_client, pending)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: CODE})
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "verification_expired"}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_code_step_keeps_session_when_butterflymx_is_unreachable(hass, mock_client):
    pending = challenge()
    pending.submit_code.side_effect = ButterflyMXConnectionError("down")
    result = await _login_until_code(hass, mock_client, pending)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: CODE})
    assert result["step_id"] == "verify"
    assert result["errors"] == {"base": "cannot_connect"}

    pending.submit_code.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: CODE})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_resend_requests_another_text_without_submitting_the_code(hass, mock_client):
    pending = challenge(can_resend=True)
    result = await _login_until_code(hass, mock_client, pending)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: CODE, CONF_RESEND: True}
    )
    assert result["step_id"] == "verify"
    assert result["errors"] == {}
    pending.resend.assert_awaited_once()
    pending.submit_code.assert_not_awaited()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: CODE, CONF_RESEND: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    pending.submit_code.assert_awaited_once_with(CODE)


async def test_resend_that_finishes_login_creates_the_entry(hass, mock_client):
    pending = challenge(can_resend=True)
    mock_client._pending_login = None
    mock_client.access_token = "finished"
    mock_client.tokens = {"access_token": "finished", "refresh_token": "fr", "expires_at": 9}
    result = await _login_until_code(hass, mock_client, pending)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CODE: CODE, CONF_RESEND: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOKENS]["access_token"] == "finished"
    pending.submit_code.assert_not_awaited()


async def test_malformed_code_is_rejected_locally(hass, mock_client):
    pending = challenge()
    result = await _login_until_code(hass, mock_client, pending)
    with pytest.raises(InvalidData) as err:
        await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: "12345"})
    assert err.value.schema_errors == {CONF_CODE: "invalid_code"}
    pending.submit_code.assert_not_awaited()

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: CODE})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    pending.submit_code.assert_awaited_once_with(CODE)


async def test_reauth_verification_code_updates_the_entry(hass, setup_integration, mock_client):
    entry = setup_integration
    pending = challenge(can_resend=False)
    mock_client.login.side_effect = pending
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new-password"})
    assert result["step_id"] == "verify"
    assert schema_keys(result) == {CONF_CODE}

    mock_client.tokens = {"access_token": "reauthed", "refresh_token": "rr", "expires_at": 4}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: CODE})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert entry.data[CONF_TOKENS]["access_token"] == "reauthed"
    pending.submit_code.assert_awaited_once_with(CODE)
    assert mock_client.login.await_count == 1


async def test_reauth_lost_code_session_asks_for_the_password_again(hass, setup_integration, mock_client):
    entry = setup_integration
    pending = challenge()
    pending.submit_code.side_effect = ButterflyMXApiError("No verification login is in progress")
    mock_client.login.side_effect = pending
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new-password"})
    assert result["step_id"] == "verify"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CODE: CODE})
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "verification_expired"}

    mock_client.login.side_effect = None
    mock_client.tokens = {"access_token": "again", "refresh_token": "ar", "expires_at": 2}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "newer-password"})
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "newer-password"


def test_strings_and_manifest_describe_verification():
    strings = json.loads((ROOT / "custom_components/butterflymx/strings.json").read_text())
    english = json.loads((ROOT / "custom_components/butterflymx/translations/en.json").read_text())
    assert strings == english
    description = strings["config"]["step"]["verify"]["description"]
    assert description == "Enter the code ButterflyMX just texted you (expires in 2 minutes)."
    assert set(strings["config"]["error"]) >= {"invalid_auth", "cannot_connect", "invalid_code", "verification_expired"}

    manifest = json.loads((ROOT / "custom_components/butterflymx/manifest.json").read_text())
    assert manifest["domain"] == DOMAIN
    assert manifest["version"] == "2.2.2"
    assert manifest["config_flow"] is True
    requirement = manifest["requirements"][0]
    prefix = "butterflymx-client @ git+https://github.com/tanvach/butterflymx-client.git@"
    assert requirement.startswith(prefix)
    sha = requirement.removeprefix(prefix)
    assert len(sha) == 40 and sha.isalnum()
    test_req = (ROOT / "requirements_test.txt").read_text()
    assert requirement in test_req
