"""Wattson inside Home Assistant: the same wattson.py loop, in a thread, with HA's own URL and a token of its
own system user. Nothing to install by hand: the dashboard and the Impara helpers are created at first start.
Advanced keys (watch, base_skip, bill, ...) still come from /config/wattson/config.json, which wins."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import timedelta

from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.start import async_at_started

from . import wattson as W

DOMAIN = "wattson"
SEMAFORO = "sensor.wattson_semaforo"
PLATFORMS = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


def _print(*a, **_):
    _LOGGER.info(" ".join(str(x) for x in a))


async def _token(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Access token of Wattson's system user (created once, removed with the integration)."""
    user = await hass.auth.async_get_user(entry.data.get("user_id", ""))
    if user is None:
        user = await hass.auth.async_create_system_user("Wattson", group_ids=[GROUP_ID_ADMIN])
        hass.config_entries.async_update_entry(entry, data={**entry.data, "user_id": user.id})
    rt = next(iter(user.refresh_tokens.values()), None) or await hass.auth.async_create_refresh_token(
        user, access_token_expiration=timedelta(days=3650))
    return hass.auth.async_create_access_token(rt)


def _file_cfg(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _main(cfg: dict) -> None:
    try:
        W.dashboard(cfg, False)     # first time: dashboard + helpers; then only the logo and new views
    except BaseException as e:      # dashboard() exits on errors (it was a CLI command)
        _LOGGER.warning("Wattson dashboard not updated: %s", e)
    while not W.STOP.is_set():      # a bug in one cycle must not freeze Wattson until the next HA restart
        try:
            W.run(False, cfg)
        except Exception:
            _LOGGER.exception("Wattson crashed, restarting in 60 s")
            W.STOP.wait(60)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    W.print = _print
    W.DATA = hass.config.path("wattson")
    o, api = entry.options, hass.config.api
    cfg = dict(W.DEFAULTS, power_entity=o["power_entity"], limit_kw=float(o["limit_kw"]),
               tolerance_kw=round(float(o["limit_kw"]) * 1.1, 2), price_eur_kwh=o["price_eur_kwh"],
               notify_entity=o.get("notify_entity"), catalog_share=o.get("catalog_share", False))
    cfg.update(await hass.async_add_executor_job(_file_cfg, os.path.join(W.DATA, "config.json")))
    # ponytail: loopback by IP, so a self-signed/real cert on HA's own port fails verification; use get_url() if it bites
    cfg["ha_url"] = "%s://127.0.0.1:%d" % ("https" if api.use_ssl else "http", api.port)
    cfg["token"] = await _token(hass, entry)
    if not er.async_get(hass).async_get(SEMAFORO):
        hass.states.async_remove(SEMAFORO)      # the old state written via REST would take the entity's id
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def start(_hass):
        W.STOP.clear()
        t = threading.Thread(target=_main, args=(cfg,), name="wattson", daemon=True)
        t.start()
        hass.data[DOMAIN] = t

    entry.async_on_unload(async_at_started(hass, start))     # the REST API answers only once HA has started
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    W.STOP.set()
    t = hass.data.pop(DOMAIN, None)
    if t:
        await hass.async_add_executor_job(t.join, 30)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    user = await hass.auth.async_get_user(entry.data.get("user_id", ""))
    if user:
        await hass.auth.async_remove_user(user)
