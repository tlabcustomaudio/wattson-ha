"""Three questions: the main meter (the biggest power sensor is proposed), the contract limit, notifications.
Sharing with the public catalog is optional: turning it on links GitHub with a code (device flow), no tokens to copy."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from . import DOMAIN
from . import wattson as W

LIMITS = ["3", "4.5", "6"]      # kW, the usual contracts (Italy); any other value can be typed


def biggest_power(hass: HomeAssistant) -> str | None:
    best, top = None, -1.0
    for s in hass.states.async_all("sensor"):
        if s.attributes.get("device_class") != "power":
            continue
        try:
            w = float(s.state) * (1000 if s.attributes.get("unit_of_measurement") == "kW" else 1)
        except ValueError:
            continue
        if w > top:
            best, top = s.entity_id, w
    return best


def schema(hass: HomeAssistant, o: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required("power_entity", default=o.get("power_entity") or biggest_power(hass) or vol.UNDEFINED):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="power")),
        vol.Required("limit_kw", default=o.get("limit_kw", "3")): selector.SelectSelector(
            selector.SelectSelectorConfig(options=LIMITS, custom_value=True)),
        vol.Required("price_eur_kwh", default=o.get("price_eur_kwh", 0.25)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=2, step=0.001, mode=selector.NumberSelectorMode.BOX)),
        vol.Optional("notify_entity", description={"suggested_value": o.get("notify_entity")}):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="notify")),
        vol.Required("catalog_share", default=o.get("catalog_share", False)): selector.BooleanSelector(),
    })


def valid(user_input: dict) -> dict:
    try:
        return {} if float(user_input["limit_kw"]) > 0 else {"limit_kw": "bad_limit"}
    except ValueError:
        return {"limit_kw": "bad_limit"}


class GitHubStep:
    """Link GitHub before saving, when catalog sharing is on and there is no token yet."""
    answers: dict
    device: dict | None = None

    async def _then_github(self, answers):
        self.answers = answers
        W.DATA = self.hass.config.path("wattson")
        linked = await self.hass.async_add_executor_job(W.gh_linked, W.DEFAULTS)
        return await self.async_step_github() if answers.get("catalog_share") and not linked else self._done()

    async def async_step_github(self, user_input=None):
        cid, errors = W.DEFAULTS["github_client_id"], {}
        try:
            if user_input is not None and self.device:
                err = await self.hass.async_add_executor_job(W.gh_poll, cid, self.device["device_code"])
                if err is None:
                    return self._done()
                errors["base"] = "gh_pending" if err in ("authorization_pending", "slow_down") else "gh_expired"
                if errors["base"] == "gh_expired":
                    self.device = None
            if not self.device:
                self.device = await self.hass.async_add_executor_job(
                    W._gh, "https://github.com/login/device/code", {"client_id": cid})
        except OSError:
            errors["base"] = "gh_offline"
            if not self.device:
                return self.async_show_form(step_id="github", errors=errors,
                                            description_placeholders={"url": "https://github.com/login/device", "code": "—"})
        return self.async_show_form(step_id="github", errors=errors, description_placeholders={
            "url": self.device["verification_uri"], "code": self.device["user_code"]})


class WattsonConfigFlow(GitHubStep, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = valid(user_input) if user_input else {}
        if user_input and not errors:
            return await self._then_github(user_input)
        return self.async_show_form(step_id="user", data_schema=schema(self.hass, user_input or {}), errors=errors)

    def _done(self):
        return self.async_create_entry(title="Wattson", data={}, options=self.answers)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return WattsonOptionsFlow()


class WattsonOptionsFlow(GitHubStep, OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = valid(user_input) if user_input else {}
        if user_input and not errors:
            return await self._then_github(user_input)
        return self.async_show_form(step_id="init", errors=errors,
                                    data_schema=schema(self.hass, user_input or dict(self.config_entry.options)))

    def _done(self):
        return self.async_create_entry(data=self.answers)
