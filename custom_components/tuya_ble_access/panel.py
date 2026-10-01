"""Register the local, administrator-only access manager."""

from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig

from .const import DOMAIN


async def async_setup_panel(hass):
    marker = f"{DOMAIN}_panel"
    if hass.data.get(marker):
        return
    await hass.http.async_register_static_paths([
        StaticPathConfig(f"/{DOMAIN}/access-panel.js", str(Path(__file__).parent / "access-panel.js"), False)
    ])
    await panel_custom.async_register_panel(
        hass, frontend_url_path="tuya-ble-access", webcomponent_name="tuya-access-panel",
        sidebar_title="Tuya BLE Access", sidebar_icon="mdi:lock-smart",
        module_url=f"/{DOMAIN}/access-panel.js?v=0.4.1", embed_iframe=False, require_admin=True,
    )
    hass.data[marker] = True
