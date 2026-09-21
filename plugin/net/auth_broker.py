"""QgsAuthManager credential broker (plugin side).

QgsAuthManager is the credential HOME; the keys form writes into it, and key
values move to the daemon over ``secret-add``, one envelope per credential, and
are NEVER logged. A provider row's key never leaves here at all - its stored
config id rides the datasource uri as ``authcfg=``. With no master password, or
no QGIS, every call is a no-op rather than a raise."""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Protocol

logger = logging.getLogger("trid3nt.auth_broker")

# Name prefix for the plugin's own QgsAuthManager entries. The credential name
# a source row declares is appended, so one entry maps 1:1 to one credential.
_NAME_PREFIX = "trid3nt-cred:"


class CredentialStore(Protocol):
    """Duck-typed credential store the broker reads/writes (mockable in tests)."""

    def providers(self) -> Dict[str, str]:
        """All stored ``{provider_id: key_value}`` (empty when unavailable)."""

    def remember(self, provider_id: str, key_value: str) -> bool:
        """Persist a credential's key; ``False`` when the store is unavailable."""

    def config_id(self, provider_id: str) -> Optional[str]:
        """The stored config id for one credential, or ``None`` when absent."""

    def names(self) -> frozenset:
        """Which credentials have an entry. Names only, no payload read."""


class QgsAuthManagerStore:
    """``QgsAuthManager``-backed credential store, keyed by ``provider_id``.
    Best-effort: a locked or unprovisioned auth DB, or an absent QGIS, yields
    an empty read and a ``False`` write, never an exception."""

    def __init__(self, auth_manager: object | None = None) -> None:
        self._am = auth_manager

    def _manager(self) -> object | None:
        if self._am is not None:
            return self._am
        try:  # lazy: QGIS is absent in headless drivers / CI
            from qgis.core import QgsApplication  # type: ignore

            am = QgsApplication.authManager()
            self._am = am
            return am
        except Exception:  # noqa: BLE001 -- QGIS unavailable -> no store
            return None

    def _config_ids_by_provider(self, am: object) -> Dict[str, str]:
        """Map ``provider_id -> authcfg_id`` for this plugin's entries."""
        out: Dict[str, str] = {}
        try:
            configs = am.availableAuthMethodConfigs()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return out
        try:
            items = configs.items()
        except AttributeError:
            return out
        for cfg_id, cfg in items:
            try:
                name = cfg.name()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(name, str) and name.startswith(_NAME_PREFIX):
                out[name[len(_NAME_PREFIX):]] = cfg_id
        return out

    def config_id(self, provider_id: str) -> Optional[str]:
        """The stored config id for one credential. The id is what a datasource
        uri carries as ``authcfg=``, so the key itself never leaves the manager."""
        am = self._manager()
        if am is None:
            return None
        return self._config_ids_by_provider(am).get(provider_id)

    def names(self) -> frozenset:
        """Which credentials have an entry, off the config names alone - no
        payload load, so this answers with the master password still locked."""
        am = self._manager()
        if am is None:
            return frozenset()
        return frozenset(self._config_ids_by_provider(am))

    def providers(self) -> Dict[str, str]:
        am = self._manager()
        if am is None:
            return {}
        result: Dict[str, str] = {}
        for provider_id, cfg_id in self._config_ids_by_provider(am).items():
            try:
                from qgis.core import QgsAuthMethodConfig  # type: ignore

                cfg = QgsAuthMethodConfig()
                ok = am.loadAuthenticationConfig(cfg_id, cfg, True)  # type: ignore[attr-defined]
                # Some bindings return (bool, cfg); normalize.
                if isinstance(ok, tuple):
                    ok, cfg = ok[0], ok[1]
                if not ok:
                    continue
                value = cfg.config("key")
            except Exception:  # noqa: BLE001 -- locked DB / binding shape
                continue
            if isinstance(value, str) and value:
                result[provider_id] = value
        return result

    def remember(self, provider_id: str, key_value: str) -> bool:
        if not provider_id or not key_value:
            return False
        am = self._manager()
        if am is None:
            return False
        try:
            from qgis.core import QgsAuthMethodConfig  # type: ignore

            existing = self._config_ids_by_provider(am).get(provider_id)
            cfg = QgsAuthMethodConfig()
            if existing:
                am.loadAuthenticationConfig(existing, cfg, True)  # type: ignore[attr-defined]
            cfg.setName(f"{_NAME_PREFIX}{provider_id}")
            cfg.setMethod("Basic")
            cfg.setConfig("key", key_value)
            stored = am.storeAuthenticationConfig(cfg)  # type: ignore[attr-defined]
            if isinstance(stored, tuple):
                stored = stored[0]
            return bool(stored)
        except Exception:  # noqa: BLE001 -- best-effort; env fallback covers it
            logger.debug("QgsAuthManager store failed for provider=%s", provider_id)
            return False


class AuthBroker:
    """Connect-time push + prompt-store over a :class:`CredentialStore`."""

    def __init__(self, store: Optional[CredentialStore] = None) -> None:
        self._store: CredentialStore = store or QgsAuthManagerStore()

    def push_all(self, push_fn: Callable[[str, str], None]) -> int:
        """Push every stored credential via ``push_fn(provider_id, key_value)``
        and return the count pushed. Never raises: a single bad entry is
        skipped and connect proceeds."""
        pushed = 0
        try:
            entries = self._store.providers()
        except Exception:  # noqa: BLE001
            return 0
        for provider_id, key_value in entries.items():
            if not provider_id or not key_value:
                continue
            try:
                push_fn(provider_id, key_value)
                pushed += 1
            except Exception:  # noqa: BLE001 -- one bad push must not block connect
                logger.debug("connect-time credential push failed provider=%s", provider_id)
        if pushed:
            logger.info("brokered %d stored credential(s) at connect", pushed)
        return pushed

    def remember(self, provider_id: str, key_value: str) -> bool:
        """Store a key the keys form took, so the next connect re-pushes it."""
        try:
            return self._store.remember(provider_id, key_value)
        except Exception:  # noqa: BLE001
            return False

    def config_id(self, provider_id: str) -> Optional[str]:
        """The stored config id for one credential, or ``None``. A row that
        names a credential nothing has stored refuses rather than opening a
        layer the provider will reject."""
        try:
            return self._store.config_id(provider_id)
        except Exception:  # noqa: BLE001
            return None

    def stored_names(self) -> frozenset:
        """Which credentials have a key stored. Names ONLY: the form shows that
        a key is present without ever reading the value back."""
        try:
            return self._store.names()
        except Exception:  # noqa: BLE001
            return frozenset()
