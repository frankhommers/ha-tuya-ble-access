"""Async Tuya mobile API client for BLE auth key retrieval.

Adapted from tuya_mobile_api.py — rewritten from requests to aiohttp.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid as uuid_mod
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Constants (same values as in original mobile API)
DEFAULT_CERT_SIGN = (
    "93:21:9F:C2:73:E2:20:0F:4A:DE:E5:F7:19:1D:C6:56:"
    "BA:2A:2D:7B:2F:F5:D2:4C:D5:5C:4B:61:55:00:1E:40"
)
DEFAULT_BMP_KEY = "f3hd7pet4p83kemjdf5wqsa5tavrv579"
DEFAULT_CLIENT_ID = "3cxxt3au9x33ytvq3h9j"
DEFAULT_APP_SECRET = "5gdtanjtf38vyxkqh87cjwfcqjhvjjqa"

MOBILE_REGIONS = {
    "us": "https://a1.tuyaus.com",
    "eu": "https://a1.tuyaeu.com",
    "cn": "https://a1.tuyacn.com",
    "in": "https://a1.tuyain.com",
    "nz": "https://a1.tuyaus.com",
}

SIGN_PARAMS = [
    "a", "v", "lat", "lon", "et", "lang", "deviceId",
    "imei", "imsi", "appVersion", "ttid", "isH5",
    "h5Token", "os", "clientId", "postData", "time",
    "n4h5", "sid", "sp", "requestId",
]


def _post_data_hash(post_data: str) -> str:
    """MD5 hash with Tuya's byte-rearrangement quirk."""
    h = hashlib.md5(post_data.encode()).hexdigest()
    return h[8:16] + h[0:8] + h[24:32] + h[16:24]


def _sign(params: dict[str, str], hmac_key: str) -> str:
    sorted_keys = sorted(params.keys())
    parts = []
    for key in sorted_keys:
        if key not in SIGN_PARAMS:
            continue
        val = str(params[key])
        if not val:
            continue
        if key == "postData":
            val = _post_data_hash(val)
        parts.append(f"{key}={val}")
    feed = "||".join(parts)
    return hmac.new(hmac_key.encode(), feed.encode(), hashlib.sha256).hexdigest()


