"""Google OAuth endpoints for Home Assistant's Application Credentials."""

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import GOOGLE_AUTHORIZE_URL, GOOGLE_TOKEN_URL


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return the Google authorization server."""
    return AuthorizationServer(authorize_url=GOOGLE_AUTHORIZE_URL, token_url=GOOGLE_TOKEN_URL)


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Placeholders for the credentials dialog."""
    return {
        "console_url": "https://console.cloud.google.com/apis/credentials",
        "redirect_url": "https://my.home-assistant.io/redirect/oauth",
    }