class TuyaMobileAPIAsync:
    """Async Tuya mobile API client (a1.tuyaus.com/api.json)."""

    def __init__(self, session, region: str = "us"):
        self._session = session
        self._region = region
        self.base_url = MOBILE_REGIONS.get(region, MOBILE_REGIONS["us"])
        self.device_id = os.urandom(32).hex()

        self.sid = ""
        self.ecode = ""
        self.uid = ""
        self._hmac_key = f"{DEFAULT_CERT_SIGN}_{DEFAULT_BMP_KEY}_{DEFAULT_APP_SECRET}"

    async def _call(
        self, action: str, version: str = "1.0",
        post_data: dict | None = None, country_code: str = "",
    ) -> dict[str, Any]:
        """Make a signed API call to /api.json."""
        request_id = str(uuid_mod.uuid4())
        t = str(int(time.time()))

        params: dict[str, str] = {
            "a": action,
            "v": version,
            "clientId": DEFAULT_CLIENT_ID,
            "deviceId": self.device_id,
            "os": "Android",
            "lang": "en",
            "ttid": "tuyaSmart",
            "appVersion": "7.2.8",
            "sdkVersion": "3.29.5",
            "time": t,
            "requestId": request_id,
        }
        if self.sid:
            params["sid"] = self.sid
        if country_code:
            params["countryCode"] = country_code
        if post_data is not None:
            params["postData"] = json.dumps(post_data, separators=(",", ":"))
        params["sign"] = _sign(params, self._hmac_key)

        url = self.base_url + "/api.json"
        headers = {"User-Agent": "TuyaSmart/7.2.8 (Android)"}
        _LOGGER.debug("Tuya API call: action=%s postData=%s", action, params.get("postData"))
        async with self._session.get(url, params=params, headers=headers) as resp:
            resp.raise_for_status()
            result = await resp.json()
            _LOGGER.debug("Tuya API response: %s", result)
            return result

    async def async_login(self, country_code: str, email: str, password: str) -> dict:
        passwd_md5 = hashlib.md5(password.encode()).hexdigest()
        payload = {
            "countryCode": country_code,
            "email": email,
            "passwd": passwd_md5,
            "options": '{"group": 1}',
            "token": "",
            "ifencrypt": 0,
        }
        result = await self._call(
            "thing.m.user.email.password.login",
            version="3.0",
            post_data=payload,
            country_code=country_code,
        )
        if result.get("success"):
            user_data = result.get("result", {})
            self.sid = user_data.get("sid", "")
            self.ecode = user_data.get("ecode", "")
            self.uid = user_data.get("uid", "")
        return result

    async def async_get_ble_auth_key(self, device_uuid: str, device_mac: str = "") -> dict:
        payload = {"uuid": device_uuid}
        if device_mac:
            payload["mac"] = device_mac
        return await self._call(
            "m.thing.device.auth.key.get",
            version="3.0",
            post_data=payload,
        )

    async def async_get_device_keys(self, dev_id: str) -> dict:
        return await self._call(
            "m.thing.device.keys.get.create",
            version="1.0",
            post_data={"devId": dev_id},
        )

    async def async_get_home_list(self) -> dict:
        return await self._call(
            "tuya.m.location.list",
            version="2.1",
            post_data={},
        )

    async def async_list_devices(self, gid: int | str) -> dict:
        request_id = str(uuid_mod.uuid4())
        t = str(int(time.time()))
        params: dict[str, str] = {
            "a": "tuya.m.my.group.device.list",
            "v": "2.0",
            "clientId": DEFAULT_CLIENT_ID,
            "deviceId": self.device_id,
            "os": "Android",
            "lang": "en",
            "ttid": "tuyaSmart",
            "appVersion": "7.2.8",
            "sdkVersion": "3.29.5",
            "time": t,
            "requestId": request_id,
            "gid": str(gid),
            "postData": json.dumps({}, separators=(",", ":")),
        }
        if self.sid:
            params["sid"] = self.sid
        params["sign"] = _sign(params, self._hmac_key)
        url = self.base_url + "/api.json"
        headers = {"User-Agent": "TuyaSmart/7.2.8 (Android)"}
        async with self._session.get(url, params=params, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def async_find_device_by_mac(self, device_mac: str) -> dict | None:
        """Look up device info by MAC address via cloud API.

        Iterates homes and their devices, matching by MAC (case-insensitive,
        colon-stripped). Returns dict with uuid, devId, localKey, secKey,
        check_code, name, productId or None.
        """
        import base64
        mac_clean = device_mac.replace(":", "").upper()
        homes_resp = await self.async_get_home_list()
        homes_result = homes_resp.get("result", {})
        if isinstance(homes_result, dict):
            homes_result = homes_result.get("result", [])
        for home in homes_result:
            gid = home.get("groupId") or home.get("gid")
            if not gid:
                continue
            devs_resp = await self.async_list_devices(gid)
            devs_result = devs_resp.get("result", {})
            if isinstance(devs_result, dict):
                devs_result = devs_result.get("result", [])
            for dev in devs_result:
                dev_mac = (dev.get("mac") or "").replace(":", "").upper()
                if dev_mac != mac_clean:
                    continue
                # Parse DP71 (ble_unlock_verify) for the 8-digit check code
                check_code = ""
                dpi = dev.get("dataPointInfo") or {}
                if isinstance(dpi, str):
                    try:
                        dpi = json.loads(dpi)
                    except Exception:
                        dpi = {}
                dp71 = (dpi.get("dps") or {}).get("71", "")
                if isinstance(dp71, str) and dp71:
                    try:
                        raw = base64.b64decode(dp71)
                        if len(raw) >= 12:
                            check_code = raw[4:12].decode("ascii", errors="ignore")
                    except Exception:
                        pass
                return {
                    "uuid": dev.get("uuid", ""),
                    "devId": dev.get("devId", ""),
                    "localKey": dev.get("localKey", ""),
                    "secKey": dev.get("secKey", ""),
                    "checkCode": check_code,
                    "name": dev.get("name", ""),
                    "productId": dev.get("productId", ""),
                    "category": dev.get("category", ""),
                    # Full current DP snapshot (as reported to the cloud). Values
                    # are either scalars (int/bool/str) or base64-encoded raw bytes
                    # for RAW-type DPs.
                    "dps": dpi.get("dps") or {},
                }
        return None


def _extract_verify_key(result: dict) -> str:
    return result.get("verifyKey") or result.get("verify_key") or result.get("sign") or ""


def _extract_auth_random(result: dict) -> str:
    return result.get("random") or result.get("randomKey") or result.get("auth_random") or ""


def _entry_creds(entry, *, new_password: str | None = None) -> tuple[str, str, str, str]:
    from .const import (
        CONF_TUYA_EMAIL, CONF_TUYA_PASSWORD,
        CONF_TUYA_COUNTRY, CONF_TUYA_REGION,
    )
    email = entry.data.get(CONF_TUYA_EMAIL, "")
    password = new_password or entry.data.get(CONF_TUYA_PASSWORD, "")
    country = entry.data.get(CONF_TUYA_COUNTRY, "")
    region = entry.data.get(CONF_TUYA_REGION, "")
    if not (email and password):
        raise RuntimeError("Hub has no usable cloud credentials")
    return email, password, country, region


async def _refresh_one(
    hass: HomeAssistant, device_store, mac: str, dev: dict,
    email: str, password: str, country: str, region: str,
) -> bool:
    """Fetch cloud data for one device and write it back to the store.
    Returns True on success, False on any failure (non-fatal)."""
    try:
        res = await async_fetch_auth_key(
            hass, dev.get("uuid", "") or "",
            email, password, country, region, device_mac=mac,
        )
    except Exception as exc:
        _LOGGER.warning("Cloud refresh for %s failed: %s", mac, exc)
        return False
    updates = {
        "local_key": res.get("local_key", "") or dev.get("local_key", ""),
        "sec_key": res.get("sec_key", "") or dev.get("sec_key", ""),
        "verify_key": res.get("verify_key", "") or dev.get("verify_key", ""),
        "auth_random": res.get("auth_random", "") or dev.get("auth_random", ""),
        "check_code": res.get("check_code", "") or dev.get("check_code", ""),
        "auth_key": res.get("auth_key", "") or dev.get("auth_key", ""),
    }
    if updates["local_key"]:
        updates["login_key"] = updates["local_key"][:6].encode().hex()
    dev_id = res.get("device_id") or ""
    if dev_id:
        updates["virtual_id"] = (
            (dev_id.encode() + b"\x00" * 22)[:22]
        ).hex()
    if res.get("dps"):
        updates["cloud_dps"] = res["dps"]
    await device_store.async_update_device(mac, **updates)
    _LOGGER.info(
        "Refreshed %s: local_key=%s sec_key=%s check_code=%s dps=%d",
        mac,
        "yes" if updates["local_key"] else "no",
        "yes" if updates["sec_key"] else "no",
        updates.get("check_code") or "(empty)",
        len(res.get("dps") or {}),
    )
    return True


async def async_refresh_all_devices(
    hass: HomeAssistant, entry, *, new_password: str | None = None,
) -> int:
    """Refresh cloud creds + DP snapshot for every device in the hub store.

    Used by the Reconfigure flow. If `new_password` is given the hub
    entry's stored password is rewritten after a successful fetch.
    Caller reloads the entry. Returns number of devices updated.
    """
    from .device_store import DeviceStore
    from .const import (
        CONF_TUYA_EMAIL, CONF_TUYA_PASSWORD,
        CONF_TUYA_COUNTRY, CONF_TUYA_REGION,
    )

    email, password, country, region = _entry_creds(entry, new_password=new_password)
    device_store = DeviceStore(hass)
    await device_store.async_load()
    refreshed = 0
    for mac, dev in list(device_store.devices.items()):
        if await _refresh_one(
            hass, device_store, mac, dev, email, password, country, region,
        ):
            refreshed += 1

    if new_password and refreshed:
        hass.config_entries.async_update_entry(
            entry,
            data={
                CONF_TUYA_EMAIL: email,
                CONF_TUYA_PASSWORD: new_password,
                CONF_TUYA_COUNTRY: country,
                CONF_TUYA_REGION: region,
            },
        )
    return refreshed


async def async_refresh_one_device(
    hass: HomeAssistant, entry, mac: str,
) -> bool:
    """Refresh cloud creds + DP snapshot for a single device. Used by the
    per-lock 'Refresh via cloud' button. Caller reloads the entry.
    """
    from .device_store import DeviceStore

    email, password, country, region = _entry_creds(entry)
    device_store = DeviceStore(hass)
    await device_store.async_load()
    dev = device_store.get_device(mac)
    if not dev:
        raise RuntimeError(f"Device {mac} not in store")
    return await _refresh_one(
        hass, device_store, mac.upper(), dev, email, password, country, region,
    )


async def async_fetch_auth_key_only(
    hass: HomeAssistant, device_uuid: str, email: str, password: str,
    country_code: str, region: str,
) -> str:
    """Lightweight helper: login + get auth key only (no device lookup).

    Returns auth_key hex string. Used by standalone pairing when UUID is already known.
    """
    session = async_get_clientsession(hass)
    client = TuyaMobileAPIAsync(session, region=region)
    login_resp = await client.async_login(country_code, email, password)
    if not login_resp.get("success"):
        error = login_resp.get("errorMsg", login_resp.get("msg", "Login failed"))
        raise Exception(f"Tuya login failed: {error}")

    resp = await client.async_get_ble_auth_key(device_uuid)
    if not resp.get("success"):
        error = resp.get("errorMsg", resp.get("msg", "Auth key fetch failed"))
        raise Exception(f"Auth key fetch failed: {error}")
    result = resp.get("result", {})
    auth_key = (
        result.get("authKey")
        or result.get("auth_key")
        or result.get("encryptedAuthKey")
        or ""
    )
    if not auth_key:
        raise Exception("Auth key not found in API response")
    return auth_key


async def async_fetch_auth_key(
    hass: HomeAssistant, device_uuid: str, email: str, password: str,
    country_code: str, region: str, device_mac: str = "",
) -> dict:
    """One-shot helper: login + get auth key + device info.

    Returns dict with keys: auth_key, uuid, local_key, device_id, name.
    If device_uuid is empty, looks up device by MAC via cloud API.
    """
    session = async_get_clientsession(hass)
    client = TuyaMobileAPIAsync(session, region=region)
    login_resp = await client.async_login(country_code, email, password)
    if not login_resp.get("success"):
        error = login_resp.get("errorMsg", login_resp.get("msg", "Login failed"))
        raise Exception(f"Tuya login failed: {error}")

    cloud_info: dict = {}
    resolved_uuid = device_uuid

    # Look up device info by MAC (needed for UUID, localKey, devId)
    if device_mac:
        cloud_info = await client.async_find_device_by_mac(device_mac) or {}
        if cloud_info:
            _LOGGER.info(
                "Cloud device info: uuid=%s devId=%s name=%s",
                cloud_info.get("uuid"), cloud_info.get("devId"), cloud_info.get("name"),
            )
            if not resolved_uuid:
                resolved_uuid = cloud_info.get("uuid", "")

    if not resolved_uuid:
        raise Exception("Auth key fetch failed: no device UUID (BLE or cloud)")

    key_info: dict = {}
    dev_id = cloud_info.get("devId", "")
    if dev_id:
        key_resp = await client.async_get_device_keys(dev_id)
        if key_resp.get("success"):
            key_info = key_resp.get("result", {}) or {}
        else:
            _LOGGER.warning("Device keys fetch failed. Response: %s", key_resp)

    resp = await client.async_get_ble_auth_key(resolved_uuid, device_mac=device_mac)
    if not resp.get("success"):
        error = resp.get("errorMsg", resp.get("msg", "Auth key fetch failed"))
        raise Exception(f"Auth key fetch failed: {error}")
    result = resp.get("result", {})
    auth_key = (
        result.get("authKey")
        or result.get("auth_key")
        or result.get("encryptedAuthKey")
        or ""
    )
    if not auth_key:
        _LOGGER.warning("Auth key not found in API response. Result: %s", result)

    return {
        "auth_key": auth_key,
        "auth_random": _extract_auth_random(result),
        "uuid": resolved_uuid,
        "local_key": key_info.get("localKey") or cloud_info.get("localKey", ""),
        "sec_key": key_info.get("secKey") or cloud_info.get("secKey", ""),
        "verify_key": _extract_verify_key(key_info) or _extract_verify_key(cloud_info),
        "check_code": cloud_info.get("checkCode", ""),
        "device_id": dev_id,
        "name": cloud_info.get("name", ""),
        "product_id": cloud_info.get("productId", ""),
        "category": cloud_info.get("category", ""),
        "dps": cloud_info.get("dps") or {},
    }
